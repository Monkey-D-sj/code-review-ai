"""Scripted full-agent-eval: the eval harness run without an LLM.

The ``scripted`` agent adapter drives the same full-agent-eval wiring the real
claude adapter uses — CLI subprocess, eval env vars, and (for the core arm) a
real MCP server subprocess connected through the stdio protocol — but replaces
the model with a deterministic script. These tests therefore run the whole
pipeline with no claude login, no tokens, and no network: they guard the
harness wiring (prompt/env plumbing, transcript persistence, scoring,
aggregation) and, in the core arm, prove the graph tools genuinely answer on
the case's index.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from code_review_ai.full_agent_eval import (
    load_full_agent_cases, run_full_agent_eval, select_full_agent_cases,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED = REPO_ROOT / "benchmarks" / "fast-repo"
MANIFEST = REPO_ROOT / "benchmarks" / "fast-cases.json"

SCRIPTED_COMMAND = [sys.executable, "-m", "code_review_ai.agent_adapter",
                    "scripted"]


def _case(slug: str):
    return select_full_agent_cases(
        load_full_agent_cases(str(MANIFEST)), [slug])[0]


def _run(slug: str, tmp_path: Path, modes: tuple[str, ...]) -> dict:
    return run_full_agent_eval(
        [_case(slug)],
        str(tmp_path / "repos"), str(tmp_path / "work"),
        SCRIPTED_COMMAND,
        modes=modes, local_repo=str(SEED),
        timeout_seconds=300, workers=2)


@pytest.mark.slow
def test_scripted_full_agent_eval_runs_both_arms_without_claude(tmp_path):
    """native and core both run to completion with no claude login/tokens."""
    report = _run("caller-return-shape", tmp_path,
                  ("native_agent", "full_project_core"))
    assert len(report["runs"]) == 2
    for run in report["runs"]:
        assert run["success"] is True, run.get("failure_reason")
        assert run["parse_error"] is None
    assert report["aggregate"]["native_agent"]["mcp_adoption_rate"] == 0.0
    assert report["aggregate"]["full_project_core"]["mcp_adoption_rate"] == 1.0


@pytest.mark.slow
def test_scripted_core_arm_really_calls_graph_tools(tmp_path):
    """The core arm's MCP calls land on the real server and return entries."""
    report = _run("caller-return-shape", tmp_path,
                  ("full_project_core",))
    core = report["runs"][0]
    assert core["success"] is True
    calls = core["tool_calls"]
    assert "mcp__code-review-ai__get_change_summary" in calls
    assert "mcp__code-review-ai__get_impact" in calls
    # get_test_impact / get_change_context are excluded from the core tool set.
    assert "mcp__code-review-ai__get_test_impact" not in calls
    assert "mcp__code-review-ai__get_change_context" not in calls
    # The transcript persisted the run so the pipeline end is reachable.
    transcript = (tmp_path / "work" / "transcripts"
                  / "caller-return-shape" / "full_project_core" / "run-1.json")
    assert transcript.is_file()


@pytest.mark.slow
def test_scripted_full_agent_eval_aggregate_shape(tmp_path):
    """The report carries per-mode aggregates and the difficulty split."""
    report = _run("caller-return-shape", tmp_path,
                  ("native_agent", "full_project_core"))
    assert report["evaluation"] == "full_project_online_tool_use"
    assert report["modes"] == ["native_agent", "full_project_core"]
    assert report["difficulty_counts"]["unclassified"] == 1
    for mode in ("native_agent", "full_project_core"):
        aggregate = report["aggregate"][mode]
        assert "macro_f1" in aggregate
        assert "mcp_adoption_rate" in aggregate
        assert "mean_actual_tool_calls" in aggregate


@pytest.mark.slow
def test_scripted_agent_adapter_entrypoint():
    """The adapter exposes a scripted subcommand producing the JSON contract."""
    completed = subprocess.run(
        SCRIPTED_COMMAND + ["--scenario", "native"],
        input="diff --git a/src/x.py b/src/x.py",
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60)
    assert completed.returncode == 0
    import json
    payload = json.loads(completed.stdout)
    assert payload["findings"][0]["file"] == "src/x.py"
    assert "Read" in payload["tool_calls"]
