import json

import pytest

from code_review_ai.agent_eval import AgentRun, GoldFinding
from code_review_ai.full_agent_eval import (
    FullAgentCase, PreparedCase, _case_config, load_full_agent_cases,
    run_full_agent_eval, rescore_full_agent_report, select_full_agent_cases,
)
from code_review_ai.full_agent_eval import _prompt


def _case():
    return FullAgentCase(
        "real-fix", "sample", "https://github.com/example/sample.git", "abc123",
        ("src/app.py",), "review it",
        (GoldFinding("bug", "src/app.py", None, None, ("regression",)),),
    )


def test_load_full_agent_cases_validates_manifest(tmp_path):
    manifest = tmp_path / "cases.json"
    manifest.write_text(json.dumps([{
        "id": "real-fix", "repo_name": "sample",
        "repo_url": "https://github.com/example/sample.git",
        "source_commit": "abc123", "mutation_paths": ["src/app.py"],
        "prompt": "review", "gold_findings": [{
            "id": "bug", "file": "src/app.py", "keywords": ["regression"]}],
    }]), encoding="utf-8")
    cases = load_full_agent_cases(str(manifest))
    assert cases[0].mutation_paths == ("src/app.py",)
    assert select_full_agent_cases(cases, ["real-fix"]) == cases
    with pytest.raises(ValueError, match="unknown full eval"):
        select_full_agent_cases(cases, ["missing"])


def test_case_config_uses_metadata_only_change_summary(tmp_path):
    prepared = PreparedCase(_case(), str(tmp_path), "diff")
    config = _case_config(prepared, str(tmp_path / "case.db"))
    assert config.summary_source == "none"
    assert config.diff_base == "HEAD"


def test_native_and_project_share_the_same_review_policy(tmp_path):
    prepared = PreparedCase(
        _case(), str(tmp_path), "diff --git a/src/app.py b/src/app.py")
    native = _prompt(prepared, "native_agent")
    project = _prompt(prepared, "full_project_agent")

    shared_requirements = (
        "For every changed symbol",
        "inspect upstream callers first",
        "Inspect downstream callees",
        "inspect relevant tests, configuration, routing, dependency injection",
        "public API boundaries",
    )
    for requirement in shared_requirements:
        assert requirement in native
        assert requirement in project

    assert "Use them to obtain the repository evidence" in native
    assert "get_change_summary" not in native
    assert "query_graph" not in native
    assert "code-review-ai MCP tools" not in native
    assert "get_change_summary" in project
    assert "query_graph" in project
    assert "do not call rebuild_index" in project


def test_run_full_eval_pairs_native_and_project(monkeypatch, tmp_path):
    case = _case()
    prepared = PreparedCase(case, str(tmp_path),
                            "diff --git a/src/app.py b/src/app.py")
    monkeypatch.setattr(
        "code_review_ai.full_agent_eval.prepare_full_agent_cases",
        lambda cases, repos_dir, work_dir: [prepared],
    )
    prebuilt_db = tmp_path / "prebuilt.db"
    monkeypatch.setattr(
        "code_review_ai.full_agent_eval._prepare_case_index",
        lambda item, work_dir, label: {
            "case_id": item.case.case_id, "db_path": str(prebuilt_db),
            "nodes": 2, "edges": 1, "flows": 0, "elapsed_ms": 3.0,
            "timed_with_agent": False,
        },
    )

    def fake_executor(command, prompt, cwd, env, timeout):
        assert env["CRAI_EVAL_TOOL_PROFILE"] in {"native", "full_project"}
        assert env["CRAI_EVAL_DB_PATH"] == str(prebuilt_db)
        if env["CRAI_EVAL_TOOL_PROFILE"] == "full_project":
            assert "do not call rebuild_index" in prompt
            assert "get_impact only when" in prompt
        calls = (["Read", "mcp__code-review-ai__query_graph"]
                 if env["CRAI_EVAL_TOOL_PROFILE"] == "full_project" else ["Read"])
        payload = {"findings": [{
            "file": "src/app.py", "line": 1, "title": "regression",
            "description": "concrete regression"}],
            "files_read": ["src/app.py"], "tool_calls": calls,
            "tool_call_count": len(calls),
            "usage": {"input_tokens": 10, "output_tokens": 2}}
        return AgentRun(0, json.dumps(payload), "", 5.0)

    report = run_full_agent_eval(
        [case], str(tmp_path / "repos"), str(tmp_path / "runs"), ["agent"],
        executor=fake_executor,
    )
    assert len(report["runs"]) == 2
    assert report["aggregate"]["native_agent"]["macro_f1"] == 1.0
    assert report["aggregate"]["full_project_agent"]["mcp_adoption_rate"] == 1.0
    adoption = report["aggregate"]["full_project_agent"]["mcp_tool_adoption_rate"]
    assert adoption["query_graph"] == 1.0
    assert adoption["rebuild_index"] == 0.0
    assert report["index_setup"][0]["timed_with_agent"] is False


def test_rescore_uses_stored_outputs_and_filters_unavailable_tools(tmp_path):
    case = _case()
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({
        "schema_version": 1, "modes": ["native_agent"], "repetitions": 1,
        "runs": [{"case_id": case.case_id, "mode": "native_agent",
                  "repetition": 1, "success": True, "precision": 0,
                  "recall": 0, "f1": 0, "elapsed_ms": 1,
                  "files_read": [], "context_files": [],
                  "tool_calls": ["Read", "Bash"], "tool_call_count": 2,
                  "usage": {"input_tokens": 1, "output_tokens": 1}}],
    }), encoding="utf-8")
    transcript_dir = tmp_path / "transcripts"
    transcript = transcript_dir / case.case_id / "native_agent" / "run-1.json"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"parsed_output": {"findings": [{
        "file": "src/app.py", "title": "regression",
        "description": "concrete regression"}]}}), encoding="utf-8")
    rescored = rescore_full_agent_report(
        str(report_path), [case], str(transcript_dir))
    assert rescored["runs"][0]["f1"] == 1.0
    assert rescored["runs"][0]["tool_calls"] == ["Read"]
    assert rescored["rescored"]["gold_finding_count"] == 1
