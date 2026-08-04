import pytest

from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.graph import query_graph
from code_review_ai.indexer import rebuild

from conftest import FIXTURES as FIX, Q


def _built_conn(tmp_path):
    cfg = load_config(FIX)
    cfg.repo_path = FIX
    conn = connect(str(tmp_path / "g.db"))
    init_schema(conn)
    rebuild(cfg, conn)
    return conn


def _hand_built_conn(tmp_path):
    conn = connect(str(tmp_path / "h.db"))
    init_schema(conn)
    return conn


def _insert_node(conn, qualified_name, kind="function", file_path="x.py"):
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,language,file_path,start_line,end_line,signature) "
        "VALUES (?,?,?,?,?,?,?)",
        (qualified_name, kind, "python", file_path, 1, 2, "sig"))


def _insert_edge(conn, source, target, kind="call", resolution="resolved"):
    conn.execute(
        "INSERT INTO edges(source,target,kind,file_path,call_line,resolution) "
        "VALUES (?,?,?,?,0,?)",
        (source, target, kind, "x.py", resolution))


def test_call_neighbors_both_directions(tmp_path):
    conn = _built_conn(tmp_path)
    out = query_graph(conn, Q("auth", "login"))
    assert out["qname"] == Q("auth", "login")
    assert out["edge_kind"] == "call" and out["direction"] == "both"
    assert [n["qname"] for n in out["in"]] == [Q("app", "main")]
    assert out["out"] == []


def test_contains_out(tmp_path):
    conn = _built_conn(tmp_path)
    out = query_graph(conn, Q("auth", "UserService"), edge_kind="contains", direction="out")
    assert [n["qname"] for n in out["out"]] == [Q("auth", "authenticate", Q("auth", "UserService"))]


def test_import_out_for_module(tmp_path):
    conn = _built_conn(tmp_path)
    out = query_graph(conn, "app", edge_kind="import", direction="out")
    assert [n["qname"] for n in out["out"]] == ["auth"]


def test_extends_and_implements_kinds(tmp_path):
    conn = _hand_built_conn(tmp_path)
    _insert_node(conn, "a::Base", kind="class")
    _insert_node(conn, "a::Iface", kind="class")
    _insert_node(conn, "b::Sub", kind="class")
    _insert_edge(conn, "b::Sub", "a::Base", kind="extends")
    _insert_edge(conn, "b::Sub", "a::Iface", kind="implements")
    out = query_graph(conn, "b::Sub", edge_kind="extends", direction="out")
    assert [n["qname"] for n in out["out"]] == ["a::Base"]
    all_out = query_graph(conn, "b::Sub", edge_kind="all", direction="out")
    assert {n["qname"] for n in all_out["out"]} == {"a::Base", "a::Iface"}


def test_direction_filters(tmp_path):
    conn = _hand_built_conn(tmp_path)
    _insert_node(conn, "a::caller")
    _insert_node(conn, "a::mid")
    _insert_node(conn, "a::callee")
    _insert_edge(conn, "a::caller", "a::mid")
    _insert_edge(conn, "a::mid", "a::callee")
    assert [n["qname"] for n in query_graph(conn, "a::mid", direction="in")["in"]] == ["a::caller"]
    assert [n["qname"] for n in query_graph(conn, "a::mid", direction="out")["out"]] == ["a::callee"]


def test_neighbors_dedup(tmp_path):
    conn = _hand_built_conn(tmp_path)
    _insert_node(conn, "a::caller")
    _insert_node(conn, "a::mid")
    _insert_edge(conn, "a::caller", "a::mid")
    _insert_edge(conn, "a::caller", "a::mid")
    assert len(query_graph(conn, "a::mid", direction="in")["in"]) == 1


def test_max_per_dir_truncates(tmp_path):
    conn = _hand_built_conn(tmp_path)
    _insert_node(conn, "a::mid")
    for index in range(3):
        _insert_node(conn, f"a::c{index}")
        _insert_edge(conn, f"a::c{index}", "a::mid")
    assert len(query_graph(conn, "a::mid", direction="in", max_per_dir=2)["in"]) == 2


def test_node_not_found(tmp_path):
    conn = _hand_built_conn(tmp_path)
    out = query_graph(conn, "nope::missing")
    assert out["found"] is False
    assert out["in"] == [] and out["out"] == []


def test_invalid_edge_kind_and_direction(tmp_path):
    conn = _hand_built_conn(tmp_path)
    with pytest.raises(ValueError, match="edge_kind"):
        query_graph(conn, "x", edge_kind="bogus")
    with pytest.raises(ValueError, match="direction"):
        query_graph(conn, "x", direction="sideways")
