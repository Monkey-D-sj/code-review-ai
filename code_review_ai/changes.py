import re
import subprocess
from pathlib import Path

from code_review_ai.config import DEFAULTS, Config
from code_review_ai.parser import parse_file


def current_head(config: Config) -> str | None:
    """Current git HEAD sha, or None if unresolvable (no commits / not a repo)."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=config.repo_path,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace")
    except OSError:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _git_diff(base: str, files: list[str] | None,
              cwd: str | None = None) -> tuple[dict[str, list[tuple[int, int]]], set[str]]:
    """Return ({file: [(start, count), ...]}, deleted_files).

    Each hunk is git's new-side ``+b,m`` (start, count) — position and size
    together. deleted_files is the set of pure deletions (``+++ /dev/null``),
    which produce no + hunks and so would otherwise be invisible.
    """
    args = ["git", "diff", "--unified=0", base]
    if files:
        args += ["--"] + files
    # git diff output is UTF-8; text=True would decode with the locale codepage
    # (GBK on zh-CN Windows) and crash on non-ASCII content. errors="replace"
    # keeps the @@ line-range parsing robust to any undecodable bytes.
    out = subprocess.run(args, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", cwd=cwd)
    if out.returncode != 0:
        raise RuntimeError(
            f"git diff failed (exit {out.returncode}): {out.stderr.strip()}"
        )
    ranges: dict[str, list[tuple[int, int]]] = {}
    deleted: set[str] = set()
    cur_file: str | None = None
    cur_a: str | None = None
    for line in out.stdout.splitlines():
        a = re.match(r"^--- a/(.+)$", line)
        if a:
            cur_a = a.group(1)
            continue
        b = re.match(r"^\+\+\+ b/(.+)$", line)
        if b:
            cur_file = b.group(1)
            ranges.setdefault(cur_file, [])
            continue
        if re.match(r"^\+\+\+ (?:b/)?/dev/null$", line) and cur_a:
            deleted.add(cur_a)
            cur_file = None
            continue
        h = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if h and cur_file:
            start = int(h.group(1))
            count = int(h.group(2)) if h.group(2) else 1
            if count > 0:
                ranges[cur_file].append((start, count))
    return ranges, deleted


def _overlaps(start: int, end: int, hunks: list[tuple[int, int]]) -> bool:
    """True if node range [start, end] overlaps any hunk (start, count)."""
    return any(not (end < s or start > s + c - 1) for s, c in hunks)


def _git_numstat(base: str, files: list[str] | None = None,
                 cwd: str | None = None) -> dict[str, tuple[int, int]]:
    """{file: (added, removed)} per changed file. Binary files map to (0, 0)
    but keep their key so files_changed still counts them."""
    args = ["git", "diff", "--numstat", base]
    if files:
        args += ["--"] + files
    out = subprocess.run(args, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", cwd=cwd)
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


def _resolve_diff_base(config: Config) -> str:
    """The git ref to diff against: an explicitly configured diff_base (any
    value other than the default) wins; otherwise the current branch's upstream
    (@{upstream}) when it exists, else HEAD^ (the last commit). The diff is
    always based on the current branch and never depends on a main ref
    existing. Falls back to the configured base so a genuinely empty repo still
    surfaces a git error instead of silently returning empty."""
    if config.diff_base != DEFAULTS["diff_base"]:
        return config.diff_base
    for candidate in ("@{upstream}", "HEAD^"):
        check = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", candidate],
            cwd=config.repo_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if check.returncode == 0:
            return candidate
    return config.diff_base


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
        except (OSError, ValueError):
            # OSError: file gone from disk. ValueError: unsupported extension
            # (e.g. *.md in the diff) — not source, nothing to report.
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
    (or the whole tree when neither is given) against a resolved base
    (diff_base, else the branch's upstream, else HEAD^).

    Raises RuntimeError if the git diff fails (e.g. no commits at all) so
    callers surface the misconfiguration instead of returning an empty list."""
    if symbols is not None:
        return list(symbols)
    diff, _ = _git_diff(_resolve_diff_base(config), files, config.repo_path)
    return [record["qname"] for record in _changed_functions(
        config, diff, kinds=("function", "method"))]


def _relative_to_repo(config: Config, file_path: str) -> str:
    try:
        return Path(file_path).resolve().relative_to(Path(config.repo_path).resolve()).as_posix()
    except ValueError:
        return file_path.replace("\\", "/")


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
    tree) against a resolved base (diff_base, else the branch's upstream, else
    HEAD^). Returns {"summary", "changed_functions"}.
    Raises RuntimeError if the git diff fails (e.g. no commits at all)."""
    if symbols is not None:
        return _symbols_summary(config, conn, symbols)
    base = _resolve_diff_base(config)
    diff, _deleted = _git_diff(base, files, config.repo_path)
    numstat = _git_numstat(base, files, config.repo_path)
    functions = _changed_functions(config, diff)
    return {"summary": {"files_changed": len(numstat),
                        "lines_added": sum(added for added, _ in numstat.values()),
                        "lines_removed": sum(removed for _, removed in numstat.values()),
                        "changed_functions": len(functions)},
            "changed_functions": functions}
