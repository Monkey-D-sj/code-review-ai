import json
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


def test_run_benchmark_reports_symbol_folds(tmp_path):
    """Multi-symbol changes produce symbol-level folds whose gold is the OTHER
    changed symbols (production impact surface); single-symbol changes produce
    none. main calls login, so each reaches the other via impact."""
    config = _config(tmp_path)
    conn = connect(config.db_path)
    init_schema(conn)
    cases = load_cases(str(_manifest(tmp_path, [{
        "id": "multi-symbol",
        "changed_symbols": [Q("app", "main"), Q("auth", "login")],
        "gold_files": ["auth.py"],
    }, {
        "id": "single-symbol",
        "changed_symbols": [Q("auth", "login")],
        "gold_files": ["auth.py"],
    }])))

    report = run_benchmark(config, conn, cases, top_k=5)

    multi = report["cases"][0]
    folds = multi["changed_symbol_folds"]
    assert len(folds) == 2
    assert {fold["seed_symbol"] for fold in folds} == {
        Q("app", "main"), Q("auth", "login")}
    # impact(main) reaches login and impact(login) reaches main — the other
    # changed symbol of the same fix sits on the impact surface.
    assert all(fold["recall_all"] == 1.0 for fold in folds)
    assert report["cases"][1]["changed_symbol_folds"] == []

    agg = report["aggregate"]
    assert agg["changed_symbol_eligible_cases"] == 1
    assert agg["changed_symbol_folds"] == 2
    assert agg["macro_changed_symbol_recall_all"] == 1.0
    assert agg["macro_changed_symbol_recall_at_k"] == 1.0
    assert "macro_changed_symbol_precision_at_k" in agg
    assert "macro_changed_symbol_precision_all" in agg
    assert agg["mean_changed_symbol_candidate_count"] is not None


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


