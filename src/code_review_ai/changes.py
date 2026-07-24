from __future__ import annotations
import re
import subprocess
from code_review_ai.config import Config
from code_review_ai.parser import parse_file


def _git_diff(base: str, files: list[str] | None) -> dict[str, list[tuple[int, int]]]:
    """Return {file_path: [(start, end), ...]} changed line ranges (added/removed)."""
    args = ["git", "diff", "--unified=0", base]
    if files:
        args += ["--"] + files
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(
            f"git diff failed (exit {out.returncode}): {out.stderr.strip()}"
        )
    ranges: dict[str, list[tuple[int, int]]] = {}
    cur_file = None
    for line in out.stdout.splitlines():
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m:
            cur_file = m.group(1)
            ranges.setdefault(cur_file, [])
            continue
        h = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if h and cur_file:
            start = int(h.group(1))
            count = int(h.group(2)) if h.group(2) else 1
            if count > 0:
                ranges[cur_file].append((start, start + count - 1))
    return ranges


def _overlaps(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(not (end < s or start > e) for s, e in ranges)


def detect_changed_symbols(config: Config,
                           symbols: list[str] | None = None,
                           files: list[str] | None = None) -> list[str]:
    if symbols is not None:
        return list(symbols)
    try:
        diff = _git_diff(config.diff_base, files)
    except RuntimeError:
        return []
    repo = config.repo_path
    out: list[str] = []
    for rel, ranges in diff.items():
        path = f"{repo}/{rel}"
        try:
            pf = parse_file(path, repo)
        except OSError:
            continue
        for n in pf.nodes:
            if n.kind not in ("function", "method"):
                continue
            if _overlaps(n.start_line, n.end_line, ranges):
                out.append(n.qualified_name)
    return out
