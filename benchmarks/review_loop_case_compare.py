"""A/B the review loop with and without the index, over the bug-injection cases.

Both arms run the shipped CLI (``code-review-ai review --arm ...``), so what is
measured is what a user runs, and the two arms differ in exactly one thing --
the arm:

    graph    worksheet mode -- the index's change summary (changed symbols ->
             candidate rows) plus get_impact's call graph, resolved through
             update_review_item.
    nograph  free-form with no index tooling -- read_file/search_code plus
             finish_review, seeing only the diff. That is a no-graph reviewer's
             input.

Driving the CLI (rather than importing the loop here) is what keeps the
comparison honest: both arms get the CLI's own neutral prompt and policy. An
earlier version passed each case's ``hint`` -- which describes the injected bug
-- to the graph arm only, so its 42/42 told us nothing about the other arm.

Both arms run the same model under the same turn/token budget. Each case is
materialized once and reused by every run: the loop is read-only, so all runs
of a case see byte-identical input, and re-copying a repo plus rebuilding its
index per run was pure waste.

The scored outcome is one thing -- did the run report the injected defect
(``eval_cases.score``). Whether the index earns its keep is read off the cost
columns (tokens, files read, tool calls), not off a second score.

Usage:
    uv run python benchmarks/review_loop_case_compare.py \
        [--case case-backend-decrypt-password-alias] [--runs 6] \
        [--arms graph nograph] [-o eval-results/review-loop-ab.json]

Both the DeepSeek provider this needs and pytest live in the ``dev`` dependency
group, which ``uv run`` syncs by default -- so a bare ``uv run`` is correct.
(Putting the provider in an *extra* made every bare ``uv run`` prune it out of
the venv. Add ``--no-sync`` only to dodge the venv's file lock while a
``code-review-ai-mcp.exe`` is running.)

Requires repo-local .env model config (CRAI_REVIEW_MODEL etc.).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from eval_cases import (DEFAULT_MANIFEST, EvalCase, load_cases, run_batch,
                        summarize)  # noqa: E402  (needs HERE on sys.path)

from code_review_ai.config import load_config  # noqa: E402
from code_review_ai.db import connect, init_schema  # noqa: E402
from code_review_ai.indexer import rebuild  # noqa: E402

ARMS = ("graph", "nograph")
MAX_TURNS = 25
MAX_TOTAL_TOKENS = 150_000
DEFAULT_OUTPUT = REPO_ROOT / "eval-results" / "review-loop-ab.json"

# Each case's scratch repo is one pristine commit plus a working-tree patch, so
# only a diff against HEAD shows the injected regression: there is no upstream
# and no HEAD^ to resolve. Set through the documented CRAI_<KEY> config channel
# (the same value prepare_case indexes with, so the CLI finds a fresh index).
_CASE_DIFF_BASE = "HEAD"


@dataclass
class Prepared:
    """One case, materialized once: patched repo + its index."""

    case: EvalCase
    path: Path
    conn: object


def _git(args: list[str], cwd: Path, *, stdin: bytes | None = None) -> str:
    completed = subprocess.run(["git", "-C", str(cwd), *args],
                               input=stdin, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", "replace"))
    return completed.stdout.decode("utf-8")


def prepare_case(case: EvalCase) -> Prepared:
    """Scratch-copy the case repo, apply its patch and build the index.

    The index is built here, once, with the same config the CLI will load when
    it runs from inside the scratch repo -- so the CLI's sync finds it current
    instead of rebuilding it on every run.
    """
    scratch = Path(tempfile.mkdtemp(prefix=f"cbe-{case.id}-"))
    shutil.copytree(case.source_dir, scratch, dirs_exist_ok=True)
    _git(["init", "-q"], scratch)
    _git(["add", "-A"], scratch)
    _git(["-c", "user.name=e2e", "-c", "user.email=e2e@local",
          "commit", "-q", "-m", "pristine"], scratch)
    _git(["apply"], scratch, stdin=case.patch.encode("utf-8"))

    config = load_config(repo_path=str(scratch))
    config.repo_path = str(scratch)
    config.diff_base = _CASE_DIFF_BASE  # working tree vs pristine == the patch
    db_path = scratch / ".code-review-ai" / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(db_path))
    init_schema(conn)
    rebuild(config, conn)
    return Prepared(case=case, path=scratch, conn=conn)


def _force_remove(function, path, _error) -> None:
    """Clear the read-only bit before retrying.

    ``git add``/``commit`` writes its object files read-only, and rmtree cannot
    delete a read-only file on Windows -- without this the scratch repo (and its
    index) survives every run, which is the waste this harness exists to avoid.
    """
    os.chmod(path, stat.S_IWRITE)
    function(path)


def release_case(prepared: Prepared) -> None:
    prepared.conn.close()
    try:
        shutil.rmtree(prepared.path, onexc=_force_remove)
    except OSError as exc:
        # Non-fatal: a leaked scratch dir must not cost the batch its results.
        print(f"warning: could not remove scratch {prepared.path}: {exc}",
              file=sys.stderr)


def _review_command(arm: str) -> list[str]:
    """The CLI invocation one run makes, from inside the case's scratch repo."""
    return [sys.executable, "-m", "code_review_ai.cli", "review",
            "--arm", arm, "--no-progress",
            "--max-turns", str(MAX_TURNS), "--max-tokens", str(MAX_TOTAL_TOKENS)]


