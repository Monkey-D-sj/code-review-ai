
import json

import sqlite3

_SIGNATURE_LIMIT = 160
_UNCERTAINTY_LIMIT = 20


def _cap_signature(value: str | None) -> str:
    """Truncate long signatures so impact briefs stay compact."""
    text = value or ""
    return text if len(text) <= _SIGNATURE_LIMIT \
        else text[:_SIGNATURE_LIMIT] + "…"


def _node_brief(conn: sqlite3.Connection, node_id: int) -> dict:
    r = conn.execute(
        "SELECT qualified_name,file_path,start_line,signature FROM nodes WHERE id=?",
        (node_id,),
    ).fetchone()
    if r is None:
        return {"qname": str(node_id), "file": "", "line": 0, "sig": ""}
    return {"qname": r["qualified_name"], "file": r["file_path"],
            "line": r["start_line"], "sig": _cap_signature(r["signature"])}


def _slice_flow(conn: sqlite3.Connection, flow_id: int, symbol_node_id: int,
                max_per_dir: int, test_filter: int | None = None
                ) -> tuple[list[dict], list[dict]]:
    rows = conn.execute(
        "SELECT node_id, position FROM flow_memberships WHERE flow_id=? ORDER BY position",
        (flow_id,),
    ).fetchall()
    sym_pos = next((r["position"] for r in rows if r["node_id"] == symbol_node_id), None)
    if sym_pos is None:
        return [], []
    # sym_pos is derived from the UNFILTERED membership: the changed symbol is a
    # production node and would be dropped by test_filter='only', which would
    # make it unlocatable. Only the up/down node sets are filtered, so
    # _node_brief is never called on a filtered-out id (which would hit its
    # str(node_id) fallback and leak a garbage qname).
    up_ids = [r["node_id"] for r in rows if r["position"] < sym_pos]
    down_ids = [r["node_id"] for r in rows if r["position"] > sym_pos]
    if test_filter is not None:
        up_ids = _filter_ids_by_test(conn, up_ids, test_filter)
        down_ids = _filter_ids_by_test(conn, down_ids, test_filter)
    up = [_node_brief(conn, nid) for nid in up_ids[-max_per_dir:]]
    down = [_node_brief(conn, nid) for nid in down_ids[:max_per_dir]]
    return up, down


