from __future__ import annotations
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.changes import detect_changed_symbols
from code_review_ai.impact import get_impact

FIX = "tests/fixtures/repo"


def test_end_to_end_impact(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "e2e.db")
    cfg.repo_path = FIX
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    # simulate a change to auth:login by passing symbols directly
    res = get_impact(conn, detect_changed_symbols(cfg, symbols=["auth:login"]))[0]
    assert res["found"] is True
    assert "app:main" in res["affected_entries"]


def test_diamond_flow_count_bounded(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "dia.db")
    cfg.repo_path = FIX
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    # number of flows <= number of (entry, reachable-node) pairs
    entries = conn.execute("SELECT DISTINCT entry_point_id FROM flows").fetchall()
    for e in entries:
        flows = conn.execute("SELECT COUNT(*) FROM flows WHERE entry_point_id=?",
                             (e["entry_point_id"],)).fetchone()[0]
        members = conn.execute(
            "SELECT COUNT(DISTINCT node_id) FROM flow_memberships fm "
            "JOIN flows f ON f.id=fm.flow_id WHERE f.entry_point_id=?",
            (e["entry_point_id"],)).fetchone()[0]
        assert flows <= members  # one flow per reachable node, not more
