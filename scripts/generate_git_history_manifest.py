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


def _history(repo_path: str, scan: int) -> list[dict]:
    output = _git(repo_path, [
        "log", "-n", str(scan), "--format=@@@%H|%P|%cs|%s", "--name-only",
        "--", "fastapi", "tests",
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


def _eligible(record: dict) -> bool:
    production = _matching(record["files"], "fastapi/")
    tests = _matching(record["files"], "tests/")
    return len(record["parents"]) == 1 and bool(production and tests)


def _matching(files: list[str], prefix: str) -> list[str]:
    return [path for path in files if path.startswith(prefix) and path.endswith(".py")]


def _select(records: list[dict], count: int) -> list[dict]:
    candidates = [record for record in records if _eligible(record)]
    selected: list[dict] = []
    used_primary_files: set[str] = set()
    for record in candidates:
        primary = _matching(record["files"], "fastapi/")[0]
        if primary not in used_primary_files:
            selected.append(record)
            used_primary_files.add(primary)
        if len(selected) == count:
            return selected
    for record in candidates:
        if record not in selected:
            selected.append(record)
        if len(selected) == count:
            return selected
    raise ValueError(f"requested {count} cases, found {len(selected)}")


def _changed_ranges(diff: str) -> dict[str, list[list[int]]]:
    ranges: dict[str, list[list[int]]] = {}
    current_file: str | None = None
    for line in diff.splitlines():
        file_match = DIFF.match(line)
        if file_match:
            current_file = file_match.group(2)
            continue
        hunk_match = HUNK.match(line)
        if not hunk_match or not current_file or not current_file.endswith(".py"):
            continue
        start = max(int(hunk_match.group(1)), 1)
        count = int(hunk_match.group(2) or 1)
        ranges.setdefault(current_file, []).append(
            [start, start + max(count, 1) - 1]
        )
    return ranges


def _case(repo_path: str, repo: str, record: dict) -> dict:
    base_commit = record["parents"][0]
    diff = _git(repo_path, [
        "diff", "--unified=0", base_commit, record["commit"], "--", "fastapi",
    ])
    return {
        "id": f"fastapi__fastapi-{record['commit'][:10]}",
        "repo": repo,
        "base_commit": base_commit,
        "source_commit": record["commit"],
        "created_at": record["date"],
        "subject": record["subject"],
        "changed_ranges": _changed_ranges(diff),
        "gold_files": _matching(record["files"], "tests/"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--repo", default="fastapi/fastapi")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--scan", type=int, default=1200)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    records = _select(_history(args.repo_path, args.scan), args.count)
    cases = [_case(args.repo_path, args.repo, record) for record in records]
    if not all(case["changed_ranges"] and case["gold_files"] for case in cases):
        raise ValueError("generated an empty change seed or test target")
    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(cases, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(cases)} cases to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
