import json
import os
from pathlib import Path

import pytest
from code_review_ai.config import load_config
from code_review_ai.change_context import build_change_context
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.mcp_server import _relativize, _relativize_path, create_server

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


def test_mcp_server_only_tools_env_registers_only_subset(tmp_path, monkeypatch):
    # eval ablation: with CRAI_MCP_ONLY_TOOLS=query_graph the server exposes
    # ONLY query_graph, so a headless model cannot even see the other tools.
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "m.db")
    cfg.repo_path = FIX
    monkeypatch.setenv("CRAI_MCP_ONLY_TOOLS", "query_graph")
    server = create_server(cfg)
    tools = server._tool_manager._tools
    assert set(tools) == {"query_graph"}
    assert tools["query_graph"].fn(qualified_name=Q("auth", "login"))


def test_get_impact_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    tools = server._tool_manager._tools
    assert "get_impact" in tools
    out = tools["get_impact"].fn(symbols=[Q("auth","login")])
    data = json.loads(out)
    assert data[0]["symbol"] == Q("auth","login")
    assert data[0]["found"] is True
    # Phase 2: uncertainty + coverage flow through the MCP JSON output
    assert data[0]["uncertainty"] == []
    assert set(data[0]["coverage"]) == {
        "resolved_edges", "candidate_edges",
        "dynamic_edges", "unresolved_edges", "truncated"}
    assert data[0]["coverage"]["resolved_edges"] == 1  # app::main -> login
    assert data[0]["coverage"]["truncated"] is False


def test_search_symbol_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    tools = server._tool_manager._tools
    out = tools["search_symbol"].fn(query="login", limit=10)
    data = json.loads(out)
    hit = next(d for d in data if d["qname"] == Q("auth", "login"))
    assert hit["kind"] == "function"
    # nodes.file_path is stored absolute (os.path.join(repo, rel) in _parse_files);
    # assert on the basename so the test is agnostic to repo-root layout.
    assert hit["file"].endswith("auth.py")
    assert hit["end_line"] >= hit["line"]
    assert "signature" in hit and "score" in hit


def test_search_symbol_glob_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    out = server._tool_manager._tools["search_symbol"].fn(query="*login*")
    data = json.loads(out)
    assert any(d["qname"] == Q("auth", "login") for d in data)


def test_search_symbol_tool_caps_broad_globs(tmp_path):
    server, conn, cfg = _server(tmp_path)
    for index in range(40):
        conn.execute(
            "INSERT INTO nodes(qualified_name,kind,language,file_path,"
            "start_line,end_line,signature) VALUES (?,?,?,?,?,?,?)",
            (f"bulk::sym{index:02d}", "function", "python",
             "src/bulk.py", index + 1, index + 2, f"def sym{index:02d}()"),
        )
    conn.commit()
    out = server._tool_manager._tools["search_symbol"].fn(query="*sym*")
    data = json.loads(out)
    assert len(data) == 30
    assert data[0]["qname"] == "bulk::sym00"


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
    assert set(data) == {"summary", "changed_functions", "uncovered_changes",
                         "delete_change"}
    assert data["summary"]["changed_functions"] == 1
    record = data["changed_functions"][0]
    assert record["qname"] == Q("auth", "login")
    assert record["file"] == "auth.py"
    assert record["start_line"] == 6
    assert record["end_line"] == 7


def test_get_change_context_tool_is_compact_and_resolves_symbol_input(tmp_path):
    server, conn, cfg = _server(tmp_path)
    tools = server._tool_manager._tools
    assert "get_change_context" in tools
    rendered = tools["get_change_context"].fn(
        symbols=[Q("auth", "login")], max_symbols=99, max_neighbors=99)
    data = json.loads(rendered)

    assert len(rendered) <= 8_000
    assert data["meta"] == {
        "source": "symbols", "direction": "in", "detected_symbols": 1,
        "selection_strategy": "file_diverse_call_value_v1",
        "include_tests": False,
        "returned_symbols": 1, "omitted_symbols": 0,
        "max_neighbors": 8, "max_chars": 8_000, "truncated": False,
    }
    change = data["changes"][0]
    assert change["qname"] == Q("auth", "login")
    assert change["file"] == "auth.py"
    assert "signature" not in change
    assert [caller["qname"] for caller in change["callers"]] == [Q("app", "main")]
    assert "signature" not in change["callers"][0]


def test_get_change_context_selects_across_files_and_prefers_prod_callers(
        tmp_path):
    server, conn, cfg = _server(tmp_path)
    root = Path(cfg.repo_path)
    changed = [
        "opt.v2::schema",
        "opt.dependencies::weak",
        "opt.dependencies::central",
        "opt.openapi::parameters",
        "opt.params::Param.__init__",
        "opt.params::Body.__init__",
    ]
    nodes = [
        (changed[0], "function", root / "opt/v2.py", 0),
        (changed[1], "function", root / "opt/dependencies.py", 0),
        (changed[2], "function", root / "opt/dependencies.py", 0),
        (changed[3], "function", root / "opt/openapi.py", 0),
        (changed[4], "method", root / "opt/params.py", 0),
        (changed[5], "method", root / "opt/params.py", 0),
        ("opt.runtime::one", "function", root / "opt/runtime.py", 0),
        ("opt.runtime::two", "function", root / "opt/runtime.py", 0),
        ("opt.tests::test_param", "function", root / "tests/test_params.py", 1),
    ]
    conn.executemany(
        "INSERT INTO nodes(qualified_name,kind,file_path,start_line,end_line,is_test) "
        "VALUES(?,?,?,?,?,?)",
        [(qname, kind, str(path), 1, 2, is_test)
         for qname, kind, path, is_test in nodes],
    )
    conn.executemany(
        "INSERT INTO edges(source,target,kind,resolution) VALUES(?,?,?,?)",
        [
            ("opt.runtime::one", changed[1], "call", "resolved"),
            ("opt.runtime::one", changed[2], "call", "resolved"),
            ("opt.runtime::two", changed[2], "call", "resolved"),
            ("opt.runtime::one", changed[3], "call", "resolved"),
            ("opt.tests::test_param", changed[4], "call", "resolved"),
        ],
    )
    conn.commit()

    data = build_change_context(cfg, conn, symbols=changed, max_symbols=4)

    assert [item["qname"] for item in data["changes"]] == [
        changed[0], changed[2], changed[3], changed[4],
    ]
    # Test-only callers do not consume the default graph payload.
    assert data["changes"][-1]["callers"] == []
    with_tests = build_change_context(
        cfg, conn, symbols=[changed[4]], include_tests=True)
    assert [item["qname"] for item in with_tests["changes"][0]["callers"]] == [
        "opt.tests::test_param"
    ]


