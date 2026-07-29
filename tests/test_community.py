from conftest import Q
from code_review_ai.community import build_communities
from code_review_ai.flow_builder import NodeRow, EdgeRow


def _make_nodes(names):
    return [NodeRow(id=i, qualified_name=Q("m", n), file_path="f.py", kind="function")
            for i, n in enumerate(names)]


def _capture(captured):
    """Stub partitioner that records the graph it was handed and assigns each
    node its own community. Lets us assert on adjacency without leidenalg."""
    def _p(ids, adj):
        captured["ids"] = list(ids)
        captured["adj"] = {k: dict(v) for k, v in adj.items()}
        return {nid: i for i, nid in enumerate(ids)}, 0.0
    return _p


def test_adjacency_symmetrizes_and_weights():
    nodes = _make_nodes(["a", "b", "c"])
    a, b, c = Q("m", "a"), Q("m", "b"), Q("m", "c")
    edges = [
        EdgeRow(a, b, "resolved"),   # a -> b
        EdgeRow(a, b, "resolved"),   # a -> b (2nd call site)
        EdgeRow(b, a, "resolved"),   # b -> a  => weight 3 total
        EdgeRow(a, c, "unresolved"),  # excluded
        EdgeRow(a, a, "resolved"),    # self-loop excluded
    ]
    captured = {}
    build_communities(nodes, edges, partitioner=_capture(captured))
    assert captured["ids"] == [0, 1]          # only a, b; c never linked
    assert captured["adj"][0][1] == 3
    assert captured["adj"][1][0] == 3
    assert 2 not in captured["adj"]           # c excluded entirely


def test_build_with_stub_partitioner_groups_clusters():
    nodes = _make_nodes(["a", "b", "c", "d", "e"])
    a, b, c, d, e = (Q("m", n) for n in ["a", "b", "c", "d", "e"])
    edges = [
        EdgeRow(a, b, "resolved"), EdgeRow(b, c, "resolved"),  # cluster 1
        EdgeRow(d, e, "resolved"),                              # cluster 2
    ]

    def stub(ids, adj):
        return ({0: 0, 1: 0, 2: 0, 3: 1, 4: 1}, 0.42)

    comms = build_communities(nodes, edges, partitioner=stub)
    assert len(comms) == 2
    assert comms[0].members == [0, 1, 2]
    assert comms[0].modularity == 0.42
    assert comms[0].label == "a"               # most common short (tie -> first)
    assert comms[1].members == [3, 4]
    assert comms[1].label == "d"


def test_isolated_nodes_excluded():
    nodes = _make_nodes(["a", "b", "c"])  # c has no resolved edge
    a, b = Q("m", "a"), Q("m", "b")
    edges = [EdgeRow(a, b, "resolved")]
    captured = {}
    comms = build_communities(nodes, edges, partitioner=_capture(captured))
    assert captured["ids"] == [0, 1]           # c (id 2) not in graph
    all_members = {nid for comm in comms for nid in comm.members}
    assert 2 not in all_members


def test_empty_graph_returns_empty():
    nodes = _make_nodes(["a", "b"])
    edges = [EdgeRow(Q("m", "a"), Q("m", "b"), "unresolved")]

    def _explode(ids, adj):
        raise AssertionError("partitioner must not run on an empty graph")

    assert build_communities(nodes, edges, partitioner=_explode) == []
