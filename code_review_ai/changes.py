import json
import os
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


def _diff_coverage(config: Config, diff_ranges: dict[str, list[tuple[int, int]]],
                   numstat: dict[str, tuple[int, int]], deleted: set[str],
                   kinds: tuple[str, ...] = ("function", "method", "class"),
                   covered_files: set[str] | None = None,
                   ) -> tuple[list[dict], list[dict]]:
    """Split a diff into function-level records and uncovered hunks.

    Every changed file (numstat ∪ diff_ranges) is accounted for: hunks that
    overlap a function/method/class node become records; everything else
    (unsupported extension, module-level change, binary, deleted) becomes an
    uncovered_changes entry — {file, hunks: [{start, count}], deleted?} — so
    no change silently disappears. Returns (records, uncovered_changes).
    `covered_files` is the set of files whose deletions delete_change reports;
    a deleted or empty-hunk file in it is suppressed instead of double
    reporting it as uncovered.
    """
    covered_files = covered_files or set()
    repo = config.repo_path
    records: list[dict] = []
    uncovered: list[dict] = []
    for rel in dict.fromkeys([*numstat, *diff_ranges]):
        if rel in deleted:
            if rel not in covered_files:
                uncovered.append({"file": rel, "hunks": [], "deleted": True})
            continue
        hunks = diff_ranges.get(rel)
        if not hunks:
            # in the diff but no line hunks — binary / rename
            if rel not in covered_files:
                uncovered.append({"file": rel, "hunks": []})
            continue
        path = f"{repo}/{rel}"
        try:
            pf = parse_file(path, repo)
        except (OSError, ValueError):
            # OSError: file gone from disk. ValueError: unsupported extension
            # (e.g. *.md in the diff) — nothing to attribute, hunks stay raw.
            uncovered.append({"file": rel,
                              "hunks": [{"start": s, "count": c} for s, c in hunks]})
            continue
        changed = [n for n in pf.nodes
                   if n.kind in kinds and _overlaps(n.start_line, n.end_line, hunks)]
        for n in changed:
            records.append({"qname": n.qualified_name, "kind": n.kind,
                            "file": rel, "start_line": n.start_line,
                            "end_line": n.end_line})
        uncovered_hunks = [h for h in hunks
                           if not any(_overlaps(n.start_line, n.end_line, [h])
                                      for n in changed)]
        if uncovered_hunks:
            uncovered.append({"file": rel,
                              "hunks": [{"start": s, "count": c} for s, c in uncovered_hunks]})
    return records, uncovered


def _delete_change(config: Config, conn, deleted_files: set[str],
                   numstat: dict[str, tuple[int, int]],
                   ) -> tuple[list[dict], set[str]]:
    """Deleted-function records for the current diff, from tombstones.

    Candidate files: deleted files + surviving files that removed lines.
    Tombstones are filtered to qnames no longer in the live graph (a tombstone
    whose qname was re-added isn't a current deletion), deduped per
    (file_path, qname) keeping the latest, and each becomes one delete_change
    record with its one-hop upstream. Returns (records, covered_files);
    covered_files is the set of files whose deletions delete_change reports so
    _diff_coverage suppresses their uncovered entries instead of double
    reporting them."""
    live = {r["qualified_name"]
            for r in conn.execute("SELECT qualified_name FROM nodes")}
    records: list[dict] = []
    covered: set[str] = set()
    candidates = set(deleted_files) | {
        rel for rel, (_, removed) in numstat.items() if removed > 0}
    for rel in sorted(candidates):
        abs_path = os.path.join(config.repo_path, rel)
        rows = conn.execute(
            "SELECT * FROM tombstones WHERE file_path=?", (abs_path,)).fetchall()
        latest: dict = {}
        for row in rows:
            if row["qname"] in live:
                continue
            key = (row["file_path"], row["qname"])
            if key not in latest or row["id"] > latest[key]["id"]:
                latest[key] = row
        file_records = [{
            "qname": row["qname"], "kind": row["kind"], "file": rel,
            "file_deleted": bool(row["file_deleted"]),
            "start_line": row["start_line"], "end_line": row["end_line"],
            "signature": row["signature"], "is_test": row["is_test"],
            "risk": 90,
            "upstream": [{"source": u["source"], "kind": u["kind"],
                          "file": _relative_to_repo(config, u["file"])}
                         for u in json.loads(row["upstream_json"] or "[]")],
        } for row in latest.values()]
        if file_records:
            file_records.sort(key=lambda r: r["start_line"] or 0)
            records.extend(file_records)
            covered.add(rel)
    return records, covered


