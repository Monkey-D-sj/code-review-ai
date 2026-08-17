import json

import pytest

from code_review_ai.agent_eval import AgentRun, GoldFinding
from code_review_ai.full_agent_eval import (
    FullAgentCase, PreparedCase, load_full_agent_cases, run_full_agent_eval,
    select_full_agent_cases,
)


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


def test_run_full_eval_pairs_native_and_project(monkeypatch, tmp_path):
    case = _case()
    prepared = PreparedCase(case, str(tmp_path),
                            "diff --git a/src/app.py b/src/app.py")
    monkeypatch.setattr(
        "code_review_ai.full_agent_eval.prepare_full_agent_cases",
        lambda cases, repos_dir, work_dir: [prepared],
    )

    def fake_executor(command, prompt, cwd, env, timeout):
        assert env["CRAI_EVAL_TOOL_PROFILE"] in {"native", "full_project"}
        calls = (["Read", "mcp__code-review-ai__get_impact"]
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


def test_review_methodology_skill_is_single_source():
    """The review methodology lives in one place — the bundled
    `code-review-methodology` skill — and both the eval harness and the
    post-commit hook derive their prompts from it, so the two can't drift.
    Every decision trigger must appear in the skill body and in the hook prompt
    composed from it; a future edit that drops a trigger fails here instead of
    silently diverging."""
    from code_review_ai.hooks import _build_review_prompt
    from code_review_ai.skills import load_skill_body
    triggers = [
        "签名",       # interface / signature change
        "参数",       # parameter removed / type / order
        "返回类型",   # return type change
        "异常",       # exception semantics / new exception
        "调用方",     # caller-dependent behavior
        "跨模块",     # cross-module call added / removed
        "路由",       # DI/routing wiring
        "内部",       # pure internal body only
        "拿不准",     # tiebreaker: doubt -> inspect
        "跨服务",     # depth: cross-service/RPC/API
        "删除",       # depth: deleted functions
        "调用点",     # depth: direct call sites vs full chain
    ]
    skill = load_skill_body("code-review-methodology")
    hook_prompt = _build_review_prompt()
    for trigger in triggers:
        assert trigger in skill, \
            f"methodology skill lost trigger {trigger!r}"
        assert trigger in hook_prompt, \
            f"hook prompt lost trigger {trigger!r}"


def test_eval_prompt_inlines_methodology_skill(tmp_path):
    """The eval forces the methodology inline rather than relying on the agent
    invoking the skill, so every benchmark run sees the exact same decision
    table regardless of the agent's skill-discipline."""
    from code_review_ai.full_agent_eval import _prompt
    from code_review_ai.skills import load_skill_body
    prepared = PreparedCase(_case(), str(tmp_path),
                            "diff --git a/src/app.py b/src/app.py")
    prompt = _prompt(prepared, "native_agent")
    assert load_skill_body("code-review-methodology") in prompt
