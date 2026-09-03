import json
from pathlib import Path

from code_review_ai.full_agent_trace import render, render_html, summarize_file


def _report(run: dict) -> dict:
    return {"schema_version": 2, "modes": [run["mode"]], "runs": [run]}


def _run(case_id: str, mode: str, sequence: int, tool: str) -> dict:
    return {
        "case_id": case_id, "mode": mode, "repetition": 1,
        "precision": 1, "recall": 1, "f1": 1, "elapsed_ms": 12,
        "files_read": ["src/a.py"], "read_calls": 1, "search_calls": 0,
        "bash_calls": 0, "unique_files_touched": ["src/a.py"],
        "tool_call_count": 1,
        "usage": {"input_tokens": 10, "output_tokens": 2,
                  "total_cost_usd": 0.01},
        "tool_trace": [{"sequence": sequence, "tool": tool,
                        "input": {"file_path": "src/a.py"},
                        "response_chars": 40}],
    }


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


def test_summarize_file_merges_multiple_reports(tmp_path: Path):
    native = tmp_path / "native.json"
    core = tmp_path / "core.json"
    native.write_text(json.dumps(_report(_run(
        "case-a", "native_agent", 1, "Read"))), encoding="utf-8")
    core.write_text(json.dumps(_report(_run(
        "case-a", "full_project_core", 1,
        "mcp__code-review-ai__get_impact"))), encoding="utf-8")

    md = summarize_file([native, core], None, None)
    assert "native_agent" in md and "full_project_core" in md

    html = summarize_file([native, core], None, None, as_html=True)
    assert html.startswith("<!DOCTYPE")
    assert "native_agent" in html and "full_project_core" in html
    assert ">READ<" in html and ">MCP<" in html


def test_render_html_default_open_and_collapsible():
    html = render_html(_report(_run("case-a", "native_full", 1, "Read")))
    assert "<section class='run open'>" in html
    assert ".run.open .steps { display:block; }" in html
    assert ".step.open .caret" in html
    # native_full uses every built-in tool; the step carries its args.
    assert ">READ<" in html and "src/a.py" in html


def test_render_html_shows_full_graph_response_json():
    run = _run("case-a", "full_project_core", 1,
               "mcp__code-review-ai__get_impact")
    run["tool_trace"] = [{
        "sequence": 1,
        "tool": "mcp__code-review-ai__get_impact",
        "input": {"symbols": ["app::target"]},
        "response_chars": 40,
        "response": '{"impacted":[{"symbol":"app::target","line":3}]}',
    }]
    html = render_html(_report(run))
    # The complete returned JSON is rendered (pretty-printed), not just a size.
    # Quotes are HTML-escaped in the <pre>, so match the escaped form.
    assert "&quot;impacted&quot;" in html
    assert "&quot;app::target&quot;" in html
    assert "40 chars" in html
    assert "response" in html


def test_render_marks_rejected_tool_calls_without_treating_them_as_errors():
    run = _run("case-a", "full_project_core", 1, "read_file")
    run["tool_trace"][0]["status"] = "rejected_policy"

    assert "REJECTED_POLICY" in render(_report(run))
    assert "REJECTED_POLICY" in render_html(_report(run))
