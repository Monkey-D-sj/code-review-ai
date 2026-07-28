from conftest import Q
from code_review_ai.flow_builder import NodeRow, EdgeRow, build_flows


def _nodes():
    return [NodeRow(id=i, qualified_name=q, file_path="f.py")
            for i, q in enumerate([Q("m","a"), Q("m","b"), Q("m","c"), Q("m","d")])]


def test_linear_chain_one_flow_per_reachable():
    # a -> b -> c
    edges = [EdgeRow(Q("m","a"), Q("m","b"), "resolved"), EdgeRow(Q("m","b"), Q("m","c"), "resolved")]
    flows = build_flows(_nodes(), edges, [Q("m","a")], max_depth=10)  # entry = a
    paths = sorted(f.path for f in flows)
    assert [0, 1] in paths   # a -> b
    assert [0, 1, 2] in paths  # a -> c
    assert all(f.entry_point_id == 0 for f in flows)
    # criticality lives on the DB row (NULL in v1), not on FlowRecord


def test_diamond_no_path_explosion():
    # a -> b -> d, a -> c -> d
    edges = [
        EdgeRow(Q("m","a"), Q("m","b"), "resolved"), EdgeRow(Q("m","b"), Q("m","d"), "resolved"),
        EdgeRow(Q("m","a"), Q("m","c"), "resolved"), EdgeRow(Q("m","c"), Q("m","d"), "resolved"),
    ]
    flows = build_flows(_nodes(), edges, [Q("m","a")], max_depth=10)
    to_d = [f for f in flows if f.path[-1] == 3]
    assert len(to_d) == 1  # one shortest path to d, not two


def test_cycle_handled():
    edges = [EdgeRow(Q("m","a"), Q("m","b"), "resolved"), EdgeRow(Q("m","b"), Q("m","a"), "resolved")]
    flows = build_flows(_nodes(), edges, [Q("m","a")], max_depth=10)
    # a reaches b; b not re-expanded to a (visited)
    assert any(f.path == [0, 1] for f in flows)


def test_depth_cap():
    edges = [EdgeRow(Q("m","a"), Q("m","b"), "resolved"), EdgeRow(Q("m","b"), Q("m","c"), "resolved"),
             EdgeRow(Q("m","c"), Q("m","d"), "resolved")]
    flows = build_flows(_nodes(), edges, [Q("m","a")], max_depth=1)
    targets = {f.path[-1] for f in flows}
    assert targets == {0, 1}  # entry (depth 0) + b (depth 1); c,d beyond cap


def test_unresolved_edges_excluded():
    edges = [EdgeRow(Q("m","a"), Q("m","b"), "resolved"), EdgeRow(Q("m","b"), Q("m","c"), "unresolved")]
    flows = build_flows(_nodes(), edges, [Q("m","a")], max_depth=10)
    assert not any(f.path[-1] == 2 for f in flows)  # c unreachable
