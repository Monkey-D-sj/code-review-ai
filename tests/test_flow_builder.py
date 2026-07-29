from conftest import Q
from code_review_ai.flow_builder import NodeRow, EdgeRow, build_flows


def _nodes():
    kinds = ["function", "function", "function", "function"]
    return [NodeRow(id=i, qualified_name=q, file_path="f.py", kind=k)
            for i, (q, k) in enumerate(zip([Q("m","a"), Q("m","b"), Q("m","c"), Q("m","d")], kinds))]


def test_linear_chain():
    # a -> b -> c; a matches name; d is root (no incoming)
    edges = [EdgeRow(Q("m","a"), Q("m","b"), "resolved"), EdgeRow(Q("m","b"), Q("m","c"), "resolved")]
    flows = build_flows(_nodes(), edges, ["a"])
    assert len(flows) == 2  # a (name match) + d (root, no incoming)
    flow_a = next(f for f in flows if f.entry_point_id == 0)
    assert flow_a.path == [0, 1, 2]
    flow_d = next(f for f in flows if f.entry_point_id == 3)
    assert flow_d.path == [3]


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
    # a (name match) + c (root) + d (root) = 3
    assert len(flows) == 3
    flow_a = next(f for f in flows if f.entry_point_id == 0)
    assert flow_a.path == [0, 1]  # a → b, no infinite loop


def test_unresolved_edges_excluded():
    edges = [EdgeRow(Q("m","a"), Q("m","b"), "resolved"), EdgeRow(Q("m","b"), Q("m","c"), "unresolved")]
    flows = build_flows(_nodes(), edges, ["a"])
    # a (name match) + c (root — b→c is unresolved, so c has no incoming) + d (root)
    assert len(flows) == 3
    flow_a = next(f for f in flows if f.entry_point_id == 0)
    assert 2 not in flow_a.path  # c unreachable from a (unresolved edge)
    # c gets its own root flow since unresolved edges don't count as inbound
    flow_c = next(f for f in flows if f.entry_point_id == 2)
    assert flow_c.path == [2]
