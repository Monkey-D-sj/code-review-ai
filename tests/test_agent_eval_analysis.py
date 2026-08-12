import pytest

from code_review_ai.agent_eval_analysis import analyze_agent_report


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


def test_analyze_agent_report_rejects_empty_or_tiny_bootstrap():
    with pytest.raises(ValueError, match="no runs"):
        analyze_agent_report({"runs": []})
    with pytest.raises(ValueError, match="at least 100"):
        analyze_agent_report({"runs": [_run("diff_only", 1, 0, 0, 0)]},
                             bootstrap_samples=10)


