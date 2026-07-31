"""Community detection over the resolved call graph (Leiden).

This module holds the *structure* of community detection - building an
undirected graph from resolved call edges, grouping the partition result back
into labelled communities, and the read-side queries the frontends use.

The Leiden algorithm itself is invoked through an injectable ``partitioner``
callable so this module has no hard dependency on ``leidenalg``/``igraph``.
The default partitioner lazy-imports them; tests inject a deterministic stub.
"""

import logging
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from code_review_ai import qname
from code_review_ai.flow_builder import NodeRow, EdgeRow


class WeightMode(StrEnum):
    """Edge weighting strategy for community detection.

    PLAIN         - each edge weighs its raw count (the historical behaviour).
    DEGREE_DAMPED - additionally down-weight edges incident to sink-like hub
                    nodes; see _damping_factors for the exact rule.
    """
    PLAIN = "plain"
    DEGREE_DAMPED = "degree_damped"

    @classmethod
    def parse(cls, value: str) -> "WeightMode":
        """Parse a config string into a WeightMode. Unknown values log a
        warning and fall back to PLAIN so a typo can't take down the whole
        community phase (consistent with Phase C's graceful degradation)."""
        try:
            return cls(value)
        except ValueError:
            logging.getLogger(__name__).warning(
                "unknown community_weight=%r; using %s", value, cls.PLAIN.value)
            return cls.PLAIN


@dataclass
class CommunityRecord:
    label: str
    modularity: float
    members: list[int]  # node ids


def build_communities(nodes: list[NodeRow], edges: list[EdgeRow],
                      partitioner=None,
                      weight_mode: WeightMode = WeightMode.PLAIN) -> list[CommunityRecord]:
    """Build communities from resolved edges. Main control only - delegates
    to adjacency build -> partition -> group. Returns [] when no resolved
    edges connect any nodes.

    weight_mode selects the edge weighting; see WeightMode. Defaults to PLAIN
    (raw count), the historical behaviour.
    """
    qname_to_id = {n.qualified_name: n.id for n in nodes}
    ids, adj = _build_undirected_adjacency(edges, qname_to_id, weight_mode)
    if not ids:
        return []
    if partitioner is None:
        partitioner = _default_partitioner
    node_to_comm, quality = partitioner(ids, adj)
    return _group_communities(node_to_comm, quality, nodes)


# Damping constants for DEGREE_DAMPED mode.
_DAMP_ALPHA = 0.5   # max fraction of weight removed from a perfect cross-module sink
_DAMP_FLOOR = 0.1   # never damp an edge below this fraction of its raw weight


def _build_undirected_adjacency(edges, qname_to_id,
                                weight_mode: WeightMode = WeightMode.PLAIN):
    """Symmetrize resolved edges into an undirected adjacency. Self-loops and
    non-resolved edges are dropped. Returns (sorted node ids, {node_id:
    {neighbour_id: weight}}).

    In PLAIN mode the weight is the raw edge count between a pair. In
    DEGREE_DAMPED mode each endpoint's factor (see _damping_factors) multiplies
    the raw count, so sink hubs spread across modules pull less on their
    neighbours.
    """
    weights: dict[tuple[int, int], int] = defaultdict(int)
    in_neighbors: dict[int, set[int]] = defaultdict(set)
    out_neighbors: dict[int, set[int]] = defaultdict(set)
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
        in_neighbors[t].add(s)
        out_neighbors[s].add(t)
        used.add(s)
        used.add(t)
    ids = sorted(used)
    id_to_qname = {nid: qn for qn, nid in qname_to_id.items() if nid in used}
    factors = _damping_factors(ids, in_neighbors, out_neighbors,
                               id_to_qname, weight_mode)
    adj: dict[int, dict[int, float]] = {i: {} for i in ids}
    for (a, b), w in weights.items():
        weight = w
        if factors is not None:
            weight = w * factors.get(a, 1.0) * factors.get(b, 1.0)
        adj[a][b] = weight
        adj[b][a] = weight
    return ids, adj


