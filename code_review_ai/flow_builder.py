
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass
class NodeRow:
    id: int
    qualified_name: str
    file_path: str


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
                entry_qnames: list[str]) -> list[FlowRecord]:
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

    flows: list[FlowRecord] = []
    for qn in entry_qnames:
        entry_id = qname_to_id.get(qn)
        if entry_id is not None:
            flows.extend(_bfs_flows(entry_id, adj, id_to_file))
    return flows


def _bfs_flows(entry: int, adj: dict[int, list[int]],
               id_to_file: dict[int, str]) -> list[FlowRecord]:
    parent: dict[int, int | None] = {entry: None}
    depth: dict[int, int] = {entry: 0}
    q = deque([entry])
    flows: list[FlowRecord] = []
    while q:
        cur = q.popleft()
        path = _reconstruct(cur, parent)
        files = {id_to_file.get(i, "") for i in path}
        flows.append(FlowRecord(
            entry_point_id=entry, name="", depth=depth[cur],
            node_count=len(path), file_count=len(files), path=path,
        ))
        for nxt in adj.get(cur, []):
            if nxt not in parent:
                parent[nxt] = cur
                depth[nxt] = depth[cur] + 1
                q.append(nxt)
    return flows


def _reconstruct(node: int, parent: dict[int, int | None]) -> list[int]:
    path: list[int] = []
    cur: int | None = node
    while cur is not None:
        path.append(cur)
        cur = parent[cur]
    path.reverse()
    return path
