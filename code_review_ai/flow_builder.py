
import fnmatch
from collections import defaultdict, deque
from dataclasses import dataclass

from code_review_ai import qname


@dataclass
class NodeRow:
    id: int
    qualified_name: str
    file_path: str
    kind: str


@dataclass
class EdgeRow:
    source: str
    target: str
    resolution: str


@dataclass
class FlowRecord:
    entry_point_id: int
    name: str
    depth: int
    node_count: int
    file_count: int
    path: list[int]


def build_flows(nodes: list[NodeRow], edges: list[EdgeRow],
                entry_names: list[str]) -> list[FlowRecord]:
    qname_to_id = {n.qualified_name: n.id for n in nodes}
    id_to_file = {n.id: n.file_path for n in nodes}
    adj: dict[int, list[int]] = defaultdict(list)  # adjacency list: target → [source]
    for e in edges:
        if e.resolution != "resolved":
            continue
        s = qname_to_id.get(e.source)
        t = qname_to_id.get(e.target)
        if s is not None and t is not None:
            adj[s].append(t)

    # Detect entry points: functions/methods whose short name matches entry_names
    entry_qnames: list[str] = []
    for n in nodes:
        if n.kind in ("function", "method"):
            short = qname.short(n.qualified_name)
            if any(fnmatch.fnmatch(short, pat) for pat in entry_names):
                entry_qnames.append(n.qualified_name)

    flows: list[FlowRecord] = []
    for qn in entry_qnames:
        entry_id = qname_to_id.get(qn)
        if entry_id is not None:
            flows.extend(_bfs_flows(entry_id, adj, id_to_file))
    return flows


def _bfs_flows(entry: int, adj: dict[int, list[int]],
               id_to_file: dict[int, str]) -> list[FlowRecord]:
    visited: set[int] = {entry}
    q: deque[tuple[int, list[int]]] = deque()
    q.append((entry, [entry]))
    flows: list[FlowRecord] = []
    while q:
        cur, path = q.popleft()
        children = [nxt for nxt in adj.get(cur, []) if nxt not in visited]
        if not children:
            files = {id_to_file.get(i, "") for i in path}
            flows.append(FlowRecord(
                entry_point_id=entry, name="", depth=len(path) - 1,
                node_count=len(path), file_count=len(files), path=path,
            ))
        for nxt in children:
            visited.add(nxt)
            q.append((nxt, path + [nxt]))
    return flows
