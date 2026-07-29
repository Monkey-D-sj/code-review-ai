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
    out = server._tool_manager._tools["get_community"].fn(qualified_name=Q("auth","login"))
    data = json.loads(out)
    assert data["found"] is True
    assert any(m["qname"] == Q("auth","login") for m in data["members"])
