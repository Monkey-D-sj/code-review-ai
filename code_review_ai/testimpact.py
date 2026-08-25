"""Test impact analysis: given changed symbols, which tests cover them?

Built on ``impact.get_impact(tests="only")`` - the reverse flow query
restricted to test nodes - then grouped into a "run only these tests"
payload. A test is affected iff the changed symbol is reachable from it
(directly or transitively), which the existing BFS flows already encode.
"""

import sqlite3

from code_review_ai import qname
from code_review_ai.impact import get_impact

# Resolutions that hide a callee behind a breakpoint: a 0-test result built on
# top of these is not trustworthy, so a full-run fallback must be advised.
_BREAKPOINT_RESOLUTIONS = {"dynamic", "candidate"}


def get_test_impact(conn: sqlite3.Connection, changed_symbols: list[str],
                    max_nodes_per_direction: int = 20) -> dict:
    """For each changed symbol, find the tests that reach it and return a
    "run these tests" payload.

    Reuses ``get_impact`` in ``tests="only"`` mode so upstream callers and
    flow entry points are already restricted to test nodes (``is_test=1``);
    this function only aggregates them by test and records which changed
    symbols each test covers.

    Also reports how far the analysis can be trusted (guide §5.4):
    ``complete`` is False and ``fallback_recommended`` True whenever a changed
    symbol is not in the index, or when no test reaches a symbol whose
    neighborhood carries dynamic/candidate breakpoints — in both cases "run
    only these tests" (or "run none") could be wrong.
    """
    impacts = get_impact(conn, changed_symbols, max_nodes_per_direction,
                         tests="only", include_call_sites=False)
    # test qname -> set of changed symbols it reaches
    covers: dict[str, set[str]] = {}
    not_found: list[str] = []
    has_breakpoint = False
    for res in impacts:
        symbol = res["symbol"]
        if not res["found"]:
            not_found.append(symbol)
            continue
        if any(u["resolution"] in _BREAKPOINT_RESOLUTIONS
               for u in res["uncertainty"]):
            has_breakpoint = True
        for node in res["upstream"]:
            covers.setdefault(node["qname"], set()).add(symbol)
        for entry in res["affected_entries"]:
            covers.setdefault(entry, set()).add(symbol)

    test_rows = _fetch_test_nodes(conn, list(covers.keys()))
    affected: list[dict] = []
    files: set[str] = set()
    for test_qname in sorted(covers.keys()):
        row = test_rows.get(test_qname)
        if row is None:
            continue  # defensive: not a test node despite the "only" filter
        files.add(row["file"])
        affected.append({
            "qname": test_qname,
            "name": qname.short(test_qname),
            "file": row["file"],
            "line": row["line"],
            "covers": sorted(covers[test_qname]),
        })
    fallback_reasons: list[str] = []
    if not_found:
        fallback_reasons.append("changed symbol not found in index")
    if not affected and has_breakpoint:
        fallback_reasons.append(
            "no tests reach the changed symbols and the call graph has "
            "dynamic/candidate edges around them")
    fallback_recommended = bool(fallback_reasons)
    return {
        "changed_symbols": list(changed_symbols),
        "affected_tests": affected,
        "test_files": sorted(files),
        "test_count": len(affected),
        "not_found": not_found,
        "complete": not fallback_recommended,
        "fallback_recommended": fallback_recommended,
        "fallback_reasons": fallback_reasons,
    }


def _fetch_test_nodes(conn: sqlite3.Connection,
                      qnames: list[str]) -> dict[str, dict]:
    """Batch-fetch {qname: {file, line}} for test nodes, verifying is_test=1."""
    if not qnames:
        return {}
    placeholders = ",".join("?" for _ in qnames)
    rows = conn.execute(
        f"SELECT qualified_name, file_path, start_line FROM nodes "
        f"WHERE is_test=1 AND qualified_name IN ({placeholders})",
        qnames,
    ).fetchall()
    return {r["qualified_name"]: {"file": r["file_path"], "line": r["start_line"]}
            for r in rows}