def _filter_ids_by_test(conn: sqlite3.Connection, ids: list[int],
                        test_filter: int) -> list[int]:
    """Keep only node ids whose is_test matches test_filter (0=business,
    1=test), preserving input order. Empty input short-circuits."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    keep = {r["id"] for r in conn.execute(
        f"SELECT id FROM nodes WHERE id IN ({placeholders}) AND is_test=?",
        (*ids, test_filter),
    )}
    return [nid for nid in ids if nid in keep]


def _edges_fallback(conn: sqlite3.Connection, qname: str, max_per_dir: int,
                    test_filter: int | None = None):
    caller_sql = ("SELECT DISTINCT source FROM edges "
                  "WHERE target=? AND resolution='resolved'")
    callee_sql = ("SELECT DISTINCT target FROM edges "
                  "WHERE source=? AND resolution='resolved'")
    caller_params: list = [qname]
    callee_params: list = [qname]
    if test_filter is not None:
        caller_sql += " AND source IN (SELECT qualified_name FROM nodes WHERE is_test=?)"
        caller_params.append(test_filter)
        callee_sql += " AND target IN (SELECT qualified_name FROM nodes WHERE is_test=?)"
        callee_params.append(test_filter)
    caller_sql += " ORDER BY source"
    callee_sql += " ORDER BY target"
    callers = [_edge_brief(conn, e["source"])
               for e in conn.execute(caller_sql, caller_params)][:max_per_dir]
    callees = [_edge_brief(conn, e["target"])
               for e in conn.execute(callee_sql, callee_params)][:max_per_dir]
    return callers, callees


def _edge_brief(conn: sqlite3.Connection, qname: str) -> dict:
    r = conn.execute("SELECT file_path,start_line,signature FROM nodes WHERE qualified_name=?",
                     (qname,)).fetchone()
    if r is None:
        return {"qname": qname, "file": "", "line": 0, "sig": ""}
    return {"qname": qname, "file": r["file_path"], "line": r["start_line"],
            "sig": _cap_signature(r["signature"])}


_TEST_FILTER = {"exclude": 0, "only": 1, "include": None}

# One-line reason per non-resolved resolution, so uncertainty items stay compact
# (guide §5.2) and test-impact can explain *why* a full-run fallback is advised.
_REASON_BY_RESOLUTION = {
    "dynamic": "receiver type not statically known",
    "unresolved": "target not found in repo",
    "candidate": "one of several possible targets",
    "external": "external dependency",
}

# Resolution priority for uncertainty ordering: breakpoints that hide the most
# likely to matter are surfaced first (direct dynamic first).
_UNCERTAINTY_PRIORITY = {"dynamic": 0, "candidate": 1, "unresolved": 2,
                         "external": 3}


def _uncertainty(conn: sqlite3.Connection, qname: str) -> list[dict]:
    """One-hop non-resolved edges around a changed symbol (target= or source=),
    capped at 20, so the AI reviewer sees resolution gaps instead of silent
    drops (guide §5.2, priority 1: edges directly touching the changed symbol).
    Computed for found and not-found symbols alike — a deleted-but-referenced
    symbol still surfaces its dangling references here."""
    rows = conn.execute(
        "SELECT source,target,resolution,rule_id,evidence_json FROM edges "
        "WHERE (target=? OR source=?) AND resolution != 'resolved' "
        "ORDER BY "
        "  CASE resolution WHEN 'dynamic' THEN 0 WHEN 'candidate' THEN 1 "
        "    WHEN 'unresolved' THEN 2 WHEN 'external' THEN 3 ELSE 4 END, source",
        (qname, qname),
    ).fetchall()
    items: list[dict] = []
    for r in rows:
        evidence: dict = {}
        if r["evidence_json"]:
            try:
                evidence = json.loads(r["evidence_json"])
            except ValueError:
                evidence = {}
        # dynamic edges store the raw call expression (e.g. "plugin.run");
        # other resolutions carry the raw target name/expression in `target`.
        expression = evidence.get("target_expr") or r["target"]
        items.append({
            "source": r["source"],
            "expression": expression,
            "resolution": r["resolution"],
            "candidates": evidence.get("candidates", []),
            "rule_id": r["rule_id"],
            "reason": _REASON_BY_RESOLUTION.get(r["resolution"],
                                                "uncertain edge"),
        })
    return items[:_UNCERTAINTY_LIMIT]


_COVERAGE_KEYS = ("resolved_edges", "semantic_edges", "candidate_edges",
                  "dynamic_edges", "unresolved_edges")


def _coverage(conn: sqlite3.Connection, qname: str,
              truncated: bool = False) -> dict:
    """Adjacent-edge counts per resolution (guide §5.1 `coverage`), so the
    reviewer can see how much of the neighborhood the determined graph covers
    versus what fell out. `truncated` reflects whether the uncertainty list
    above hit its cap."""
    counts: dict[str, int] = {}
    for r in conn.execute(
            "SELECT resolution, COUNT(*) AS cnt FROM edges "
            "WHERE target=? OR source=? GROUP BY resolution",
            (qname, qname)):
        counts[r["resolution"]] = r["cnt"]
    return {
        "resolved_edges": counts.get("resolved", 0),
        "semantic_edges": counts.get("semantic", 0),
        "candidate_edges": counts.get("candidate", 0),
        "dynamic_edges": counts.get("dynamic", 0),
        "unresolved_edges": counts.get("unresolved", 0),
        "truncated": truncated,
    }


def get_impact(conn: sqlite3.Connection, changed_symbols: list[str],
               max_nodes_per_direction: int = 20,
               tests: str = "exclude") -> list[dict]:
    """Impact analysis for changed symbols. `tests` selects which nodes the
    upstream/downstream/affected_entries contain: 'exclude' (default, business
    impact) drops test nodes, 'only' keeps only test nodes (for test-impact
    analysis), 'include' keeps everything.

    Every result carries `uncertainty` (one-hop non-resolved edges around the
    symbol, capped at 20) and `coverage` (adjacent-edge counts per resolution).
    Both are computed regardless of the `tests` filter so get_test_impact can
    read them in tests="only" mode."""
    if tests not in _TEST_FILTER:
        raise ValueError(f"tests must be one of {list(_TEST_FILTER)}, got {tests!r}")
    test_filter = _TEST_FILTER[tests]
    results: list[dict] = []
    for qname in changed_symbols:
        uncertainty = _uncertainty(conn, qname)
        coverage = _coverage(conn, qname,
                             truncated=len(uncertainty) >= _UNCERTAINTY_LIMIT)
        node = conn.execute("SELECT id FROM nodes WHERE qualified_name=?", (qname,)).fetchone()
        if node is None:
            results.append({"symbol": qname, "found": False, "upstream": [],
                            "downstream": [], "affected_entries": [],
                            "uncertainty": uncertainty, "coverage": coverage})
            continue
        nid = node["id"]
        flows = conn.execute(
            "SELECT flow_id FROM flow_memberships WHERE node_id=? ORDER BY flow_id", (nid,),
        ).fetchall()
        up_all, down_all, entries = [], [], set()
        if flows:
            direct_up, direct_down = _edges_fallback(conn, qname, max_nodes_per_direction, test_filter)
            for f in flows:
                up, down = _slice_flow(conn, f["flow_id"], nid, max_nodes_per_direction, test_filter)
                up_all.extend(up)
                down_all.extend(down)
                entry_sql = ("SELECT n.qualified_name FROM flows f"
                             " JOIN nodes n ON f.entry_point_id=n.id"
                             " WHERE f.id=?")
                entry_params: list = [f["flow_id"]]
                if test_filter is not None:
                    entry_sql += " AND n.is_test=?"
                    entry_params.append(test_filter)
                entry = conn.execute(entry_sql, entry_params).fetchone()
                if entry:
                    entries.add(entry["qualified_name"])
            up_all = direct_up + up_all
            down_all = direct_down + down_all
        else:
            up_all, down_all = _edges_fallback(conn, qname, max_nodes_per_direction, test_filter)
        # dedup by qname preserving order
        results.append({
            "symbol": qname, "found": True,
            "upstream": _dedup(up_all), "downstream": _dedup(down_all),
            "affected_entries": sorted(entries),
            "uncertainty": uncertainty, "coverage": coverage,
        })
    return results


def _dedup(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        if it["qname"] not in seen:
            seen.add(it["qname"])
            out.append(it)
    return out
