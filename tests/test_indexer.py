from code_review_ai.config import Config, load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import ParseCache, rebuild, is_stale

from conftest import FIXTURES as FIX


def _cfg(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "index.db")
    cfg.repo_path = FIX
    return cfg


def test_rebuild_writes_all_tables(tmp_path):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    stats = rebuild(cfg, conn)
    assert stats.node_count > 0 and stats.edge_count > 0 and stats.flow_count > 0
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == stats.node_count
    assert conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0] == stats.flow_count
    # flows from entry main exist
    assert conn.execute(
        "SELECT COUNT(*) FROM flows WHERE name='main'"
    ).fetchone()[0] > 0


def test_rebuild_atomic_on_failure_preserves_old(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    old_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    # inject failure into flow writing by breaking build_flows
    import code_review_ai.indexer as idx
    monkeypatch.setattr(idx, "build_flows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        rebuild(cfg, conn)
    except RuntimeError:
        pass
    # nodes/edges rolled back too (single transaction) -> old preserved
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == old_nodes


def test_is_stale_detects_mtime(tmp_path):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    assert is_stale(cfg, conn) is False
    # touch a file in the future, then restore original mtime
    import os, time
    p = "tests/fixtures/repo/util.py"
    orig_mtime = os.path.getmtime(p)
    fut = time.time() + 100
    os.utime(p, (fut, fut))
    try:
        assert is_stale(cfg, conn) is True
    finally:
        os.utime(p, (orig_mtime, orig_mtime))


def test_rebuild_cache_skips_unchanged_files(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    cache = ParseCache()
    import code_review_ai.indexer as idx
    calls = {"n": 0}
    real = idx.parse_file

    def counting(path, repo, lang=None):
        calls["n"] += 1
        return real(path, repo, lang)

    monkeypatch.setattr(idx, "parse_file", counting)

    rebuild(cfg, conn, cache)          # prime: parses every file
    assert calls["n"] > 0

    calls["n"] = 0
    stats2 = rebuild(cfg, conn, cache)  # nothing changed -> all cache hits
    assert calls["n"] == 0
    assert stats2.node_count > 0

    # touch one file's mtime -> only that file re-parses
    import os, time
    p = "tests/fixtures/repo/util.py"
    orig_mtime = os.path.getmtime(p)
    fut = time.time() + 100
    os.utime(p, (fut, fut))
    try:
        calls["n"] = 0
        rebuild(cfg, conn, cache)
        assert calls["n"] == 1
    finally:
        os.utime(p, (orig_mtime, orig_mtime))
