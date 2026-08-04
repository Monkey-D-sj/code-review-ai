from code_review_ai import qname

import fnmatch
import json
import threading
import urllib.request
import urllib.error

from code_review_ai.changes import build_change_summary, detect_changed_symbols
from code_review_ai.community import get_community as _get_community
from code_review_ai.community import list_communities as _list_communities
from code_review_ai.config import Config
from code_review_ai.db import connect, init_schema
from code_review_ai.impact import get_impact as _get_impact
from code_review_ai.indexer import ParseCache, rebuild


def _conn(config: Config):
    conn = connect(config.db_path)
    init_schema(conn)
    return conn


def create_server(config: Config):
    from mcp.server import MCPServer
    mcp = MCPServer("code-review-ai")
    conn = _conn(config)
    cache = ParseCache()
    lock = threading.Lock()

    @mcp.tool()
    def rebuild_index() -> str:
        """Rebuild the code graph index from the working tree (parse, resolve,
        flows, communities). Returns a JSON object with node/edge/flow counts
        and per-stage timings. Normally the watcher keeps the index current;
        call only when you need fresh data right now."""
        with lock:  # serialize against the watcher's rebuilds
            stats = rebuild(config, conn, cache)
        return json.dumps({"nodes": stats.node_count, "edges": stats.edge_count,
                           "flows": stats.flow_count, "built_at": stats.built_at,
                           "timings_ms": stats.stage_timings})

    @mcp.tool()
    def get_impact(symbols: list[str] | None = None,
                   files: list[str] | None = None) -> str:
        """Impact analysis for changed symbols: the affected business entry
        points plus upstream callers / downstream callees per flow. Pass
        explicit `symbols` (e.g. ["auth::login"]) or `files`; if both omitted,
        changed symbols are derived from git diff (diff_base). Prefer this over
        grepping when assessing what a code change breaks."""
        changed = detect_changed_symbols(config, symbols=symbols, files=files)
        return json.dumps(_get_impact(conn, changed))

    @mcp.tool()
    def get_change_summary(symbols: list[str] | None = None,
                           files: list[str] | None = None) -> str:
        """Change summary: from the git diff (diff_base) compute `summary`
        (diff stats) + `changed_functions` (changed function/method/class
        detail). Pass explicit `symbols` to resolve those qnames from the
        graph instead of the diff. Returns a JSON object."""
        return json.dumps(build_change_summary(config, conn,
                                               symbols=symbols, files=files))

    @mcp.tool()
    def search_symbol(query: str) -> str:
        """Discover symbols by glob on their short name (e.g. "*login*",
        "UserService"). Returns a JSON list of {qname, kind, file, line}. Use
        to find qualified names before get_symbol_detail / get_impact."""
        rows = conn.execute(
            "SELECT qualified_name,kind,file_path,start_line FROM nodes WHERE kind IN ('function','method','class')"
        ).fetchall()
        out = [{"qname": r["qualified_name"], "kind": r["kind"],
                "file": r["file_path"], "line": r["start_line"]}
               for r in rows if fnmatch.fnmatch(qname.short(r["qualified_name"]), query)]
        return json.dumps(out)

    @mcp.tool()
    def get_symbol_detail(qualified_name: str) -> str:
        """Detail for one fully-qualified symbol, e.g. "auth::UserService.login":
        kind, file, line, signature, in/out degree, and direct resolved
        callers/callees as qnames. Returns a JSON object, or
        {"error": "symbol not found"}."""
        r = conn.execute("SELECT * FROM nodes WHERE qualified_name=?", (qualified_name,)).fetchone()
        if r is None:
            return json.dumps({"error": "symbol not found"})
        callers = [row["source"] for row in conn.execute(
            "SELECT DISTINCT source FROM edges WHERE target=? AND resolution='resolved'", (qualified_name,))]
        callees = [row["target"] for row in conn.execute(
            "SELECT DISTINCT target FROM edges WHERE source=? AND resolution='resolved'", (qualified_name,))]
        return json.dumps({"qname": r["qualified_name"], "kind": r["kind"],
                           "file": r["file_path"], "line": r["start_line"],
                           "signature": r["signature"],
                           "in_degree": r["in_degree"], "out_degree": r["out_degree"],
                           "callers": callers, "callees": callees})

    @mcp.tool()
    def list_entry_points() -> str:
        """List the designated business entry points (matched by entry_names)
        that have reachable flows. Returns a JSON list of {qname, name, file}.
        Useful to see the top-level business flows the index has built."""
        rows = conn.execute(
            "SELECT DISTINCT f.name, n.qualified_name, n.file_path FROM flows f "
            "JOIN nodes n ON n.id=f.entry_point_id"
        ).fetchall()
        return json.dumps([{"qname": r["qualified_name"], "name": r["name"],
                            "file": r["file_path"]} for r in rows])

    @mcp.tool()
    def get_communities() -> str:
        """List all detected communities (Leiden over structural edges) with
        their members. Returns a JSON list of {id, label, node_count,
        modularity, members: [qnames]}. Horizontal view of which modules
        cluster together."""
        return json.dumps(_list_communities(conn))

    @mcp.tool()
    def get_community(qualified_name: str) -> str:
        """One symbol's community: label, modularity, and co-members (its
        structural blast radius). Complements get_impact (vertical
        caller/callee chains) with the horizontal cluster view. Returns a JSON
        object with "found": false and a reason if the symbol is missing or on
        no structural edge."""
        return json.dumps(_get_community(conn, qualified_name))

    @mcp.tool()
    def call_external_service(body: str) -> str:
        """POST a JSON string to the external review feedback service
        (config.external_service_url). Returns the raw response text, or JSON
        {"error": ...} on HTTP/network failure. Used by the code-review skill
        to submit review reports."""
        data = body.encode("utf-8")
        req = urllib.request.Request(
            config.external_service_url, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return json.dumps({"error": f"HTTP {e.code}", "body": e.read().decode("utf-8", errors="replace")})
        except urllib.error.URLError as e:
            return json.dumps({"error": str(e.reason)})

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
