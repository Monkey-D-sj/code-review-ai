"""Generate benchmark cases from a repository's real Git history."""

import argparse
import json
import re
import subprocess
from pathlib import Path


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
DIFF = re.compile(r"^diff --git a/(.+?) b/(.+?)$")


def _git(repo_path: str, arguments: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", repo_path, *arguments], capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout


def _history(repo_path: str, scan: int,
             production_prefix: str, test_prefix: str) -> list[dict]:
    output = _git(repo_path, [
        "log", "-n", str(scan), "--format=@@@%H|%P|%cs|%s", "--name-only",
        "--", production_prefix, test_prefix,
    ])
    records: list[dict] = []
    current: dict | None = None
    for line in output.splitlines():
        if line.startswith("@@@"):
            if current:
                records.append(current)
            commit, parents, date, subject = line[3:].split("|", 3)
            current = {"commit": commit, "parents": parents.split(),
                       "date": date, "subject": subject, "files": []}
        elif line.strip() and current:
            current["files"].append(line.strip())
    if current:
        records.append(current)
    return records


def _eligible(record: dict, production_prefix: str,
              test_prefix: str, suffix: str) -> bool:
    production = _matching(record["files"], production_prefix, suffix)
    tests = _matching(record["files"], test_prefix, suffix)
    return len(record["parents"]) == 1 and bool(production and tests)


def _matching(files: list[str], prefix: str, suffix: str) -> list[str]:
    return [path for path in files
            if path.startswith(prefix) and path.endswith(suffix)]


def _ordered_candidates(records: list[dict], production_prefix: str,
                        test_prefix: str, suffix: str,
                        prefer_recent: bool = False) -> list[dict]:
    candidates = [record for record in records
                  if _eligible(record, production_prefix, test_prefix, suffix)]
    if prefer_recent:
        return candidates
    selected: list[dict] = []
    used_primary_files: set[str] = set()
    for record in candidates:
        primary = _matching(record["files"], production_prefix, suffix)[0]
        if primary not in used_primary_files:
            selected.append(record)
            used_primary_files.add(primary)
    for record in candidates:
        if record not in selected:
            selected.append(record)
    return selected


def _changed_ranges(diff: str, suffix: str) -> dict[str, list[list[int]]]:
    ranges: dict[str, list[list[int]]] = {}
    current_file: str | None = None
    for line in diff.splitlines():
        file_match = DIFF.match(line)
        if file_match:
            current_file = file_match.group(2)
            continue
        hunk_match = HUNK.match(line)
        if not hunk_match or not current_file or not current_file.endswith(suffix):
            continue
        start = max(int(hunk_match.group(1)), 1)
        count = int(hunk_match.group(2) or 1)
        ranges.setdefault(current_file, []).append(
            [start, start + max(count, 1) - 1]
        )
    return ranges


def _case(repo_path: str, repo: str, record: dict,
          production_prefix: str, test_prefix: str, suffix: str) -> dict:
    base_commit = record["parents"][0]
    diff = _git(repo_path, [
        "diff", "--unified=0", base_commit, record["commit"],
        "--", production_prefix,
    ])
    return {
        "id": f"{repo.replace('/', '__')}-{record['commit'][:10]}",
        "repo": repo,
        "base_commit": base_commit,
        "source_commit": record["commit"],
        "created_at": record["date"],
        "subject": record["subject"],
        "changed_ranges": _changed_ranges(diff, suffix),
        "gold_files": _matching(record["files"], test_prefix, suffix),
    }


def _generate_cases(repo_path: str, repo: str, records: list[dict], count: int,
                    production_prefix: str, test_prefix: str, suffix: str,
                    excluded_subjects: list[str], max_gold_files: int,
                    max_production_files: int) -> list[dict]:
    cases: list[dict] = []
    for record in records:
        subject = record["subject"].lower()
        if any(term.lower() in subject for term in excluded_subjects):
            continue
        case = _case(repo_path, repo, record, production_prefix, test_prefix, suffix)
        if not case["changed_ranges"] or not case["gold_files"]:
            continue
        if len(case["gold_files"]) > max_gold_files:
            continue
        if len(case["changed_ranges"]) > max_production_files:
            continue
        cases.append(case)
        if len(cases) == count:
            return cases
    raise ValueError(f"requested {count} cases, found {len(cases)} after filtering")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--repo", default="fastapi/fastapi")
    parser.add_argument("--production-prefix", default="fastapi/")
    parser.add_argument("--test-prefix", default="tests/")
    parser.add_argument("--suffix", default=".py")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--scan", type=int, default=1200)
    parser.add_argument("--exclude-subject", action="append", default=[])
    parser.add_argument("--max-gold-files", type=int, default=20)
    parser.add_argument("--max-production-files", type=int, default=20)
    parser.add_argument("--prefer-recent", action="store_true")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    history = _history(args.repo_path, args.scan,
                       args.production_prefix, args.test_prefix)
    records = _ordered_candidates(history, args.production_prefix,
                                  args.test_prefix, args.suffix, args.prefer_recent)
    cases = _generate_cases(
        args.repo_path, args.repo, records, args.count, args.production_prefix,
        args.test_prefix, args.suffix, args.exclude_subject, args.max_gold_files,
        args.max_production_files,
    )
    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(cases, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(cases)} cases to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
