"""Incremental index updates.

Watcher keeps nodes/edges fresh (update_nodes_edges); git hooks and startup
recompute flows/communities from the DB (update_flows/update_communities).
The DB is the source of truth — no in-memory parse cache."""

import json
import os

from code_review_ai import qname
from code_review_ai import manifest
from code_review_ai.changes import current_head
from code_review_ai.community import WeightMode, build_communities, inter_community_edges
from code_review_ai.db import transaction
from code_review_ai.flow_builder import EdgeRow, NodeRow, build_flows
from code_review_ai.indexer import recompute_degrees, _stamp_built_at
from code_review_ai.parser import (SOURCE_GLOBS, filter_excluded,
                                   list_source_files, parse_file)
from code_review_ai.resolver import resolve_edges


def changed_files(config, conn) -> tuple[set[str], set[str], set[str]]:
    """(changed, added, deleted) repo-relative paths vs the `files` manifest."""
    repo = config.repo_path
    current = set(filter_excluded(
        list_source_files(repo, SOURCE_GLOBS), config.exclude))
    manifest_entries = manifest.read(conn)
    changed: set[str] = set()
    added: set[str] = set()
    deleted: set[str] = set()
    for rel in current:
        abs_path = os.path.join(repo, rel)
        try:
            st = os.stat(abs_path)
        except OSError:
            deleted.add(rel)  # listed by git but gone from disk
            continue
        entry = manifest_entries.get(rel)
        if entry is None:
            added.add(rel)
            continue
        mtime, size, file_hash = entry
        if abs(st.st_mtime - mtime) < 1e-6 and st.st_size == size:
            continue  # fast path: unchanged
        if manifest.hash_file(abs_path) == file_hash:
            continue  # touch-only; content identical
        changed.add(rel)
    # files no longer listed by git (e.g. committed removal)
    deleted |= set(manifest_entries) - current
    return changed, added, deleted


def needs_nodes_update(config, conn) -> bool:
    changed, added, deleted = changed_files(config, conn)
    return bool(changed or added or deleted)


def repair_resolutions(conn) -> int:
    """Re-evaluate non-dynamic edge labels against the current node qname set.

    Matches what a full rebuild would resolve: for unchanged files, target is
    derived from stable imports, so only existence changes. Call edges whose
    target has no '::' are raw/unresolvable in a full rebuild too — skipped."""
    qnames = {r["qualified_name"]
              for r in conn.execute("SELECT qualified_name FROM nodes")}
    rows = conn.execute(
        "SELECT id,kind,target,resolution FROM edges").fetchall()
    updates: list[tuple[str, int]] = []
    for row in rows:
        resolution = row["resolution"]
        if resolution == "dynamic":
            continue
        target = row["target"]
        if row["kind"] == "call" and "::" not in target:
            continue
        new_label = "resolved" if target in qnames else "unresolved"
        if new_label != resolution:
            updates.append((new_label, row["id"]))
    if updates:
        conn.executemany("UPDATE edges SET resolution=? WHERE id=?", updates)
    return len(updates)


def update_nodes_edges(config, conn, changed_paths: list[str] | None = None) -> dict:
    """Incremental nodes/edges/degrees update. With changed_paths (watcher
    events, repo-relative) re-parse exactly those; without, scan the manifest
    for changes. Always ends with the resolution repair pass."""
    repo = config.repo_path
    if changed_paths is not None:
        changed, added, deleted = _classify_hint(config, conn, changed_paths)
    else:
        changed, added, deleted = changed_files(config, conn)
    if not (changed or added or deleted):
        repair_resolutions(conn)
        return {"nodes": 0, "edges": 0, "parsed_files": 0,
                "changed": [], "deleted": []}
    parse_paths = sorted(added | changed)
    parsed = [parse_file(os.path.join(repo, rel), repo) for rel in parse_paths]
    with transaction(conn):
        nodes, edges = _apply_nodes_edges_delta(
            conn, repo, parsed, changed | added, deleted)
        repair_resolutions(conn)
        _sync_manifest(conn, repo, parse_paths, deleted)
        _stamp_built_at(conn)
    return {"nodes": nodes, "edges": edges, "parsed_files": len(parse_paths),
            "changed": sorted(changed | added), "deleted": sorted(deleted)}


