"""Checks for the real business repo (case-backend).

The unified manifest ``benchmarks/case-backend-cases.json`` carries one
``gold`` object for graph retrieval and agent-review scoring. These tests guard
the schema, referenced files, and answer-key leakage.

``test_case_backend_cases_do_not_leak_keywords`` is the cheap half: it holds the
manifest to the rule that makes the eval discriminating at all — a gold keyword
may not appear in anything the model reads for free (the diff, or the ``hint``
prose of the ``--hinted`` arm). A keyword visible in the input is earned by
paraphrasing rather than by traversing the call graph, which is exactly the
work the graph tools exist to do.
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT / "full_agent_eval" / "case-backend"
MANIFEST = ROOT / "benchmarks" / "case-backend-cases.json"


def _load_cases() -> list[dict]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_case_backend_cases_do_not_leak_keywords():
    cases = _load_cases()
    assert len(cases) >= 8, "manifest must carry the recall + full-eval cases"

    for case in cases:
        case_id = case["id"]
        assert "prompt" not in case, (
            f"{case_id}: per-case prose belongs in 'hint' (blind by default), "
            "not in the legacy 'prompt' key")
        hint = case.get("hint", "")
        assert isinstance(hint, str), f"{case_id}: hint must be a string"

        visible = f"{case.get('patch', '')}\n{hint}".lower()
        findings = case["gold"]["root_causes"]
        assert findings, f"{case_id}: needs gold.root_causes"
        for finding in findings:
            keywords = finding["mechanism_terms"]
            assert finding["min_matches"] <= len(keywords), case_id
            for keyword in keywords:
                assert keyword.lower() not in visible, (
                    f"{case_id}: gold keyword {keyword!r} is already visible in "
                    "the diff or hint — it can be paraphrased instead of traversed")


def test_case_backend_uses_one_structured_gold_source():
    from code_review_ai.full_agent_eval import load_full_agent_cases

    records = _load_cases()
    loaded = load_full_agent_cases(str(MANIFEST))
    assert len(records) == len(loaded) >= 8
    for record, case in zip(records, loaded):
        assert "gold_findings" not in record
        assert "gold_files" not in record
        context = record["gold"]["context"]
        assert set(context) == {
            "symbols", "files", "entries", "tests", "hard_negatives"}
        for dimension in ("symbols", "files", "entries", "tests"):
            assert context[dimension], (
                f"{case.case_id}: gold.context.{dimension} must be annotated")
        assert any(context["hard_negatives"].values()), (
            f"{case.case_id}: needs at least one hard negative")
        assert case.gold_context.files == tuple(context["files"])
        assert case.gold.root_causes == case.gold_findings
        for path in (*context["files"],
                     *context["hard_negatives"]["files"]):
            assert (REPO / path).is_file(), (
                f"{case.case_id}: gold context file not in repo: {path}")


@pytest.mark.slow
def test_case_backend_graph_retrieval_uses_structured_context_gold(tmp_path):
    from code_review_ai.full_agent_eval import (
        load_full_agent_cases,
        preflight_full_agent_eval,
    )

    report = preflight_full_agent_eval(
        load_full_agent_cases(str(MANIFEST)),
        str(tmp_path / "repos"), str(tmp_path / "work"))
    aggregate = report["aggregate"]["graph_retrieval"]
    assert aggregate["symbol_found_rate"] == 1.0
    for dimension in ("symbols", "files", "entries", "tests"):
        score = aggregate["dimensions"][dimension]
        assert score["applicable_cases"] == len(report["cases"])
        assert score["macro_recall"] == 1.0
    assert aggregate["hard_negative_correctness"] is not None
    for case in report["cases"]:
        score = case["graph_retrieval"]["score"]
        for dimension in ("symbols", "files", "entries", "tests"):
            assert score[dimension]["applicable"] is True
            assert score[dimension]["misses"] == []
        test_evidence = case["graph_retrieval"]["evidence"]["tests"]
        assert not any(target.startswith(("app.", "app/"))
                       for target in test_evidence), (
            f"{case['case_id']}: production symbol misclassified as a test")
