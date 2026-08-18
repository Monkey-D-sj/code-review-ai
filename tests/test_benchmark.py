import json
from collections import Counter
from pathlib import Path

import pytest

from conftest import FIXTURES, Q

from code_review_ai.benchmark import load_cases, run_benchmark
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema


def _manifest(tmp_path, records):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


def _config(tmp_path):
    config = load_config(FIXTURES)
    config.repo_path = FIXTURES
    config.db_path = str(tmp_path / "benchmark.db")
    config.community_detection = False
    return config


def test_load_cases_validates_and_normalizes_paths(tmp_path):
    path = _manifest(tmp_path, [{
        "id": "change-1",
        "changed_symbols": [Q("auth", "login")],
        "gold_files": ["./auth.py", "ts\\app.ts"],
    }])
    cases = load_cases(str(path))
    assert cases[0].case_id == "change-1"
    assert cases[0].gold_files == ["auth.py", "ts/app.ts"]
    assert cases[0].changed_ranges == {}


def test_load_cases_rejects_empty_manifest(tmp_path):
    path = _manifest(tmp_path, [])
    with pytest.raises(ValueError, match="non-empty"):
        load_cases(str(path))


def test_committed_swebench_suite_has_expected_distribution():
    manifest = Path(__file__).parents[1] / "benchmarks" / "swe-bench-verified-30.json"
    cases = load_cases(str(manifest))
    assert len(cases) == 30
    assert Counter(case.repo for case in cases) == {
        "pallets/flask": 1,
        "psf/requests": 8,
        "pytest-dev/pytest": 11,
        "pydata/xarray": 10,
    }
    assert all(case.base_commit and case.changed_ranges for case in cases)


def test_combined_suite_adds_fastapi_history_cases():
    root = Path(__file__).parents[1]
    fastapi_cases = load_cases(str(root / "benchmarks" / "fastapi-history-10.json"))
    combined = load_cases(str(root / "benchmarks" / "historical-suite-40.json"))
    assert len(fastapi_cases) == 10
    assert len(combined) == 40
    assert {case.repo for case in fastapi_cases} == {"fastapi/fastapi"}
    assert all(case.base_commit and case.changed_ranges for case in fastapi_cases)


def test_extended_suite_adds_spring_petclinic_history_cases():
    root = Path(__file__).parents[1]
    java_cases = load_cases(
        str(root / "benchmarks" / "spring-petclinic-history-10.json")
    )
    combined = load_cases(str(root / "benchmarks" / "historical-suite-50.json"))
    assert len(java_cases) == 10
    assert len(combined) == 50
    assert {case.repo for case in java_cases} == {
        "spring-projects/spring-petclinic"
    }
    assert all(case.base_commit and case.changed_ranges for case in java_cases)
    assert all(
        file_path.endswith(".java")
        for case in java_cases
        for file_path in (*case.changed_ranges, *case.gold_files)
    )


def test_run_benchmark_reports_recall_and_index_metrics(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.db_path)
    init_schema(conn)
    cases = load_cases(str(_manifest(tmp_path, [{
        "id": "login-change",
        "changed_symbols": [Q("auth", "login")],
        "gold_files": ["auth.py", "app.py"],
    }])))

    report = run_benchmark(config, conn, cases, top_k=5)

    assert report["schema_version"] == 2
    assert report["aggregate"]["macro_patch_file_recall_at_k"] == 1.0
    assert report["aggregate"]["macro_patch_file_precision_at_k"] == 1.0
    assert report["aggregate"]["macro_patch_file_recall_all"] == 1.0
    assert report["cases"][0]["all_candidate_files_count"] >= 2
    assert report["aggregate"]["symbol_found_rate"] == 1.0
    assert report["cases"][0]["candidate_files"][:2] == ["auth.py", "app.py"]
    assert report["index"]["nodes"] > 0
    assert report["index"]["source_files"] > 0
    assert report["index"]["database_bytes"] > 0
    assert "resolved" in report["index"]["call_resolutions"]


def test_changed_ranges_resolve_symbols_from_index(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.db_path)
    init_schema(conn)
    cases = load_cases(str(_manifest(tmp_path, [{
        "id": "login-lines",
        "changed_ranges": {"auth.py": [[6, 7]]},
        "gold_files": ["auth.py", "app.py"],
    }])))

    report = run_benchmark(config, conn, cases, top_k=5)

    assert Q("auth", "login") in report["cases"][0]["changed_symbols"]
    assert report["aggregate"]["macro_patch_file_recall_at_k"] == 1.0


