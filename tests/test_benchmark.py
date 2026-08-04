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

    assert report["schema_version"] == 1
    assert report["aggregate"]["macro_patch_file_recall_at_k"] == 1.0
    assert report["aggregate"]["macro_patch_file_precision_at_k"] == 1.0
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
