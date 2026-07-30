"""Community detection over the resolved call graph (Leiden).

This module holds the *structure* of community detection - building an
undirected graph from resolved call edges, grouping the partition result back
into labelled communities, and the read-side queries the frontends use.

The Leiden algorithm itself is invoked through an injectable ``partitioner``
callable so this module has no hard dependency on ``leidenalg``/``igraph``.
The default partitioner lazy-imports them; tests inject a deterministic stub.
"""

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass

from code_review_ai import qname
from code_review_ai.flow_builder import NodeRow, EdgeRow


@dataclass
class CommunityRecord:
    label: str
    modularity: float
    members: list[int]  # node ids


def build_communities(nodes: list[NodeRow], edges: list[EdgeRow],
                      partitioner=None) -> list[CommunityRecord]:
    """Build communities from resolved edges. Main control only - delegates
    to adjacency build -> partition -> group. Returns [] when no resolved
    edges connect any nodes."""
    qname_to_id = {n.qualified_name: n.id for n in nodes}
    ids, adj = _build_undirected_adjacency(edges, qname_to_id)
    if not ids:
        return []
    if partitioner is None:
        partitioner = _default_partitioner
    node_to_comm, quality = partitioner(ids, adj)
    return _group_communities(node_to_comm, quality, nodes)


def _build_undirected_adjacency(edges, qname_to_id):
    """Symmetrize resolved edges into an undirected, call-count-weighted
    adjacency. Self-loops and non-resolved edges are dropped. Returns
    (sorted node ids, {node_id: {neighbour_id: weight}})."""
    weights: dict[tuple[int, int], int] = defaultdict(int)
    used: set[int] = set()
    for e in edges:
        if e.resolution != "resolved":
            continue
        s = qname_to_id.get(e.source)
        t = qname_to_id.get(e.target)
        if s is None or t is None or s == t:
            continue
        a, b = (s, t) if s < t else (t, s)
        weights[(a, b)] += 1
        used.add(s)
        used.add(t)
    ids = sorted(used)
    adj: dict[int, dict[int, float]] = {i: {} for i in ids}
    for (a, b), w in weights.items():
        adj[a][b] = w
        adj[b][a] = w
    return ids, adj


def _group_communities(node_to_comm: dict[int, int], quality: float,
                       nodes: list[NodeRow]) -> list[CommunityRecord]:
    """Group partition output (node_id -> community_id) into records, labelled
    by the most common short name in each community."""
    by_comm: dict[int, list[int]] = defaultdict(list)
    for nid, c in node_to_comm.items():
        by_comm[c].append(nid)
    id_to_qname = {n.id: n.qualified_name for n in nodes}
    out: list[CommunityRecord] = []
    for c in sorted(by_comm):
        members = by_comm[c]
        out.append(CommunityRecord(
            label=_pick_label(members, id_to_qname),
            modularity=quality,
            members=members,
        ))
    return out


def _pick_label(members: list[int], id_to_qname: dict[int, str]) -> str:
    shorts = [qname.short(id_to_qname[m]) for m in members if m in id_to_qname]
    if not shorts:
        return f"community_{members[0]}"
    return Counter(shorts).most_common(1)[0][0]


def _default_partitioner(ids: list[int],
                         adj: dict[int, dict[int, float]]):
    """Run Leiden with modularity on the symmetrized graph. Lazy-imports
    leidenalg/igraph so the dependency stays optional. Returns
    ({node_id: community_id}, modularity). Deterministic via seed=42."""
    import igraph as ig
    import leidenalg
    idx = {nid: i for i, nid in enumerate(ids)}
    edge_list, weights = [], []
    seen: set[tuple[int, int]] = set()
    for source in ids:
        for target, weight in adj.get(source, {}).items():
            key = (source, target) if source < target else (target, source)
            if key in seen:
                continue
            seen.add(key)
            edge_list.append((idx[source], idx[target]))
            weights.append(weight)
    g = ig.Graph(n=len(ids), edges=edge_list, directed=False)
    part = leidenalg.find_partition(
        g, leidenalg.ModularityVertexPartition,
        weights=weights or None, seed=42,
    )
    node_to_comm = {ids[i]: part.membership[i] for i in range(len(ids))}
    return node_to_comm, part.quality()


# --- read-side queries (frontends) ---

def _node_brief(conn: sqlite3.Connection, node_id: int) -> dict:
    r = conn.execute(
        "SELECT qualified_name,file_path,start_line,signature FROM nodes WHERE id=?",
        (node_id,),
    ).fetchone()
    if r is None:
        return {"qname": str(node_id), "file": "", "line": 0, "sig": ""}
    return {"qname": r["qualified_name"], "file": r["file_path"],
            "line": r["start_line"], "sig": r["signature"]}


def list_communities(conn: sqlite3.Connection) -> list[dict]:
    comms = conn.execute(
        "SELECT id,label,node_count,modularity FROM communities ORDER BY id"
    ).fetchall()
    out = []
    for c in comms:
        members = [r["qualified_name"] for r in conn.execute(
            "SELECT n.qualified_name FROM community_memberships cm"
            " JOIN nodes n ON n.id=cm.node_id"
            " WHERE cm.community_id=? ORDER BY n.qualified_name", (c["id"],)
        )]
        out.append({"id": c["id"], "label": c["label"],
                    "node_count": c["node_count"], "modularity": c["modularity"],
                    "members": members})
    return out


def get_community(conn: sqlite3.Connection, qualified_name: str) -> dict:
    node = conn.execute(
        "SELECT id FROM nodes WHERE qualified_name=?", (qualified_name,)
    ).fetchone()
    if node is None:
        return {"found": False, "symbol": qualified_name,
                "reason": "symbol not found"}
    row = conn.execute(
        "SELECT cm.community_id, c.label, c.modularity"
        " FROM community_memberships cm JOIN communities c ON c.id=cm.community_id"
        " WHERE cm.node_id=?", (node["id"],)
    ).fetchone()
    if row is None:
        return {"found": False, "symbol": qualified_name,
                "reason": "not in any community"}
    members = [_node_brief(conn, r["node_id"]) for r in conn.execute(
        "SELECT node_id FROM community_memberships WHERE community_id=?",
        (row["community_id"],)
    )]
    return {"found": True, "symbol": qualified_name,
            "community_id": row["community_id"], "label": row["label"],
            "modularity": row["modularity"], "members": members}