def _changed_functions(config: Config, diff_ranges: dict[str, list[tuple[int, int]]],
                       kinds: tuple[str, ...] = ("function", "method", "class")) -> list[dict]:
    """Backward-compat wrapper: function/method/class records only."""
    records, _ = _diff_coverage(config, diff_ranges, {}, set(), kinds=kinds)
    return records


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


def assess_symbol_risk(conn, symbol: str, deleted: bool = False) -> int:
    """0-100 blast-radius score for one changed symbol (spec 2026-08-09).

    deleted -> 90; any cross-module resolved caller -> min(100, 60+10*n);
    same-module callers only -> min(59, 30+5*n); resolved leaf -> 10;
    unresolved (not in graph) -> 50. Cross-module means the caller node's
    file_path differs from the target's (module == file in this graph).
    """
    if deleted:
        return 90
    target = conn.execute(
        "SELECT file_path FROM nodes WHERE qualified_name=?", (symbol,)).fetchone()
    if target is None:
        return 50
    target_file = target["file_path"]
    incoming = conn.execute(
        "SELECT DISTINCT source FROM edges WHERE target=? AND resolution='resolved'",
        (symbol,)).fetchall()
    cross = same = 0
    for edge in incoming:
        source = conn.execute(
            "SELECT file_path FROM nodes WHERE qualified_name=?",
            (edge["source"],)).fetchone()
        if source is None:
            continue
        if source["file_path"] != target_file:
            cross += 1
        else:
            same += 1
    if cross:
        return min(100, 60 + 10 * cross)
    if same:
        return min(59, 30 + 5 * same)
    return 10


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
                            "start_line": 0, "end_line": 0,
                            "risk": assess_symbol_risk(conn, symbol)})
            continue
        rel = _relative_to_repo(config, row["file_path"])
        files.add(rel)
        records.append({"qname": symbol, "kind": row["kind"], "file": rel,
                        "start_line": row["start_line"], "end_line": row["end_line"],
                        "risk": assess_symbol_risk(conn, symbol)})
    return {"summary": {"files_changed": len(files), "lines_added": 0,
                        "lines_removed": 0, "changed_functions": len(symbols),
                        "uncovered_changes": 0, "delete_change": 0},
            "changed_functions": records,
            "uncovered_changes": [],
            "delete_change": []}


def build_change_summary(config: Config, conn, symbols: list[str] | None = None,
                         files: list[str] | None = None) -> dict:
    """Change summary + changed functions + uncovered changes + deletions.
    With `symbols`, resolve each qname from the graph; otherwise compute from
    the git diff of `files` (or the whole tree) against a resolved base
    (diff_base, else the branch's upstream, else HEAD^). `uncovered_changes`
    lists files whose changes no function/class covers — module-level hunks,
    unsupported extensions, binary and deleted files — so the review sees
    what the graph cannot attribute. Deleted files/functions that were
    tombstoned on the incremental update appear in `delete_change` (with
    one-hop upstream) and are suppressed from `uncovered_changes`; deletions
    without a tombstone fall back to uncovered. Returns {"summary",
    "changed_functions", "uncovered_changes", "delete_change"}.
    Raises RuntimeError if the git diff fails (e.g. no commits at all)."""
    if symbols is not None:
        return _symbols_summary(config, conn, symbols)
    base = _resolve_diff_base(config)
    diff, deleted = _git_diff(base, files, config.repo_path)
    numstat = _git_numstat(base, files, config.repo_path)
    delete_change, covered_files = _delete_change(config, conn, deleted, numstat)
    functions, uncovered = _diff_coverage(config, diff, numstat, deleted,
                                          covered_files=covered_files)
    for record in functions:
        record["risk"] = assess_symbol_risk(conn, record["qname"])
    return {"summary": {"files_changed": len(numstat),
                        "lines_added": sum(added for added, _ in numstat.values()),
                        "lines_removed": sum(removed for _, removed in numstat.values()),
                        "changed_functions": len(functions),
                        "uncovered_changes": len(uncovered),
                        "delete_change": len(delete_change)},
            "changed_functions": functions,
            "uncovered_changes": uncovered,
            "delete_change": delete_change}
