"""The review-loop A/B harness's scoring and batching core.

The harness itself lives in ``benchmarks/`` (it is a dev tool, not part of the
shipped package), so the tests put that directory on ``sys.path`` to reach it.
Nothing here runs a model: the batching is exercised through injected prepare/
execute, which is also how the scorer is checked against a perfect and an empty
reporter.
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from code_review_ai.review_loop.schemas import Finding

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "benchmarks"))

from eval_cases import (  # noqa: E402
    EvalCase, GoldCause, files_read, load_cases, row_from, run_batch, score,
    summarize, tool_calls,
)

CASE = EvalCase(id="synth", source_dir=Path("unused"), patch="", prompt="",
                difficulty="hard",
                causes=(GoldCause(id="rc", fix_file="app/x.py"),))
CASE_WITH_ALTERNATE = EvalCase(
    id="synth-alt", source_dir=Path("unused"), patch="", prompt="",
    difficulty="medium",
    causes=(GoldCause(id="rc", fix_file="app/x.py",
                      alternate_files=("app/y.py",)),))


def _finding(file, title="t", description="d", line=1):
    return Finding(file=file, line=line, title=title, description=description)


@dataclass
class _Result:
    """Stand-in for a LoopResult, so the batching runs without a model."""

    findings: list = field(default_factory=list)
    review_complete: bool = True
    failure_reason: str | None = None
    usage: dict = field(default_factory=dict)
    tool_trace: list = field(default_factory=list)


class _FindingObject:
    """A Finding-shaped object rather than a dict, to pin the duck typing."""

    def __init__(self, file):
        self.file = file


def _batch(cases=None, findings_for=None, arms=("graph", "nograph"), runs=2):
    """Drive run_batch with fakes, reporting what prepare/execute/release saw."""
    cases = cases if cases is not None else [CASE, CASE_WITH_ALTERNATE]
    prepared, released, calls = [], [], []

    def prepare(case):
        prepared.append(case.id)
        return {"case": case}

    def execute(arm, case, _prepared):
        calls.append((arm, case.id))
        return _Result(findings=(findings_for or {}).get(arm, []))

    rows = run_batch(cases, arms=arms, runs=runs, prepare=prepare,
                     execute=execute,
                     release=lambda item: released.append(item["case"].id))
    return rows, prepared, released, calls


class TestLoadCases:
    def test_reads_the_case_backend_manifest(self):
        cases = load_cases()

        assert len(cases) == 21
        assert all(case.patch and case.prompt and case.causes for case in cases)
        assert {case.difficulty for case in cases} == {"trivial", "medium", "hard"}

    def test_every_case_has_a_fix_site_and_a_present_fixture(self):
        for case in load_cases():
            assert case.fix_sites, f"{case.id}: no fix site"
            assert case.source_dir.is_absolute() and case.source_dir.is_dir(), \
                f"{case.id}: fixture {case.source_dir} is missing"

    def test_narrows_to_requested_ids(self):
        cases = load_cases(case_ids=["case-backend-decrypt-password-alias"])

        assert [case.id for case in cases] == ["case-backend-decrypt-password-alias"]

    def test_rejects_unknown_ids(self):
        with pytest.raises(ValueError, match="unknown case id"):
            load_cases(case_ids=["nope"])

    def test_rejects_a_gold_shape_it_cannot_score(self, tmp_path):
        # A manifest whose gold uses another shape (``gold_findings`` with
        # different key names) would be silently mis-read, not scored wrongly.
        manifest = tmp_path / "other-cases.json"
        manifest.write_text(json.dumps([{
            "id": "other", "source_dir": "unused", "prompt": "p",
            "gold_findings": [{"id": "g", "file": "app/x.py"}],
        }]), encoding="utf-8")

        with pytest.raises(ValueError, match="case-backend manifest shape"):
            load_cases(manifest)


class TestScore:
    def test_hits_the_fix_file(self):
        result = score([_finding("app/x.py")], CASE)

        assert result.hit is True
        assert result.reported == 1

    def test_hits_an_alternate_file(self):
        assert score([_finding("app/y.py")], CASE_WITH_ALTERNATE).hit is True

    def test_misses_another_file_and_still_counts_it_as_reported(self):
        result = score([_finding("app/other.py")], CASE)

        assert result.hit is False
        assert result.reported == 1

    def test_one_hit_among_several_findings_is_enough(self):
        findings = [_finding("app/a.py"), _finding("app/x.py"), _finding("app/b.py")]

        assert score(findings, CASE).hit is True

    def test_no_findings_is_not_a_hit(self):
        assert score([], CASE).hit is False

    @pytest.mark.parametrize("path", ["app/x.py", "./app/x.py", "app\\x.py",
                                      "  app/x.py  "])
    def test_normalizes_both_sides_of_the_comparison(self, path):
        assert score([_finding(path)], CASE).hit is True

    def test_accepts_finding_objects_as_well_as_dicts(self):
        assert score([_FindingObject("app/x.py")], CASE).hit is True


class TestTraceDerivations:
    TRACE = [
        {"tool": "read_file", "input": {"path": "./app/x.py"}, "status": "ok"},
        {"tool": "get_impact", "input": {"symbols": ["a::b"]}, "status": "ok"},
        {"tool": "read_file", "input": {"path": "app/x.py"}, "status": "ok"},
        {"tool": "read_file", "input": {"path": "app\\y.py"}, "status": "ok"},
        {"tool": "update_review_item", "input": {}, "status": "ok"},
    ]

    def test_files_read_normalizes_and_dedupes_in_first_seen_order(self):
        assert files_read(self.TRACE) == ["app/x.py", "app/y.py"]

    def test_tool_calls_lists_every_call_in_order(self):
        assert tool_calls(self.TRACE) == ["read_file", "get_impact", "read_file",
                                          "read_file", "update_review_item"]

    def test_empty_trace_yields_nothing(self):
        assert files_read([]) == [] and tool_calls([]) == []


class TestRowAndBatch:
    def test_row_keeps_the_raw_evidence_and_the_score(self):
        row = row_from(
            _Result(findings=[_finding("app/x.py")],
                    usage={"input_tokens": 10, "output_tokens": 2},
                    tool_trace=[{"tool": "read_file", "input": {"path": "a.py"}}]),
            CASE, "graph", 1)

        assert row["hit"] is True and row["reported"] == 1
        assert row["findings"][0]["file"] == "app/x.py"
        assert row["tool_trace"][0]["tool"] == "read_file"
        assert row["usage"]["input_tokens"] == 10
        assert row["case_id"] == "synth" and row["difficulty"] == "hard"

    def test_each_case_is_materialized_once_however_many_runs(self):
        _rows, prepared, _released, _calls = _batch(runs=3)

        assert prepared == [CASE.id, CASE_WITH_ALTERNATE.id]

    def test_arms_alternate_within_each_repetition(self):
        _rows, _prepared, _released, calls = _batch(runs=2)

        assert calls == [("graph", CASE.id), ("nograph", CASE.id),
                         ("graph", CASE.id), ("nograph", CASE.id),
                         ("graph", CASE_WITH_ALTERNATE.id), ("nograph", CASE_WITH_ALTERNATE.id),
                         ("graph", CASE_WITH_ALTERNATE.id), ("nograph", CASE_WITH_ALTERNATE.id)]

    def test_every_prepared_case_is_released(self):
        _rows, _prepared, released, _calls = _batch()

        assert released == [CASE.id, CASE_WITH_ALTERNATE.id]

    def test_one_row_per_case_arm_run(self):
        rows, _prepared, _released, _calls = _batch(runs=3)

        assert len(rows) == 2 * 2 * 3
        assert {row["run"] for row in rows} == {1, 2, 3}


class TestCallerOwnedRows:
    """``run_batch`` must fill the list the caller keeps, not a private one.

    A harvester that writes its output file from inside ``on_row`` reads that
    list, so a second list the callback has to remember to append to is a
    silent way to persist nothing while every run reports success.
    """

    def test_on_row_sees_the_caller_owned_list_growing(self):
        rows: list[dict] = []
        seen: list[int] = []
        rows = run_batch([CASE, CASE_WITH_ALTERNATE], arms=("graph",), runs=2,
                         rows=rows, prepare=lambda case: case,
                         execute=lambda arm, case, prepared: _Result(
                             findings=[_finding("app/x.py")]),
                         on_row=lambda row: seen.append(len(rows)))

        # 2 cases x 1 arm x 2 runs: the count grows as each row lands.
        assert seen == [1, 2, 3, 4], "on_row must observe rows already appended"
        assert len(rows) == 4
        assert all(row["hit"] for row in rows)

    def test_without_a_caller_list_it_still_returns_every_row(self):
        rows = run_batch([CASE], arms=("graph",), runs=1,
                         prepare=lambda case: case,
                         execute=lambda arm, case, prepared: _Result())

        assert len(rows) == 1


class TestSummarize:
    def test_a_perfect_reporter_scores_one_and_an_empty_one_scores_zero(self):
        rows, _p, _r, _c = _batch(findings_for={"graph": [_finding("app/x.py")]})

        summary = summarize(rows)

        assert summary["graph"]["hit_rate"] == 1.0
        assert summary["nograph"]["hit_rate"] == 0.0
        # 2 cases x 2 runs per arm.
        assert summary["graph"]["hits"] == 4 and summary["graph"]["runs"] == 4

    def test_a_failed_run_counts_as_a_miss_and_as_an_error(self):
        def execute(_arm, _case, _prepared):
            return _Result(failure_reason="no submission")

        rows = run_batch([CASE], arms=("graph",), runs=1,
                         prepare=lambda case: case, execute=execute)
        summary = summarize(rows)["graph"]

        assert summary["hit_rate"] == 0.0 and summary["errors"] == 1

    def test_means_cover_tokens_cost_files_and_tool_calls(self):
        trace = [{"tool": "read_file", "input": {"path": "a.py"}},
                 {"tool": "get_impact", "input": {}}]

        def execute(_arm, _case, _prepared):
            return _Result(findings=[_finding("app/x.py")],
                           usage={"input_tokens": 100, "output_tokens": 40,
                                  "cache_read": 60},
                           tool_trace=trace)

        rows = run_batch([CASE], arms=("graph",), runs=2,
                         prepare=lambda case: case, execute=execute)
        summary = summarize(rows)["graph"]

        assert summary["mean_input_tokens"] == 100
        assert summary["mean_output_tokens"] == 40
        assert summary["mean_cache_read_tokens"] == 60
        assert summary["mean_files_read"] == 1
        assert summary["mean_tool_calls"] == 2
        assert summary["mean_reported"] == 1
        # 60 cache-hit + 40 cache-miss input, 40 output, at the loop's rates.
        assert summary["mean_cost_yuan"] == pytest.approx(
            (60 * 0.05 + 40 * 1.5 + 40 * 4.5) / 1_000_000, abs=1e-9)

    def test_empty_input_summarizes_to_nothing(self):
        assert summarize([]) == {}
