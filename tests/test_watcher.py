import threading
import time
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import ParseCache
from code_review_ai.watcher import startup_rebuild, run_watcher

from conftest import FIXTURES as FIX


def _cfg(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "w.db")
    cfg.repo_path = FIX
    cfg.watch_debounce_ms = 100
    return cfg


def _built_at(db_path):
    """Read built_at on a fresh connection (guaranteed to see the watcher's
    WAL commit, regardless of snapshot caching on a long-lived connection)."""
    c = connect(db_path)
    row = c.execute("SELECT value FROM build_meta WHERE key='built_at'").fetchone()
    c.close()
    return row[0] if row else None


def test_startup_rebuild_when_stale(tmp_path):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuilt = startup_rebuild(cfg, conn)
    assert rebuilt is True  # empty db is stale


def test_run_watcher_triggers_rebuild_on_change(tmp_path):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    cache = ParseCache()
    lock = threading.Lock()
    startup_rebuild(cfg, conn, cache, lock)
    before = _built_at(cfg.db_path)

    stop = threading.Event()
    t = threading.Thread(target=run_watcher, args=(cfg, cache, lock, stop),
                         daemon=True)
    t.start()
    p = "tests/fixtures/repo/util.py"
    orig = open(p, encoding="utf-8").read()
    after = before
    try:
        # let watchfiles set its baseline before we mutate (run_watcher opens
        # its own conn + inits schema before watching); writing too early is
        # swallowed as the initial state and never reported as a change.
        time.sleep(0.5)
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n# touch\n")
        # poll until the watcher's rebuild commits a new built_at
        deadline = time.time() + 8
        while time.time() < deadline and after == before:
            time.sleep(0.1)
            after = _built_at(cfg.db_path)
    finally:
        with open(p, "w", encoding="utf-8") as f:
            f.write(orig)
    stop.set()
    t.join(timeout=8)
    assert not t.is_alive()
    # the real assertion: a watch-triggered rebuild actually ran and committed
    assert after != before
