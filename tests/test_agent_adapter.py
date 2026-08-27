import json
import subprocess

import pytest

from code_review_ai.agent_adapter import (DENIED_BASH_RULES,
                                          ONLINE_MCP_TOOL_NAMES,
                                          READ_ONLY_BASH_RULES,
                                          READ_ONLY_NATIVE_TOOLS, _error_payload,
                                          _mcp_config, _online_mcp_tools,
                                          normalize_claude_result,
                                          normalize_claude_stream, run_claude)


def test_normalize_claude_structured_output_and_usage():
    payload = normalize_claude_result({
        "structured_output": {
            "findings": [], "files_read": [], "tool_calls": []},
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 20,
                  "cache_creation_input_tokens": 5, "output_tokens": 3},
        "total_cost_usd": 0.01,
        "modelUsage": {"sonnet": {"canonicalModel": "claude-sonnet-test"}},
    })
    assert payload["usage"] == {
        "input_tokens": 10, "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 5, "output_tokens": 3,
        "total_cost_usd": 0.01, "model": "claude-sonnet-test"}


def test_normalize_claude_text_result_and_defaults():
    payload = normalize_claude_result({
        "result": json.dumps({"findings": [{"file": "a.py"}]})})
    assert payload["findings"] == [{"file": "a.py"}]
    assert payload["files_read"] == []
    assert payload["tool_calls"] == []


def test_normalize_claude_discards_self_reported_tool_telemetry():
    payload = normalize_claude_result({"structured_output": {
        "findings": [], "files_read": ["secret.py"], "tool_calls": ["Read"]}})
    assert payload["files_read"] == []
    assert payload["tool_calls"] == []


def test_normalize_claude_rejects_missing_result():
    with pytest.raises(ValueError, match="no structured_output"):
        normalize_claude_result({"type": "result"})


def test_normalize_claude_stream_uses_observed_tool_events(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "src" / "app.py"
    events = [
        {"type": "system", "subtype": "init",
         "tools": ["Read", "mcp__code-review-ai__get_impact", "StructuredOutput"]},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read",
             "id": "read-1", "input": {"file_path": str(source)}},
            {"type": "tool_use", "name": "mcp__code-review-ai__get_impact",
             "id": "impact-1", "input": {"files": ["src/app.py"]}},
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "should-not-count"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "read-1",
             "content": "source text"},
            {"type": "tool_result", "tool_use_id": "impact-1",
             "content": {"affected": ["src/app.py"]}},
        ]}},
        {"type": "result", "structured_output": {
            "findings": [], "files_read": ["fake.py"],
            "tool_calls": ["fake"]},
         "usage": {"input_tokens": 10, "output_tokens": 2}},
    ]
    payload = normalize_claude_stream("\n".join(json.dumps(e) for e in events))
    assert payload["files_read"] == ["src/app.py"]
    assert payload["tool_calls"] == ["Read", "mcp__code-review-ai__get_impact"]
    assert payload["tool_call_count"] == 2
    assert payload["tool_trace"] == [
        {"sequence": 1, "tool": "Read",
         "input": {"file_path": "src/app.py"}, "response_chars": 11},
        {"sequence": 2, "tool": "mcp__code-review-ai__get_impact",
         "input": {"files": ["src/app.py"]}, "response_chars": 27,
         "response": '{"affected":["src/app.py"]}'},
    ]


