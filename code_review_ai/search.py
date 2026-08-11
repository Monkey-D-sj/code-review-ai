"""Full-text search over indexed symbols via an FTS5 external-content table.

`fts_nodes` mirrors the `nodes` table (content='nodes', rowid = nodes.id). This
module owns the write-side helpers — index_fts (incremental insert), deindex_fts
(incremental delete), reindex_all (full 'rebuild' command) — and the query,
fts_search. Query semantics: wildcard queries (* / ?) keep the legacy short-name
glob; plain words run FTS token match with per-token prefix expansion + bm25
ranking; 0 hits fall back to a case-insensitive LIKE infix over the node's
searchable columns.
"""

import fnmatch
import json
import sqlite3

from code_review_ai import qname

_INDEX_COLUMNS = ("qualified_name", "file_path", "signature", "decorators", "end_line")
_SEARCH_KINDS = "('function','method','class')"


def _fts_insert_sql() -> str:
    columns = ",".join(_INDEX_COLUMNS)
    placeholders = ",".join("?" for _ in _INDEX_COLUMNS)
    return f"INSERT INTO fts_nodes(rowid,{columns}) VALUES(?,{placeholders})"


def _node_fts_row(node, qname_to_id: dict[str, int]) -> tuple:
    """One fts row per parsed node; values mirror what a full 'rebuild' would
    read from the nodes table so the index stays consistent either way."""
    return (qname_to_id[node.qualified_name], node.qualified_name,
            node.file_path, node.signature, json.dumps(node.decorators),
            node.end_line)


def index_fts(conn, parsed_nodes, qname_to_id: dict[str, int]) -> None:
    """Index nodes just written to `nodes` into fts_nodes (incremental path)."""
    rows = [_node_fts_row(node, qname_to_id) for node in parsed_nodes
            if node.qualified_name in qname_to_id]
    if rows:
        conn.executemany(_fts_insert_sql(), rows)


def deindex_fts(conn, node_ids: list[int]) -> None:
    """Remove fts rows for deleted node ids (fts rowid = nodes.id). No-op on
    an empty list."""
    if not node_ids:
        return
    placeholders = ",".join("?" for _ in node_ids)
    conn.execute(
        f"DELETE FROM fts_nodes WHERE rowid IN ({placeholders})", node_ids)


def reindex_all(conn) -> None:
    """Rebuild the FTS index from the nodes content table (external-content
    'rebuild' command). Called by the full-rebuild path after nodes are
    written."""
    conn.execute("INSERT INTO fts_nodes(fts_nodes) VALUES('rebuild')")


def fts_search(conn, query: str, limit: int = 50) -> list[dict]:
    """Search indexed symbols.

    Returns [{qname, kind, file, line, end_line, signature, score}] sorted by
    relevance (score = bm25 in FTS mode, None in glob mode). `query` containing
    `*`/`?` runs the legacy short-name glob; plain words run FTS with a LIKE
    infix fallback when nothing matches."""
    if any(ch in query for ch in "*?"):
        return _glob_search(conn, query, limit)
    match_expr = _match_expr(query)
    if match_expr is None:
        return []
    rows = _fts_match(conn, match_expr, limit)
    if rows:
        return rows
    return _like_fallback(conn, query, limit)


def _match_expr(query: str) -> str | None:
    """Build an FTS5 MATCH expression from a plain-word query: sanitize each
    whitespace token to [A-Za-z0-9_], prefix-expand, AND-join. None when no
    usable token survives (e.g. all-punctuation input)."""
    tokens = []
    for word in query.split():
        token = "".join(ch for ch in word if ch.isalnum() or ch == "_")
        if token and any(ch.isalnum() for ch in token):
            tokens.append(f"{token}*")
    return " AND ".join(tokens) if tokens else None


def _fts_match(conn, match_expr: str, limit: int) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT n.qualified_name AS qname, n.kind, n.file_path AS file, "
        "n.start_line AS line, n.end_line, n.signature, bm25(fts_nodes) AS score "
        "FROM fts_nodes JOIN nodes n ON n.id = fts_nodes.rowid "
        f"WHERE fts_nodes MATCH ? AND n.kind IN {_SEARCH_KINDS} "
        "ORDER BY bm25(fts_nodes) LIMIT ?", (match_expr, limit))]


def _like_fallback(conn, query: str, limit: int) -> list[dict]:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    return [dict(row) for row in conn.execute(
        "SELECT qualified_name AS qname, kind, file_path AS file, "
        "start_line AS line, end_line, signature, NULL AS score FROM nodes "
        f"WHERE kind IN {_SEARCH_KINDS} AND "
        "lower(qualified_name||' '||file_path||' '||signature||' '||"
        "COALESCE(decorators,'')||' '||end_line) LIKE ? ESCAPE '\\' "
        "LIMIT ?", (pattern.lower(), limit))]


def _glob_search(conn, query: str, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT qualified_name,kind,file_path,start_line,end_line,signature "
        f"FROM nodes WHERE kind IN {_SEARCH_KINDS}").fetchall()
    out: list[dict] = []
    for row in rows:
        if fnmatch.fnmatch(qname.short(row["qualified_name"]), query):
            out.append({"qname": row["qualified_name"], "kind": row["kind"],
                        "file": row["file_path"], "line": row["start_line"],
                        "end_line": row["end_line"], "signature": row["signature"],
                        "score": None})
            if len(out) >= limit:
                break
    return out
