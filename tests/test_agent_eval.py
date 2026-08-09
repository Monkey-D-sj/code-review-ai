import json
from pathlib import Path

import pytest

from conftest import FIXTURES, Q
from code_review_ai.agent_eval import (AgentRun, load_agent_cases,
                                       parse_agent_command, preflight_agent_eval,
                                       run_agent_eval,
                                       select_agent_cases, _context_files)
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


def test_real_smoke_manifest_has_ten_provenanced_cases():
    manifest = Path(__file__).parents[1] / "benchmarks" / "agent-eval-real-10.json"
    cases = load_agent_cases(str(manifest))
    assert len(cases) == 10
    assert len({case.case_id for case in cases}) == 10
    assert all(case.source_commit for case in cases)


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