def test_normalize_claude_stream_counts_bash_access_and_marks_unknown(
        monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('ok')", encoding="utf-8")
    events = [
        {"type": "system", "subtype": "init", "tools": [
            "Read", "Grep", "Bash", "mcp__code-review-ai__query_graph",
            "StructuredOutput"]},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "id": "read-1",
             "input": {"file_path": str(source)}},
            {"type": "tool_use", "name": "Grep", "id": "grep-1",
             "input": {"pattern": "print", "path": "src/app.py"}},
            {"type": "tool_use", "name": "Bash", "id": "bash-1",
             "input": {"command": "rg print src/app.py"}},
            {"type": "tool_use", "name": "Bash", "id": "bash-2",
             "input": {"command": "cat src/app.py"}},
            {"type": "tool_use", "name": "Bash", "id": "bash-3",
             "input": {"command": "rg print"}},
            {"type": "tool_use", "name": "Bash", "id": "bash-4",
             "input": {"command": "git status --short"}},
            {"type": "tool_use", "name": "mcp__code-review-ai__query_graph",
             "id": "mcp-1", "input": {}}]}},
        {"type": "user", "message": {"content": [
            *[{"type": "tool_result", "tool_use_id": tool_id,
               "content": "x"} for tool_id in (
                   "read-1", "grep-1", "bash-1", "bash-2", "bash-3",
                   "bash-4")],
            {"type": "tool_result", "tool_use_id": "mcp-1",
             "content": "mcp"}]}},
        {"type": "result", "structured_output": {
            "findings": [], "files_read": [], "tool_calls": []},
         "usage": {"input_tokens": 10, "output_tokens": 2}},
    ]

    payload = normalize_claude_stream(
        "\n".join(json.dumps(event) for event in events))

    assert payload["read_calls"] == 2  # Read + cat
    assert payload["search_calls"] == 3  # Grep + two rg calls
    assert payload["bash_calls"] == 4
    assert payload["unique_files_touched"] == ["src/app.py"]
    assert payload["files_read"] == ["src/app.py"]
    assert payload["unknown_file_access"] is True
    assert len(payload["unknown_file_access_details"]) == 2
    assert payload["native_response_chars"] == 6
    assert payload["mcp_response_chars"] == 3
    assert payload["total_tool_calls"] == 7


def test_online_eval_uses_prebuilt_index_without_rebuild_tool(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRAI_EVAL_DB_PATH", str(tmp_path / "prebuilt.db"))
    config = _mcp_config()["mcpServers"]["code-review-ai"]
    assert "rebuild_index" not in ONLINE_MCP_TOOL_NAMES
    assert config["env"]["CRAI_SKIP_STARTUP_SYNC"] == "true"
    assert config["env"]["CRAI_DISABLE_WATCHER"] == "true"
    assert config["env"]["CRAI_DB_PATH"].endswith("prebuilt.db")


def test_mcp_config_passes_tool_subset_to_server_env(monkeypatch, tmp_path):
    # the server-side allowlist is driven by the eval's CRAI_EVAL_MCP_TOOLS,
    # so the server subprocess can register ONLY the allowed tools.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRAI_EVAL_DB_PATH", str(tmp_path / "prebuilt.db"))
    monkeypatch.setenv("CRAI_EVAL_MCP_TOOLS", "query_graph")
    config = _mcp_config()["mcpServers"]["code-review-ai"]
    assert config["env"]["CRAI_MCP_ONLY_TOOLS"] == "query_graph"


def test_normalize_claude_stream_error_result_without_findings():
    payload = normalize_claude_stream(json.dumps({
        "type": "result", "subtype": "error_max_budget_usd",
        "usage": {"input_tokens": 5, "output_tokens": 2}}))
    assert payload["failure_reason"] == "error_max_budget_usd"
    assert payload["findings"] == []
    assert payload["tool_calls"] == []


def test_normalize_claude_stream_success_result_has_no_failure_reason():
    payload = normalize_claude_stream(json.dumps({
        "type": "result", "subtype": "success", "structured_output": {
            "findings": [], "files_read": [], "tool_calls": []}}))
    assert "failure_reason" not in payload


def test_error_payload_preserves_budget_stream_telemetry():
    stream = "\n".join(json.dumps(event) for event in [
        {"type": "system", "subtype": "init", "tools": ["Read"]},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read",
             "input": {"file_path": "src/app.py"}},
        ]}},
        {"type": "result", "subtype": "error_max_budget_usd",
         "usage": {"input_tokens": 100, "output_tokens": 50},
         "total_cost_usd": 1.09},
    ])
    payload = _error_payload(stream)
    assert payload["failure_reason"] == "error_max_budget_usd"
    assert payload["tool_calls"] == ["Read"]
    assert payload["files_read"] == ["src/app.py"]
    assert payload["usage"]["input_tokens"] == 100
    assert payload["findings"] == []


def test_error_payload_blank_stdout_returns_contract():
    payload = _error_payload("")
    assert payload["findings"] == []
    assert payload["files_read"] == []
    assert payload["unknown_file_access"] is False
    assert payload["total_tool_calls"] == 0