def _classify_hint(config, conn, changed_paths: list[str]):
    """Split watcher event paths into (changed, added, deleted) by disk+manifest."""
    repo = config.repo_path
    manifest_entries = manifest.read(conn)
    present: set[str] = set()
    deleted: set[str] = set()
    for rel in changed_paths:
        if os.path.isfile(os.path.join(repo, rel)):
            present.add(rel)
        else:
            deleted.add(rel)
    added = present - set(manifest_entries)
    changed = present & set(manifest_entries)
    return changed, added, deleted


def _apply_nodes_edges_delta(conn, repo, parsed, changed_set: set[str],
                             deleted_set: set[str]) -> tuple[int, int]:
    touch = [os.path.join(repo, rel) for rel in changed_set | deleted_set]
    removed_ids: list[int] = []
    for abs_path in touch:
        removed_ids += [r["id"] for r in conn.execute(
            "SELECT id FROM nodes WHERE file_path=?", (abs_path,))]
        conn.execute("DELETE FROM edges WHERE file_path=?", (abs_path,))
        conn.execute("DELETE FROM nodes WHERE file_path=?", (abs_path,))
    if removed_ids:
        _delete_memberships(conn, removed_ids)
    remaining = {r["qualified_name"]
                 for r in conn.execute("SELECT qualified_name FROM nodes")}
    new_qnames = {n.qualified_name for pf in parsed for n in pf.nodes}
    global_set = remaining | new_qnames
    node_count = _insert_nodes(conn, parsed)
    edges = resolve_edges(parsed, global_set)
    _insert_edges(conn, edges)
    recompute_degrees(conn)
    return node_count, len(edges)


def _insert_nodes(conn, parsed) -> int:
    rows = [(n.qualified_name, n.kind, n.language, n.file_path,
             n.start_line, n.end_line, n.signature)
            for pf in parsed for n in pf.nodes]
    conn.executemany(
        "INSERT INTO nodes(qualified_name,kind,language,file_path,start_line,"
        "end_line,signature,parent_id) VALUES(?,?,?,?,?,?,?,NULL)", rows)
    qname_to_id = {r["qualified_name"]: r["id"]
                   for r in conn.execute("SELECT id,qualified_name FROM nodes")}
    parent_updates = [
        (qname_to_id[n.parent_qname], qname_to_id[n.qualified_name])
        for pf in parsed for n in pf.nodes
        if n.parent_qname and n.parent_qname in qname_to_id]
    if parent_updates:
        conn.executemany(
            "UPDATE nodes SET parent_id=? WHERE id=?", parent_updates)
    return len(rows)


def _insert_edges(conn, edges) -> None:
    conn.executemany(
        "INSERT INTO edges(source,target,kind,file_path,call_line,resolution)"
        " VALUES(?,?,?,?,?,?)",
        [(e.source, e.target, e.kind, e.file_path, e.call_line, e.resolution)
         for e in edges])


def _delete_memberships(conn, node_ids: list[int]) -> None:
    placeholders = ",".join("?" for _ in node_ids)
    conn.execute(
        f"DELETE FROM flow_memberships WHERE node_id IN ({placeholders})",
        node_ids)
    conn.execute(
        f"DELETE FROM community_memberships WHERE node_id IN ({placeholders})",
        node_ids)


def _sync_manifest(conn, repo, parse_paths: list[str], deleted: set[str]) -> None:
    entries: dict[str, tuple[float, int, str]] = {}
    for rel in parse_paths:
        abs_path = os.path.join(repo, rel)
        st = os.stat(abs_path)
        entries[rel] = (st.st_mtime, st.st_size, manifest.hash_file(abs_path))
    manifest.update(conn, entries)
    manifest.remove(conn, sorted(deleted))


