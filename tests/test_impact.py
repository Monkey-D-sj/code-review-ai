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
    qnames = [node["qname"] for node in res["upstream"]]
    assert qnames.index("d::direct") < qnames.index("a::entry")
    assert qnames.index("b::helper") < qnames.index("a::entry")


def test_impact_call_sites_on_direct_neighbors(tmp_path):
    conn = _tmp_idx(tmp_path)
    res = get_impact(conn, ["c::target"])[0]
    by_qname = {node["qname"]: node for node in res["upstream"]}
    # direct callers carry call_site with the call-line code snippet
    assert "call_site" in by_qname["b::helper"]
    assert "call_site" in by_qname["d::direct"]
    assert "target()" in by_qname["d::direct"]["call_site"]["code"]
    # the purely-transitive hop (a::entry -> b::helper -> target) stays qname-only
    assert "call_site" not in by_qname["a::entry"]
    # opt-out drops the field entirely
    res_off = get_impact(conn, ["c::target"], include_call_sites=False)[0]
    assert all("call_site" not in node for node in res_off["upstream"])


def _diamond_idx(tmp_path):
    """BFS-order noise fixture: main calls both helper (-> target) and sibling
    (never calls target). BFS discovers sibling before target, so a position
    slice would mis-report sibling as upstream — true reachability must not."""
    (tmp_path / "a.py").write_text(
        "from b import helper\nfrom c import sibling\n"
        "def main():\n    helper()\n    sibling()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "from c import target\ndef helper():\n    target()\n", encoding="utf-8")
    (tmp_path / "c.py").write_text(
        "def target():\n    pass\n\ndef sibling():\n    pass\n", encoding="utf-8")
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


def test_impact_upstream_excludes_sibling_branch(tmp_path):
    # sibling is BFS-discovered before target but never calls it: a position
    # slice would report it as upstream. True flow-constrained reachability
    # must keep only main (entry) and helper (direct caller).
    conn = _diamond_idx(tmp_path)
    res = get_impact(conn, ["c::target"])[0]
    qnames = [n["qname"] for n in res["upstream"]]
    assert "a::main" in qnames and "b::helper" in qnames
    assert "c::sibling" not in qnames
    assert res["downstream"] == []


def test_impact_include_signatures_opt_in(tmp_path):
    conn = _idx(tmp_path)
    no_sig = get_impact(conn, [Q("auth", "login")], include_signatures=False)[0]
    assert no_sig["upstream"] and all("sig" not in n for n in no_sig["upstream"])
    with_sig = get_impact(conn, [Q("auth", "login")], include_signatures=True)[0]
    assert with_sig["upstream"] and all("sig" in n for n in with_sig["upstream"])


def _dyn_idx(tmp_path):
    """Repo with one dynamic call: dispatch(handler) -> handler.handle() —
    the receiver type is a parameter, so the resolver leaves it unresolved."""
    (tmp_path / "dyn.py").write_text(
        "def dispatch(handler):\n"
        "    handler.handle()\n", encoding="utf-8")
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


def test_impact_uncertainty_lists_dynamic_edge_not_upstream(tmp_path):
    # A dynamic edge is returned as uncertainty (guide §5.2) so the resolution
    # gap is visible — but never pollutes the determined upstream/downstream.
    conn = _dyn_idx(tmp_path)
    res = get_impact(conn, ["dyn::dispatch"])[0]
    assert res["found"] is True
    dyn = [u for u in res["uncertainty"] if u["resolution"] == "dynamic"]
    assert dyn and dyn[0]["expression"] == "handler.handle"
    assert dyn[0]["source"] == "dyn::dispatch"
    assert dyn[0]["reason"] == "receiver type not statically known"
    assert res["upstream"] == [] and res["downstream"] == []
    # coverage counts the dynamic adjacency apart from resolved
    assert res["coverage"]["dynamic_edges"] == 1
    assert res["coverage"]["resolved_edges"] == 0
    assert res["coverage"]["truncated"] is False


def test_impact_coverage_counts_resolved_adjacency(tmp_path):
    conn = _tmp_idx(tmp_path)
    # c::target has two incoming resolved call edges (b::helper, d::direct);
    # a::entry reaches it only transitively, so no edge touches target.
    res = get_impact(conn, ["c::target"])[0]
    assert res["coverage"]["resolved_edges"] == 2
    assert res["coverage"]["dynamic_edges"] == 0
    assert res["uncertainty"] == []
    assert res["coverage"]["truncated"] is False