def test_run_claude_error_preserves_stream_telemetry(monkeypatch):
    def fake_run(command, **kwargs):
        stream = "\n".join(json.dumps(event) for event in [
            {"type": "system", "subtype": "init", "tools": ["Read"]},
            {"type": "result", "subtype": "error_max_budget_usd",
             "usage": {"input_tokens": 10, "output_tokens": 3}},
        ])
        return subprocess.CompletedProcess(command, 1, stream, "warning")

    monkeypatch.setattr("code_review_ai.agent_adapter.subprocess.run", fake_run)
    returncode, payload, _ = run_claude("review", tool_profile="native")
    assert returncode == 1
    assert payload["failure_reason"] == "error_max_budget_usd"
    assert payload["usage"]["input_tokens"] == 10


def test_online_mcp_tools_filters_by_env_subset(monkeypatch):
    monkeypatch.delenv("CRAI_EVAL_MCP_TOOLS", raising=False)
    assert _online_mcp_tools() == ONLINE_MCP_TOOL_NAMES
    monkeypatch.setenv("CRAI_EVAL_MCP_TOOLS", "query_graph")
    assert _online_mcp_tools() == ("query_graph",)
    # Unknown names are dropped; an explicit mode may opt into rebuild_index.
    monkeypatch.setenv("CRAI_EVAL_MCP_TOOLS", "query_graph,rebuild_index,nope")
    assert _online_mcp_tools() == ("rebuild_index", "query_graph")


@pytest.mark.parametrize("profile", ["native", "full_project"])
def test_streaming_eval_uses_bare_read_only_claude_session(monkeypatch, profile):
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        stdout = "\n".join(json.dumps(event) for event in [
            {"type": "system", "subtype": "init", "tools": ["Read"]},
            {"type": "result", "structured_output": {
                "findings": [], "files_read": [], "tool_calls": []}},
        ])
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr("code_review_ai.agent_adapter.subprocess.run", fake_run)
    returncode, _, _ = run_claude("review", tool_profile=profile)
    assert returncode == 0
    assert "--bare" in observed["command"]
    assert "--setting-sources" not in observed["command"]
    tools = observed["command"][observed["command"].index("--tools") + 1]
    allowed = observed["command"][
        observed["command"].index("--allowedTools") + 1]
    denied = observed["command"][
        observed["command"].index("--disallowedTools") + 1]
    assert tools == ",".join(READ_ONLY_NATIVE_TOOLS)
    allowed_names = allowed.split(",")
    assert allowed_names[:3] == list(READ_ONLY_NATIVE_TOOLS[:3])
    assert set(READ_ONLY_BASH_RULES).issubset(allowed_names)
    if profile == "full_project":
        assert any(name.startswith("mcp__code-review-ai__")
                   for name in allowed_names)
    else:
        assert allowed_names == [*READ_ONLY_NATIVE_TOOLS[:3],
                                 *READ_ONLY_BASH_RULES]
    assert "Bash" not in allowed_names
    assert denied.split(",") == list(DENIED_BASH_RULES)
    assert "Bash(*>*)" in denied
    assert "Bash(*<*)" in denied
    assert "Bash(rg *--pre *)" in denied
    assert "Bash(pip *)" in denied
    assert "Bash(rm *)" in denied
    assert "Bash(git show *)" in denied
    assert "Bash(git log *)" in denied
    assert "Bash(git show *)" not in allowed_names
    assert "Bash(git log *)" not in allowed_names
    assert "Bash(git diff *)" not in allowed_names
    assert "Bash(git diff)" not in allowed_names
    assert "Bash(git diff *)" in denied
    assert "Bash(git diff)" in denied


def test_native_full_uses_all_builtin_tools_no_mcp(monkeypatch):
    """native_full grants every built-in tool and excludes external MCP."""
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        stdout = "\n".join(json.dumps(event) for event in [
            {"type": "system", "subtype": "init", "tools": ["Read"]},
            {"type": "result", "structured_output": {
                "findings": [], "files_read": [], "tool_calls": []}},
        ])
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr("code_review_ai.agent_adapter.subprocess.run", fake_run)
    returncode, _, _ = run_claude("review", tool_profile="native_full")
    assert returncode == 0
    command = observed["command"]
    assert "--bare" in command
    assert command[command.index("--tools") + 1] == "default"
    # --tools default enables the whole built-in set; no --allowedTools to gate it.
    assert "--allowedTools" not in command
    denied = command[command.index("--disallowedTools") + 1]
    assert denied.split(",") == list(DENIED_BASH_RULES)
    # No external MCP: an empty strict mcp config, never the product server.
    assert command[command.index("--mcp-config") + 1] == '{"mcpServers": {}}'