def needs_flows_update(config, conn) -> bool:
    row = conn.execute(
        "SELECT value FROM build_meta WHERE key='flows_as_of_head'").fetchone()
    stored = row["value"] if row else None
    return stored != current_head(config)


def update_flows(config, conn) -> int:
    """Rebuild flows from the DB's nodes+edges. No-op if HEAD is unchanged
    (flows represent the last committed state)."""
    if not needs_flows_update(config, conn):
        return 0
    nodes = [NodeRow(r["id"], r["qualified_name"], r["file_path"], r["kind"])
             for r in conn.execute(
                 "SELECT id,qualified_name,file_path,kind FROM nodes")]
    erows = [EdgeRow(r["source"], r["target"], r["resolution"])
             for r in conn.execute(
                 "SELECT source,target,resolution FROM edges WHERE kind='call'")]
    flows = build_flows(nodes, erows, config.entry_names)
    id_to_qname = {n.id: n.qualified_name for n in nodes}
    with transaction(conn):
        conn.execute("DELETE FROM flow_memberships")
        conn.execute("DELETE FROM flows")
        membership_rows: list[tuple[int, int, int]] = []
        for f in flows:
            name = qname.short(id_to_qname.get(f.entry_point_id, ""))
            cur = conn.execute(
                "INSERT INTO flows(name,entry_point_id,depth,node_count,"
                "file_count,criticality,path_json) VALUES(?,?,?,?,?,?,?)",
                (name, f.entry_point_id, f.depth, f.node_count, f.file_count,
                 None, json.dumps(f.path)))
            fid = cur.lastrowid
            membership_rows.extend((fid, nid, pos) for pos, nid in enumerate(f.path))
        if membership_rows:
            conn.executemany(
                "INSERT INTO flow_memberships(flow_id,node_id,position) "
                "VALUES(?,?,?)", membership_rows)
        conn.execute(
            "INSERT OR REPLACE INTO build_meta(key,value) "
            "VALUES('flows_as_of_head',?)", (current_head(config) or "",))
    return len(flows)


def update_communities(config, conn) -> int:
    """Rebuild communities from the DB's structural (non-call) resolved edges.
    Opt-in via config.community_detection; degrades gracefully if libs missing."""
    if not config.community_detection:
        return 0
    nodes = [NodeRow(r["id"], r["qualified_name"], r["file_path"], r["kind"])
             for r in conn.execute(
                 "SELECT id,qualified_name,file_path,kind FROM nodes")]
    erows = [EdgeRow(r["source"], r["target"], "resolved")
             for r in conn.execute(
                 "SELECT source,target FROM edges "
                 "WHERE kind!='call' AND resolution='resolved'")]
    try:
        communities = build_communities(
            nodes, erows,
            weight_mode=WeightMode.parse(config.community_weight))
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "leidenalg/igraph not installed; skipping community detection")
        return 0
    qname_to_id = {n.qualified_name: n.id for n in nodes}
    membership_rows: list[tuple[int, int]] = []
    with transaction(conn):
        conn.execute("DELETE FROM community_edges")
        conn.execute("DELETE FROM community_memberships")
        conn.execute("DELETE FROM communities")
        for c in communities:
            cur = conn.execute(
                "INSERT INTO communities(label,node_count,modularity) "
                "VALUES(?,?,?)", (c.label, len(c.members), c.modularity))
            cid = cur.lastrowid
            membership_rows.extend((cid, nid) for nid in c.members)
        if membership_rows:
            conn.executemany(
                "INSERT INTO community_memberships(community_id,node_id) "
                "VALUES(?,?)", membership_rows)
            node_to_comm = {nid: cid for cid, nid in membership_rows}
            comm_edges = inter_community_edges(erows, qname_to_id, node_to_comm)
            if comm_edges:
                conn.executemany(
                    "INSERT INTO community_edges(community_id_a,community_id_b,"
                    "weight) VALUES(?,?,?)",
                    [(a, b, w) for (a, b), w in comm_edges.items()])
    return len(communities)
