from conftest import Q
from code_review_ai.flow_builder import NodeRow, EdgeRow, build_flows


def _nodes():
    kinds = ["function", "function", "function", "function"]
    return [NodeRow(id=i, qualified_name=q, file_path="f.py", kind=k)
            for i, (q, k) in enumerate(zip([Q("m","a"), Q("m","b"), Q("m","c"), Q("m","d")], kinds))]


def test_linear_chain():
    # a -> b -> c; one entry = one flow, all reachable nodes in BFS order
    edges = [EdgeRow(Q("m","a"), Q("m","b"), "resolved"), EdgeRow(Q("m","b"), Q("m","c"), "resolved")]
    flows = build_flows(_nodes(), edges, ["a"])
    assert len(flows) == 1
    assert flows[0].path == [0, 1, 2]
    assert flows[0].entry_point_id == 0


def test_diamond_no_path_explosion():
    # a -> b -> d, a -> c -> d; one flow, d appears once
    edges = [
        EdgeRow(Q("m","a"), Q("m","b"), "resolved"), EdgeRow(Q("m","b"), Q("m","d"), "resolved"),
        EdgeRow(Q("m","a"), Q("m","c"), "resolved"), EdgeRow(Q("m","c"), Q("m","d"), "resolved"),
    ]
    flows = build_flows(_nodes(), edges, ["a"])
    assert len(flows) == 1
    assert flows[0].path.count(3) == 1  # d appears once, not twice


def test_cycle_handled():
    edges = [EdgeRow(Q("m","a"), Q("m","b"), "resolved"), EdgeRow(Q("m","b"), Q("m","a"), "resolved")]
    flows = build_flows(_nodes(), edges, ["a"])
    assert len(flows) == 1
    assert flows[0].path == [0, 1]  # a → b, no infinite loop


def test_unresolved_edges_excluded():
    edges = [EdgeRow(Q("m","a"), Q("m","b"), "resolved"), EdgeRow(Q("m","b"), Q("m","c"), "unresolved")]
    flows = build_flows(_nodes(), edges, ["a"])
    assert len(flows) == 1
    assert 2 not in flows[0].path  # c unreachable
