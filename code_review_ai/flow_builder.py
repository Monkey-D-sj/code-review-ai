
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

    flows: list[FlowRecord] = []
    for n in nodes:
        if n.kind not in ("function", "method"):
            continue
        short = qname.short(n.qualified_name)
        if not any(fnmatch.fnmatch(short, pat) for pat in entry_names):
            continue
        entry_id = qname_to_id.get(n.qualified_name)
        if entry_id is None:
            continue

        # BFS from entry, collect all reachable nodes into one flat path
        visited: set[int] = {entry_id}
        q: deque[int] = deque([entry_id])
        path: list[int] = []
        while q:
            cur = q.popleft()
            path.append(cur)
            for nxt in adj.get(cur, []):
                if nxt not in visited:
                    visited.add(nxt)
                    q.append(nxt)

        files = {id_to_file.get(i, "") for i in path}
        flows.append(FlowRecord(
            entry_point_id=entry_id, name="", depth=0,
            node_count=len(path), file_count=len(files), path=path,
        ))
    return flows
