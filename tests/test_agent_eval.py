import json
import subprocess
from pathlib import Path

import pytest

from conftest import FIXTURES, Q
from code_review_ai.agent_eval import (AgentRun, GoldFinding, load_agent_cases,
                                       parse_agent_command, preflight_agent_eval,
                                       run_agent_eval,
                                       select_agent_cases, _context_files,
                                       _agent_prompt, _mode_metrics, _score)
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema


def _manifest(tmp_path, records):
    path = tmp_path / "agent-cases.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def _case_record():
    return {
        "id": "login-review",
        "prompt": "Review the login change for authentication regressions.",
        "diff": "- return False\n+ return True",
        "changed_symbols": [Q("auth", "login")],
        "gold_findings": [{
            "id": "auth-bypass", "file": "auth.py",
            "line_start": 4, "line_end": 8,
            "keywords": ["authentication", "bypass"],
        }],
    }


def _config(tmp_path):
    config = load_config(FIXTURES)
    config.repo_path = FIXTURES
    config.db_path = str(tmp_path / "agent-eval.db")
    config.community_detection = False
    return config


def test_load_agent_cases_validates_manifest(tmp_path):
    cases = load_agent_cases(str(_manifest(tmp_path, [_case_record()])))
    assert cases[0].case_id == "login-review"
    assert cases[0].gold_findings[0].keywords == ("authentication", "bypass")


def test_load_agent_cases_rejects_missing_golds(tmp_path):
    record = _case_record()
    record["gold_findings"] = []
    with pytest.raises(ValueError, match="gold_findings"):
        load_agent_cases(str(_manifest(tmp_path, [record])))


def test_controlled_prompt_uses_shared_review_policy():
    prompt = _agent_prompt("diff_only", "TASK\nreview\n\nDIFF\npatch")
    assert "对于每个发生变更的符号" in prompt
    assert "先检查上游调用方" in prompt
    assert "检查下游被调用方" in prompt


def test_select_agent_cases_and_reject_unknown(tmp_path):
    first = _case_record()
    second = {**_case_record(), "id": "second-case"}
    cases = load_agent_cases(str(_manifest(tmp_path, [first, second])))
    assert [case.case_id for case in select_agent_cases(
        cases, ["second-case"])] == ["second-case"]
    with pytest.raises(ValueError, match="unknown"):
        select_agent_cases(cases, ["missing-case"])


