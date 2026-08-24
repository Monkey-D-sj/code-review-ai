"""Zero-LLM recall eval for the real business repo (case-backend).

Mirrors ``test_cli_benchmark_writes_report``: ``run_benchmark`` rebuilds the
index internally, so this just drives the CLI and asserts the aggregate metrics
stay in a healthy range. The unified manifest ``benchmarks/case-backend-cases.json``
carries both the recall fields (``changed_symbols`` / ``gold_files``) and the
full-eval fields (``patch`` / ``hint`` / ``gold_findings``), so this test also
guards that every gold file referenced by the manifest actually exists in the
repo — a wrong gold path would silently zero the recall signal.

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

from code_review_ai.cli import main

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
        findings = case["gold_findings"]
        assert findings, f"{case_id}: needs gold_findings"
        for finding in findings:
            keywords = finding["keywords"]
            assert finding["min_matches"] <= len(keywords), case_id
            for keyword in keywords:
                assert keyword.lower() not in visible, (
                    f"{case_id}: gold keyword {keyword!r} is already visible in "
                    "the diff or hint — it can be paraphrased instead of traversed")


@pytest.mark.slow
def test_case_backend_recall(tmp_path):
    cases = _load_cases()
    assert len(cases) >= 8, "manifest must carry the recall + full-eval cases"

    db = str(tmp_path / "bench.db")
    out = tmp_path / "report.json"
    code = main(["benchmark", "--repo", str(REPO), "--db", db,
                 "--cases", str(MANIFEST), "--top-k", "10",
                 "--out", str(out)])
    assert code == 0

    report = json.loads(out.read_text(encoding="utf-8"))
    aggregate = report["aggregate"]
    # every changed symbol must resolve to a node with a reachable flow
    assert aggregate["symbol_found_rate"] >= 0.9
    # top-10 candidate files must cover most gold files (measured 0.975)
    assert aggregate["macro_patch_file_recall_at_k"] >= 0.9
    # the full (untruncated) candidate set must cover them all
    assert aggregate["macro_patch_file_recall_all"] >= 0.9

    for case in report["cases"]:
        assert case["symbol_found_rate"] >= 0.9, case["id"]
        assert case["gold_files"], case["id"]
        for gold in case["gold_files"]:
            assert (REPO / gold).is_file(), (
                f"{case['id']}: gold file not in repo: {gold}")
