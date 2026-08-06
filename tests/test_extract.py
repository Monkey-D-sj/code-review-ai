import json
from pathlib import Path

from code_review_ai.extract import extract_review, trace_review


def test_extract_review_writes_last_result_text(tmp_path):
    debug = tmp_path / "debug.jsonl"
    out = tmp_path / "answer.md"
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "let me check"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "..."}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "draft"}]}},
        {"type": "result", "result": "## final review\n\nfound a bug", "is_error": False},
    ]
    debug.write_text("\n".join(json.dumps(event) for event in events) + "\n",
                     encoding="utf-8")
    assert extract_review(str(debug), str(out))
    assert out.read_text(encoding="utf-8") == "## final review\n\nfound a bug\n"


def test_extract_review_falls_back_to_last_assistant_text(tmp_path):
    debug = tmp_path / "debug.jsonl"
    out = tmp_path / "answer.md"
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "second answer"}]}},
    ]
    debug.write_text("\n".join(json.dumps(event) for event in events) + "\n",
                     encoding="utf-8")
    assert extract_review(str(debug), str(out))
    assert out.read_text(encoding="utf-8") == "second answer\n"


def test_extract_review_returns_false_when_no_answer(tmp_path):
    debug = tmp_path / "debug.jsonl"
    out = tmp_path / "answer.md"
    debug.write_text(json.dumps({"type": "result", "result": "", "is_error": True}) + "\n",
                     encoding="utf-8")
    assert not extract_review(str(debug), str(out))
    assert not out.exists()


def test_extract_review_skips_malformed_lines(tmp_path):
    debug = tmp_path / "debug.jsonl"
    out = tmp_path / "answer.md"
    debug.write_text("not json\n" + json.dumps({"type": "result", "result": "ok"}) + "\n",
                     encoding="utf-8")
    assert extract_review(str(debug), str(out))
    assert out.read_text(encoding="utf-8") == "ok\n"


def test_trace_review_writes_concise_per_tool_trace(tmp_path):
    debug = tmp_path / "debug.jsonl"
    trace = tmp_path / "trace.log"
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": "code-review-python"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "reviewing 3 files"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "src/app.py"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "def foo():\n    pass"}]}},
        {"type": "result", "result": "done", "is_error": False},
    ]
    debug.write_text("\n".join(json.dumps(event) for event in events) + "\n",
                     encoding="utf-8")
    assert trace_review(str(debug), str(trace)) == 2
    content = trace.read_text(encoding="utf-8")
    assert "tool: Skill skill=code-review-python" in content
    assert "tool: Read file_path=src/app.py" in content
    assert "-> reviewing 3 files" in content
    assert "result: done" in content


def test_trace_review_no_tools_writes_result_line(tmp_path):
    debug = tmp_path / "debug.jsonl"
    trace = tmp_path / "trace.log"
    debug.write_text(json.dumps({"type": "result", "result": "ok", "is_error": False}) + "\n",
                     encoding="utf-8")
    assert trace_review(str(debug), str(trace)) == 0
    assert trace.read_text(encoding="utf-8") == "result: ok\n"


def test_trace_review_does_not_truncate_values(tmp_path):
    debug = tmp_path / "debug.jsonl"
    trace = tmp_path / "trace.log"
    long_path = "very/long/path/" + "p" * 200
    long_result = "r" * 300
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": long_path}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": long_result}]}},
        {"type": "result", "result": "ok", "is_error": False},
    ]
    debug.write_text("\n".join(json.dumps(event) for event in events) + "\n",
                     encoding="utf-8")
    assert trace_review(str(debug), str(trace)) == 1
    content = trace.read_text(encoding="utf-8")
    assert long_path in content
    assert long_result in content
    assert "..." not in content
