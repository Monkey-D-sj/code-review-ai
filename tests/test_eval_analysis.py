import pytest

from code_review_ai.eval_analysis import analyze_agent_report


def _run(mode, repetition, f1, recall, precision, cost=0.1, success=True):
    return {"case_id": "case-1", "mode": mode, "repetition": repetition,
            "f1": f1, "recall": recall, "precision": precision,
            "success": success, "usage": {"total_cost_usd": cost}}


def test_analyze_agent_report_builds_mode_and_paired_statistics():
    runs = [
        _run("diff_only", 1, 0.0, 0.0, 0.0),
        _run("diff_only", 2, 1.0, 1.0, 1.0),
        _run("search_baseline", 1, 1.0, 1.0, 1.0),
        _run("search_baseline", 2, 1.0, 1.0, 1.0),
    ]
    analysis = analyze_agent_report(
        {"schema_version": 1, "repetitions": 2, "runs": runs},
        bootstrap_samples=200)
    assert analysis["modes"]["diff_only"]["f1"]["mean"] == 0.5
    assert analysis["modes"]["search_baseline"]["stable_case_hits"] == 1
    paired = analysis["paired_vs_diff_only"]["search_baseline"]
    assert paired["f1_delta"]["mean"] == 0.5
    assert (paired["f1_wins"], paired["f1_ties"], paired["f1_losses"]) == (1, 1, 0)
    assert analysis["bootstrap_samples"] == 200


def _tier_run(case_id, difficulty, mode, f1, tokens, files):
    return {
        "case_id": case_id, "difficulty": difficulty, "mode": mode,
        "repetition": 1, "f1": f1, "recall": f1, "precision": f1,
        "success": True, "elapsed_ms": 10, "files_read": files,
        "tool_calls": ["Read"], "tool_call_count": 1,
        "usage": {"input_tokens": tokens, "output_tokens": 10,
                  "total_cost_usd": 0.1},
    }


def test_analyze_agent_report_groups_paired_efficiency_by_difficulty():
    runs = [
        _tier_run("easy", "trivial", "loop_nograph", 1.0, 30, ["a.py"]),
        _tier_run("easy", "trivial", "loop_full", 1.0, 40,
                  ["a.py"]),
        _tier_run("deep", "hard", "loop_nograph", 0.0, 100,
                  ["a.py", "b.py", "c.py"]),
        _tier_run("deep", "hard", "loop_full", 1.0, 40,
                  ["a.py"]),
    ]
    analysis = analyze_agent_report({
        "schema_version": 2, "baseline_mode": "loop_nograph",
        "repetitions": 1, "runs": runs,
    }, bootstrap_samples=200)

    assert set(analysis["by_difficulty"]) == {"trivial", "hard"}
    hard = analysis["by_difficulty"]["hard"]
    assert hard["case_count"] == 1
    paired = hard["paired_vs_loop_nograph"]["loop_full"]
    assert paired["f1_delta"]["mean"] == 1.0
    assert paired["input_tokens_delta"]["mean"] == -60.0
    assert paired["total_tokens_delta"]["mean"] == -60.0
    assert paired["files_read_delta"]["mean"] == -2.0


def test_analyze_agent_report_rejects_empty_or_tiny_bootstrap():
    with pytest.raises(ValueError, match="no runs"):
        analyze_agent_report({"runs": []})
    with pytest.raises(ValueError, match="at least 100"):
        analyze_agent_report({"runs": [_run("diff_only", 1, 0, 0, 0)]},
                             bootstrap_samples=10)

