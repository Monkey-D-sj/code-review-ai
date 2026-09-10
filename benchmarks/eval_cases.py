"""Case loading, scoring and aggregation for the review-loop A/B harness.

The manifest (``case-backend-cases.json``) is bug-injection shaped: a pristine
repo, a patch that introduces one regression, and the gold sites that regression
can be fixed at. Scoring is deliberately one rule -- see :func:`score` -- because
the only outcome we measure is whether a run pointed at the injected defect.

Whether the index earns its keep is *not* a second score. It is read off the
cost columns (tokens, files read, tool calls) that :func:`summarize` derives
from the same rows, which is why a row keeps its raw findings and tool trace:
the headline can be recomputed from them without re-running a model.

Pure: no model, no repo, no index. I/O is reading the manifest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from code_review_ai.review_loop.pricing import compute_cost

HERE = Path(__file__).resolve().parent
DEFAULT_MANIFEST = HERE / "case-backend-cases.json"


@dataclass(frozen=True)
class GoldCause:
    """One injected defect and where fixing it counts as finding it.

    ``alternate_files`` exists because a regression can be repaired on either
    side of the broken contract: the changed callee (``fix_file``) or a caller
    that relied on the old behaviour. A finding on any of them found this bug.
    """

    id: str
    fix_file: str
    alternate_files: tuple[str, ...] = ()

    @property
    def fix_sites(self) -> frozenset[str]:
        return frozenset(_normalize_path(path)
                         for path in (self.fix_file, *self.alternate_files))


@dataclass(frozen=True)
class EvalCase:
    """One bug-injection case, ready to materialize."""

    id: str
    source_dir: Path
    patch: str
    prompt: str
    difficulty: str
    causes: tuple[GoldCause, ...]

    @property
    def fix_sites(self) -> frozenset[str]:
        """Every file a finding may name and still count as this bug found."""
        sites: set[str] = set()
        for cause in self.causes:
            sites |= cause.fix_sites
        return frozenset(sites)


@dataclass(frozen=True)
class RunScore:
    """The one outcome plus what it was derived from."""

    hit: bool
    reported: int
    files: tuple[str, ...]


def _normalize_path(path: object) -> str:
    """Repo-relative, forward slashes, no ``./`` prefix.

    Both sides of the comparison go through this: gold paths are authored by
    hand, and a model writes ``file`` as free text ("app/x.py", "./app/x.py",
    "app\\x.py" are all the same file).
    """
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _finding_file(finding: object) -> str:
    """``file`` off a finding dict or a Finding model."""
    if isinstance(finding, dict):
        return _normalize_path(finding.get("file"))
    return _normalize_path(getattr(finding, "file", ""))


def load_cases(manifest: Path | str = DEFAULT_MANIFEST,
               case_ids: list[str] | None = None) -> list[EvalCase]:
    """Read the manifest, optionally narrowed to ``case_ids`` (unknown ids error)."""
    records = json.loads(Path(manifest).read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"{manifest}: expected a list of cases")
    cases = [_to_case(record, manifest) for record in records]
    if case_ids:
        wanted = set(case_ids)
        unknown = sorted(wanted - {case.id for case in cases})
        if unknown:
            raise ValueError(f"unknown case id(s): {unknown}")
        cases = [case for case in cases if case.id in wanted]
    return cases


def _to_case(record: dict, manifest: Path | str) -> EvalCase:
    """One manifest record -> EvalCase, rejecting any other gold shape.

    A manifest whose gold uses another shape (``gold_findings``, different key
    names) would be silently mis-read rather than scored wrongly, so it fails
    loudly instead.
    """
    gold = record.get("gold") or {}
    root_causes = gold.get("root_causes")
    if not isinstance(root_causes, list) or not root_causes:
        raise ValueError(
            f"{record.get('id')}: no gold.root_causes in {manifest} -- this "
            "loader only understands the case-backend manifest shape")
    return EvalCase(
        id=record["id"],
        # The manifest stores repo-relative fixture paths; resolve them against
        # the repo so the harness does not depend on the caller's cwd.
        source_dir=_resolve_source_dir(record["source_dir"]),
        patch=record["patch"],
        prompt=record.get("hint") or record.get("prompt") or "",
        difficulty=record.get("difficulty") or "unknown",
        causes=tuple(
            GoldCause(id=str(cause.get("id", "")), fix_file=cause["fix_file"],
                      alternate_files=tuple(cause.get("alternate_files") or []))
            for cause in root_causes),
    )


def _resolve_source_dir(source_dir: object) -> Path:
    """Manifest fixture paths are repo-relative; absolute ones pass through."""
    path = Path(str(source_dir))
    return path if path.is_absolute() else (HERE.parent / path).resolve()


def score(findings, case: EvalCase) -> RunScore:
    """Did this run report the injected defect?

    A hit is a reported finding whose file is a gold fix site. Findings outside
    the fix sites are not failures and are not counted against the run -- the
    cost columns are where over-reporting shows up.
    """
    files = tuple(_finding_file(finding) for finding in findings)
    return RunScore(hit=any(path in case.fix_sites for path in files),
                    reported=len(files),
                    files=files)


def files_read(tool_trace) -> list[str]:
    """Repo-relative paths the run read, in first-seen order."""
    paths: list[str] = []
    for record in tool_trace or ():
        if not isinstance(record, dict) or record.get("tool") != "read_file":
            continue
        args = record.get("input")
        path = _normalize_path(args.get("path")) if isinstance(args, dict) else ""
        if path and path not in paths:
            paths.append(path)
    return paths


def tool_calls(tool_trace) -> list[str]:
    """Tool names the run invoked, in order."""
    return [record["tool"] for record in tool_trace or ()
            if isinstance(record, dict) and isinstance(record.get("tool"), str)]


def _loop_usage(usage: object) -> dict:
    """The review payload's usage keys -> the loop's accumulator keys.

    ``loop_result_payload`` publishes ``cache_read_input_tokens``, while the
    accumulator and :func:`compute_cost` read ``cache_read``. Mapping matters:
    without it every cached input token would be priced as a cache miss.
    """
    published = usage if isinstance(usage, dict) else {}
    return {
        "input_tokens": _int(published.get("input_tokens")),
        "output_tokens": _int(published.get("output_tokens")),
        "cache_read": _int(published.get("cache_read_input_tokens")),
    }


def row_from(payload: dict, case: EvalCase, arm: str, run_no: int) -> dict:
    """One run's review payload (the CLI's JSON contract) -> the persisted row.

    The row keeps the raw findings and the tool trace, not just the derived
    numbers: the earlier harness stored only the match booleans and counts, so
    2.3 GB of its results can no longer be re-scored. Everything
    :func:`summarize` prints is recomputable from what is kept here.
    """
    findings = payload.get("findings") or []
    run_score = score(findings, case)
    return {
        "case_id": case.id,
        "difficulty": case.difficulty,
        "arm": arm,
        "run": run_no,
        "hit": run_score.hit,
        "reported": run_score.reported,
        "complete": bool(payload.get("review_complete", False)),
        "failure": payload.get("failure_reason"),
        "usage": _loop_usage(payload.get("usage")),
        "findings": findings,
        "tool_trace": payload.get("tool_trace") or [],
    }


def run_batch(cases, *, arms, runs: int, prepare, execute, rows=None,
              release=None, on_row=None) -> list[dict]:
    """Run every arm against every case, ``runs`` times each.

    ``prepare(case)`` materializes the case once and hands back whatever
    ``execute(arm, case, prepared)`` needs; ``release(prepared)`` tears it down.
    Both are injected so the batching is testable without a repo or a model.

    ``rows`` may be a caller-owned list to append into. Pass one when
    ``on_row`` needs the rows so far -- an incremental output file, say -- so
    there is a single list rather than a second one the callback must remember
    to keep in step.

    Arms alternate inside each repetition (rather than one arm draining the
    whole batch) so a provider that slows or degrades mid-run affects both
    equally.
    """
    rows = [] if rows is None else rows
    for case in cases:
        prepared = prepare(case)
        try:
            for run_no in range(1, runs + 1):
                for arm in arms:
                    result = execute(arm, case, prepared)
                    row = row_from(result, case, arm, run_no)
                    rows.append(row)
                    if on_row is not None:
                        on_row(row)
        finally:
            if release is not None:
                release(prepared)
    return rows


_RUNNING_SUMS = {"runs": 0, "hits": 0, "errors": 0, "input": 0, "output": 0,
                 "cache": 0, "cost": 0.0, "files": 0, "tools": 0, "reported": 0}


def summarize(rows: list[dict]) -> dict:
    """Per-arm outcome and cost, plus the run count each mean is over."""
    totals: dict[str, dict] = {}
    for row in rows:
        arm = totals.setdefault(row["arm"], dict(_RUNNING_SUMS))
        usage = row.get("usage") or {}
        trace = row.get("tool_trace")
        arm["runs"] += 1
        arm["hits"] += 1 if row.get("hit") else 0
        arm["errors"] += 1 if row.get("failure") else 0
        arm["input"] += _int(usage.get("input_tokens"))
        arm["output"] += _int(usage.get("output_tokens"))
        arm["cache"] += _int(usage.get("cache_read"))
        arm["cost"] += compute_cost(usage)
        arm["files"] += len(files_read(trace))
        arm["tools"] += len(tool_calls(trace))
        arm["reported"] += _int(row.get("reported"))
    return {name: _arm_stats(counts) for name, counts in totals.items()}


def _arm_stats(counts: dict) -> dict:
    """One arm's running sums -> its report line. Means divide by runs."""
    runs = max(counts["runs"], 1)
    return {
        "runs": counts["runs"],
        "hits": counts["hits"],
        "hit_rate": round(counts["hits"] / runs, 4),
        "errors": counts["errors"],
        "mean_input_tokens": round(counts["input"] / runs, 1),
        "mean_output_tokens": round(counts["output"] / runs, 1),
        "mean_cache_read_tokens": round(counts["cache"] / runs, 1),
        # 6 dp: one run costs a fraction of a yuan, so 4 dp would report a mean
        # of ~0.0002 with a single significant digit.
        "mean_cost_yuan": round(counts["cost"] / runs, 6),
        "mean_files_read": round(counts["files"] / runs, 1),
        "mean_tool_calls": round(counts["tools"] / runs, 1),
        "mean_reported": round(counts["reported"] / runs, 1),
    }


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


__all__ = [
    "DEFAULT_MANIFEST", "EvalCase", "GoldCause", "RunScore",
    "files_read", "load_cases", "row_from", "run_batch", "score",
    "summarize", "tool_calls",
]