def test_run_agent_eval_scores_modes_and_writes_transcripts(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.db_path)
    init_schema(conn)
    cases = load_agent_cases(str(_manifest(tmp_path, [_case_record()])))
    seen_prompts = []

    def fake_executor(command, prompt, cwd, environment, timeout):
        seen_prompts.append((environment["CRAI_EVAL_MODE"], prompt))
        output = {
            "findings": [{"file": "auth.py", "line": 5,
                          "title": "Authentication bypass", "description": "bad"}],
            "files_read": ["auth.py"], "tool_calls": ["get_impact"],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        return AgentRun(0, json.dumps(output), "", 12.5)

    report = run_agent_eval(config, conn, cases, ["fake-agent"],
                            str(tmp_path / "runs"), repetitions=2,
                            executor=fake_executor)

    assert len(report["runs"]) == 8
    assert report["aggregate"]["graph_agent"]["macro_recall"] == 1.0
    assert report["aggregate"]["diff_only"]["mean_input_tokens"] == 100.0
    assert report["aggregate"]["diff_only"]["mean_cache_read_input_tokens"] == 0.0
    assert any("CODE GRAPH CONTEXT" in prompt for _, prompt in seen_prompts)
    assert any("LEXICAL SEARCH CONTEXT" in prompt for _, prompt in seen_prompts)
    assert any("HYBRID CODE CONTEXT" in prompt for _, prompt in seen_prompts)
    assert (tmp_path / "runs/login-review/graph_agent/run-2.json").exists()
    graph_prompt = next(prompt for mode, prompt in seen_prompts
                        if mode == "graph_agent")
    assert str(FIXTURES).replace("\\", "/") not in graph_prompt.replace("\\", "/")
    hybrid_prompt = next(prompt for mode, prompt in seen_prompts
                         if mode == "hybrid_agent")
    assert "def login" in hybrid_prompt
    assert "max_chars" in hybrid_prompt
    hybrid_payload = json.loads(hybrid_prompt.split("HYBRID CODE CONTEXT\n", 1)[1])
    assert len(json.dumps(hybrid_payload, ensure_ascii=False)) <= hybrid_payload["max_chars"]


def test_run_agent_eval_records_invalid_json_without_crashing(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.db_path)
    init_schema(conn)
    cases = load_agent_cases(str(_manifest(tmp_path, [_case_record()])))

    def bad_executor(command, prompt, cwd, environment, timeout):
        return AgentRun(0, "not-json", "", 1.0)

    report = run_agent_eval(config, conn, cases, ["fake"], str(tmp_path / "runs"),
                            modes=("diff_only",), executor=bad_executor)
    assert report["runs"][0]["success"] is False
    assert report["runs"][0]["parse_error"].startswith("invalid JSON")
    assert report["aggregate"]["diff_only"]["success_rate"] == 0.0


def test_run_agent_eval_parallel_workers_preserve_job_order(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.db_path)
    init_schema(conn)
    cases = load_agent_cases(str(_manifest(tmp_path, [_case_record()])))

    def fake_executor(command, prompt, cwd, environment, timeout):
        output = {"findings": [], "files_read": [], "tool_calls": []}
        return AgentRun(0, json.dumps(output), "", 1.0)

    report = run_agent_eval(
        config, conn, cases, ["fake"], str(tmp_path / "runs"),
        modes=("diff_only", "graph_agent"), repetitions=3, workers=3,
        executor=fake_executor)
    assert report["workers"] == 3
    assert [(run["mode"], run["repetition"]) for run in report["runs"]] == [
        ("diff_only", 1), ("diff_only", 2), ("diff_only", 3),
        ("graph_agent", 1), ("graph_agent", 2), ("graph_agent", 3),
    ]


def test_preflight_reports_symbol_and_context_coverage(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.db_path)
    init_schema(conn)
    cases = load_agent_cases(str(_manifest(tmp_path, [_case_record()])))
    report = preflight_agent_eval(config, conn, cases,
                                  modes=("diff_only", "hybrid_agent"))
    assert report["dry_run"] is True
    assert report["aggregate"]["symbol_found_rate"] == 1.0
    assert report["aggregate"]["modes"]["hybrid_agent"]["max_characters"] > 0


def test_parse_agent_command_supports_quoted_arguments():
    assert parse_agent_command('agent --model "model name"') == [
        "agent", "--model", "model name"]


def test_parse_agent_command_preserves_windows_path():
    command = parse_agent_command(
        r'C:\Users\person\project\.venv\Scripts\python.exe -m adapter')
    assert command[0] == r"C:\Users\person\project\.venv\Scripts\python.exe"


def test_parse_agent_command_accepts_json_array():
    assert parse_agent_command('["agent", "--model", "model name"]') == [
        "agent", "--model", "model name"]


def test_context_files_does_not_count_json_as_javascript(tmp_path):
    config = _config(tmp_path)
    context = '"manifest": "cases.json", "source": "src/app.js"'
    assert _context_files(config, context) == ["src/app.js"]


def test_score_uses_maximum_matching_for_related_golds():
    golds = (
        GoldFinding("general", "a.py", None, None, ("serializer",)),
        GoldFinding("specific", "a.py", None, None, ("urlsafe",)),
    )
    predictions = [
        {"file": "a.py", "title": "URLSafe serializer forwarding",
         "description": "urlsafe serializer"},
        {"file": "a.py", "title": "Serializer override removed",
         "description": "serializer"},
    ]
    score = _score(predictions, golds)
    assert score["f1"] == 1.0
    assert {match["gold_id"] for match in score["matched_findings"]} == {
        "general", "specific"}


def test_score_accepts_an_alternate_root_cause_file():
    golds = (GoldFinding(
        "cross-layer", "src/schema.sql", None, None, ("unique",),
        ("src/controller.java",)),)
    score = _score([{
        "file": "src/controller.java", "title": "Missing unique handling",
        "description": "duplicate names are not rejected",
    }], golds)
    assert score["f1"] == 1.0


def test_score_min_matches_requires_causal_description():
    # Surface keyword echo ("none") alone must not score: min_matches=2 needs a
    # second diagnostic term, e.g. the crash site or the error type.
    golds = (GoldFinding(
        "deep-crash", "src/config.py", None, None,
        ("typeerror", "compute_wait", "none"), min_matches=2),)
    surface = _score([{
        "file": "src/config.py", "title": "timeout may be None",
        "description": "timeout default dropped, needs a fallback",
    }], golds)
    assert surface["f1"] == 0.0
    causal = _score([{
        "file": "src/config.py", "title": "timeout None crashes wait math",
        "description": "compute_wait(None) raises TypeError on None * 1000",
    }], golds)
    assert causal["f1"] == 1.0


def test_agent_eval_builds_context_from_mutated_source_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email",
                    "eval@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Eval"],
                   check=True)
    source = repo / "app.py"
    filler = "".join(f"# filler {index}\n" for index in range(20))
    source.write_text(
        'def target():\n    return "broken"\n\n' + filler +
        '\ndef other():\n    return "other-broken"\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "bug"],
                   check=True)
    source.write_text(
        'def target():\n    return "fixed"\n\n' + filler +
        '\ndef other():\n    return "other-fixed"\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "fix"],
                   check=True)
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True).stdout.strip()
    record = _case_record()
    record.update({
        "source_commit": commit,
        "changed_symbols": ["app::target"],
        "diff": ("diff --git a/app.py b/app.py\n"
                 "--- a/app.py\n+++ b/app.py\n"
                 "@@ -1,2 +1,2 @@\n def target():\n"
                 "-    return \"fixed\"\n+    return \"broken\""),
    })
    config = load_config(str(repo))
    config.repo_path = str(repo)
    config.db_path = str(tmp_path / "index.db")
    config.community_detection = False
    conn = connect(config.db_path)
    init_schema(conn)
    observed = {}

    def fake_executor(command, prompt, cwd, environment, timeout):
        observed["cwd"] = cwd
        observed["source"] = (Path(cwd) / "app.py").read_text(encoding="utf-8")
        observed["prompt"] = prompt
        return AgentRun(0, json.dumps({"findings": [], "files_read": [],
                                       "tool_calls": []}), "", 1.0)

    run_agent_eval(
        config, conn, load_agent_cases(str(_manifest(tmp_path, [record]))),
        ["fake"], str(tmp_path / "runs"), modes=("hybrid_agent",),
        executor=fake_executor)

    assert observed["cwd"] != str(repo)
    assert 'return "broken"' in observed["source"]
    assert 'return "other-fixed"' in observed["source"]
    assert 'return "other-broken"' not in observed["source"]
    assert 'return "broken"' in observed["prompt"]
    assert 'return "fixed"' in source.read_text(encoding="utf-8")
    assert not Path(observed["cwd"]).exists()


def test_mode_metrics_accepts_reports_without_usage():
    metrics = _mode_metrics([{
        "success": True, "precision": 0.0, "recall": 0.0, "f1": 0.0,
        "elapsed_ms": 1.0, "files_read": [], "context_files": [],
        "tool_calls": [],
    }])
    assert metrics["mean_input_tokens"] == 0.0
    assert metrics["total_cost_usd"] == 0.0
