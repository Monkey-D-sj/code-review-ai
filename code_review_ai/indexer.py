from code_review_ai import qname

import json
import os
import sqlite3
import datetime
import time
from dataclasses import dataclass

from code_review_ai.community import build_communities
from code_review_ai.config import Config
from code_review_ai.db import transaction
from code_review_ai.flow_builder import NodeRow, EdgeRow, FlowRecord, build_flows
from code_review_ai.parser import ParsedFile, SOURCE_GLOBS, filter_excluded, list_source_files, parse_file
from code_review_ai.resolver import resolve_edges


def _ms(seconds: float) -> float:
    """Convert perf_counter delta to milliseconds, rounded to 1 decimal."""
    return round(seconds * 1000, 1)


@dataclass
class _CacheEntry:
    mtime: float
    size: int
    parsed: ParsedFile


class ParseCache:
    """mtime+size keyed cache of parsed files, to skip re-parsing unchanged
    files on rebuild.

    Thread-safety: access must be serialized by the caller (the rebuild lock
    in mcp_server/watcher). No internal locking by design - rebuild is the
    only mutator and it runs under that lock.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _CacheEntry] = {}

    def get(self, path: str, mtime: float, size: int) -> ParsedFile | None:
        e = self._entries.get(path)
        if e is not None and e.mtime == mtime and e.size == size:
            return e.parsed
        return None

    def put(self, path: str, mtime: float, size: int, parsed: ParsedFile) -> None:
        self._entries[path] = _CacheEntry(mtime, size, parsed)

    def prune(self, current_paths: set[str]) -> None:
        """Drop entries for files no longer in the repo (deleted/renamed)."""
        for p in [p for p in self._entries if p not in current_paths]:
            del self._entries[p]


@dataclass
class RebuildStats:
    node_count: int
    edge_count: int
    flow_count: int
    community_count: int
    built_at: str
    stage_timings: dict[str, float]  # stage name → elapsed ms


def _parse_files(files: list[str], repo: str,
                 cache: ParseCache | None) -> list[ParsedFile]:
    """Parse every file. With a cache, reuse the ParsedFile when (mtime, size)
    is unchanged; otherwise parse and store. Without a cache, parse all."""
    if cache is None:
        return [parse_file(os.path.join(repo, f), repo) for f in files]
    parsed: list[ParsedFile] = []
    current: set[str] = set()
    for f in files:
        path = os.path.join(repo, f)
        current.add(path)
        st = os.stat(path)
        hit = cache.get(path, st.st_mtime, st.st_size)
        if hit is not None:
            parsed.append(hit)
        else:
            pf = parse_file(path, repo)
            cache.put(path, st.st_mtime, st.st_size, pf)
            parsed.append(pf)
    cache.prune(current)
    return parsed


def rebuild(config: Config, conn: sqlite3.Connection,
            cache: ParseCache | None = None) -> RebuildStats:
    """Parse the tree, resolve calls, persist everything in one atomic
    transaction. Orchestration only — writing is delegated to the _write_*."""
    t_start = time.perf_counter()
    repo = config.repo_path
    files = filter_excluded(
        list_source_files(repo, SOURCE_GLOBS),
        config.exclude,
    )
    t_files = time.perf_counter()

    parsed = _parse_files(files, repo, cache)
    t_parse = time.perf_counter()

    qnames = {n.qualified_name for pf in parsed for n in pf.nodes}
    all_edges = resolve_edges(parsed, qnames)
    t_resolve = time.perf_counter()

    with transaction(conn):
        _clear_tables(conn)
        qname_to_id = _write_nodes(conn, parsed)
        _write_edges(conn, all_edges)
        call_edges = [e for e in all_edges if e.kind == "call"]
        flow_count = _write_flows(conn, parsed, call_edges, qname_to_id, config)
        t_comm_start = time.perf_counter()
        community_count = _write_communities(conn, parsed, all_edges, qname_to_id, config)
        t_communities = _ms(time.perf_counter() - t_comm_start)
        built_at = _stamp_built_at(conn)
    t_db = time.perf_counter()

    timings = {
        "list_files": _ms(t_files - t_start),
        "parse": _ms(t_parse - t_files),
        "resolve": _ms(t_resolve - t_parse),
        "write_db": _ms(t_db - t_resolve),
        "communities": t_communities,
        "total": _ms(t_db - t_start),
    }
    return RebuildStats(len(qname_to_id), len(all_edges), flow_count,
                        community_count, built_at, timings)


def _clear_tables(conn: sqlite3.Connection) -> None:
    """Delete every table, child-first for FK safety on nodes.parent_id."""
    conn.execute("DELETE FROM flow_memberships")
    conn.execute("DELETE FROM flows")
    conn.execute("DELETE FROM community_memberships")
    conn.execute("DELETE FROM communities")
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM nodes")


def _write_nodes(conn, parsed) -> dict[str, int]:
    """Insert all nodes with parent_id NULL, then backfill parent_id in one
    batch. Returns the qname -> id map (ids are db-assigned)."""
    conn.executemany(
        "INSERT INTO nodes(qualified_name,kind,language,file_path,"
        "start_line,end_line,signature,parent_id) VALUES(?,?,?,?,?,?,?,NULL)",
        [(n.qualified_name, n.kind, n.language, n.file_path,
          n.start_line, n.end_line, n.signature)
         for pf in parsed for n in pf.nodes],
    )
    qname_to_id = {r["qualified_name"]: r["id"]
                   for r in conn.execute("SELECT id, qualified_name FROM nodes")}
    parent_updates = [
        (qname_to_id[n.parent_qname], qname_to_id[n.qualified_name])
        for pf in parsed for n in pf.nodes
        if n.parent_qname and n.parent_qname in qname_to_id
    ]
    if parent_updates:
        conn.executemany("UPDATE nodes SET parent_id=? WHERE id=?", parent_updates)
    return qname_to_id


def _write_edges(conn, edges) -> None:
    conn.executemany(
        "INSERT INTO edges(source,target,kind,file_path,call_line,resolution)"
        " VALUES(?,?,?,?,?,?)",
        [(e.source, e.target, e.kind, e.file_path, e.call_line, e.resolution)
         for e in edges],
    )


def _write_flows(conn, parsed, edges, qname_to_id: dict[str, int],
                 config: Config) -> int:
    """Build flows from in-memory nodes/edges (no db round-trip to reload them),
    persist flows row-by-row (each needs its lastrowid) then memberships in one
    batch. Returns the flow count."""
    nodes = [NodeRow(qname_to_id[n.qualified_name], n.qualified_name,
                     n.file_path, n.kind)
             for pf in parsed for n in pf.nodes]
    erows = [EdgeRow(e.source, e.target, e.resolution) for e in edges]
    id_to_qname = {n.id: n.qualified_name for n in nodes}
    flows = build_flows(nodes, erows, config.entry_names)

    membership_rows: list[tuple[int, int, int]] = []
    for f in flows:
        name = qname.short(id_to_qname.get(f.entry_point_id, ""))
        cur = conn.execute(
            "INSERT INTO flows(name,entry_point_id,depth,node_count,file_count,"
            "criticality,path_json) VALUES(?,?,?,?,?,?,?)",
            (name, f.entry_point_id, f.depth, f.node_count, f.file_count,
             None, json.dumps(f.path)),
        )
        fid = cur.lastrowid
        membership_rows.extend((fid, nid, pos) for pos, nid in enumerate(f.path))
    if membership_rows:
        conn.executemany(
            "INSERT INTO flow_memberships(flow_id,node_id,position) VALUES(?,?,?)",
            membership_rows,
        )
    return len(flows)


def _write_communities(conn, parsed, edges, qname_to_id: dict[str, int],
                       config: Config) -> int:
    """Phase C: detect communities over structural edges (contains, import,
    inherits) — not call edges. Opt-in via config.community_detection."""
    if not config.community_detection:
        return 0
    nodes = [NodeRow(qname_to_id[n.qualified_name], n.qualified_name,
                     n.file_path, n.kind)
             for pf in parsed for n in pf.nodes]
    # Use structural (non-call) edges only
    erows = [
        EdgeRow(e.source, e.target, "resolved")
        for e in edges
        if e.kind != "call" and e.resolution == "resolved"
    ]
    try:
        communities = build_communities(nodes, erows)
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "leidenalg/igraph not installed; skipping community detection "
            "(install with: uv sync --extra community)")
        return 0
    membership_rows: list[tuple[int, int]] = []
    for c in communities:
        cur = conn.execute(
            "INSERT INTO communities(label,node_count,modularity) VALUES(?,?,?)",
            (c.label, len(c.members), c.modularity))
        cid = cur.lastrowid
        membership_rows.extend((cid, nid) for nid in c.members)
    if membership_rows:
        conn.executemany(
            "INSERT INTO community_memberships(community_id,node_id) VALUES(?,?)",
            membership_rows)
    return len(communities)


def _stamp_built_at(conn: sqlite3.Connection) -> str:
    built_at = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")
    conn.execute("INSERT OR REPLACE INTO build_meta(key,value) VALUES('built_at',?)",
                 (built_at,))
    return built_at


def is_stale(config: Config, conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM build_meta WHERE key='built_at'").fetchone()
    if row is None:
        return True
    dt = datetime.datetime.strptime(row["value"], "%Y-%m-%dT%H:%M:%S.%f")
    built = time.mktime(dt.timetuple())
    files = filter_excluded(
        list_source_files(config.repo_path, SOURCE_GLOBS),
        config.exclude,
    )
    for f in files:
        try:
            if os.path.getmtime(os.path.join(config.repo_path, f)) > built:
                return True
        except OSError:
            return True
    return False