def _damping_factors(ids, in_neighbors, out_neighbors, id_to_qname, weight_mode):
    """Per-node multiplier in [_DAMP_FLOOR, 1.0] on incident edge weights.

    Returns None for PLAIN (no damping). For DEGREE_DAMPED:

        sinkness(n) = (in_deg - out_deg) / (in_deg + out_deg)   # in [-1,1]
        spread(n)   = 1 - own_in / in_deg                       # in [0,1]
        factor(n)   = 1 - ALPHA * max(sinkness, 0) * spread

    where own_in = in-neighbours that share the node's own module and in_deg =
    total in-neighbours. spread is the fraction of a node's *dependents* that
    live OUTSIDE its home module: 0.0 = all dependents co-located with the node
    (a local core, keep full weight); 1.0 = every dependent is in another module
    (a cross-cutting hub, damp). Only sinks (sinkness > 0 - high in, low out,
    e.g. a base class many inherit from, or a util module many import) are
    damped. spread uses in-edges only (it tracks where dependents sit); sinkness
    still uses in+out. No global module count - each node is scored on its own
    neighbours.
    """
    if weight_mode is not WeightMode.DEGREE_DAMPED:
        return None
    factors: dict[int, float] = {}
    for nid in ids:
        in_nbrs = in_neighbors.get(nid, ())
        out_nbrs = out_neighbors.get(nid, ())
        in_deg = len(in_nbrs)
        out_deg = len(out_nbrs)
        total = in_deg + out_deg
        if total == 0:
            factors[nid] = 1.0
            continue
        sinkness = (in_deg - out_deg) / total
        if sinkness <= 0:
            factors[nid] = 1.0
            continue
        spread = _cross_module_in_fraction(nid, in_nbrs, id_to_qname)
        factors[nid] = max(1.0 - _DAMP_ALPHA * sinkness * spread, _DAMP_FLOOR)
    return factors


def _cross_module_in_fraction(nid, in_nbrs, id_to_qname) -> float:
    """Fraction of the node's in-neighbours that live OUTSIDE its own module:
    0.0 = every dependent is co-located with the node (local core), 1.0 = every
    dependent is in another module (cross-cutting hub). in-edges only - this
    measures where the node's dependents sit, not what it calls. in_nbrs is
    non-empty whenever this is called (sinks have in_deg > 0)."""
    own_module = qname.module(id_to_qname[nid])
    in_deg = len(in_nbrs)
    if in_deg == 0:
        return 0.0
    own_in = sum(1 for src in in_nbrs
                 if qname.module(id_to_qname[src]) == own_module)
    return 1 - own_in / in_deg


def _group_communities(node_to_comm: dict[int, int], quality: float,
                       nodes: list[NodeRow]) -> list[CommunityRecord]:
    """Group partition output (node_id -> community_id) into records, labelled
    by the longest common prefix of member qualified names."""
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
    # Strip common prefix shared by most communities (ignore outliers).
    labels = [r.label for r in out]
    # Use the first label as reference; find its last dot-bounded segment that
    # appears as a prefix in >80% of all labels.
    best = ""
    ref = labels[0] if labels else ""
    dots = [i for i, ch in enumerate(ref) if ch == "."]
    for i in dots:
        prefix = ref[:i + 1]
        if sum(1 for lb in labels if lb.startswith(prefix)) > len(labels) * 0.8:
            best = prefix
    if best:
        for r in out:
            if r.label.startswith(best):
                r.label = r.label[len(best):]
    return out


def _pick_label(members: list[int], id_to_qname: dict[int, str]) -> str:
    qnames = [id_to_qname[m] for m in members if m in id_to_qname]
    if not qnames:
        return f"community_{members[0]}"
    prefix = os.path.commonprefix(qnames)
    # Strip trailing partial segment: keep last complete segment boundary
    # e.g. "src.components.CURD.in" → "src.components.CURD"
    prefix = prefix.rstrip(".:")
    sep = max(prefix.rfind("."), prefix.rfind("::"))
    if sep > 0:
        prefix = prefix[:sep]
    # If prefix ends with "index", strip it (Vue convention)
    if prefix.endswith(".index") or prefix.endswith("::index"):
        prefix = prefix[:prefix.rfind("index") - len(".")]
    return prefix if prefix else qnames[0].split("::")[0]


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
