"""Run one field-contract case through the review CLI, and score the result.

The corpus (`field-contract-cases.json`) and its scorer (`field_contract_eval.py`)
are only half a benchmark; this is the half that produces numbers. It
materializes one case, runs one arm against it, and writes the payload and the
score to a file whose name says which configuration produced them.

Three things it does that the scratch scripts it replaces did not:

- **Every run gets its own file.** Those scripts wrote one fixed path, so a
  second run overwrote the first and every trace but the last was lost -- which
  is what made "why did these two runs differ?" unanswerable. That question is
  the whole point of an A/B here, so the result has to survive the next run.
- **The configuration is recorded, not remembered.** Case, arm, whether the
  summary was injected, and the budget all land in the filename and inside the
  file, so the runs stay distinguishable without trusting the operator.
- **The scratch repo is released.** Each one carries a multi-megabyte index.

Unlike `review_loop_case_compare.py` this drives the newer field-contract
corpus, whose cases are cross-layer contract regressions scored by set recall
rather than by "did a finding land on the fix site". It shares the CLI
contract with it -- same command, same `CRAI_DIFF_BASE=HEAD` -- so what runs
here is what a user runs.
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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(HERE))

from field_contract_eval import load_cases, missed_sites, score  # noqa: E402

# The repo the cases are planted into: one pristine commit plus a patch.
SOURCE_REPO = REPO_ROOT / "full_agent_eval" / "case-backend"
DEFAULT_OUT_DIR = REPO_ROOT / "eval-results"

ARMS = ("graph", "nograph")
DEFAULT_MAX_TURNS = 25
# 900k, not the 250k the older harness used. A budget kill is recorded as a
# plain miss -- the run stops mid-research and submits nothing -- so a cap that
# sits anywhere near what a case actually costs quietly corrupts the score.
# One case already reached 144,668 tokens, 96% of a 150k cap.
DEFAULT_MAX_TOKENS = 900_000


@dataclass
class Prepared:
    """A materialized case: its scratch repo, and the index built in it."""

    case: object
    path: Path


def _git(args: list[str], cwd: Path, *, stdin: bytes | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   input=stdin)


def _force_remove(function, path, _error) -> None:
    """Clear the read-only bit before deleting -- git object files are read-only
    on Windows, and `shutil.rmtree` gives up on them, leaking the scratch dir."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def materialize(case) -> Prepared:
    """Lay the case down: one pristine commit, then the patch in the worktree.

    Committing first and applying second is what makes the diff *be* the case:
    the CLI computes it against `HEAD` (see `_review_env`), so a clean commit
    plus a dirty worktree is exactly the change under review.
    """
    scratch = Path(tempfile.mkdtemp(prefix=f"fc-{case.id}-"))
    shutil.rmtree(scratch, onexc=_force_remove)
    shutil.copytree(SOURCE_REPO, scratch)
    _git(["init", "-q"], scratch)
    _git(["add", "-A"], scratch)
    _git(["-c", "user.name=eval", "-c", "user.email=eval@local",
          "commit", "-q", "-m", "pristine"], scratch)
    applied = subprocess.run(["git", "apply", "-"], cwd=scratch,
                             input=case.diff.encode("utf-8"),
                             capture_output=True)
    if applied.returncode != 0:
        shutil.rmtree(scratch, onexc=_force_remove)
        raise RuntimeError(f"{case.id}: the patch does not apply -- "
                           f"{applied.stderr.decode('utf-8', 'replace')}")
    return Prepared(case=case, path=scratch)


def release(prepared: Prepared) -> None:
    """Delete the scratch repo. A leak must never cost the batch its results."""
    try:
        shutil.rmtree(prepared.path, onexc=_force_remove)
    except OSError as exc:
        print(f"warning: could not remove {prepared.path}: {exc}", file=sys.stderr)


def _review_env() -> dict[str, str]:
    """The environment the CLI runs in.

    `CRAI_DIFF_BASE=HEAD` is what turns the worktree patch into the diff under
    review, and `PYTHONPATH` is what lets `-m code_review_ai.cli` resolve from
    inside the scratch repo. The keys and model name come from the process
    environment: `resolve_setting` reads a `.env` next to the *reviewed* repo,
    which here is the scratch copy and has none, so the harness loads the
    product repo's `.env` into the child's environment itself.
    """
    env = dict(os.environ)
    env["CRAI_DIFF_BASE"] = "HEAD"
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def _load_dotenv() -> None:
    """Seed the process environment from the product repo's `.env`."""
    from dotenv import dotenv_values
    for name, value in dotenv_values(REPO_ROOT / ".env").items():
        if isinstance(name, str) and isinstance(value, str):
            os.environ.setdefault(name, value)


