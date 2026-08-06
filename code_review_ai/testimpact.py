"""Test impact analysis: given changed symbols, which tests cover them?

Built on ``impact.get_impact(tests="only")`` - the reverse flow query
restricted to test nodes - then grouped into a "run only these tests"
payload. A test is affected iff the changed symbol is reachable from it
(directly or transitively), which the existing BFS flows already encode.
"""

import sqlite3

from code_review_ai import qname
from code_review_ai.impact import get_impact


def get_test_impact(conn: sqlite3.Connection, changed_symbols: list[str],
                    max_nodes_per_direction: int = 50) -> dict:
    """For each changed symbol, find the tests that reach it and return a
    "run these tests" payload.

    Reuses ``get_impact`` in ``tests="only"`` mode so upstream callers and
    flow entry points are already restricted to test nodes (``is_test=1``);
    this function only aggregates them by test and records which changed
    symbols each test covers.
    """
    impacts = get_impact(conn, changed_symbols, max_nodes_per_direction,
                         tests="only")
    # test qname -> set of changed symbols it reaches
    covers: dict[str, set[str]] = {}
    not_found: list[str] = []
    for res in impacts:
        symbol = res["symbol"]
        if not res["found"]:
            not_found.append(symbol)
            continue
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
    return {
        "changed_symbols": list(changed_symbols),
        "affected_tests": affected,
        "test_files": sorted(files),
        "test_count": len(affected),
        "not_found": not_found,
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
