
import fnmatch
import hashlib
import json
from collections import defaultdict, deque
from dataclasses import dataclass, field

from code_review_ai import qname


@dataclass
class NodeRow:
    id: int
    qualified_name: str
    file_path: str
    kind: str
    decorators: list[str] = field(default_factory=list)


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


def _decorators(raw: str | None) -> list[str]:
    """Decode the decorators JSON column, tolerating NULL / empty / bad JSON."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def flow_input_hash(conn) -> str:
    """Stable hash of exactly what build_flows consumes: entry-candidate
    function/method nodes (qname, kind, file, decorators) plus resolved call
    edges. Same input -> same flows, so update_flows can skip a rebuild when the
    call graph didn't structurally change (e.g. a body-only edit that alters no
    edges). Decorators are included because entry_decorators drives entry
    selection — an annotation-only edit must invalidate the flows."""
    nodes = conn.execute(
        "SELECT qualified_name, kind, file_path, decorators FROM nodes "
        "WHERE kind IN ('function','method') ORDER BY qualified_name").fetchall()
    edges = conn.execute(
        "SELECT source, target FROM edges "
        "WHERE kind='call' AND resolution='resolved' ORDER BY source, target"
    ).fetchall()
    parts = [f"n:{row['qualified_name']}|{row['kind']}|{row['file_path']}"
             f"|{sorted(_decorators(row['decorators']))}"
             for row in nodes]
    parts += [f"e:{row['source']}->{row['target']}" for row in edges]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def build_flows(nodes: list[NodeRow], edges: list[EdgeRow],
                entry_names: list[str],
                entry_decorators: list[str] | None = None) -> list[FlowRecord]:
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
    has_incoming = {qname_to_id.get(e.target) for e in edges
                    if e.resolution == "resolved"}
    has_incoming.discard(None)

    for n in nodes:
        if n.kind not in ("function", "method"):
            continue
        short = qname.short(n.qualified_name)
        entry_id = qname_to_id.get(n.qualified_name)
        if entry_id is None:
            continue
        name_match = any(fnmatch.fnmatch(short, pat) for pat in entry_names)
        deco_match = any(
            fnmatch.fnmatch(dec, pat)
            for dec in n.decorators for pat in (entry_decorators or []))
        is_root = entry_id not in has_incoming
        if not name_match and not deco_match and not is_root:
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