def test_multi_file_change_runs_leave_one_file_out_folds(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.db_path)
    init_schema(conn)
    cases = load_cases(str(_manifest(tmp_path, [{
        "id": "cross-file-change",
        "changed_ranges": {"auth.py": [[6, 7]], "app.py": [[3, 4]]},
        "gold_files": ["auth.py"],
    }])))

    report = run_benchmark(config, conn, cases, top_k=5)

    folds = report["cases"][0]["production_file_folds"]
    assert len(folds) == 2
    assert {fold["seed_file"] for fold in folds} == {"auth.py", "app.py"}
    assert report["aggregate"]["production_file_eligible_cases"] == 1
    assert report["aggregate"]["production_file_folds"] == 2
    assert report["aggregate"]["macro_related_production_file_recall_at_k"] == 0.5
    assert report["aggregate"]["macro_related_production_file_recall_all"] == 0.5
    assert all("all_candidate_files_count" in fold for fold in folds)


def test_run_benchmark_rejects_invalid_top_k(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.db_path)
    init_schema(conn)
    with pytest.raises(ValueError, match="top_k"):
        run_benchmark(config, conn, [], top_k=0)


def test_run_benchmark_rejects_empty_cases(tmp_path):
    config = _config(tmp_path)
    conn = connect(config.db_path)
    init_schema(conn)
    with pytest.raises(ValueError, match="at least one"):
        run_benchmark(config, conn, [])


def test_classify_golds_direct_vs_cochange(tmp_path):
    from code_review_ai.benchmark import BenchmarkCase, _classify_golds
    prod = tmp_path / "prod"
    test = tmp_path / "test"
    prod.mkdir(); test.mkdir()
    (prod / "Service.java").write_text(
        "package com.p;\nclass Service {}\n", encoding="utf-8")
    (test / "ServiceTests.java").write_text(
        "package com.t;\nimport com.p.Service;\nclass ServiceTests {}\n",
        encoding="utf-8")
    (test / "OtherTests.java").write_text(
        "package com.t;\nclass OtherTests {}\n", encoding="utf-8")
    config = load_config(str(tmp_path))
    config.repo_path = str(tmp_path)
    case = BenchmarkCase("x", [], {"prod/Service.java": [(1, 2)]},
                         ["test/ServiceTests.java", "test/OtherTests.java"])
    direct, cochange = _classify_golds(config, case)
    assert direct == ["test/ServiceTests.java"]     # 导入改动文件里的类
    assert cochange == ["test/OtherTests.java"]      # 无引用 -> 提交级 co-change


def test_run_benchmark_reports_direct_recall_keys(tmp_path):
    from code_review_ai.benchmark import BenchmarkCase
    config = _config(tmp_path)
    conn = connect(config.db_path)
    init_schema(conn)
    cases = load_cases(str(_manifest(tmp_path, [{
        "id": "login-change",
        "changed_symbols": [Q("auth", "login")],
        "gold_files": ["auth.py", "app.py"],
    }])))
    report = run_benchmark(config, conn, cases, top_k=5)
    agg = report["aggregate"]
    assert "macro_direct_test_file_recall_all" in agg
    assert "cochange_gold_count" in agg
    assert "direct_gold_files" in report["cases"][0]
    assert "cochange_gold_files" in report["cases"][0]


def test_benchmark_includes_test_nodes_in_candidates(tmp_path):
    """Python test files tagged is_test=1 (test_globs) must stay in benchmark
    candidates — regression for the tests='exclude' default zeroing recall."""
    import subprocess
    from code_review_ai.benchmark import BenchmarkCase
    (tmp_path / "svc.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    tdir = tmp_path / "tests"
    tdir.mkdir()
    (tdir / "test_svc.py").write_text(
        "from svc import run\n\ndef test_run():\n    assert run() == 1\n",
        encoding="utf-8")
    for cmd in (["git", "init"], ["git", "add", "-A"], ["git", "commit", "-m", "x"]):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
    config = load_config(str(tmp_path))
    config.repo_path = str(tmp_path)
    config.db_path = str(tmp_path / "b.db")
    config.community_detection = False
    conn = connect(config.db_path)
    init_schema(conn)
    case = BenchmarkCase("x", ["svc::run"], {}, ["tests/test_svc.py"])
    report = run_benchmark(config, conn, [case], top_k=5)
    assert report["cases"][0]["patch_file_recall_all"] == 1.0


def test_agentic_manifest_drives_both_harnesses():
    """One unified manifest must parse in both the impact harness (benchmark)
    and the full-agent harness, so a single case set yields both cheap impact
    metrics and expensive agent F1. Every case needs changed_ranges (impact
    seeds) and gold_files (gold test files) alongside the agent-side fields."""
    from code_review_ai.full_agent_eval import load_full_agent_cases
    manifest = (Path(__file__).resolve().parents[1]
                / "benchmarks" / "agentic-eval-real-repos.json")
    impact_cases = load_cases(str(manifest))
    agent_cases = load_full_agent_cases(str(manifest))
    assert len(impact_cases) == len(agent_cases) == 12
    for impact, agent in zip(impact_cases, agent_cases):
        assert impact.case_id == agent.case_id
        assert impact.changed_ranges, "impact harness needs changed_ranges"
        assert impact.gold_files, "impact harness needs gold test files"
        assert impact.prompt == agent.prompt
        assert impact.source_commit == agent.source_commit
        assert impact.gold_findings
