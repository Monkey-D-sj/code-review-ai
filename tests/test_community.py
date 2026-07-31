from conftest import Q
from code_review_ai.community import build_communities, WeightMode
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
    assert comms[0].label == "m"               # longest common prefix of qnames
    assert comms[1].members == [3, 4]
    assert comms[1].label == "m"  # same module prefix


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


def test_degree_damped_downweights_cross_module_sink():
    """A base class inherited by subclasses in several modules is a sink hub
    (high in-degree, low out-degree) whose in-neighbours span modules - its
    edges should be damped relative to plain mode."""
    nodes = [NodeRow(id=i, qualified_name=q, file_path="f.py", kind="class")
             for i, q in enumerate(["core::Base", "mod1::S1", "mod2::S2",
                                    "mod3::S3", "mod4::S4"])]
    base = "core::Base"
    edges = [EdgeRow(f"mod{m}::S{m}", base, "resolved") for m in range(1, 5)]

    plain, damped = {}, {}
    build_communities(nodes, edges, partitioner=_capture(plain),
                      weight_mode=WeightMode.PLAIN)
    build_communities(nodes, edges, partitioner=_capture(damped),
                      weight_mode=WeightMode.DEGREE_DAMPED)

    assert plain["ids"] == damped["ids"] == [0, 1, 2, 3, 4]
    assert plain["adj"][0][1] == 1            # raw count, untouched
    # sinkness=1.0, all 4 dependents outside Base's own module -> spread 1.0
    # -> factor 1 - 0.5*1*1 = 0.5
    assert damped["adj"][0][1] == 0.5
    assert damped["adj"][0][1] < plain["adj"][0][1]


def test_degree_damped_keeps_single_module_sink():
    """A sink whose dependents all live in its own module is a local core, not a
    cross-cutting hub - spread is 0 so it keeps full weight."""
    nodes = [NodeRow(id=i, qualified_name=q, file_path="f.py", kind="class")
             for i, q in enumerate(["m::Base", "m::S1", "m::S2"])]
    base = "m::Base"
    edges = [EdgeRow("m::S1", base, "resolved"), EdgeRow("m::S2", base, "resolved")]

    plain, damped = {}, {}
    build_communities(nodes, edges, partitioner=_capture(plain),
                      weight_mode=WeightMode.PLAIN)
    build_communities(nodes, edges, partitioner=_capture(damped),
                      weight_mode=WeightMode.DEGREE_DAMPED)

    # dependents co-located with Base -> own_in = in_deg -> spread 0 -> no damping
    assert damped["adj"][0][1] == plain["adj"][0][1] == 1


def test_weight_mode_parse_falls_back_on_unknown():
    assert WeightMode.parse("degree_damped") is WeightMode.DEGREE_DAMPED
    assert WeightMode.parse("plain") is WeightMode.PLAIN
    # unknown value must not raise - it degrades to PLAIN
    assert WeightMode.parse("nonsense") is WeightMode.PLAIN


def test_degree_damped_keys_off_own_module_dependents():
    """spread is the fraction of dependents OUTSIDE the node's own module. Two
    pure sinks of equal degree: the one whose dependents are partly co-located
    (own_in=2/4 -> spread 0.5) is damped less than the one whose dependents all
    live elsewhere (own_in=0/4 -> spread 1.0)."""
    nodes = [NodeRow(id=i, qualified_name=q, file_path="f.py", kind="class")
             for i, q in enumerate([
                 "m::H1", "m::H2",                       # 0,1  hubs, both in module m
                 "m::A1", "m::A2", "o::B1", "o::B2",     # H1: 2 own + 2 outside
                 "o::B3", "o::B4", "o::B5", "o::B6",     # H2: 0 own + 4 outside
             ])]
    h1, h2 = "m::H1", "m::H2"
    edges = [
        EdgeRow("m::A1", h1, "resolved"), EdgeRow("m::A2", h1, "resolved"),
        EdgeRow("o::B1", h1, "resolved"), EdgeRow("o::B2", h1, "resolved"),
        EdgeRow("o::B3", h2, "resolved"), EdgeRow("o::B4", h2, "resolved"),
        EdgeRow("o::B5", h2, "resolved"), EdgeRow("o::B6", h2, "resolved"),
    ]
    captured = {}
    build_communities(nodes, edges, partitioner=_capture(captured),
                      weight_mode=WeightMode.DEGREE_DAMPED)

    h1_edge = captured["adj"][0][2]   # H1 <-> A1
    h2_edge = captured["adj"][1][6]   # H2 <-> B3
    # both sinkness=1; H1 spread 0.5 -> factor 0.75, H2 spread 1.0 -> factor 0.5.
    assert h1_edge == 0.75
    assert h2_edge == 0.5
    assert h2_edge < h1_edge < 1      # more own-module dependents -> less damping
