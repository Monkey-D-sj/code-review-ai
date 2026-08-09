import json

import pytest

from code_review_ai.agent_adapter import (normalize_claude_result,
                                          normalize_claude_stream)


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
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read",
             "input": {"file_path": str(source)}},
            {"type": "tool_use", "name": "mcp__code-review-ai__get_impact",
             "input": {"files": ["src/app.py"]}},
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
