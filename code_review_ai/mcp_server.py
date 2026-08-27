import json
import os
import threading
import urllib.request
import urllib.error
from pathlib import Path

import toon_format

from code_review_ai.changes import build_change_summary, detect_changed_symbols
from code_review_ai.change_context import build_change_context
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

# Cap discovery results so a broad glob cannot flood a single tool result
# with thousands of node briefs (a real fresh-token cost in agentic use).
_SEARCH_SYMBOL_LIMIT = 30


def _relativize_path(path: str, repo_root: str) -> str:
    """Rewrite an absolute repo file path to a repo-relative one, so MCP
    results stay compact. The agent's cwd is the repo root, so a relative path
    reads identically while costing a fraction of the absolute form (which can
    embed a long worktree/Windows prefix repeated per node brief). Paths that
    resolve outside the repo (e.g. other repos, or already-relative values)
    pass through unchanged."""
    try:
        resolved = Path(path).resolve()
        root = Path(repo_root).resolve()
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return path


def _relativize(value: object, repo_root: str) -> object:
    """Recursively relativize the ``file``/``file_path`` keys of a tool result.

    Every file value in a result is re-sent (as cache-read) on each later tool
    call, so shrinking it shrinks total run cost multiplicatively, not just the
    call that produced it."""
    if isinstance(value, dict):
        return {
            key: (_relativize_path(item, repo_root)
                  if key in {"file", "file_path"} and isinstance(item, str)
                  else _relativize(item, repo_root))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_relativize(item, repo_root) for item in value]
    return value


def _conn(config: Config):
    conn = connect(config.db_path)
    init_schema(conn)
    return conn


