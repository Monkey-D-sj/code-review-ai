import sqlite3

VALID_KINDS = ("call", "contains", "import", "extends", "implements", "all")
VALID_DIRECTIONS = ("in", "out", "both")


def _node_brief(conn: sqlite3.Connection, qualified_name: str) -> dict:
    row = conn.execute(
        "SELECT qualified_name,kind,file_path,start_line,signature "
        "FROM nodes WHERE qualified_name=?", (qualified_name,),
    ).fetchone()
    if row is None:
        return {"qname": qualified_name, "kind": None, "file": "",
                "line": 0, "signature": ""}
    return {"qname": row["qualified_name"], "kind": row["kind"],
            "file": row["file_path"], "line": row["start_line"],
            "signature": row["signature"]}


def _dedup(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for item in items:
        if item["qname"] not in seen:
            seen.add(item["qname"])
            out.append(item)
    return out


def _neighbors(conn: sqlite3.Connection, select_column: str, where_column: str,
               match_qname: str, edge_kind: str, max_per_dir: int) -> list[dict]:
    """Resolved-edge neighbors of match_qname. select_column/where_column are
    fixed literals ("source"/"target"), never user input."""
    kind_clause = "" if edge_kind == "all" else " AND kind=?"
    params = [match_qname, "resolved"] + ([edge_kind] if edge_kind != "all" else [])
    rows = conn.execute(
        f"SELECT DISTINCT {select_column} FROM edges WHERE {where_column}=? "
        f"AND resolution=?{kind_clause}", params)
    briefs = (_node_brief(conn, row[select_column]) for row in rows)
    return _dedup([brief for brief in briefs if brief["kind"] is not None])[:max_per_dir]


def query_graph(conn: sqlite3.Connection, qualified_name: str,
                edge_kind: str = "call", direction: str = "both",
                max_per_dir: int = 50) -> dict:
    """Neighbors of one symbol via resolved edges: `in` = nodes pointing to it,
    `out` = nodes it points to. Raises ValueError on invalid edge_kind/direction."""
    if edge_kind not in VALID_KINDS:
        raise ValueError(
            f"invalid edge_kind {edge_kind!r}; expected one of {', '.join(VALID_KINDS)}")
    if direction not in VALID_DIRECTIONS:
        raise ValueError(
            f"invalid direction {direction!r}; expected one of {', '.join(VALID_DIRECTIONS)}")
    brief = _node_brief(conn, qualified_name)
    if brief["kind"] is None:
        return {"qname": qualified_name, "found": False, "in": [], "out": []}
    result = {"qname": qualified_name, "kind": brief["kind"],
              "file": brief["file"], "line": brief["line"],
              "signature": brief["signature"],
              "edge_kind": edge_kind, "direction": direction, "in": [], "out": []}
    if direction in ("in", "both"):
        result["in"] = _neighbors(conn, "source", "target", qualified_name,
                                  edge_kind, max_per_dir)
    if direction in ("out", "both"):
        result["out"] = _neighbors(conn, "target", "source", qualified_name,
                                   edge_kind, max_per_dir)
    return result
