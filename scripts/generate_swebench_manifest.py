"""Generate compact code-review-ai cases from Hugging Face rows JSON."""

import argparse
import json
import re
from pathlib import Path


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
DIFF = re.compile(r"^diff --git a/(.+?) b/(.+?)$")


def _diff_ranges(patch: str) -> dict[str, list[list[int]]]:
    ranges: dict[str, list[list[int]]] = {}
    current_file: str | None = None
    for line in patch.splitlines():
        file_match = DIFF.match(line)
        if file_match:
            current_file = file_match.group(2)
            continue
        hunk_match = HUNK.match(line)
        if not hunk_match or not current_file or not current_file.endswith(".py"):
            continue
        start = max(int(hunk_match.group(1)), 1)
        count = int(hunk_match.group(2) or 1)
        end = start + max(count, 1) - 1
        ranges.setdefault(current_file, []).append([start, end])
    return ranges


def _diff_files(patch: str) -> list[str]:
    return list(dict.fromkeys(
        match.group(2) for line in patch.splitlines()
        if (match := DIFF.match(line)) and match.group(2).endswith(".py")
    ))


def _load_rows(paths: list[str]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        rows.extend(item["row"] for item in payload["rows"])
    return rows


def _parse_quotas(values: list[str]) -> dict[str, int]:
    quotas: dict[str, int] = {}
    for value in values:
        repo, count = value.rsplit("=", 1)
        quotas[repo] = int(count)
    return quotas


def _eligible(row: dict) -> bool:
    return bool(_diff_ranges(row["patch"]) and _diff_files(row["test_patch"]))


def _select(rows: list[dict], quotas: dict[str, int]) -> list[dict]:
    selected: list[dict] = []
    for repo, count in quotas.items():
        candidates = sorted(
            (row for row in rows if row["repo"] == repo and _eligible(row)),
            key=lambda row: (-len(_diff_files(row["patch"])), row["instance_id"]),
        )
        if len(candidates) < count:
            raise ValueError(f"{repo}: requested {count}, found {len(candidates)}")
        selected.extend(candidates[:count])
    return selected


def _compact(row: dict) -> dict:
    return {
        "id": row["instance_id"],
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "difficulty": row.get("difficulty"),
        "changed_ranges": _diff_ranges(row["patch"]),
        "gold_files": _diff_files(row["test_patch"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--repo", action="append", required=True,
                        help="repository quota, for example pytest-dev/pytest=10")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    cases = [_compact(row) for row in _select(
        _load_rows(args.inputs), _parse_quotas(args.repo)
    )]
    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(cases, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(cases)} cases to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
