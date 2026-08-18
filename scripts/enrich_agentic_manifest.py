"""Merge derived benchmark fields into agentic-eval-real-repos.json so both
the full-agent harness (full_agent_eval) and the impact harness (benchmark /
run_swebench_suite) can consume the same case set.

Derives, per case and from its real fix commit:
  repo          — owner/name derived from repo_url
  base_commit   — parent of source_commit (the buggy state the impact line
                  indexes; matches full_agent_eval's reverse mutation)
  changed_ranges— file -> [start, end] line ranges on the base side (the
                  buggy lines the fix rewrites), parsed from `git diff`
  gold_files    — test files the fix touched (the tests that verify it)

Output is re-serialized in the manifest's original style (2-space indent,
scalar arrays inline) so the diff shows only the added fields.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPOS_DIR = Path(".code-review-ai/external-repos")
MANIFEST = Path("benchmarks/agentic-eval-real-repos.json")

_DIFF = re.compile(r"^diff --git a/(.+?) b/(.+?)$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_SCALARS = (str, int, float, bool) + (type(None),)


def _git(repo: Path, args: list[str]) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace").stdout


def _changed_ranges(diff: str) -> dict[str, list[list[int]]]:
    """Map repo-relative file -> list of [start, end] line ranges (base side)."""
    ranges: dict[str, list[list[int]]] = {}
    current_file: str | None = None
    for line in diff.splitlines():
        file_match = _DIFF.match(line)
        if file_match:
            current_file = file_match.group(2)
            continue
        hunk_match = _HUNK.match(line)
        if not hunk_match or not current_file:
            continue
        old_start = max(int(hunk_match.group(1)), 1)
        old_count = max(int(hunk_match.group(2) or 1), 1)
        ranges.setdefault(current_file, []).append(
            [old_start, old_start + old_count - 1])
    return ranges


def _is_test_file(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    basename = parts[-1]
    return ("test" in parts) or basename.startswith("test") \
        or basename.startswith("Test")


def _derive(case: dict) -> dict:
    repo = REPOS_DIR / case["repo_name"]
    source = case["source_commit"]
    base = _git(repo, ["rev-parse", f"{source}^"]).strip()
    diff = _git(repo, ["diff", "--unified=0", base, source, "--",
                       *case["mutation_paths"]])
    names = [name for name in _git(repo, ["diff", "--name-only", base, source])
             .splitlines() if name.strip()]
    gold_files = [name for name in names if _is_test_file(name)]
    return {
        "repo": case["repo_url"].replace("https://github.com/", "").replace(".git", ""),
        "base_commit": base,
        "changed_ranges": _changed_ranges(diff),
        "gold_files": gold_files,
    }


# load-payload-forwarding's fix commit touched only itsdangerous.py; the repo's
# single test module (tests.py) already exercises load_payload at that commit,
# so it is the gold test file for the impact line.
GOLD_FALLBACK = {"itsdangerous-load-payload-forwarding": ["tests.py"]}


def _scalar(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _format(value: object, indent: int = 0) -> str:
    """Pretty-print matching the manifest's hand style: 2-space indent, scalar
    arrays inline, object arrays expanded."""
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = [f'{pad}  "{key}": {_format(item, indent + 1)}'
                 for key, item in value.items()]
        return "{\n" + ",\n".join(lines) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(isinstance(item, _SCALARS) for item in value):
            return "[" + ", ".join(_scalar(item) for item in value) + "]"
        lines = [f"{pad}  {_format(item, indent + 1)}" for item in value]
        return "[\n" + ",\n".join(lines) + f"\n{pad}]"
    return _scalar(value)


def main() -> None:
    cases = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for case in cases:
        derived = _derive(case)
        case["repo"] = derived["repo"]
        case["base_commit"] = derived["base_commit"]
        case["changed_ranges"] = derived["changed_ranges"]
        case["gold_files"] = GOLD_FALLBACK.get(case["id"], derived["gold_files"])
    MANIFEST.write_text(_format(cases) + "\n", encoding="utf-8")
    print(f"wrote {len(cases)} cases to {MANIFEST}")


if __name__ == "__main__":
    main()
