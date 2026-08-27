"""Full-text search over indexed symbols via an FTS5 external-content table.

`fts_nodes` mirrors the `nodes` table (content='nodes', rowid = nodes.id) and
`fts_imports` mirrors `imports` (content='imports', rowid = imports.id). This
module owns the write-side helpers — index_fts (incremental insert), deindex_fts
(incremental delete), reindex_all (full 'rebuild' command), and index_imports_fts
(import bindings) — and the query, fts_search. Query semantics: wildcard queries
(* / ?) keep the legacy short-name glob; plain words run FTS token match with
per-token prefix expansion + bm25 ranking; 0 hits fall back to a case-insensitive
LIKE infix over the node's searchable columns. fts_search merges node hits with
import-alias hits (see _fts_imports_match), so an alias like
`from app.x import decrypt_password as decrypt_storage_password` is findable by
its bound name.
"""

import fnmatch
import json

from code_review_ai import qname

_INDEX_COLUMNS = ("qualified_name", "file_path", "signature", "decorators", "end_line")
_IMPORT_FTS_COLUMNS = ("local_name", "module", "file_path")
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
    infix fallback when nothing matches. Node hits are merged with import-alias
    hits (alias hits appended after nodes, total capped at `limit`)."""
    if any(char in query for char in "*?"):
        return _glob_search(conn, query, limit)
    match_expr = _match_expr(query)
    if match_expr is None:
        return []
    rows = _fts_match(conn, match_expr, limit)
    if not rows:
        rows = _like_fallback(conn, query, limit)
    alias_rows = _fts_imports_match(conn, match_expr, limit)
    if not alias_rows:
        alias_rows = _like_imports_fallback(conn, query, limit)
    return (rows + alias_rows)[:limit]


def _match_expr(query: str) -> str | None:
    """Build an FTS5 MATCH expression from a plain-word query: sanitize each
    whitespace token to [A-Za-z0-9_], prefix-expand, AND-join. None when no
    usable token survives (e.g. all-punctuation input)."""
    tokens = []
    for word in query.split():
        token = "".join(char for char in word if char.isalnum() or char == "_")
        if token and any(char.isalnum() for char in token):
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


def _is_aliased_import(row) -> bool:
    """True when an import binding is a genuine alias worth indexing.

    ``from m import x as y`` (local != imported) or ``import m as y`` (module
    alias, local != the module's leaf name). Excludes plain ``from m import x``,
    plain ``import m``, star imports, and ESM default binds whose local name
    merely differs from the synthetic ``"default"`` — so a search for a common
    exported name does not surface every file that imported it."""
    if row["is_star"]:
        return False
    imported = row["imported_name"]
    if imported and imported != "default":
        return row["local_name"] != imported
    return row["local_name"] != row["module"].split(".")[-1]


def index_imports_fts(conn, import_rows) -> None:
    """Index import bindings just written to `imports` into fts_imports.

    Only genuine aliases are indexed (see _is_aliased_import); the plain
    non-aliased import rows stay searchable through nodes instead. Rows are
    dicts/sqlite Rows carrying id, local_name, module,
    imported_name, is_star, file_path."""
    columns = ",".join(_IMPORT_FTS_COLUMNS)
    placeholders = ",".join("?" for _ in _IMPORT_FTS_COLUMNS)
    rows = [(row["id"], row["local_name"], row["module"], row["file_path"])
            for row in import_rows if _is_aliased_import(row)]
    if rows:
        conn.executemany(
            f"INSERT INTO fts_imports(rowid,{columns}) VALUES(?,{placeholders})",
            rows)


def reindex_imports_fts(conn) -> None:
    """Rebuild the alias index from the imports content table.

    fts_imports is a SUBSET index (only aliased imports are indexed), so per-row
    maintenance is fragile: `DELETE FROM fts_imports WHERE rowid=?` on an
    external-content table throws "database disk image is malformed" whenever the
    rowid isn't in the index — the normal state, since most imports are plain.
    Clearing with 'delete-all' then re-indexing the aliases from content is
    O(aliases) and always consistent. Called after an incremental file delta
    replaces a file's import bindings."""
    conn.execute("INSERT INTO fts_imports(fts_imports) VALUES('delete-all')")
    rows = [dict(r) for r in conn.execute(
        "SELECT id, local_name, module, imported_name, is_star, file_path "
        "FROM imports")]
    index_imports_fts(conn, rows)


def _alias_hit(row) -> dict:
    """Shape an import-binding row into the fts_search result dict.

    qname = the resolved target, so a hit on the alias name lands on the real
    symbol (the node carrying the definition), while file/line point at the
    import site where the alias is bound — the reviewer Reads the binding, then
    the target."""
    return {"qname": row["resolved_target"] or row["module"],
            "kind": row["node_kind"],
            "file": row["file_path"], "line": row["line"], "end_line": None,
            "signature": f"imported as {row['local_name']} from {row['module']}",
            "score": row["score"]}


def _fts_imports_match(conn, match_expr: str, limit: int) -> list[dict]:
    return [_alias_hit(row) for row in conn.execute(
        "SELECT im.local_name, im.module, im.file_path, im.line, "
        "im.resolved_target, n.kind AS node_kind, bm25(fts_imports) AS score "
        "FROM fts_imports JOIN imports im ON im.id = fts_imports.rowid "
        "LEFT JOIN nodes n ON n.qualified_name = im.resolved_target "
        "WHERE fts_imports MATCH ? "
        "ORDER BY bm25(fts_imports) LIMIT ?", (match_expr, limit))]


def _like_imports_fallback(conn, query: str, limit: int) -> list[dict]:
    """LIKE infix fallback over import bindings (alias FTS found nothing).
    Fetches a bounded candidate superset then filters to aliases in Python —
    keeps the module-leaf comparison out of SQL."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    rows = conn.execute(
        "SELECT im.local_name, im.module, im.file_path, im.line, "
        "im.imported_name, im.is_star, im.resolved_target, "
        "n.kind AS node_kind, NULL AS score "
        "FROM imports im LEFT JOIN nodes n ON n.qualified_name = im.resolved_target "
        "WHERE lower(im.local_name||' '||im.module||' '||im.file_path) "
        "LIKE ? ESCAPE '\\' LIMIT ?", (pattern.lower(), limit * 5)).fetchall()
    return [_alias_hit(row) for row in rows if _is_aliased_import(row)][:limit]


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
