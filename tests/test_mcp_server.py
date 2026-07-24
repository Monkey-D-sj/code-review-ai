import json

from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.mcp_server import create_server

from conftest import FIXTURES as FIX


def _server(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "m.db")
    cfg.repo_path = FIX
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return create_server(cfg), conn, cfg


def test_get_impact_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    tools = server._tool_manager._tools
    assert "get_impact" in tools
    out = tools["get_impact"].fn(symbols=["auth::login"])
    data = json.loads(out)
    assert data[0]["symbol"] == "auth::login"
    assert data[0]["found"] is True


def test_search_symbol_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    out = server._tool_manager._tools["search_symbol"].fn(query="login")
    data = json.loads(out)
    assert any(d["qname"] == "auth::login" for d in data)


def test_list_entry_points_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    out = server._tool_manager._tools["list_entry_points"].fn()
    data = json.loads(out)
    assert any(e["qname"] == "app::main" for e in data)
