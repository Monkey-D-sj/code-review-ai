import json
from pathlib import Path

import pytest

from code_review_ai.agent_eval import (AgentRun, GoldFinding,
                                       SHARED_REVIEW_POLICY)
from code_review_ai.full_agent_eval import (
    DEFAULT_FULL_EVAL_MODES, FULL_EVAL_MODES, FullAgentCase, PreparedCase,
    _case_config,
    load_full_agent_cases,
    run_full_agent_eval, rescore_full_agent_report, select_full_agent_cases,
)
from code_review_ai.full_agent_eval import _prompt


def _case():
    return FullAgentCase(
        "real-fix", "sample", "https://github.com/example/sample.git", "abc123",
        ("src/app.py",), "review it",
        (GoldFinding("bug", "src/app.py", None, None, ("regression",)),),
        difficulty="medium",
    )


def test_load_full_agent_cases_validates_manifest(tmp_path):
    manifest = tmp_path / "cases.json"
    manifest.write_text(json.dumps([{
        "id": "real-fix", "repo_name": "sample",
        "repo_url": "https://github.com/example/sample.git",
        "source_commit": "abc123", "mutation_paths": ["src/app.py"],
        "difficulty": "hard",
        "prompt": "review", "gold_findings": [{
            "id": "bug", "file": "src/app.py", "keywords": ["regression"]}],
    }]), encoding="utf-8")
    cases = load_full_agent_cases(str(manifest))
    assert cases[0].mutation_paths == ("src/app.py",)
    assert cases[0].difficulty == "hard"
    assert select_full_agent_cases(cases, ["real-fix"]) == cases
    with pytest.raises(ValueError, match="unknown full eval"):
        select_full_agent_cases(cases, ["missing"])


