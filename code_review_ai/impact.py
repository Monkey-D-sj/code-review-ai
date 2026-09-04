
import json

import sqlite3

from code_review_ai.change_context import (
    _MAX_ARG_CHARS,
    _MAX_ARGS,
    _MAX_SNIPPET_CHARS,
    _call_site_code,
    _clip,
)
from code_review_ai.search import _is_aliased_import

_SIGNATURE_LIMIT = 160
_UNCERTAINTY_LIMIT = 20


def _cap_signature(value: str | None) -> str:
    """Truncate long signatures so impact briefs stay compact."""
    text = value or ""
    return text if len(text) <= _SIGNATURE_LIMIT \
        else text[:_SIGNATURE_LIMIT] + "…"


def _node_brief(conn: sqlite3.Connection, node_id: int,
                include_signatures: bool = True) -> dict:
    r = conn.execute(
        "SELECT qualified_name,file_path,start_line,signature FROM nodes WHERE id=?",
        (node_id,),
    ).fetchone()
    if r is None:
        brief = {"qname": str(node_id), "file": "", "line": 0}
        if include_signatures:
            brief["sig"] = ""
        return brief
    brief = {"qname": r["qualified_name"], "file": r["file_path"],
             "line": r["start_line"]}
    if include_signatures:
        brief["sig"] = _cap_signature(r["signature"])
    return brief


def _resolved_call_adjacency(conn: sqlite3.Connection
                             ) -> tuple[dict[int, list[int]], dict[int, list[int]],
                                        dict[int, str]]:
    """Forward + reverse resolved-call adjacency in node-id space, loaded once
    per get_impact call so every symbol shares the map (E rows, sub-ms for the
    eval repos; a fixed O(E) cost, the same order as a single query_graph).
    Also returns qname_by_id so chain output can be ordered by (level, qname)."""
    id_by_qname: dict[str, int] = {}
    qname_by_id: dict[int, str] = {}
    for row in conn.execute("SELECT id, qualified_name FROM nodes"):
        id_by_qname[row["qualified_name"]] = row["id"]
        qname_by_id[row["id"]] = row["qualified_name"]
    forward: dict[int, list[int]] = {}
    reverse: dict[int, list[int]] = {}
    for row in conn.execute(
            "SELECT source, target FROM edges "
            "WHERE kind='call' AND resolution='resolved'"):
        source = id_by_qname.get(row["source"])
        target = id_by_qname.get(row["target"])
        if source is None or target is None:
            continue
        forward.setdefault(source, []).append(target)
        reverse.setdefault(target, []).append(source)
    return forward, reverse, qname_by_id


def _bfs_levels(adjacency: dict[int, list[int]], start: int
                ) -> tuple[dict[int, int], dict[int, int]]:
    """({node_id: level}, {node_id: parent}) reachable from start via
    adjacency, whole graph. level = BFS call-distance from start (1 = direct
    neighbors). parent = the node this one was reached FROM — the call edge
    that pulls it into the chain — used to attach a slim `via` marker to
    transitive hops. First-parent wins (deterministic BFS order), so a diamond
    merge keeps one stable propagation edge."""
    levels: dict[int, int] = {}
    parents: dict[int, int] = {}
    frontier = list(adjacency.get(start, ()))
    for child in frontier:
        parents[child] = start
    distance = 1
    while frontier:
        next_frontier: list[int] = []
        for node in frontier:
            if node in levels:
                continue
            levels[node] = distance
            for child in adjacency.get(node, ()):
                if child not in levels:
                    parents.setdefault(child, node)
                next_frontier.append(child)
        frontier = next_frontier
        distance += 1
    return levels, parents


