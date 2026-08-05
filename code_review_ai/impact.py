
import sqlite3


def _node_brief(conn: sqlite3.Connection, node_id: int) -> dict:
    r = conn.execute(
        "SELECT qualified_name,file_path,start_line,signature FROM nodes WHERE id=?",
        (node_id,),
    ).fetchone()
    if r is None:
        return {"qname": str(node_id), "file": "", "line": 0, "sig": ""}
    return {"qname": r["qualified_name"], "file": r["file_path"],
            "line": r["start_line"], "sig": r["signature"]}


def _slice_flow(conn: sqlite3.Connection, flow_id: int, symbol_node_id: int,
                max_per_dir: int) -> tuple[list[dict], list[dict]]:
    rows = conn.execute(
        "SELECT node_id, position FROM flow_memberships WHERE flow_id=? ORDER BY position",
        (flow_id,),
    ).fetchall()
    sym_pos = next((r["position"] for r in rows if r["node_id"] == symbol_node_id), None)
    if sym_pos is None:
        return [], []
    up = [_node_brief(conn, r["node_id"]) for r in rows if r["position"] < sym_pos]
    down = [_node_brief(conn, r["node_id"]) for r in rows if r["position"] > sym_pos]
    return up[-max_per_dir:], down[:max_per_dir]


def _edges_fallback(conn: sqlite3.Connection, qname: str, max_per_dir: int):
    callers = [_edge_brief(conn, e["source"]) for e in conn.execute(
        "SELECT DISTINCT source FROM edges WHERE target=? AND resolution='resolved'", (qname,))][:max_per_dir]
    callees = [_edge_brief(conn, e["target"]) for e in conn.execute(
        "SELECT DISTINCT target FROM edges WHERE source=? AND resolution='resolved'", (qname,))][:max_per_dir]
    return callers, callees


def _edge_brief(conn: sqlite3.Connection, qname: str) -> dict:
    r = conn.execute("SELECT file_path,start_line,signature FROM nodes WHERE qualified_name=?",
                     (qname,)).fetchone()
    if r is None:
        return {"qname": qname, "file": "", "line": 0, "sig": ""}
    return {"qname": qname, "file": r["file_path"], "line": r["start_line"], "sig": r["signature"]}


def get_impact(conn: sqlite3.Connection, changed_symbols: list[str],
               max_nodes_per_direction: int = 50) -> list[dict]:
    results: list[dict] = []
    for qname in changed_symbols:
        node = conn.execute("SELECT id FROM nodes WHERE qualified_name=?", (qname,)).fetchone()
        if node is None:
            results.append({"symbol": qname, "found": False, "upstream": [],
                            "downstream": [], "affected_entries": []})
            continue
        nid = node["id"]
        flows = conn.execute(
            "SELECT flow_id FROM flow_memberships WHERE node_id=?", (nid,),
        ).fetchall()
        up_all, down_all, entries = [], [], set()
        if flows:
            direct_up, direct_down = _edges_fallback(conn, qname, max_nodes_per_direction)
            for f in flows:
                up, down = _slice_flow(conn, f["flow_id"], nid, max_nodes_per_direction)
                up_all.extend(up)
                down_all.extend(down)
                entry = conn.execute(
                    "SELECT n.qualified_name FROM flows f"
                    " JOIN nodes n ON f.entry_point_id=n.id"
                    " WHERE f.id=?", (f["flow_id"],)).fetchone()
                if entry:
                    entries.add(entry["qualified_name"])
            up_all = direct_up + up_all
            down_all = direct_down + down_all
        else:
            up_all, down_all = _edges_fallback(conn, qname, max_nodes_per_direction)
        # dedup by qname preserving order
        results.append({
            "symbol": qname, "found": True,
            "upstream": _dedup(up_all), "downstream": _dedup(down_all),
            "affected_entries": sorted(entries),
        })
    return results


def _dedup(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        if it["qname"] not in seen:
            seen.add(it["qname"])
            out.append(it)
    return out