def _review_command(arm: str, summary: bool, max_turns: int,
                    max_tokens: int) -> list[str]:
    command = [sys.executable, "-m", "code_review_ai.cli", "review",
               "--arm", arm, "--no-progress",
               "--max-turns", str(max_turns), "--max-tokens", str(max_tokens)]
    if summary:
        command.append("--summary")
    return command


def run_once(case, *, arm: str, summary: bool, max_turns: int,
             max_tokens: int, run_index: int) -> dict:
    """Materialize, run one arm, score it, and return the whole record."""
    prepared = materialize(case)
    try:
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        started = time.perf_counter()
        completed = subprocess.run(
            _review_command(arm, summary, max_turns, max_tokens),
            cwd=str(prepared.path), capture_output=True, encoding="utf-8",
            errors="replace", env=_review_env())
        elapsed = time.perf_counter() - started
    finally:
        release(prepared)

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"{case.id} ({arm}): the CLI produced no payload. "
            f"stderr tail:\n{(completed.stderr or '')[-1500:]}")

    result = score(case, payload.get("findings") or [])
    return {
        "run": {"case_id": case.id, "arm": arm, "summary": summary,
                "max_turns": max_turns, "max_tokens": max_tokens,
                "run_index": run_index, "started_at": started_at,
                "elapsed_s": round(elapsed, 1)},
        "score": _score_record(case, result, payload),
        "payload": payload,
    }


def _score_record(case, result, payload: dict) -> dict:
    """The score, plus what a reader needs to interpret it without the payload."""
    return {
        "hits": result.hits,
        "gold_total": result.gold_total,
        "reported": result.reported,
        "on_neutral": result.on_neutral,
        "recall": result.recall,
        "precision": result.precision,
        "empty_case": result.empty,
        "clean": result.clean,
        "missed": [f"{site.file}:{site.start}-{site.end}"
                   for site in missed_sites(case, payload.get("findings") or [])],
        "review_complete": payload.get("review_complete"),
        "failure_reason": payload.get("failure_reason"),
        "change_summary_chars": payload.get("change_summary_chars"),
        "tool_call_count": payload.get("tool_call_count"),
    }


def _output_path(out_dir: Path, record: dict) -> Path:
    """One file per configuration, named so a directory listing reads as the
    experiment: case, arm, whether the summary was injected, budget, repeat."""
    run = record["run"]
    summary_tag = "sum" if run["summary"] else "nosum"
    name = (f"{run['case_id']}__{run['arm']}__{summary_tag}"
            f"__t{run['max_tokens']}__r{run['run_index']}.json")
    return out_dir / name


def _report(record: dict, path: Path) -> None:
    score_record = record["score"]
    recall = score_record["recall"]
    recall_text = "n/a (empty case)" if recall is None else f"{recall:.2f}"
    print(f"  [{score_record['hits']}/{score_record['gold_total']} "
          f"recall={recall_text} precision={score_record['precision']:.2f} "
          f"reported={score_record['reported']} "
          f"complete={score_record['review_complete']}] -> {path.name}")
    for site in score_record["missed"]:
        print(f"      missed {site}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True,
                        help="case id from the field-contract manifest")
    parser.add_argument("--arm", default="graph", choices=ARMS)
    parser.add_argument("--summary", action="store_true",
                        help="inject the index's change summary (graph arm only)")
    parser.add_argument("--runs", type=int, default=1,
                        help="repeat the run N times, to see whether it is stable")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = parser.parse_args()

    if args.summary and args.arm == "nograph":
        parser.error("--summary needs the index; the nograph arm has none")
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    _load_dotenv()
    case = next((candidate for candidate in load_cases()
                 if candidate.id == args.case), None)
    if case is None:
        known = ", ".join(candidate.id for candidate in load_cases())
        parser.error(f"unknown case {args.case!r}; known cases: {known}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{case.id}  [{args.arm}, summary={args.summary}, "
          f"budget={args.max_turns} turns / {args.max_tokens} tokens]")
    print(f"gold sites: {len(case.gold)}   motive: {case.motive}")

    failures = 0
    for run_index in range(1, args.runs + 1):
        try:
            record = run_once(case, arm=args.arm, summary=args.summary,
                              max_turns=args.max_turns,
                              max_tokens=args.max_tokens, run_index=run_index)
        except RuntimeError as exc:
            failures += 1
            print(f"  run {run_index}: FAILED -- {exc}", file=sys.stderr)
            continue
        path = _output_path(out_dir, record)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        _report(record, path)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