def test_get_change_context_resolves_changed_qname_from_file(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "callee.py").write_text(
        "def target(value):\n    return value + 1\n", encoding="utf-8")
    (repo / "caller.py").write_text(
        "from callee import target\n\ndef use():\n    return target(1)\n",
        encoding="utf-8",
    )
    commands = (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "seed"],
    )
    for command in commands:
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    (repo / "callee.py").write_text(
        "def target(value):\n    return value + 2\n", encoding="utf-8")

    cfg = load_config(str(repo))
    cfg.repo_path = str(repo)
    cfg.db_path = str(tmp_path / "context.db")
    cfg.diff_base = "HEAD"
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    server = create_server(cfg)
    data = json.loads(server._tool_manager._tools["get_change_context"].fn(
        files=["callee.py"]
    ))

    assert data["meta"]["source"] == "files"
    assert data["meta"]["detected_symbols"] == 1
    assert data["changes"][0]["qname"] == "callee::target"
    assert [item["qname"] for item in data["changes"][0]["callers"]] == [
        "caller::use"
    ]


def test_get_change_context_enforces_total_character_budget(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    cfg.repo_path = FIX

    def large_graph(conn, qname, **kwargs):
        neighbors = [{
            "qname": f"pkg::{'neighbor' * 8}{index}",
            "kind": "function",
            "file": str(Path(FIX) / f"{'module' * 8}{index}.py"),
            "line": index + 1,
            "signature": "def f(" + "argument, " * 15 + ")",
            "call_site": {
                "call_form": "function",
                "line": index + 10,
                "args": ["expression_" * 20 for _ in range(8)],
            },
        } for index in range(8)]
        return {
            "qname": qname, "found": True, "kind": "function",
            "file": str(Path(FIX) / "changed.py"), "line": 1,
            "signature": "def changed(" + "argument, " * 15 + ")",
            "in": list(neighbors), "out": list(neighbors),
        }

    monkeypatch.setattr("code_review_ai.change_context.query_graph", large_graph)
    data = build_change_context(
        cfg, None, symbols=[f"pkg::changed{index}" for index in range(8)],
        direction="both", max_symbols=8, max_neighbors=8,
        include_signatures=True,
    )

    assert len(json.dumps(data)) <= 8_000
    assert data["meta"]["truncated"] is True
    assert data["meta"]["returned_symbols"] == len(data["changes"])


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
    # Phase 2: the fallback safety contract flows through (no unknown symbol,
    # full coverage -> complete, no fallback)
    assert data["complete"] is True
    assert data["fallback_recommended"] is False
    assert data["fallback_reasons"] == []


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
    qnames = {symbol["qname"] for symbol in data["symbols"]}
    assert Q("util", "hash_pw") in qnames
    assert Q("app", "main") not in qnames
    assert any(file_entry["qname"] == "util" for file_entry in data["files"])
    assert data["meta"]["symbol_count"] == len(data["symbols"])
    assert data["meta"]["file_count"] == len(data["files"])


def test_relativize_path_rewrites_absolute_to_repo_relative(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    abs_path = str(root / "fastapi" / "background.py")
    assert _relativize_path(abs_path, str(root)) == "fastapi/background.py"
    # already-relative values pass through
    assert _relativize_path("fastapi/background.py", str(root)) == "fastapi/background.py"
    # paths outside the repo pass through unchanged
    outside = str(tmp_path / "other" / "x.py")
    assert _relativize_path(outside, str(root)) == outside


def test_relativize_walks_nested_tool_result(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    abs_path = str(root / "auth.py")
    payload = {"in": [{"qname": "auth::login", "file": abs_path,
                       "signature": "def login()"}],
               "files": [{"qname": "auth", "file_path": abs_path}]}
    out = _relativize(payload, str(root))
    assert out["in"][0]["file"] == "auth.py"
    assert out["files"][0]["file_path"] == "auth.py"
    assert out["in"][0]["signature"] == "def login()"


def test_query_graph_tool_returns_repo_relative_paths(tmp_path):
    server, conn, cfg = _server(tmp_path)
    tools = server._tool_manager._tools
    out = json.loads(tools["query_graph"].fn(qualified_name=Q("auth", "login")))
    assert out["qname"] == Q("auth", "login")
    assert out["file"] == "auth.py"
    for neighbor in out["in"] + out["out"]:
        # absolute worktree prefixes must be relativized away
        assert not os.path.isabs(neighbor["file"])
        assert Path(cfg.repo_path, neighbor["file"]).resolve().is_file()
