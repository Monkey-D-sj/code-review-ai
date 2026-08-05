import subprocess

from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.impact import get_impact

from conftest import FIXTURES as FIX, Q


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
    res = get_impact(conn, [Q("auth","login")])[0]
    # auth::login is downstream of app::main (entry). It has no downstream callees.
    assert Q("app","main") in [n["qname"] for n in res["upstream"]]
    assert res["downstream"] == []
    assert Q("app","main") in res["affected_entries"]


def test_impact_off_flow_fallback_to_edges(tmp_path):
    conn = _idx(tmp_path)
    # util::hash_pw is reachable only if on a flow; if not, fallback to edges
    res = get_impact(conn, [Q("util","helper")])[0]
    # helper is not called by anyone -> empty impact, no crash
    assert res["upstream"] == [] and res["downstream"] == []


def _tmp_idx(tmp_path):
    (tmp_path / "a.py").write_text(
        "from b import helper\ndef entry():\n    helper()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "from c import target\ndef helper():\n    target()\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("def target():\n    pass\n", encoding="utf-8")
    (tmp_path / "d.py").write_text(
        "from c import target\ndef direct():\n    target()\n", encoding="utf-8")
    for cmd in (["git", "init"], ["git", "add", "-A"],
                ["git", "commit", "-m", "fixture"]):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
    cfg = load_config(str(tmp_path))
    cfg.db_path = str(tmp_path / "i.db")
    cfg.repo_path = str(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return conn


def test_impact_puts_direct_callers_first(tmp_path):
    conn = _tmp_idx(tmp_path)
    # target has direct callers d::direct and b::helper, plus a purely-transitive
    # caller a::entry -> b::helper -> target. The direct callers must rank before
    # the transitive-only one. (Both direct callers are asserted because
    # _edges_fallback's DISTINCT query has no ORDER BY between them.)
    res = get_impact(conn, ["c::target"])[0]
    assert res["found"] and res["upstream"]
    qnames = [n["qname"] for n in res["upstream"]]
    assert qnames.index("d::direct") < qnames.index("a::entry")
    assert qnames.index("b::helper") < qnames.index("a::entry")
