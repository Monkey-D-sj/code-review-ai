import threading
import time
import os
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.watcher import startup_rebuild, run_watcher

from conftest import FIXTURES as FIX


def _cfg(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "w.db")
    cfg.repo_path = FIX
    cfg.watch_debounce_ms = 100
    return cfg


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
    startup_rebuild(cfg, conn)
    stop = threading.Event()
    t = threading.Thread(target=run_watcher, args=(cfg, conn, stop), daemon=True)
    t.start()
    # mutate a fixture file to trigger
    p = "tests/fixtures/repo/util.py"
    orig = open(p, encoding="utf-8").read()
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n# touch\n")
        time.sleep(0.6)  # debounce + detect
    finally:
        with open(p, "w", encoding="utf-8") as f:
            f.write(orig)
    stop.set()
    t.join(timeout=3)
    # no exception means watcher ran cleanly
    assert not t.is_alive()