def _true_chain_ids(conn: sqlite3.Connection, symbol_node_id: int,
                    test_filter: int | None,
                    adjacency: tuple[dict[int, list[int]], dict[int, list[int]],
                                     dict[int, str]],
                    direction: str, max_nodes_per_direction: int) -> list[int]:
    """Exact transitive callers ('up') / callees ('down') of symbol_node_id via
    a WHOLE-GRAPH resolved-call BFS — no flow-membership restriction.

    Equivalent to the union of the per-flow constrained BFS: every true
    caller/callee Y of the symbol lies in at least one flow that also contains
    the symbol (if Y is a root, Y's own flow contains the symbol; otherwise some
    entry E reaches Y, hence reaches the symbol, so Y ∈ flow(E)). So one
    whole-graph BFS reproduces the flow union exactly — one pass instead of one
    per flow, no flow_memberships lookup, and it also covers symbols on no flow
    (the old edges fallback). The BFS naturally stops at the symbol's blast
    radius; no member set needed.

    Output ids are ordered by (BFS level, qname): level 1 (direct
    callers/callees) first, ties by qname. Both keys are id-independent, so an
    incremental-synced index and a full rebuild (which renumbers node ids)
    produce identical output order (contract test). Capped globally per
    direction at max_nodes_per_direction.

    Returns (ids, levels, parents): ids as ordered above; levels is the
    {node_id: BFS depth} map (for the `level` field on every brief); parents is
    {node_id: the node it was reached from} — the propagation edge a transitive
    hop hangs off, so get_impact can attach a slim `via` marker instead of a
    full call_site.
    """
    forward, reverse, qname_by_id = adjacency
    graph = reverse if direction == "up" else forward
    levels, parents = _bfs_levels(graph, symbol_node_id)
    ids = sorted(levels, key=lambda nid: (levels[nid], qname_by_id.get(nid, "")))
    ids = [nid for nid in ids if nid != symbol_node_id]
    ids = ids[:max_nodes_per_direction]
    if test_filter is not None:
        ids = _filter_ids_by_test(conn, ids, test_filter)
    return ids, levels, parents


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


_COVERAGE_KEYS = ("resolved_edges", "candidate_edges",
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
        "WHERE kind='call' AND (target=? OR source=?) GROUP BY resolution",
        (qname, qname)):
        counts[r["resolution"]] = r["cnt"]
    return {
        "resolved_edges": counts.get("resolved", 0),
        "candidate_edges": counts.get("candidate", 0),
        "dynamic_edges": counts.get("dynamic", 0),
        "unresolved_edges": counts.get("unresolved", 0),
        "truncated": truncated,
    }


def _affected_entries(conn: sqlite3.Connection, symbol_node_id: int,
                      test_filter: int | None) -> set[str]:
    """Business entry points of the flows containing the symbol (guide §4
    contract: the flows a changed symbol sits in name the business entries its
    change reaches). Still flow-derived — a whole-graph BFS has no notion of
    entry point, so this is the one remaining flow read in get_impact."""
    flows = conn.execute(
        "SELECT flow_id FROM flow_memberships WHERE node_id=? ORDER BY flow_id",
        (symbol_node_id,),
    ).fetchall()
    entries: set[str] = set()
    for f in flows:
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
    return entries


def affected_entries(conn: sqlite3.Connection, qname: str,
                     tests: str = "exclude") -> list[str]:
    """Deterministic business entry points of the flows containing one symbol.

    Public, BFS-free counterpart to the per-symbol ``affected_entries`` inside
    get_impact: resolves the qname to a node and returns the sorted entry
    points of the flows it sits in, honoring the same ``tests`` filter. Absent
    symbols return an empty list. Consumers such as the review runner use this
    to fill a report's affected-entries deterministically instead of asking the
    model to author them.
    """
    if tests not in _TEST_FILTER:
        raise ValueError(f"tests must be one of {list(_TEST_FILTER)}, got {tests!r}")
    node = conn.execute(
        "SELECT id FROM nodes WHERE qualified_name=?", (qname,)).fetchone()
    if node is None:
        return []
    return sorted(_affected_entries(conn, node["id"], _TEST_FILTER[tests]))


def _direct_call_site(conn: sqlite3.Connection, caller_qname: str,
                      callee_qname: str, with_code: bool = True) -> dict | None:
    """First resolved call edge caller→callee as {line, args, code},
    mirroring get_change_context's call_site shape so a contract change
    (params/return/exception) is visible at the exact call point without
    opening the caller file. None when the edge is missing or the snippet is
    unreadable — the neighbor then simply omits call_site. `with_code=False`
    skips the code-snippet file read (used for transitive via markers, which
    need the propagation path, not the break-point context)."""
    row = conn.execute(
        "SELECT evidence_json, file_path FROM edges "
        "WHERE kind='call' AND resolution='resolved' AND source=? AND target=? "
        "LIMIT 1",
        (caller_qname, callee_qname),
    ).fetchone()
    if row is None:
        return None
    evidence: dict = {}
    if row["evidence_json"]:
        try:
            evidence = json.loads(row["evidence_json"])
        except ValueError:
            evidence = {}
    call_site: dict = {}
    line = evidence.get("call_line")
    if line:
        call_site["line"] = line
    args = evidence.get("args")
    if isinstance(args, list):
        call_site["args"] = [_clip(str(arg), _MAX_ARG_CHARS)
                             for arg in args[:_MAX_ARGS]]
    if line and with_code:
        snippet = _call_site_code(row["file_path"], line)
        if snippet:
            call_site["code"] = _clip(snippet, _MAX_SNIPPET_CHARS)
    return call_site or None


