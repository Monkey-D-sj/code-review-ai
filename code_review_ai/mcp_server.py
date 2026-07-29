from code_review_ai import qname

import fnmatch
import json
import threading

from code_review_ai.changes import detect_changed_symbols
from code_review_ai.config import Config
from code_review_ai.db import connect, init_schema
from code_review_ai.impact import get_impact as _get_impact
from code_review_ai.indexer import ParseCache, rebuild


def _conn(config: Config):
    conn = connect(config.db_path)
    init_schema(conn)
    return conn


def create_server(config: Config):
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("code-review-ai")
    conn = _conn(config)
    cache = ParseCache()
    lock = threading.Lock()

    @mcp.tool()
    def rebuild_index(force: bool = False) -> str:
        """Rebuild the index from the working tree."""
        with lock:  # serialize against the watcher's rebuilds
            stats = rebuild(config, conn, cache)
        return json.dumps({"nodes": stats.node_count, "edges": stats.edge_count,
                           "flows": stats.flow_count, "built_at": stats.built_at,
                           "timings_ms": stats.stage_timings})

    @mcp.tool()
    def get_impact(symbols: list[str] | None = None,
                   files: list[str] | None = None) -> str:
        """Return impact chains for changed symbols. If neither symbols nor files
        given, derives changed symbols from git diff."""
        changed = detect_changed_symbols(config, symbols=symbols, files=files)
        return json.dumps(_get_impact(conn, changed))

    @mcp.tool()
    def search_symbol(query: str) -> str:
        """Find symbols by name glob."""
        rows = conn.execute(
            "SELECT qualified_name,kind,file_path,start_line FROM nodes WHERE kind IN ('function','method','class')"
        ).fetchall()
        out = [{"qname": r["qualified_name"], "kind": r["kind"],
                "file": r["file_path"], "line": r["start_line"]}
               for r in rows if fnmatch.fnmatch(qname.short(r["qualified_name"]), query)]
        return json.dumps(out)

    @mcp.tool()
    def get_symbol_detail(qualified_name: str) -> str:
        """Node detail + direct callees/callers."""
        r = conn.execute("SELECT * FROM nodes WHERE qualified_name=?", (qualified_name,)).fetchone()
        if r is None:
            return json.dumps({"error": "symbol not found"})
        callers = [row["source"] for row in conn.execute(
            "SELECT DISTINCT source FROM edges WHERE target=? AND resolution='resolved'", (qualified_name,))]
        callees = [row["target"] for row in conn.execute(
            "SELECT DISTINCT target FROM edges WHERE source=? AND resolution='resolved'", (qualified_name,))]
        return json.dumps({"qname": r["qualified_name"], "kind": r["kind"],
                           "file": r["file_path"], "line": r["start_line"],
                           "signature": r["signature"], "callers": callers, "callees": callees})

    @mcp.tool()
    def list_entry_points() -> str:
        """List designated entry points."""
        rows = conn.execute(
            "SELECT DISTINCT f.name, n.qualified_name, n.file_path FROM flows f "
            "JOIN nodes n ON n.id=f.entry_point_id"
        ).fetchall()
        return json.dumps([{"qname": r["qualified_name"], "name": r["name"],
                            "file": r["file_path"]} for r in rows])

    # attach conn/cache/lock for main() to wire into startup + watcher
    mcp._conn = conn
    mcp._cache = cache
    mcp._lock = lock
    return mcp


def main():
    import logging
    import threading
    from code_review_ai.config import load_config
    from code_review_ai.watcher import run_watcher, startup_rebuild
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    server = create_server(config)
    startup_rebuild(config, server._conn, server._cache, server._lock)
    t = threading.Thread(target=run_watcher,
                         args=(config, server._cache, server._lock), daemon=True)
    t.start()
    server.run()


if __name__ == "__main__":
    main()
