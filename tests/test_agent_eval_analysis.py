import json
import pytest
from pathlib import Path

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


def _seed_transcript(runs_dir, case_id, mode, repetition, f1):
    path = Path(runs_dir) / case_id / mode
    path.mkdir(parents=True, exist_ok=True)
    (path / f"run-{repetition}.json").write_text(
        json.dumps({"result": {"f1": f1}}), encoding="utf-8")


def test_route_check_analysis_groups_by_risk(tmp_path):
    from code_review_ai.agent_eval import load_agent_cases
    from code_review_ai.agent_eval_analysis import route_check_analysis
    from code_review_ai.db import connect, init_schema

    conn = connect(str(tmp_path / "r.db"))
    init_schema(conn)
    for qname, file_path in [("a::target", "a.py"), ("b::external", "b.py")]:
        conn.execute("INSERT INTO nodes(qualified_name, kind, file_path) VALUES(?,?,?)",
                     (qname, "function", file_path))
    conn.execute("INSERT INTO edges(source, target, kind, resolution) "
                 "VALUES('b::external','a::target','call','resolved')")  # a::target 跨模块

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps([
        {"id": "case-high", "prompt": "p", "diff": "x",
         "changed_symbols": ["a::target"],
         "gold_findings": [{"id": "g", "file": "a.py", "keywords": ["k"]}]},
        {"id": "case-low", "prompt": "p", "diff": "x",
         "changed_symbols": ["b::external"],
         "gold_findings": [{"id": "g", "file": "b.py", "keywords": ["k"]}]},
    ]), encoding="utf-8")
    cases = load_agent_cases(str(manifest_path))

    runs_dir = tmp_path / "runs"
    _seed_transcript(runs_dir, "case-high", "diff_only", 1, 0.3)
    _seed_transcript(runs_dir, "case-high", "graph_agent", 1, 0.9)
    _seed_transcript(runs_dir, "case-high", "hybrid_agent", 1, 0.8)
    _seed_transcript(runs_dir, "case-low", "diff_only", 1, 0.8)
    _seed_transcript(runs_dir, "case-low", "graph_agent", 1, 0.4)
    _seed_transcript(runs_dir, "case-low", "hybrid_agent", 1, 0.5)

    analysis = route_check_analysis(conn, cases, str(runs_dir))
    by_case = {row["case_id"]: row for row in analysis["cases"]}
    assert by_case["case-high"]["max_risk"] == 70
    assert by_case["case-high"]["graph_delta_f1"] == 0.6
    assert by_case["case-low"]["max_risk"] == 10
    assert by_case["case-low"]["graph_delta_f1"] == -0.4
    assert analysis["correlation"]["graph_delta_f1"] > 0   # 高风险 -> 图更有利
    assert analysis["groups"]["high_risk"]["mean_graph_delta"] == 0.6
    assert analysis["groups"]["low_risk"]["mean_graph_delta"] == -0.4


def test_route_check_rejects_missing_mode_transcripts(tmp_path):
    from code_review_ai.agent_eval import load_agent_cases
    from code_review_ai.agent_eval_analysis import route_check_analysis
    from code_review_ai.db import connect, init_schema

    conn = connect(str(tmp_path / "r.db"))
    init_schema(conn)
    conn.execute("INSERT INTO nodes(qualified_name, kind, file_path) "
                 "VALUES('a::target','function','a.py')")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps([{
        "id": "case", "prompt": "p", "diff": "x",
        "changed_symbols": ["a::target"],
        "gold_findings": [{"id": "g", "file": "a.py", "keywords": ["k"]}],
    }]), encoding="utf-8")
    runs_dir = tmp_path / "runs"
    _seed_transcript(runs_dir, "case", "diff_only", 1, 0.5)
    _seed_transcript(runs_dir, "case", "graph_agent", 1, 0.5)
    with pytest.raises(ValueError, match="hybrid_agent"):
        route_check_analysis(
            conn, load_agent_cases(str(manifest_path)), str(runs_dir))
