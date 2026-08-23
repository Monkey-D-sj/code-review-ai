import json
from pathlib import Path

from code_review_ai.full_agent_trace import render


def test_render_keeps_complete_order_and_compacts_worktree_prefix(
        tmp_path: Path):
    transcripts = tmp_path / "transcripts"
    transcript = transcripts / "case-a" / "full_project_core" / "run-1.json"
    transcript.parent.mkdir(parents=True)
    repo = str(tmp_path / "worktree")
    transcript.write_text(json.dumps({"repo_path": repo}), encoding="utf-8")
    report = {"runs": [{
        "case_id": "case-a", "mode": "full_project_core", "repetition": 1,
        "precision": 1, "recall": 1, "f1": 1, "elapsed_ms": 12,
        "files_read": ["src/a.py"],
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "tool_trace": [
            {"sequence": 1, "tool": "Read",
             "input": {"file_path": f"{repo}/src/a.py", "offset": 5,
                       "limit": 3}, "response_chars": 40},
            {"sequence": 2,
             "tool": "mcp__code-review-ai__get_change_summary",
             "input": {}, "response_chars": 80},
        ],
    }]}
    output = render(report, transcripts)
    assert f"cwd: `{repo}`" in output
    assert "01. Read | src/a.py:5-7 | response=40 chars" in output
    assert "02. MCP | get_change_summary {} | response=80 chars" in output