def _attach_chain_details(conn: sqlite3.Connection, briefs: list[dict],
                          node_ids: list[int], levels: dict[int, int],
                          parents: dict[int, int], qname_by_id: dict[int, str],
                          direct_ids: set[int], symbol_qname: str,
                          direction: str, include_call_sites: bool) -> None:
    """Attach edge detail to one upstream/downstream chain. Every node gets a
    `level` (BFS hop count, structural — always attached). Under
    include_call_sites, direct neighbors (level 1) get the full break-point
    `call_site` and transitive hops get a slim `via` marker (the propagation
    path, no code snippet). direction 'up'/'down' selects the call_site edge
    orientation."""
    for brief, node_id in zip(briefs, node_ids):
        brief["level"] = levels[node_id]
        if not include_call_sites:
            continue
        if node_id in direct_ids:
            if direction == "up":
                site = _direct_call_site(conn, brief["qname"], symbol_qname)
            else:
                site = _direct_call_site(conn, symbol_qname, brief["qname"])
            if site:
                brief["call_site"] = site
        else:
            _attach_via(conn, node_id, brief, parents, qname_by_id, direction)


def _attach_via(conn: sqlite3.Connection, reached_id: int, brief: dict,
                parents: dict[int, int], qname_by_id: dict[int, str],
                direction: str) -> None:
    """Slim propagation marker for a TRANSITIVE hop: {via, line, args} naming
    the parent function whose call pulls this node into the chain. `via` is the
    parent's full qname (the node this one was reached FROM in the BFS); line +
    args are the call expression on that connecting edge — the data flowing
    through the hop, without the code snippet. direction 'up' = this node calls
    the parent; 'down' = the parent calls it. The parent always sits one BFS
    level closer to the symbol, so the reviewer can cross-reference it in the
    same list. No-op when the edge is absent (deterministic first-parent wins
    for diamond merges)."""
    parent_id = parents.get(reached_id)
    parent_qname = qname_by_id.get(parent_id) if parent_id is not None else None
    if parent_qname is None:
        return
    if direction == "up":
        site = _direct_call_site(conn, brief["qname"], parent_qname,
                                 with_code=False)
    else:
        site = _direct_call_site(conn, parent_qname, brief["qname"],
                                 with_code=False)
    if site is None:
        return
    marker: dict = {"via": parent_qname}
    if "line" in site:
        marker["line"] = site["line"]
    if site.get("args"):
        marker["args"] = site["args"]
    brief["via"] = marker


def get_symbol_aliases(conn: sqlite3.Connection, qualified_name: str) -> list[dict]:
    """The alias names binding this symbol — ``from m import x as y`` ->
    local_name, each with the import-site file/line (same predicate as the FTS
    index, search._is_aliased_import). The reverse direction of the import map:
    given the symbol, which bindings rename it. Only genuine aliases surface —
    a plain ``from m import x`` binds the same name and is already visible
    through call sites / the defining node, so it would only add payload.
    Empty when the symbol has no aliases; surfaces names like
    ``decrypt_storage_password`` that live only as import bindings (no node of
    their own) so a reviewer sees the alias in the main channel instead of
    grepping for it."""
    return [{"name": r["local_name"], "file": r["file_path"], "line": r["line"]}
            for r in conn.execute(
                "SELECT local_name,file_path,line,module,imported_name,is_star "
                "FROM imports WHERE resolved_target=? ORDER BY file_path,line",
                (qualified_name,))
            if _is_aliased_import(r)]


def _attach_aliases(result: dict, conn: sqlite3.Connection,
                    qualified_name: str) -> None:
    """Add a compact ``aliases`` key to a get_impact result — only when the
    symbol has any (a few short strings, ~0.3% payload, non-empty only)."""
    aliases = get_symbol_aliases(conn, qualified_name)
    if aliases:
        result["aliases"] = aliases


