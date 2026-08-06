import json

import pytest
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.mcp_server import create_server

from conftest import FIXTURES as FIX, Q


def _server(tmp_path, community=False):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "m.db")
    cfg.repo_path = FIX
    if community:
        cfg.community_detection = True
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return create_server(cfg), conn, cfg


def test_rebuild_index_tool_returns_new_shape(tmp_path):
    # empty DB: rebuild_index -> sync -> full rebuild with the new return shape
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "m.db")
    cfg.repo_path = FIX
    server = create_server(cfg)
    tools = server._tool_manager._tools
    assert "rebuild_index" in tools
    data = json.loads(tools["rebuild_index"].fn())
    assert set(data) == {"nodes", "edges", "flows", "communities", "full_rebuild"}
    assert data["full_rebuild"] is True
    assert data["nodes"] > 0 and data["edges"] > 0


def test_get_impact_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    tools = server._tool_manager._tools
    assert "get_impact" in tools
    out = tools["get_impact"].fn(symbols=[Q("auth","login")])
    data = json.loads(out)
    assert data[0]["symbol"] == Q("auth","login")
    assert data[0]["found"] is True


def test_search_symbol_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    out = server._tool_manager._tools["search_symbol"].fn(query="login")
    data = json.loads(out)
    assert any(d["qname"] == Q("auth","login") for d in data)


def test_list_entry_points_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    out = server._tool_manager._tools["list_entry_points"].fn()
    data = json.loads(out)
    assert any(e["qname"] == Q("app","main") for e in data)


def test_get_communities_tool(tmp_path):
    pytest.importorskip("leidenalg")
    server, conn, cfg = _server(tmp_path, community=True)
    tools = server._tool_manager._tools
    assert "get_communities" in tools and "get_community" in tools
    data = json.loads(tools["get_communities"].fn())
    assert isinstance(data, list) and len(data) > 0


def test_get_community_tool(tmp_path):
    pytest.importorskip("leidenalg")
    server, conn, cfg = _server(tmp_path, community=True)
    out = server._tool_manager._tools["get_community"].fn(qualified_name=Q("auth","UserService"))
    data = json.loads(out)
    assert data["found"] is True
    # UserService and authenticate are connected via CONTAINS edge
    assert any(m["qname"] == Q("auth","authenticate","auth::UserService") for m in data["members"])


def test_get_symbol_detail_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    out = server._tool_manager._tools["get_symbol_detail"].fn(qualified_name=Q("auth", "login"))
    data = json.loads(out)
    assert data["qname"] == Q("auth", "login")
    # importance signal surfaced to the AI reviewer
    assert data["in_degree"] == 1   # called by app::main only
    assert data["out_degree"] == 0  # calls nothing resolved
    assert data["callers"] == [Q("app", "main")]


def test_get_change_summary_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    tools = server._tool_manager._tools
    assert "get_change_summary" in tools
    data = json.loads(tools["get_change_summary"].fn(symbols=[Q("auth", "login")]))
    assert set(data) == {"summary", "changed_functions"}
    assert data["summary"]["changed_functions"] == 1
    record = data["changed_functions"][0]
    assert record["qname"] == Q("auth", "login")
    assert record["file"] == "auth.py"
    assert record["start_line"] == 6
    assert record["end_line"] == 7


def test_get_test_impact_tool(tmp_path):
    import subprocess
    # isolated repo with a test file (FIX has none)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "prod.py").write_text(
        "def login(user, pw):\n    return user\n", encoding="utf-8")
    (repo / "test_prod.py").write_text(
        "from prod import login\n\ndef test_login():\n    login('u','p')\n",
        encoding="utf-8")
    for cmd in (["git", "init"], ["git", "add", "-A"], ["git", "commit", "-m", "x"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    cfg = load_config(str(repo))
    cfg.db_path = str(tmp_path / "ti.db")
    cfg.repo_path = str(repo)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    server = create_server(cfg)
    tools = server._tool_manager._tools
    assert "get_test_impact" in tools
    data = json.loads(tools["get_test_impact"].fn(symbols=["prod::login"]))
    assert data["test_count"] == 1
    assert data["affected_tests"][0]["qname"] == "test_prod::test_login"
    assert data["affected_tests"][0]["covers"] == ["prod::login"]


def test_query_graph_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    tools = server._tool_manager._tools
    assert "query_graph" in tools
    out = json.loads(tools["query_graph"].fn(qualified_name=Q("auth", "login")))
    assert out["qname"] == Q("auth", "login")
    assert out["edge_kind"] == "call" and out["direction"] == "both"
    assert [n["qname"] for n in out["in"]] == [Q("app", "main")]


def test_find_dead_code_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    tools = server._tool_manager._tools
    assert "find_dead_code" in tools
    data = json.loads(tools["find_dead_code"].fn())
    assert set(data) == {"symbols", "files", "meta"}
    qnames = {s["qname"] for s in data["symbols"]}
    assert Q("util", "hash_pw") in qnames
    assert Q("app", "main") not in qnames
    assert any(f["qname"] == "util" for f in data["files"])
    assert data["meta"]["symbol_count"] == len(data["symbols"])
    assert data["meta"]["file_count"] == len(data["files"])