def _run_arm(prepared: Prepared, arm: str) -> dict:
    """One review run through the product's own entry point.

    cwd is the scratch repo, which is how the CLI is meant to be used: config
    is read from the project being reviewed, and the default ``--db`` lands on
    the index ``prepare_case`` already built.
    """
    completed = subprocess.run(
        _review_command(arm), cwd=str(prepared.path), capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "CRAI_DIFF_BASE": _CASE_DIFF_BASE})
    return _payload(completed, arm, prepared.case.id)


def _payload(completed: subprocess.CompletedProcess, arm: str, case_id: str) -> dict:
    """The CLI's JSON payload, or a stand-in that records the crash as a row.

    A run whose CLI died must cost its own row, not the rows after it: a batch
    this long is paid for as it goes.
    """
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        detail = (completed.stderr or completed.stdout).strip()[-400:]
        print(f"[{arm} {case_id}] CLI produced no payload: {detail}",
              file=sys.stderr, flush=True)
        return {"findings": [], "review_complete": False, "usage": {},
                "tool_trace": [],
                "failure_reason": f"cli exit {completed.returncode}: {detail}"}


def _progress(row: dict) -> None:
    print(f"[{row['arm']:7} {row['case_id']} #{row['run']}] "
          f"hit={row['hit']} reported={row['reported']} "
          f"tools={len(row['tool_trace'])} "
          f"tokens={row['usage'].get('input_tokens', 0)}+"
          f"{row['usage'].get('output_tokens', 0)} "
          f"fail={row['failure']!r}", flush=True)


def _print_summary(summary: dict) -> None:
    print(f"\n{'arm':8} {'runs':>5} {'hit':>6} {'rate':>6} {'err':>4} "
          f"{'in_tok':>9} {'out_tok':>8} {'files':>6} {'tools':>6} {'yuan':>8}")
    for arm, stats in sorted(summary.items()):
        print(f"{arm:8} {stats['runs']:>5} {stats['hits']:>6} "
              f"{stats['hit_rate']:>6.2f} {stats['errors']:>4} "
              f"{stats['mean_input_tokens']:>9.0f} "
              f"{stats['mean_output_tokens']:>8.0f} "
              f"{stats['mean_files_read']:>6.1f} "
              f"{stats['mean_tool_calls']:>6.1f} "
              f"{stats['mean_cost_yuan']:>8.3f}")


def _write_output(output: Path, cases, arms, runs: int, rows: list[dict]) -> None:
    """Persist what has been paid for; called after every row, not just at the end."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(
        {"cases": [case.id for case in cases], "arms": list(arms),
         "runs": runs, "summary": summarize(rows), "rows": rows},
        ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(DEFAULT_MANIFEST),
                        help="case manifest (default: %(default)s)")
    parser.add_argument("--case", nargs="*", default=None,
                        help="case ids to run (default: all in the manifest)")
    parser.add_argument("--arms", nargs="*", default=list(ARMS),
                        choices=list(ARMS))
    parser.add_argument("--runs", type=int, default=1,
                        help="repetitions per case per arm (default: 1)")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    for name, value in dotenv_values(REPO_ROOT / ".env").items():
        if isinstance(name, str) and isinstance(value, str):
            os.environ.setdefault(name, value)

    cases = load_cases(args.cases, args.case)
    output = Path(args.output)
    print(f"{len(cases)} case(s) x {len(args.arms)} arm(s) x {args.runs} run(s) "
          f"-> {output}", flush=True)

    rows: list[dict] = []

    def record(row: dict) -> None:
        # Rewrite the file after every run: a batch this long is paid for as it
        # goes, so a mid-batch failure must not cost the runs already spent.
        _progress(row)
        _write_output(output, cases, args.arms, args.runs, rows)

    run_batch(cases, arms=args.arms, runs=args.runs, rows=rows,
              prepare=prepare_case,
              execute=lambda arm, case, prepared: _run_arm(prepared, arm),
              release=release_case, on_row=record)

    _print_summary(summarize(rows))
    print(f"\nwrote {output}")


if __name__ == "__main__":
    main()
