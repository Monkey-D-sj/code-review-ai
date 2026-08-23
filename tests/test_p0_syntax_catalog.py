from __future__ import annotations

import json
from pathlib import Path

from p0_conformance import catalog_metrics, load_p0_cases, validate_syntax_catalog


ROOT = Path(__file__).resolve().parents[1]
CATALOG_FILE = ROOT / "tests" / "p0" / "syntax-catalog.json"


def _catalog() -> dict:
    return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))


def test_syntax_catalog_and_cases_are_bidirectionally_consistent():
    catalog = _catalog()
    cases = load_p0_cases(ROOT)
    assert validate_syntax_catalog(catalog, cases) == []


def test_catalog_is_a_complete_five_edge_denominator():
    catalog = _catalog()
    items = catalog["items"]
    assert len({item["id"] for item in items}) == len(items)
    for language in ("python", "typescript", "java"):
        assert {item["edge_kind"] for item in items if item["language"] == language} == {
            "call", "contains", "import", "extends", "implements"
        }
    assert [
        item for item in items
        if item["language"] == "python" and item["edge_kind"] == "implements"
    ] == [
        {
            "id": "PY-IMPLEMENTS-NONE",
            "language": "python",
            "edge_kind": "implements",
            "syntax": "NONE",
            "classification": "static",
            "status": "not_applicable",
            "case_ids": [],
            "expected_resolution": "not_applicable",
            "reason": "Python has no language-level implements syntax.",
            "limitations": ["Python has no language-level implements syntax."],
        }
    ]


def test_catalog_metrics_use_catalog_statuses_not_hand_written_denominators():
    catalog = _catalog()
    cases = load_p0_cases(ROOT)
    metrics = catalog_metrics(catalog, cases)
    assert metrics["denominator"] == "syntax-catalog.json"
    assert sum(metrics["totals"].values()) == len(catalog["items"])
    assert metrics["registered_cases"] == len(cases)
    assert all(0 <= row["static_coverage"] <= 1 for row in metrics["rows"])


def test_dynamic_catalog_items_have_boundary_case_evidence():
    catalog = _catalog()
    cases = load_p0_cases(ROOT)
    boundary_ids = {
        case["case_id"] for case in cases
        if case.get("negative_kind") or case.get("negative_kinds")
    }
    dynamic = [item for item in catalog["items"] if item["status"] == "dynamic"]
    assert dynamic
    assert all(set(item["case_ids"]) & boundary_ids for item in dynamic)
