from code_review_ai import qname

import json
import os
import sqlite3
import datetime
import time
from dataclasses import dataclass

from code_review_ai.config import Config
from code_review_ai.db import transaction
from code_review_ai.flow_builder import NodeRow, EdgeRow, FlowRecord, build_flows
from code_review_ai.parser import list_python_files, parse_file
from code_review_ai.resolver import resolve_calls


@dataclass
class RebuildStats:
    node_count: int
    edge_count: int
    flow_count: int
    built_at: str


def rebuild(config: Config, conn: sqlite3.Connection) -> RebuildStats:
    """Parse the tree, resolve calls, persist everything in one atomic
    transaction. Orchestration only — writing is delegated to the _write_*."""
    repo = config.repo_path
    files = list_python_files(repo)
    parsed = [parse_file(os.path.join(repo, f), repo) for f in files]
    qnames = {n.qualified_name for pf in parsed for n in pf.nodes}
    edges = resolve_calls(parsed, qnames)

    with transaction(conn):
        _clear_tables(conn)
        qname_to_id = _write_nodes(conn, parsed)
        _write_edges(conn, edges)
        flow_count = _write_flows(conn, parsed, edges, qname_to_id, config)
        built_at = _stamp_built_at(conn)
    return RebuildStats(len(qname_to_id), len(edges), flow_count, built_at)


def _clear_tables(conn: sqlite3.Connection) -> None:
    """Delete every table, child-first for FK safety on nodes.parent_id."""
    conn.execute("DELETE FROM flow_memberships")
    conn.execute("DELETE FROM flows")
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM nodes")


def _write_nodes(conn, parsed) -> dict[str, int]:
    """Insert all nodes with parent_id NULL, then backfill parent_id in one
    batch. Returns the qname -> id map (ids are db-assigned)."""
    conn.executemany(
        "INSERT INTO nodes(qualified_name,kind,language,file_path,"
        "start_line,end_line,signature,parent_id) VALUES(?,?,?,?,?,?,?,NULL)",
        [(n.qualified_name, n.kind, "python", n.file_path,
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
    files = list_python_files(config.repo_path)
    for f in files:
        try:
            if os.path.getmtime(os.path.join(config.repo_path, f)) > built:
                return True
        except OSError:
            return True
    return False
