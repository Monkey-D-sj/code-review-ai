from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.impact import get_impact

FIX = "tests/fixtures/repo"


def _idx(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "i.db")
    cfg.repo_path = FIX
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return conn


def test_impact_slices_prefix_suffix(tmp_path):
    conn = _idx(tmp_path)
    res = get_impact(conn, ["auth:login"])[0]
    # auth:login is downstream of app:main (entry). It has no downstream callees.
    assert "app:main" in [n["qname"] for n in res["upstream"]]
    assert res["downstream"] == []
    assert "app:main" in res["affected_entries"]


def test_impact_off_flow_fallback_to_edges(tmp_path):
    conn = _idx(tmp_path)
    # util:hash_pw is reachable only if on a flow; if not, fallback to edges
    res = get_impact(conn, ["util:helper"])[0]
    # helper is not called by anyone -> empty impact, no crash
    assert res["upstream"] == [] and res["downstream"] == []
