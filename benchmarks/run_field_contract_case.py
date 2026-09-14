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
  summary was injected, whether a harness skill was, and the budget all land in
  the filename and inside the file, so the runs stay distinguishable without
  trusting the operator.
- **The scratch repo is released.** Each one carries a multi-megabyte index.

`--harness-skill` injects a process-discipline skill as the review's second
system message; `--skill-review` then has the loop retrospect each run and write
a candidate skill. Both default off, and a run with neither is byte-identical to
one from before they existed. The candidates all land in one directory under
timestamped names, so a batch does not overwrite itself -- but a reader cannot
tell which case produced which candidate, which is why the record carries each
run's `skill_review` block (cost included, kept apart from the review's cost).

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


def _review_command(arm: str, summary: bool, max_turns: int, max_tokens: int,
                    harness_skill: Path | None,
                    skill_review_dir: Path | None) -> list[str]:
    command = [sys.executable, "-m", "code_review_ai.cli", "review",
               "--arm", arm, "--no-progress",
               "--max-turns", str(max_turns), "--max-tokens", str(max_tokens)]
    if summary:
        command.append("--summary")
    if harness_skill:
        command.extend(["--harness-skill", str(harness_skill)])
    if skill_review_dir:
        command.extend(["--skill-review", str(skill_review_dir)])
    return command


def run_once(case, *, arm: str, summary: bool, max_turns: int, max_tokens: int,
             run_index: int, harness_skill: Path | None = None,
             skill_review_dir: Path | None = None) -> dict:
    """Materialize, run one arm, score it, and return the whole record."""
    prepared = materialize(case)
    try:
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        started = time.perf_counter()
        completed = subprocess.run(
            _review_command(arm, summary, max_turns, max_tokens, harness_skill,
                            skill_review_dir),
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
                "harness_skill": str(harness_skill) if harness_skill else None,
                "skill_review_dir": (str(skill_review_dir)
                                     if skill_review_dir else None),
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
        "skill_review": _skill_review_record(payload),
    }


def _skill_review_record(payload: dict) -> dict | None:
    """The retrospective's own cost, kept apart from the review's.

    It rides along in the same payload and is a fixed addition per run whose
    input is the review's entire history. Folding it into the review's cost
    would make an arm comparison -- which is about the review -- read as though
    the arm that ran a retrospective were the more expensive one.
    """
    block = payload.get("skill_review")
    if not isinstance(block, dict):
        return None
    usage = block.get("usage") or {}
    input_tokens = usage.get("input_tokens") or 0
    cached = usage.get("cache_read_input_tokens") or 0
    return {
        "chars": block.get("chars"),
        "changes": len(block.get("changes") or []),
        "review_complete": block.get("review_complete"),
        "failure_reason": block.get("failure_reason"),
        "cost": block.get("cost"),
        "input_tokens": input_tokens,
        "cache_read_input_tokens": cached,
        "cache_hit_rate": round(cached / input_tokens, 3) if input_tokens else None,
    }


def _output_path(out_dir: Path, record: dict) -> Path:
    """One file per configuration, named so a directory listing reads as the
    experiment: case, arm, whether the summary was injected, whether a harness
    skill was, budget, repeat.

    The harness tag is in the name because it changes the *request*: two runs
    that differ only there are not comparable, and a listing that hid the
    difference would invite comparing them. Whether a retrospective ran is not
    named -- it cannot change the review it reads -- so that axis lives in the
    record alone.
    """
    run = record["run"]
    summary_tag = "sum" if run["summary"] else "nosum"
    harness_tag = "hs" if run["harness_skill"] else "nohs"
    name = (f"{run['case_id']}__{run['arm']}__{summary_tag}__{harness_tag}"
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
    retrospective = score_record.get("skill_review")
    if retrospective:
        cached = retrospective["cache_hit_rate"]
        cached_text = "n/a" if cached is None else f"{cached:.0%}"
        print(f"      retrospective: {retrospective['chars']} chars, "
              f"{retrospective['changes']} change(s), "
              f"{retrospective['cost']:.4f} yuan, cache hit {cached_text}")


def _run_paths(args, out_dir: Path) -> tuple:
    """Resolve the two new paths against *this* process, not the scratch repo.

    The review runs with `cwd=<scratch repo>`, so a relative path handed to it
    would resolve inside a directory that is deleted moments later. Both are
    made absolute here, before they ever reach the CLI.
    """
    harness_skill = Path(args.harness_skill).resolve() if args.harness_skill else None
    skill_review_dir = None
    if args.skill_review:
        chosen = args.skill_review_dir or (out_dir / "skill-candidates")
        skill_review_dir = Path(chosen).resolve()
    return harness_skill, skill_review_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True,
                        help="case id from the field-contract manifest")
    parser.add_argument("--arm", default="graph", choices=ARMS)
    parser.add_argument("--summary", action="store_true",
                        help="inject the index's change summary (graph arm only)")
    parser.add_argument("--harness-skill", default=None,
                        help="process-discipline skill to inject as the review's "
                             "second system message (off by default)")
    parser.add_argument("--skill-review", action="store_true",
                        help="also have the loop retrospect each run, writing a "
                             "candidate skill (needs --harness-skill)")
    parser.add_argument("--skill-review-dir", default=None,
                        help="where candidates go "
                             "(default: <out-dir>/skill-candidates)")
    parser.add_argument("--runs", type=int, default=1,
                        help="repeat the run N times, to see whether it is stable")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    args = parser.parse_args()

    if args.summary and args.arm == "nograph":
        parser.error("--summary needs the index; the nograph arm has none")
    if args.skill_review and not args.harness_skill:
        # Caught here rather than by the CLI, which would fail once per run
        # after materializing a scratch repo and building an index each time.
        parser.error("--skill-review needs --harness-skill: the retrospective "
                     "revises the skill injected as the second system message")
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
    harness_skill, skill_review_dir = _run_paths(args, out_dir)
    print(f"{case.id}  [{args.arm}, summary={args.summary}, "
          f"harness_skill={harness_skill.name if harness_skill else None}, "
          f"budget={args.max_turns} turns / {args.max_tokens} tokens]")
    print(f"gold sites: {len(case.gold)}   motive: {case.motive}")
    if skill_review_dir is not None:
        print(f"candidates -> {skill_review_dir}")

    failures = 0
    for run_index in range(1, args.runs + 1):
        try:
            record = run_once(case, arm=args.arm, summary=args.summary,
                              max_turns=args.max_turns,
                              max_tokens=args.max_tokens, run_index=run_index,
                              harness_skill=harness_skill,
                              skill_review_dir=skill_review_dir)
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
