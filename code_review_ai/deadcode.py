"""Dead-code / orphan symbol detection: candidates for safe removal.

Pure read-side query over the index (mirrors testimpact.py). A symbol is a
dead-code candidate when it has no resolved callers (``nodes.in_degree == 0``)
and is not an entry point — an ``entry_names`` short-name glob match or an
``entry_decorators`` decorator match. A file is a candidate when no other
module imports it (no resolved import edge into its module node) and it
contains no entry or test symbol. flow/community are deliberately NOT
criteria: under the current flat-flow / structural-community model every
callerless function is its own flow root and every function is anchored into a
community, so both are vacuous (see the design spec).
"""

import fnmatch
import json
import sqlite3

from code_review_ai import qname


def find_dead_code(conn: sqlite3.Connection, config) -> dict:
    """Return the dead-code candidate report: {"symbols", "files", "meta"}.

    ``symbols`` — function/method/class with in_degree == 0 and not an entry.
    ``files``  — whole files nothing imports, rolled up with their dead symbols.
    ``meta``   — counts plus a static-analysis disclaimer.
    """
    symbols = _dead_symbols(conn, config)
    files = _dead_files(conn, config, symbols)
    return {
        "symbols": symbols,
        "files": files,
        "meta": {
            "symbol_count": len(symbols),
            "file_count": len(files),
            "note": ("候选是静态分析的删码候选，不是自动删除令：动态调用、反射、"
                     "多态覆盖与框架魔法不可见，删除前请人工核对。"),
        },
    }


def _dead_symbols(conn: sqlite3.Connection, config) -> list[dict]:
    """Symbol-tier candidates: function/method/class nodes with no resolved
    callers (in_degree == 0), excluding test nodes and entry points."""
    rows = conn.execute(
        "SELECT qualified_name, kind, file_path, start_line, signature, decorators "
        "FROM nodes "
        "WHERE kind IN ('function','method','class') "
        "AND is_test = 0 AND in_degree = 0"
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        decorators = _decorators(row["decorators"])
        if _is_entry(row["qualified_name"], decorators, config):
            continue
        out.append({
            "qname": row["qualified_name"],
            "kind": row["kind"],
            "file": row["file_path"],
            "line": row["start_line"],
            "signature": row["signature"],
            "decorators": decorators,
        })
    return out


def _dead_files(conn: sqlite3.Connection, config,
                symbols: list[dict]) -> list[dict]:
    """File-tier candidates: module nodes nothing imports (no resolved import
    edge targeting them), excluding __init__.py and files holding an entry or
    test symbol. Dead symbols inside each file are rolled up."""
    rows = conn.execute(
        "SELECT n.qualified_name, n.file_path "
        "FROM nodes n "
        "WHERE n.kind = 'module' "
        "AND n.file_path NOT LIKE '%__init__.py' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM edges e "
        "  WHERE e.kind = 'import' AND e.resolution = 'resolved' "
        "    AND e.target = n.qualified_name)"
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        file_path = row["file_path"]
        if _file_has_entry_or_test(conn, file_path, config):
            continue
        inner = [s for s in symbols if s["file"] == file_path]
        out.append({
            "path": file_path,
            "qname": row["qualified_name"],
            "symbol_count": len(inner),
            "symbols": [s["qname"] for s in inner],
        })
    return out


def _file_has_entry_or_test(conn: sqlite3.Connection, file_path: str,
                            config) -> bool:
    """True when a file holds an entry symbol or any test node — such a file
    is reachable/runnable without a static importer, so it is not a candidate."""
    rows = conn.execute(
        "SELECT qualified_name, decorators, is_test FROM nodes WHERE file_path = ?",
        (file_path,),
    ).fetchall()
    return any(
        row["is_test"] or _is_entry(row["qualified_name"],
                                    _decorators(row["decorators"]), config)
        for row in rows
    )


def _is_entry(qualified_name: str, decorators: list[str], config) -> bool:
    """True when a symbol is an entry point: short-name glob match on
    entry_names, or any decorator matching an entry_decorators glob."""
    if any(fnmatch.fnmatch(qname.short(qualified_name), pat)
           for pat in config.entry_names):
        return True
    return any(
        fnmatch.fnmatch(decorator, pat)
        for decorator in decorators
        for pat in config.entry_decorators
    )


def _decorators(raw: str | None) -> list[str]:
    """Decode the decorators JSON column, tolerating NULL / empty / bad JSON."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []
