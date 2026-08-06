import json
from pathlib import Path

from code_review_ai.extract import extract_review


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
