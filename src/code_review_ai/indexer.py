
import fnmatch
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


def _entry_points(parsed, cfg: Config) -> list[str]:
    """Return qnames of designated entry-point functions."""
    out: list[str] = []
    for pf in parsed:
        for n in pf.nodes:
            if n.kind not in ("function", "method"):
                continue
            short = n.qualified_name.rsplit(":", 1)[-1]
            if any(fnmatch.fnmatch(short, pat) for pat in cfg.entry_names):
                out.append(n.qualified_name)
    return out


def _decorator_matches(pf, cfg: Config) -> list[str]:
    # v1: entry_decorators matching requires decorator extraction in parser;
    # skipped here (names cover common cases). Implement when parser exposes decorators.
    return []


def rebuild(config: Config, conn: sqlite3.Connection) -> RebuildStats:
    repo = config.repo_path
    files = list_python_files(repo)
    parsed = [parse_file(os.path.join(repo, f), repo) for f in files]
    qnames = {n.qualified_name for pf in parsed for n in pf.nodes}
    edges = resolve_calls(parsed, qnames)
    entry_qnames = _entry_points(parsed, config)

    with transaction(conn):
        conn.execute("DELETE FROM flow_memberships")
        conn.execute("DELETE FROM flows")
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM nodes")
        # insert nodes (parent_id NULL first)
        qname_to_id: dict[str, int] = {}
        for pf in parsed:
            for n in pf.nodes:
                cur = conn.execute(
                    "INSERT INTO nodes(qualified_name,kind,language,file_path,"
                    "start_line,end_line,signature,parent_id) VALUES(?,?,?,?,?,?,?,NULL)",
                    (n.qualified_name, n.kind, "python", n.file_path,
                     n.start_line, n.end_line, n.signature),
                )
                qname_to_id[n.qualified_name] = cur.lastrowid
        # fill parent_id
        for pf in parsed:
            for n in pf.nodes:
                if n.parent_qname and n.parent_qname in qname_to_id:
                    conn.execute("UPDATE nodes SET parent_id=? WHERE id=?",
                                 (qname_to_id[n.parent_qname], qname_to_id[n.qualified_name]))
        # insert edges
        for e in edges:
            conn.execute(
                "INSERT INTO edges(source,target,kind,file_path,call_line,resolution)"
                " VALUES(?,?,?,?,?,?)",
                (e.source, e.target, e.kind, e.file_path, e.call_line, e.resolution),
            )
        # Phase B: load rows + build flows
        nodes = [NodeRow(r["id"], r["qualified_name"], r["file_path"])
                 for r in conn.execute("SELECT id,qualified_name,file_path FROM nodes")]
        erows = [EdgeRow(r["source"], r["target"], r["resolution"])
                 for r in conn.execute("SELECT source,target,resolution FROM edges")]
        entry_ids = [qname_to_id[q] for q in entry_qnames if q in qname_to_id]
        id_to_qname = {n.id: n.qualified_name for n in nodes}
        flows = build_flows(nodes, erows, entry_ids, config.max_depth)
        for f in flows:
            name = id_to_qname.get(f.entry_point_id, "").rsplit(":", 1)[-1]
            cur = conn.execute(
                "INSERT INTO flows(name,entry_point_id,depth,node_count,file_count,"
                "criticality,path_json) VALUES(?,?,?,?,?,?,?)",
                (name, f.entry_point_id, f.depth, f.node_count, f.file_count,
                 None, json.dumps(f.path)),
            )
            fid = cur.lastrowid
            for pos, nid in enumerate(f.path):
                conn.execute(
                    "INSERT INTO flow_memberships(flow_id,node_id,position) VALUES(?,?,?)",
                    (fid, nid, pos),
                )
        built_at = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")
        conn.execute("INSERT OR REPLACE INTO build_meta(key,value) VALUES('built_at',?)",
                     (built_at,))
        stats = RebuildStats(len(nodes), len(edges), len(flows), built_at)
    return stats


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
