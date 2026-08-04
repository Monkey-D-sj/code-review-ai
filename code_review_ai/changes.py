import re
import subprocess
from pathlib import Path

from code_review_ai.config import Config
from code_review_ai.parser import parse_file


def _git_diff(base: str, files: list[str] | None) -> dict[str, list[tuple[int, int]]]:
    """Return {file_path: [(start, end), ...]} changed line ranges (added/removed)."""
    args = ["git", "diff", "--unified=0", base]
    if files:
        args += ["--"] + files
    # git diff output is UTF-8; text=True would decode with the locale codepage
    # (GBK on zh-CN Windows) and crash on non-ASCII content. errors="replace"
    # keeps the @@ line-range parsing robust to any undecodable bytes.
    out = subprocess.run(args, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
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


def _git_numstat(base: str, files: list[str] | None = None) -> dict[str, tuple[int, int]]:
    """{file: (added, removed)} per changed file. Binary files map to (0, 0)
    but keep their key so files_changed still counts them."""
    args = ["git", "diff", "--numstat", base]
    if files:
        args += ["--"] + files
    out = subprocess.run(args, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(
            f"git diff failed (exit {out.returncode}): {out.stderr.strip()}"
        )
    stats: dict[str, tuple[int, int]] = {}
    for line in out.stdout.splitlines():
        added_s, removed_s, path = line.split("\t", 2)
        if added_s == "-" or removed_s == "-":
            stats[path] = (0, 0)
            continue
        stats[path] = (int(added_s), int(removed_s))
    return stats


def _changed_functions(config: Config, diff_ranges: dict[str, list[tuple[int, int]]],
                       kinds: tuple[str, ...] = ("function", "method", "class")) -> list[dict]:
    """Rich records for nodes overlapping changed line ranges.

    Returns [{qname, kind, file, start_line, end_line}] with repo-relative file.
    """
    repo = config.repo_path
    out: list[dict] = []
    for rel, ranges in diff_ranges.items():
        path = f"{repo}/{rel}"
        try:
            pf = parse_file(path, repo)
        except OSError:
            continue
        for node in pf.nodes:
            if node.kind not in kinds:
                continue
            if _overlaps(node.start_line, node.end_line, ranges):
                out.append({"qname": node.qualified_name, "kind": node.kind,
                            "file": rel, "start_line": node.start_line,
                            "end_line": node.end_line})
    return out


def detect_changed_symbols(config: Config,
                           symbols: list[str] | None = None,
                           files: list[str] | None = None) -> list[str]:
    """Changed symbol qnames: explicit `symbols`, or the git diff of `files`
    (or the whole tree when neither is given) against config.diff_base.

    Raises RuntimeError if the git diff fails (e.g. diff_base doesn't exist) so
    callers surface the misconfiguration instead of returning an empty list."""
    if symbols is not None:
        return list(symbols)
    diff = _git_diff(config.diff_base, files)
    return [record["qname"] for record in _changed_functions(
        config, diff, kinds=("function", "method"))]


def _relative_to_repo(config: Config, file_path: str) -> str:
    try:
        return str(Path(file_path).resolve().relative_to(Path(config.repo_path).resolve()))
    except ValueError:
        return file_path


def _symbols_summary(config: Config, conn, symbols: list[str]) -> dict:
    files: set[str] = set()
    records: list[dict] = []
    for symbol in symbols:
        row = conn.execute(
            "SELECT kind, file_path, start_line, end_line FROM nodes WHERE qualified_name=?",
            (symbol,),
        ).fetchone()
        if row is None:
            records.append({"qname": symbol, "kind": None, "file": None,
                            "start_line": 0, "end_line": 0})
            continue
        rel = _relative_to_repo(config, row["file_path"])
        files.add(rel)
        records.append({"qname": symbol, "kind": row["kind"], "file": rel,
                        "start_line": row["start_line"], "end_line": row["end_line"]})
    return {"summary": {"files_changed": len(files), "lines_added": 0,
                        "lines_removed": 0, "changed_functions": len(symbols)},
            "changed_functions": records}


def build_change_summary(config: Config, conn, symbols: list[str] | None = None,
                         files: list[str] | None = None) -> dict:
    """Change summary + changed functions. With `symbols`, resolve each qname
    from the graph; otherwise compute from the git diff of `files` (or the whole
    tree) against config.diff_base. Returns {"summary", "changed_functions"}.
    Raises RuntimeError if the git diff fails (bad diff_base)."""
    if symbols is not None:
        return _symbols_summary(config, conn, symbols)
    diff = _git_diff(config.diff_base, files)
    numstat = _git_numstat(config.diff_base, files)
    functions = _changed_functions(config, diff)
    return {"summary": {"files_changed": len(numstat),
                        "lines_added": sum(added for added, _ in numstat.values()),
                        "lines_removed": sum(removed for _, removed in numstat.values()),
                        "changed_functions": len(functions)},
            "changed_functions": functions}
