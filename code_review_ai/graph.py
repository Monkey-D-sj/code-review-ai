import json
import sqlite3

VALID_KINDS = ("call", "contains", "import", "extends", "implements", "all")
VALID_DIRECTIONS = ("in", "out", "both")


_SIGNATURE_LIMIT = 160


def _cap_signature(value: str | None) -> str:
    """Truncate long signatures so graph briefs stay compact (signatures are
    the single largest field per neighbor)."""
    text = value or ""
    return text if len(text) <= _SIGNATURE_LIMIT \
        else text[:_SIGNATURE_LIMIT] + "…"


def _node_brief(conn: sqlite3.Connection, qualified_name: str) -> dict:
    row = conn.execute(
        "SELECT qualified_name,kind,file_path,start_line,signature,is_test "
        "FROM nodes WHERE qualified_name=?", (qualified_name,),
    ).fetchone()
    if row is None:
        return {"qname": qualified_name, "kind": None, "file": "",
                "line": 0, "signature": "", "is_test": False}
    return {"qname": row["qualified_name"], "kind": row["kind"],
            "file": row["file_path"], "line": row["start_line"],
            "signature": _cap_signature(row["signature"]),
            "is_test": bool(row["is_test"])}


def _attach_call_site(brief: dict, evidence_json: str | None) -> None:
    """Fold a resolved call edge's evidence into a neighbor brief.

    The call site (line + argument texts) is what lets a reviewer judge a
    contract change — e.g. a newly required argument — without reading the
    neighbor's file. Edges with no evidence (contains/import, or pre-v8
    indexes) are left untouched, so the payload shape stays backward
    compatible."""
    if not evidence_json:
        return
    try:
        evidence = json.loads(evidence_json)
    except (TypeError, ValueError):
        return
    if not isinstance(evidence, dict):
        return
    call_site: dict = {}
    if isinstance(evidence.get("call_line"), int):
        call_site["line"] = evidence["call_line"]
    if isinstance(evidence.get("args"), list):
        call_site["args"] = evidence["args"]
    if call_site:
        brief["call_site"] = call_site


def _neighbors(conn: sqlite3.Connection, select_column: str, where_column: str,
               match_qname: str, edge_kind: str, max_per_dir: int) -> list[dict]:
    """Resolved-edge neighbors of match_qname, each folded with the call-site
    evidence of the connecting edge (when present). select_column/where_column
    are fixed literals ("source"/"target"), never user input."""
    kind_clause = "" if edge_kind == "all" else " AND kind=?"
    params = [match_qname, "resolved"] + ([edge_kind] if edge_kind != "all" else [])
    rows = conn.execute(
        f"SELECT {select_column} AS qn, MAX(evidence_json) AS evidence "
        f"FROM edges WHERE {where_column}=? AND resolution=?"
        f"{kind_clause} GROUP BY qn ORDER BY qn", params)
    briefs = []
    for row in rows:
        brief = _node_brief(conn, row["qn"])
        if brief["kind"] is None:
            continue
        _attach_call_site(brief, row["evidence"])
        briefs.append(brief)
    return briefs[:max_per_dir]


def query_graph(conn: sqlite3.Connection, qualified_name: str,
                edge_kind: str = "call", direction: str = "both",
                max_per_dir: int = 20) -> dict:
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
              "is_test": brief["is_test"],
              "edge_kind": edge_kind, "direction": direction, "in": [], "out": []}
    if direction in ("in", "both"):
        result["in"] = _neighbors(conn, "source", "target", qualified_name,
                                  edge_kind, max_per_dir)
    if direction in ("out", "both"):
        result["out"] = _neighbors(conn, "target", "source", qualified_name,
                                   edge_kind, max_per_dir)
    return result