def create_server(config: Config):
    from mcp.server import MCPServer
    mcp = MCPServer("code-review-ai")
    conn = _conn(config)
    lock = threading.Lock()

    def _emit(value: object, toon: bool = False) -> str:
        """Serialize a tool result with repo-absolute paths relativized.
        ``toon=True`` returns the TOON text encoding instead of JSON."""
        payload = _relativize(value, config.repo_path)
        if toon:
            return toon_format.encode(payload)
        return json.dumps(payload)

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
                   files: list[str] | None = None,
                   max_nodes_per_direction: int = 20,
                   include_signatures: bool = False,
                   include_call_sites: bool = True,
                   max_level: int = 1,
                   toon: bool = True) -> str:
        """Impact for changed symbols: affected business entries plus upstream
        callers / downstream callees. Query by `symbols` (e.g.
        ["auth::login"]), `files`, or omit both to derive from git diff.
        `max_level` bounds BFS hops: 1 (default) = DIRECT neighbors + a depth
        summary ({upstream_max, downstream_max, upstream_total,
        downstream_total}); 0 = full transitive closure. Direct neighbors
        carry a `call_site` code snippet by default — the calling code is
        already there, so you usually don't need to Read caller files. Pass
        `include_call_sites=false` for topology-only queries (responses are
        token-heavy). Each result carries `uncertainty` (resolution gaps) and
        `coverage`. Query each changed symbol once; to walk deeper, query a
        specific direct-neighbor qname — never re-request a symbol or file you
        already have results for. Responses are TOON text by default."""
        changed = detect_changed_symbols(config, symbols=symbols, files=files)
        return _emit(_get_impact(
            conn, changed,
            max_nodes_per_direction=max_nodes_per_direction,
            include_signatures=include_signatures,
            include_call_sites=include_call_sites,
            max_level=max_level), toon=toon)

    @mcp.tool()
    def get_test_impact(symbols: list[str] | None = None,
                        files: list[str] | None = None) -> str:
        """Test impact analysis: for changed symbols, the tests that reach
        them (directly or transitively) -> "run only these tests". Pass
        explicit `symbols` (e.g. ["auth::login"]) or `files`; if both
        omitted, changed symbols are derived from the git diff (diff_base).
        Returns affected tests grouped by file with the changed symbols each
        covers, plus `complete` / `fallback_recommended` / `fallback_reasons`
        — when a symbol is not in the index or no test reaches it but the call
        graph has dynamic/candidate edges around it, the result is marked
        incomplete so the reviewer falls back to running the full suite.
        Prefer this over get_impact when the question is "which tests must I
        run", not "which business code breaks"."""
        changed = detect_changed_symbols(config, symbols=symbols, files=files)
        return _emit(_get_test_impact(conn, changed))

    @mcp.tool()
    def find_dead_code() -> str:
        """Dead-code / orphan detection: symbols with no static callers that
        are not entry points (entry_names glob / entry_decorators decorator),
        plus whole files nothing imports. Returns a JSON candidate list —
        symbols + files — with a note that these are static-analysis
        candidates, not deletion orders."""
        return _emit(_find_dead_code(conn, config))

    @mcp.tool()
    def get_change_summary(symbols: list[str] | None = None,
                           files: list[str] | None = None,
                           toon: bool = True) -> str:
        """Change summary: from the git diff (diff_base) compute `summary`
        (diff stats incl. uncovered_changes + delete_change counts) +
        `changed_functions` (changed function/method/class detail) +
        `uncovered_changes` (files whose changes no function/class covers —
        module-level hunks, unsupported extensions, binary, and deleted files
        without a tombstone) + `delete_change` (deleted functions/modules with
        their one-hop upstream, from tombstones written at update time). Pass
        explicit `symbols` to resolve those qnames from the graph instead of
        the diff. Returns TOON text by default."""
        return _emit(build_change_summary(config, conn,
                                          symbols=symbols, files=files),
                     toon=toon)

    @mcp.tool()
    def get_change_context(symbols: list[str] | None = None,
                           files: list[str] | None = None,
                           direction: str = "in", max_symbols: int = 4,
                           max_neighbors: int = 5,
                           include_signatures: bool = False,
                           include_tests: bool = False) -> str:
        """Compact graph expansion for changes the reviewer has already judged
        non-local. Pass exact qnames in `symbols`, or changed paths in `files`
        and the server resolves their changed qnames internally; omit both to
        use the whole git diff. Returns resolved call neighbors only: upstream
        callers by default, optional downstream callees with direction=out or
        both. Selection covers distinct changed files before adding more
        symbols from one file, and favors symbols with production callers.
        Results omit signatures and test-only callers by default, cap
        symbols/neighbors, and enforce an 8 KB response budget. Do not call for
        self-contained changes and do not call search_symbol first."""
        return _emit(build_change_context(
            config, conn, symbols=symbols, files=files,
            direction=direction, max_symbols=max_symbols,
            max_neighbors=max_neighbors,
            include_signatures=include_signatures,
            include_tests=include_tests,
        ))

    @mcp.tool()
    def query_graph(qualified_name: str, edge_kind: str = "call",
                    direction: str = "both", max_neighbors: int = 20) -> str:
        """Graph neighborhood query: a symbol's resolved edges of a given
        kind (call|contains|import|extends|implements|all, default call);
        in=users of it, out=what it uses. max_neighbors caps the results
        per direction (default 20) to bound context size. Returns a JSON
        object."""
        return _emit(_query_graph(conn, qualified_name,
                                  edge_kind=edge_kind, direction=direction,
                                  max_per_dir=max_neighbors))

    @mcp.tool()
    def search_symbol(query: str, limit: int = 50) -> str:
        """Discover symbols by full-text search or glob on their short name
        (e.g. "*login*", "UserService", "login"). Pure-word queries run FTS
        token match + bm25 ranking, falling back to substring on 0 hits.
        Returns a JSON list of {qname, kind, file, line, end_line, signature,
        score}. Use to find qualified names before get_impact."""
        return _emit(fts_search(
            conn, query, limit=min(limit, _SEARCH_SYMBOL_LIMIT)))

    @mcp.tool()
    def get_communities() -> str:
        """List all detected communities (Leiden over structural edges) with
        their members. Returns a JSON list of {id, label, node_count,
        modularity, members: [qnames]}. Horizontal view of which modules
        cluster together."""
        return _emit(_list_communities(conn))

    @mcp.tool()
    def get_community(qualified_name: str) -> str:
        """One symbol's community: label, modularity, and co-members (its
        structural blast radius). Complements get_impact (vertical
        caller/callee chains) with the horizontal cluster view. Returns a JSON
        object with "found": false and a reason if the symbol is missing or on
        no structural edge."""
        return _emit(_get_community(conn, qualified_name))

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

    # Eval ablation: restrict which tools the server registers so a headless
    # model genuinely cannot see the others. --allowedTools only gates native
    # tools; MCP tools exposed by the server stay visible/callable. Empty env
    # (normal use) registers everything, unchanged.
    only_tools = os.environ.get("CRAI_MCP_ONLY_TOOLS", "").strip()
    if only_tools:
        wanted = {name.strip() for name in only_tools.split(",") if name.strip()}
        for name in list(mcp._tool_manager._tools):
            if name not in wanted:
                mcp.remove_tool(name)

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
    skip_sync = os.environ.get("CRAI_SKIP_STARTUP_SYNC", "").lower() in {
        "1", "true", "yes"}
    disable_watcher = os.environ.get("CRAI_DISABLE_WATCHER", "").lower() in {
        "1", "true", "yes"}
    if not skip_sync:
        startup_sync(config, server._conn, server._lock)
    if not disable_watcher:
        t = threading.Thread(target=run_watcher,
                             args=(config, server._lock), daemon=True)
        t.start()
    server.run()


if __name__ == "__main__":
    main()