def get_impact(conn: sqlite3.Connection, changed_symbols: list[str],
               max_nodes_per_direction: int = 20,
               tests: str = "exclude",
               include_signatures: bool = False,
               include_call_sites: bool = True,
               max_level: int = 0) -> list[dict]:
    """Impact analysis for changed symbols. `tests` selects which nodes the
    upstream/downstream/affected_entries contain: 'exclude' (default, business
    impact) drops test nodes, 'only' keeps only test nodes (for test-impact
    analysis), 'include' keeps everything.

    Upstream/downstream are the EXACT transitive callers/callees of the symbol
    via a WHOLE-GRAPH resolved-call BFS (`_true_chain_ids`), ordered by
    (BFS level, qname) and capped globally per direction at
    max_nodes_per_direction. The flow-constrained BFS it replaces was
    equivalent (a symbol's flows together cover every true caller/callee), so
    correctness is unchanged — a sibling branch that never calls the symbol is
    never reported. `include_signatures=True` adds the `sig` field; signatures
    are ~26% of payload, so the default (False) omits them for compact
    responses.
    `max_level` bounds how many BFS hops are returned: 0 (default) keeps the
    full transitive closure; 1 keeps only DIRECT neighbors and attaches a
    `depth` summary ({upstream_max, downstream_max, upstream_total,
    downstream_total}) so a reviewer sees how far impact propagates without
    paying for every transitive node — the propagation path is recoverable by
    querying each direct neighbor's own get_impact. Transitive-only consumers
    (get_test_impact) must keep max_level=0 to preserve reachability.
    `include_call_sites=True` attaches a `call_site` (line/args/code — the
    call's arguments plus the single calling-file line that invokes the
    symbol) to DIRECT upstream/downstream neighbors — the call points where a
    contract change (params/return/exception) actually breaks a caller. Transitive hops instead carry a slim `via` marker
    ({via, line, args} — the parent function the hop hangs off + the connecting
    call's line and arguments, no code snippet), so the propagation path is
    visible without the break-point context the direct sites carry. Every node
    also carries a `level` (BFS hop count; 1 = direct) so the two are
    distinguishable at a glance. Mirrors get_change_context's call_site shape
    so the two tools agree.

    Every result carries `uncertainty` (one-hop non-resolved edges around the
    symbol, capped at 20) and `coverage` (adjacent-edge counts per resolution).
    Both are computed regardless of the `tests` filter so get_test_impact can
    read them in tests="only" mode."""
    if tests not in _TEST_FILTER:
        raise ValueError(f"tests must be one of {list(_TEST_FILTER)}, got {tests!r}")
    test_filter = _TEST_FILTER[tests]
    adjacency = _resolved_call_adjacency(conn)
    forward, reverse, qname_by_id = adjacency
    results: list[dict] = []
    for qname in changed_symbols:
        uncertainty = _uncertainty(conn, qname)
        coverage = _coverage(conn, qname,
                             truncated=len(uncertainty) >= _UNCERTAINTY_LIMIT)
        node = conn.execute("SELECT id FROM nodes WHERE qualified_name=?", (qname,)).fetchone()
        if node is None:
            result = {"symbol": qname, "found": False, "upstream": [],
                      "downstream": [], "affected_entries": [],
                      "uncertainty": uncertainty, "coverage": coverage}
            _attach_aliases(result, conn, qname)
            results.append(result)
            continue
        nid = node["id"]
        up_ids, up_levels, up_parents = _true_chain_ids(
            conn, nid, test_filter, adjacency, "up", max_nodes_per_direction)
        down_ids, down_levels, down_parents = _true_chain_ids(
            conn, nid, test_filter, adjacency, "down", max_nodes_per_direction)
        direct_up = set(reverse.get(nid, ()))
        direct_down = set(forward.get(nid, ()))
        depth: dict[str, int] | None = None
        if max_level >= 1:
            # Report how far impact propagates before dropping transitive nodes,
            # so the reviewer knows a deeper chain exists to query per-neighbor.
            depth = {
                "upstream_max": max((up_levels.get(id_, 0) for id_ in up_ids),
                                    default=0),
                "downstream_max": max((down_levels.get(id_, 0)
                                       for id_ in down_ids), default=0),
                "upstream_total": len(up_ids),
                "downstream_total": len(down_ids),
            }
            up_ids = [id_ for id_ in up_ids
                      if up_levels.get(id_, 1) <= max_level]
            down_ids = [id_ for id_ in down_ids
                        if down_levels.get(id_, 1) <= max_level]
        up_all = [_node_brief(conn, nid_, include_signatures) for nid_ in up_ids]
        down_all = [_node_brief(conn, nid_, include_signatures) for nid_ in down_ids]
        _attach_chain_details(conn, up_all, up_ids, up_levels, up_parents,
                              qname_by_id, direct_up, qname, "up",
                              include_call_sites)
        _attach_chain_details(conn, down_all, down_ids, down_levels, down_parents,
                              qname_by_id, direct_down, qname, "down",
                              include_call_sites)
        entries = _affected_entries(conn, nid, test_filter)
        result = {
            "symbol": qname, "found": True,
            "upstream": _dedup(up_all), "downstream": _dedup(down_all),
            "affected_entries": sorted(entries),
            "uncertainty": uncertainty, "coverage": coverage,
        }
        if depth is not None:
            result["depth"] = depth
        _attach_aliases(result, conn, qname)
        results.append(result)
    return results


def _dedup(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        if it["qname"] not in seen:
            seen.add(it["qname"])
            out.append(it)
    return out
