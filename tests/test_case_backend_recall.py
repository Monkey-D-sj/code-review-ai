"""Checks for the real business repo (case-backend).

The unified manifest ``benchmarks/case-backend-cases.json`` carries the full-eval
fields (``patch`` / ``hint`` / ``gold_findings``), and this test guards that
those cases do not leak answer keywords into the model-visible input.

``test_case_backend_cases_do_not_leak_keywords`` is the cheap half: it holds the
manifest to the rule that makes the eval discriminating at all — a gold keyword
may not appear in anything the model reads for free (the diff, or the ``hint``
prose of the ``--hinted`` arm). A keyword visible in the input is earned by
paraphrasing rather than by traversing the call graph, which is exactly the
work the graph tools exist to do.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
