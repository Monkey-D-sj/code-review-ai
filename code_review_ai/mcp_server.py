import json
import threading
import urllib.request
import urllib.error

from code_review_ai.changes import build_change_summary, detect_changed_symbols
from code_review_ai.community import get_community as _get_community
from code_review_ai.community import list_communities as _list_communities
from code_review_ai.config import Config
from code_review_ai.db import connect, init_schema
from code_review_ai.graph import query_graph as _query_graph
from code_review_ai.impact import get_impact as _get_impact
from code_review_ai.testimpact import get_test_impact as _get_test_impact
from code_review_ai.deadcode import find_dead_code as _find_dead_code
from code_review_ai.search import fts_search
from code_review_ai.update import sync


def _conn(config: Config):
    conn = connect(config.db_path)
    init_schema(conn)
    return conn


def create_server(config: Config):
    from mcp.server import MCPServer
    mcp = MCPServer("code-review-ai")
    conn = _conn(config)
    lock = threading.Lock()

    @mcp.tool()
    def rebuild_index() -> str:
        """Refresh the code graph index from the working tree (incremental
        nodes/edges, then flows/communities; full rebuild on config/version
        change). Returns a JSON object with counts. Normally the watcher keeps
        nodes/edges current and git hooks keep flows current; call only when
        you need fresh data right now."""
        with lock:
            result = sync(config, conn)
        return json.dumps({"nodes": result["nodes"], "edges": result["edges"],
                           "flows": result["flows"],
                           "communities": result["communities"],
                           "full_rebuild": result["full_rebuild"]})

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
    def get_test_impact(symbols: list[str] | None = None,
                        files: list[str] | None = None) -> str:
        """Test impact analysis: for changed symbols, the tests that reach
        them (directly or transitively) -> "run only these tests". Pass
        explicit `symbols` (e.g. ["auth::login"]) or `files`; if both
        omitted, changed symbols are derived from the git diff (diff_base).
        Returns affected tests grouped by file with the changed symbols each
        covers. Prefer this over get_impact when the question is "which tests
        must I run", not "which business code breaks"."""
        changed = detect_changed_symbols(config, symbols=symbols, files=files)
        return json.dumps(_get_test_impact(conn, changed))

    @mcp.tool()
    def find_dead_code() -> str:
        """Dead-code / orphan detection: symbols with no static callers that
        are not entry points (entry_names glob / entry_decorators decorator),
        plus whole files nothing imports. Returns a JSON candidate list —
        symbols + files — with a note that these are static-analysis
        candidates, not deletion orders."""
        return json.dumps(_find_dead_code(conn, config))

    @mcp.tool()
    def get_change_summary(symbols: list[str] | None = None,
                           files: list[str] | None = None) -> str:
        """Change summary: from the git diff (diff_base) compute `summary`
        (diff stats incl. uncovered_changes + delete_change counts) +
        `changed_functions` (changed function/method/class detail) +
        `uncovered_changes` (files whose changes no function/class covers —
        module-level hunks, unsupported extensions, binary, and deleted files
        without a tombstone) + `delete_change` (deleted functions/modules with
        their one-hop upstream, from tombstones written at update time). Pass
        explicit `symbols` to resolve those qnames from the graph instead of
        the diff. Returns a JSON object."""
        return json.dumps(build_change_summary(config, conn,
                                               symbols=symbols, files=files))

    @mcp.tool()
    def query_graph(qualified_name: str, edge_kind: str = "call",
                    direction: str = "both") -> str:
        """图邻域查询：某符号通过指定边类型（call|contains|import|extends|
        implements|all，默认 call）的 resolved 边，in=用了它的节点，out=它用的
        节点。返回 JSON 对象。"""
        return json.dumps(_query_graph(conn, qualified_name,
                                       edge_kind=edge_kind, direction=direction))

    @mcp.tool()
    def search_symbol(query: str, limit: int = 50) -> str:
        """Discover symbols by full-text search or glob on their short name
        (e.g. "*login*", "UserService", "login"). Pure-word queries run FTS
        token match + bm25 ranking, falling back to substring on 0 hits.
        Returns a JSON list of {qname, kind, file, line, end_line, signature,
        score}. Use to find qualified names before get_symbol_detail /
        get_impact."""
        return json.dumps(fts_search(conn, query, limit=limit))

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
            "JOIN nodes n ON n.id=f.entry_point_id WHERE n.is_test=0"
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

    # attach conn/lock for main() to wire into startup + watcher
    mcp._conn = conn
    mcp._lock = lock
    return mcp


def main():
    import logging
    import threading
    from code_review_ai.config import load_config
    from code_review_ai.watcher import run_watcher, startup_sync
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    server = create_server(config)
    startup_sync(config, server._conn, server._lock)
    t = threading.Thread(target=run_watcher, args=(config, server._lock), daemon=True)
    t.start()
    server.run()


if __name__ == "__main__":
    main()