def test_load_full_agent_cases_rejects_invalid_difficulty(tmp_path):
    manifest = tmp_path / "cases.json"
    manifest.write_text(json.dumps([{
        "id": "real-fix", "repo_name": "sample",
        "repo_url": "https://github.com/example/sample.git",
        "source_commit": "abc123", "mutation_paths": ["src/app.py"],
        "difficulty": "impossible", "prompt": "review",
        "gold_findings": [{
            "id": "bug", "file": "src/app.py", "keywords": ["regression"]}],
    }]), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid difficulty"):
        load_full_agent_cases(str(manifest))


def test_case_config_uses_metadata_only_change_summary(tmp_path):
    prepared = PreparedCase(_case(), str(tmp_path), "diff")
    config = _case_config(prepared, str(tmp_path / "case.db"))
    assert config.summary_source == "none"
    assert config.diff_base == "HEAD"


def test_loop_arms_share_policy_and_differ_only_in_graph_tools(tmp_path):
    prepared = PreparedCase(
        _case(), str(tmp_path), "diff --git a/src/app.py b/src/app.py")
    full = _prompt(prepared, "loop_full")
    nograph = _prompt(prepared, "loop_nograph")

    assert SHARED_REVIEW_POLICY in full
    assert SHARED_REVIEW_POLICY in nograph

    assert "get_impact" in full
    assert "get_change_summary" in full
    assert "未开放图检索工具" not in full

    assert "未开放图检索工具" in nograph
    assert "使用这些工具获取评审策略所需的仓库证据" in nograph
    assert "get_impact" not in nograph
    assert "get_change_summary" not in nograph

    # Single-fix-unit guidance and the read-only guard survive in every arm.
    assert "将同一缺陷的多个表现合并为一个发现" in full
    assert "按独立修复单元组织发现" in full
    assert "修复一个生产代码位置后另一个回归仍然存在" in full
    assert "不要用一个宽泛总括项吞并多个可独立修复的缺陷" in full
    assert "本评估强制以只读方式执行" in full
    assert "禁止使用 git log、git show 或任何 git diff" in full


def test_loop_full_prompt_strips_guidance_when_env_set(tmp_path, monkeypatch):
    prepared = PreparedCase(
        _case(), str(tmp_path), "diff --git a/src/app.py b/src/app.py")
    stripped = _prompt(prepared, "loop_full")
    assert SHARED_REVIEW_POLICY in stripped
    assert "这是本模式区别于" in stripped

    monkeypatch.setenv("CRAI_EVAL_NO_GUIDANCE", "1")
    stripped = _prompt(prepared, "loop_full")
    assert SHARED_REVIEW_POLICY not in stripped
    assert "这是本模式区别于" not in stripped
    # The task contract, read-only guard and output schema stay intact.
    assert "你正在对" in stripped
    assert "本评估强制以只读方式执行" in stripped
    assert "任务" in stripped
    assert "差异" in stripped


def test_run_full_eval_pairs_loop_arms(monkeypatch, tmp_path):
    case = _case()
    prepared = PreparedCase(case, str(tmp_path),
                            "diff --git a/src/app.py b/src/app.py")
    monkeypatch.setattr(
        "code_review_ai.full_agent_eval.prepare_full_agent_cases",
        lambda cases, repos_dir, work_dir, **kwargs: [prepared],
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
    monkeypatch.setattr(
        "code_review_ai.full_agent_eval._graph_retrieval_result",
        lambda item, setup: {
            "case_id": item.case.case_id,
            "changed_symbols": ["app::target"],
            "found_symbols": ["app::target"],
            "evidence": {"symbols": [], "files": [],
                         "entries": [], "tests": []},
            "score": {
                **{name: {"applicable": False, "expected": 0,
                          "returned": 0, "hits": [], "misses": [],
                          "precision": None, "recall": None, "f1": None}
                   for name in ("symbols", "files", "entries", "tests")},
                "macro_recall": None,
                "hard_negatives": {"applicable": False, "expected": 0,
                                   "hits": {"symbols": [], "files": []},
                                   "correctness": None},
            },
        },
    )

    def fake_executor(command, prompt, cwd, env, timeout):
        assert env["CRAI_EVAL_TOOL_PROFILE"] in {"native", "full_project"}
        assert env["CRAI_EVAL_DB_PATH"] == str(prebuilt_db)
        mode = env["CRAI_EVAL_MODE"]
        assert mode in FULL_EVAL_MODES
        if mode == "loop_full":
            assert env["CRAI_EVAL_TOOL_PROFILE"] == "full_project"
            assert "get_impact" in prompt
            assert "get_change_summary" in prompt
            assert "未开放图检索工具" not in prompt
            calls = ["get_change_summary", "get_impact", "read_file"]
        else:
            assert env["CRAI_EVAL_TOOL_PROFILE"] == "native"
            assert "未开放图检索工具" in prompt
            assert "get_impact" not in prompt
            calls = ["read_file", "search_code"]
        payload = {"findings": [{
            "file": "src/app.py", "line": 1, "title": "regression",
            "description": "concrete regression"}],
            "files_read": ["src/app.py"], "tool_calls": calls,
            "tool_call_count": len(calls),
            "usage": {"input_tokens": 10, "output_tokens": 2}}
        return AgentRun(0, json.dumps(payload), "", 5.0)

    report = run_full_agent_eval(
        [case], str(tmp_path / "repos"), str(tmp_path / "runs"), ["agent"],
        modes=FULL_EVAL_MODES,
        executor=fake_executor,
    )
    assert len(report["runs"]) == 2
    assert report["aggregate"]["loop_full"]["macro_f1"] == 1.0
    assert report["aggregate"]["loop_nograph"]["macro_f1"] == 1.0
    # The loop arms drive their own tools, not the MCP server.
    assert report["aggregate"]["loop_full"]["mcp_adoption_rate"] == 0.0
    assert report["aggregate"]["loop_nograph"]["mcp_adoption_rate"] == 0.0
    assert report["difficulty_counts"] == {"medium": 1}
    assert {run["difficulty"] for run in report["runs"]} == {"medium"}
    assert report["index_setup"][0]["timed_with_agent"] is False
    assert report["graph_retrieval"]["aggregate"]["symbol_found_rate"] == 1.0


def test_default_full_eval_is_the_loop_with_graph_tools():
    assert DEFAULT_FULL_EVAL_MODES == ("loop_full",)


def test_run_once_injects_eval_model_env(monkeypatch, tmp_path):
    """CRAI_EVAL_MODEL is forwarded so every arm runs the same model."""
    monkeypatch.setenv("CRAI_EVAL_MODEL", "deepseek-v4-flash")
    case = _case()
    prepared = PreparedCase(case, str(tmp_path),
                            "diff --git a/src/app.py b/src/app.py")
    monkeypatch.setattr(
        "code_review_ai.full_agent_eval.prepare_full_agent_cases",
        lambda cases, repos_dir, work_dir, **kwargs: [prepared],
    )
    monkeypatch.setattr(
        "code_review_ai.full_agent_eval._prepare_case_index",
        lambda item, work_dir, label: {
            "case_id": item.case.case_id, "db_path": str(tmp_path / "x.db"),
            "nodes": 1, "edges": 0, "flows": 0, "elapsed_ms": 1.0,
            "timed_with_agent": False,
        },
    )
    monkeypatch.setattr(
        "code_review_ai.full_agent_eval._graph_retrieval_result",
        lambda item, setup: {
            "case_id": item.case.case_id, "changed_symbols": [],
            "found_symbols": [], "evidence": {"symbols": [], "files": [],
                                              "entries": [], "tests": []},
            "score": {
                **{name: {"applicable": False, "expected": 0,
                          "returned": 0, "hits": [], "misses": [],
                          "precision": None, "recall": None, "f1": None}
                   for name in ("symbols", "files", "entries", "tests")},
                "macro_recall": None,
                "hard_negatives": {"applicable": False, "expected": 0,
                                   "hits": {"symbols": [], "files": []},
                                   "correctness": None},
            },
        },
    )
    observed = {}

    def fake_executor(command, prompt, cwd, env, timeout):
        observed["env"] = env
        payload = {"findings": [], "files_read": [], "tool_calls": [],
                   "tool_call_count": 0,
                   "usage": {"input_tokens": 1, "output_tokens": 1}}
        return AgentRun(0, json.dumps(payload), "", 1.0)

    run_full_agent_eval(
        [case], str(tmp_path / "repos"), str(tmp_path / "runs"), ["agent"],
        modes=("loop_full",), executor=fake_executor,
    )
    assert observed["env"]["CRAI_EVAL_MODEL"] == "deepseek-v4-flash"


def test_rescore_uses_stored_outputs_and_keeps_tool_calls(tmp_path):
    case = _case()
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({
        "schema_version": 1, "modes": ["loop_nograph"], "repetitions": 1,
        "runs": [{"case_id": case.case_id, "mode": "loop_nograph",
                  "repetition": 1, "success": True, "precision": 0,
                  "recall": 0, "f1": 0, "elapsed_ms": 1,
                  "files_read": [], "context_files": [],
                  "tool_calls": ["Read", "Bash"], "tool_call_count": 2,
                  "usage": {"input_tokens": 1, "output_tokens": 1}}],
    }), encoding="utf-8")
    transcript_dir = tmp_path / "transcripts"
    transcript = transcript_dir / case.case_id / "loop_nograph" / "run-1.json"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"parsed_output": {"findings": [{
        "file": "src/app.py", "title": "regression",
        "description": "concrete regression"}]}}), encoding="utf-8")
    rescored = rescore_full_agent_report(
        str(report_path), [case], str(transcript_dir))
    assert rescored["runs"][0]["f1"] == 1.0
    assert rescored["runs"][0]["tool_calls"] == ["Read", "Bash"]
    assert rescored["runs"][0]["difficulty"] == "medium"
    assert rescored["rescored"]["gold_finding_count"] == 1
