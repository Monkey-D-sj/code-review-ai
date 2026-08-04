from code_review_ai.config import Config, load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild, is_stale

import pytest
from conftest import FIXTURES as FIX, Q


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
    # community detection is opt-in (default off) -> nothing written
    assert stats.community_count == 0
    assert conn.execute("SELECT COUNT(*) FROM communities").fetchone()[0] == 0


def test_rebuild_writes_communities_when_enabled(tmp_path):
    pytest.importorskip("leidenalg")
    cfg = _cfg(tmp_path)
    cfg.community_detection = True
    conn = connect(cfg.db_path)
    init_schema(conn)
    stats = rebuild(cfg, conn)
    assert stats.community_count > 0
    members = conn.execute("SELECT COUNT(*) FROM community_memberships").fetchone()[0]
    total = conn.execute("SELECT SUM(node_count) FROM communities").fetchone()[0]
    assert members == total


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


def test_rebuild_then_update_parses_only_changed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    from code_review_ai import update as upd
    rebuild(cfg, conn)                          # 全量，填充 manifest（Task 7）
    calls = {"n": 0}
    real = upd.parse_file

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(upd, "parse_file", counting)
    # 无变化 -> 不 parse 任何文件
    upd.update_nodes_edges(cfg, conn)
    assert calls["n"] == 0
    # 只改 util.py -> 只 parse 一个
    p = "tests/fixtures/repo/util.py"
    orig = open(p, encoding="utf-8").read()
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n# x\n")
        calls["n"] = 0
        upd.update_nodes_edges(cfg, conn)
        assert calls["n"] == 1
    finally:
        with open(p, "w", encoding="utf-8") as f:
            f.write(orig)


def test_rebuild_writes_node_degrees(tmp_path):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)

    def deg(qname):
        row = conn.execute(
            "SELECT in_degree, out_degree FROM nodes WHERE qualified_name=?",
            (qname,)).fetchone()
        assert row is not None, qname
        return row["in_degree"], row["out_degree"]

    # auth::login: called only by app::main -> in 1; calls nothing resolved -> out 0
    assert deg(Q("auth", "login")) == (1, 0)
    # app::main: calls auth::login twice (login() + a.login()) -> out 1 (deduped); not called -> in 0
    assert deg(Q("app", "main")) == (0, 1)
    # util::hash_pw: isolate, on no resolved call edge -> 0/0
    assert deg(Q("util", "hash_pw")) == (0, 0)
