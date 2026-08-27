import json
from pathlib import Path

import pytest

from code_review_ai.agent_eval import (AgentRun, GoldFinding,
                                       SHARED_REVIEW_POLICY)
from code_review_ai.full_agent_eval import (
    DEFAULT_FULL_EVAL_MODES, FULL_EVAL_MODES, FullAgentCase, PreparedCase,
    _CORE_EXCLUDED_MCP_TOOLS, _CORE_MCP_TOOLS, _case_config,
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


def test_native_and_project_share_the_same_review_policy(tmp_path):
    prepared = PreparedCase(
        _case(), str(tmp_path), "diff --git a/src/app.py b/src/app.py")
    native = _prompt(prepared, "native_agent")
    project = _prompt(prepared, "full_project_agent")

    assert SHARED_REVIEW_POLICY in native
    assert SHARED_REVIEW_POLICY in project

    assert "使用这些工具获取评审策略所需的仓库证据" in native
    assert "get_change_summary" not in native
    assert "query_graph" not in native
    assert "code-review-ai MCP tools" not in native
    assert "get_change_summary" in project
    assert "query_graph" in project
    assert "不要调用 rebuild_index" in project


def test_querygraph_mode_prompt_mentions_only_query_graph(tmp_path):
    prepared = PreparedCase(
        _case(), str(tmp_path), "diff --git a/src/app.py b/src/app.py")
    querygraph = _prompt(prepared, "full_project_querygraph")
    assert "query_graph" in querygraph
    assert "get_change_summary" not in querygraph
    assert "get_impact" not in querygraph
    assert "不要调用 rebuild_index" in querygraph
    assert "最多调用两次 query_graph" in querygraph
    assert "max_neighbors=5" in querygraph
    assert "不要查询每个变更符号" in querygraph
    assert SHARED_REVIEW_POLICY in querygraph


def test_core_mode_exposes_review_tools_except_explicit_exclusions(tmp_path):
    prepared = PreparedCase(
        _case(), str(tmp_path), "diff --git a/src/app.py b/src/app.py")
    core = _prompt(prepared, "full_project_core")
    assert _CORE_MCP_TOOLS == (
        "get_impact", "get_change_summary", "search_symbol",
    )
    assert _CORE_EXCLUDED_MCP_TOOLS == {
        "rebuild_index", "get_communities", "get_community",
        "call_external_service", "find_dead_code", "query_graph",
        "get_change_context", "get_test_impact",
    }
    for tool in (*_CORE_MCP_TOOLS, *_CORE_EXCLUDED_MCP_TOOLS):
        assert tool in core
    # get_change_context is off: get_impact's direct call_site already carries
    # the call line code, so a separate per-symbol expansion would be redundant.
    assert "get_change_context（已关闭）" in core
    # get_test_impact is off too: test selection is CI's job, not LLM review.
    assert "get_test_impact（测试选择）是 CI 的职责" in core
    # get_symbol_detail is removed entirely: get_impact covers its info.
    assert "get_symbol_detail 的信息已被 get_impact 覆盖，已删除" in core
    # The first tool call must be get_change_summary, before any native tool.
    assert "第一个工具调用必须是 get_change_summary" in core
    assert "在任何其他工具之前（包括所有原生只读工具）" in core
    # get_impact returns direct neighbors + a depth summary, not the full closure.
    assert "depth 摘要" in core
    assert "max_level=0" in core
    assert "将同一缺陷的多个表现合并为一个发现" in core
    assert "按独立修复单元组织发现" in core
    assert "修复一个生产代码位置后另一个回归仍然存在" in core
    assert "不要用一个宽泛总括项吞并多个可独立修复的缺陷" in core
    assert "本评估强制以只读方式执行" in core
    assert "允许列表中的只读 Bash 命令" in core
    assert "不能运行脚本、测试、包管理器" in core
    assert "禁止使用 git log、git show 或任何 git diff" in core
    assert "Read/Glob/Grep/Bash" not in core
    assert SHARED_REVIEW_POLICY in core


def test_core_mode_prompt_strips_guidance_when_env_set(tmp_path, monkeypatch):
    prepared = PreparedCase(
        _case(), str(tmp_path), "diff --git a/src/app.py b/src/app.py")
    stripped = _prompt(prepared, "full_project_core")
    assert SHARED_REVIEW_POLICY in stripped
    assert "评审主通道是 get_impact" in stripped

    monkeypatch.setenv("CRAI_EVAL_NO_GUIDANCE", "1")
    stripped = _prompt(prepared, "full_project_core")
    assert SHARED_REVIEW_POLICY not in stripped
    assert "评审主通道是 get_impact" not in stripped
    # The task contract, read-only guard and output schema stay intact.
    assert "你正在对" in stripped
    assert "本评估强制以只读方式执行" in stripped
    assert "任务" in stripped
    assert "差异" in stripped


def test_run_full_eval_pairs_native_and_project(monkeypatch, tmp_path):
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
        assert env["CRAI_EVAL_TOOL_PROFILE"] in {"native", "native_full",
                                                 "full_project"}
        assert env["CRAI_EVAL_DB_PATH"] == str(prebuilt_db)
        mode = env["CRAI_EVAL_MODE"]
        if mode == "native_full":
            assert env["CRAI_EVAL_TOOL_PROFILE"] == "native_full"
            assert "Claude Code 自带的全部内置工具" in prompt
            assert "get_change_summary" not in prompt
            calls = ["Read"]
        elif mode == "full_project_agent":
            assert "不要调用 rebuild_index" in prompt
            assert "query_graph" in prompt
            assert "get_change_summary" in prompt
            assert "get_impact" not in prompt
            calls = ["Read", "mcp__code-review-ai__query_graph"]
        elif mode == "full_project_querygraph":
            assert "不要调用 rebuild_index" in prompt
            assert "query_graph" in prompt
            assert "get_change_summary" not in prompt
            assert "get_impact" not in prompt
            assert env["CRAI_EVAL_MCP_TOOLS"] == "query_graph"
            calls = ["Read", "mcp__code-review-ai__query_graph"]
        elif mode == "full_project_summary":
            assert "不要调用 rebuild_index" in prompt
            assert "get_change_summary" in prompt
            assert "query_graph" not in prompt
            assert env["CRAI_EVAL_MCP_TOOLS"] == "get_change_summary"
            calls = ["Read", "mcp__code-review-ai__get_change_summary"]
        elif mode == "full_project_search":
            assert "不要调用 rebuild_index" in prompt
            assert "search_symbol" in prompt
            assert "query_graph" not in prompt
            assert env["CRAI_EVAL_MCP_TOOLS"] == "search_symbol"
            calls = ["Read", "mcp__code-review-ai__search_symbol"]
        elif mode == "full_project_core":
            assert "get_change_context（已关闭）" in prompt
            assert "get_test_impact（测试选择）是 CI 的职责" in prompt
            assert "get_symbol_detail 的信息已被 get_impact 覆盖" in prompt
            assert "query_graph" in prompt
            assert "get_change_summary" in prompt
            assert "search_symbol" in prompt
            assert "get_impact" in prompt
            assert env["CRAI_EVAL_MCP_TOOLS"] == (
                "get_impact,get_change_summary,search_symbol")
            calls = ["Read", "mcp__code-review-ai__search_symbol"]
        else:
            calls = ["Read"]
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
    assert len(report["runs"]) == 7
    assert report["aggregate"]["native_agent"]["macro_f1"] == 1.0
    assert report["aggregate"]["native_full"]["mcp_adoption_rate"] == 0.0
    assert report["difficulty_counts"] == {"medium": 1}
    assert {run["difficulty"] for run in report["runs"]} == {"medium"}
    assert report["aggregate"]["full_project_agent"]["mcp_adoption_rate"] == 1.0
    adoption = report["aggregate"]["full_project_agent"]["mcp_tool_adoption_rate"]
    assert adoption["query_graph"] == 1.0
    assert adoption["rebuild_index"] == 0.0
    assert report["aggregate"]["full_project_querygraph"]["mcp_adoption_rate"] == 1.0
    assert report["aggregate"]["full_project_core"]["mcp_adoption_rate"] == 1.0
    assert report["aggregate"]["full_project_core"][
        "mcp_tool_adoption_rate"
    ]["search_symbol"] == 1.0
    # get_change_context and get_test_impact are excluded from the core set.
    assert report["aggregate"]["full_project_core"][
        "mcp_tool_adoption_rate"
    ]["get_change_context"] == 0.0
    assert report["aggregate"]["full_project_core"][
        "mcp_tool_adoption_rate"
    ]["get_test_impact"] == 0.0
    assert report["index_setup"][0]["timed_with_agent"] is False
    assert report["graph_retrieval"]["aggregate"]["symbol_found_rate"] == 1.0


def test_default_full_eval_is_native_vs_compact_core():
    assert DEFAULT_FULL_EVAL_MODES == ("native_agent", "full_project_core")


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
        modes=("native_agent",), executor=fake_executor,
    )
    assert observed["env"]["CRAI_EVAL_MODEL"] == "deepseek-v4-flash"


def test_rescore_uses_stored_outputs_and_keeps_native_bash_tools(tmp_path):
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
    assert rescored["runs"][0]["tool_calls"] == ["Read", "Bash"]
    assert rescored["runs"][0]["difficulty"] == "medium"
    assert rescored["rescored"]["gold_finding_count"] == 1
