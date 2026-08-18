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
from code_review_ai.config import config_hash as _config_hash
from code_review_ai.db import INDEX_VERSION, transaction
from code_review_ai.flow_builder import (EdgeRow, NodeRow, build_flows,
                                         flow_input_hash)
from code_review_ai.indexer import rebuild, recompute_degrees, _stamp_built_at
from code_review_ai.parser import (SOURCE_GLOBS, filter_excluded,
                                   is_test_node, list_source_files, parse_file)
from code_review_ai.resolver import resolve_edges
from code_review_ai.search import deindex_fts, index_fts


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
            # tracked by git but gone from disk -> deleted
            deleted.add(rel)
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
    # Files removed from git tracking entirely (git rm): present in the
    # manifest but no longer listed by git, so absent from `current`. The
    # OSError branch above handles tracked-but-gone-from-disk; this union
    # handles the untracked-from-git case, and the two never overlap.
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
            conn, repo, parsed, changed | added, deleted, config)
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


def _tombstone_upstream(conn, qname: str, deleted_qnames: set[str]) -> list[dict]:
    """One-hop upstream (call/inherits/import) of a deleted qname, excluding
    sources being deleted in this same batch. Runs before the delete loop so
    the edges are still the resolved pre-deletion state."""
    rows = conn.execute(
        "SELECT source, kind, file_path FROM edges "
        "WHERE target=? AND kind IN ('call','inherits','import')",
        (qname,)).fetchall()
    return [{"source": r["source"], "kind": r["kind"], "file": r["file_path"]}
            for r in rows if r["source"] not in deleted_qnames]


def _collect_tombstones(conn, repo, parsed, changed_set: set[str],
                        deleted_set: set[str], config) -> list[tuple]:
    """Tombstone rows for deletions in this batch, captured BEFORE the delete
    loop (edges still resolved). Whole-file deletions tombstone every old node
    (file_deleted=1); deletions inside a re-parsed surviving file are the
    old−new qname delta (file_deleted=0). Returns rows in _insert_tombstones
    column order."""
    parsed_qnames = {n.qualified_name for pf in parsed for n in pf.nodes}
    deleted_qnames: set[str] = set()
    pending: list[tuple] = []   # (old_node_row, file_deleted)
    for rel in sorted(changed_set | deleted_set):
        abs_path = os.path.join(repo, rel)
        rows = conn.execute(
            "SELECT * FROM nodes WHERE file_path=?", (abs_path,)).fetchall()
        if rel in deleted_set:
            pending.extend((r, 1) for r in rows)
            deleted_qnames.update(r["qualified_name"] for r in rows)
        else:
            delta = [r for r in rows if r["qualified_name"] not in parsed_qnames]
            pending.extend((r, 0) for r in delta)
            deleted_qnames.update(r["qualified_name"] for r in delta)
    head = current_head(config)
    out: list[tuple] = []
    for row, file_deleted in pending:
        upstream = _tombstone_upstream(conn, row["qualified_name"],
                                       deleted_qnames)
        out.append((row["qualified_name"], row["kind"], row["language"],
                    row["file_path"], row["start_line"], row["end_line"],
                    row["signature"], row["is_test"], row["decorators"],
                    head, file_deleted, json.dumps(upstream)))
    return out


def _insert_tombstones(conn, rows: list[tuple]) -> None:
    conn.executemany(
        "INSERT INTO tombstones(qname,kind,language,file_path,start_line,"
        "end_line,signature,is_test,decorators,deleted_at_head,file_deleted,"
        "upstream_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def _apply_nodes_edges_delta(conn, repo, parsed, changed_set: set[str],
                             deleted_set: set[str], config) -> tuple[int, int]:
    tombstone_rows = _collect_tombstones(
        conn, repo, parsed, changed_set, deleted_set, config)
    _insert_tombstones(conn, tombstone_rows)
    touch = [os.path.join(repo, rel) for rel in changed_set | deleted_set]
    parsed_qnames = {n.qualified_name for pf in parsed for n in pf.nodes}
    survivors: dict[str, int] = {}   # qname -> node id kept across the re-parse
    removed_ids: list[int] = []
    for abs_path in touch:
        # Edges are fully replaced below (re-resolved from the fresh parse).
        conn.execute("DELETE FROM edges WHERE file_path=?", (abs_path,))
        rows = conn.execute(
            "SELECT id, qualified_name FROM nodes WHERE file_path=?",
            (abs_path,)).fetchall()
        for row in rows:
            if row["qualified_name"] in parsed_qnames:
                # Same qname re-parsed: keep the node id so flow/community
                # memberships survive a body-only edit.
                survivors[row["qualified_name"]] = row["id"]
                continue
            removed_ids.append(row["id"])
    if removed_ids:
        # 单条语句删除全部 removed nodes —— FK 检查在语句结束时进行，
        # 父子节点同批删除不会触发 parent_id 外键冲突。
        deindex_fts(conn, removed_ids)   # 必须在 DELETE nodes 之前
        placeholders = ",".join("?" for _ in removed_ids)
        conn.execute(
            f"DELETE FROM nodes WHERE id IN ({placeholders})", removed_ids)
        _delete_memberships(conn, removed_ids)
    _update_survivors(conn, parsed, survivors, config)
    remaining = {r["qualified_name"]
                 for r in conn.execute("SELECT qualified_name FROM nodes")}
    new_qnames = {n.qualified_name for pf in parsed for n in pf.nodes}
    global_set = remaining | new_qnames
    node_count = _insert_nodes(conn, parsed, config, skip_qnames=remaining)
    edges = resolve_edges(parsed, global_set, config.path_aliases,
                          config.dependency_markers, config.di_annotations)
    _insert_edges(conn, edges)
    recompute_degrees(conn)
    return node_count, len(edges)


def _update_survivors(conn, parsed, survivors: dict[str, int], config) -> None:
    """Refresh surviving nodes' metadata in place, keeping their ids (and thus
    flow/community memberships) stable across a re-parse. FTS is external-content
    on `nodes`, so keeping the rowid keeps the index fresh."""
    updates: list[tuple] = []
    for pf in parsed:
        for n in pf.nodes:
            node_id = survivors.get(n.qualified_name)
            if node_id is None:
                continue
            updates.append((
                n.kind, n.language, n.file_path, n.start_line, n.end_line,
                n.signature,
                1 if is_test_node(n.file_path, n.qualified_name,
                                  config.test_globs, config.test_names,
                                  config.repo_path, n.decorators,
                                  config.test_decorators) else 0,
                json.dumps(n.decorators), node_id))
    if updates:
        conn.executemany(
            "UPDATE nodes SET kind=?, language=?, file_path=?, start_line=?, "
            "end_line=?, signature=?, is_test=?, decorators=? WHERE id=?",
            updates)


def _insert_nodes(conn, parsed, config, skip_qnames=frozenset()) -> int:
    """Insert the changed files' nodes, deduping by qualified_name against
    ``skip_qnames`` (qnames already in the DB — e.g. a Java package module node
    owned by another file) and against earlier nodes in this batch."""
    seen = set(skip_qnames)
    inserted: list = []
    rows: list[tuple] = []
    for pf in parsed:
        for n in pf.nodes:
            if n.qualified_name in seen:
                continue
            seen.add(n.qualified_name)
            inserted.append(n)
            rows.append((n.qualified_name, n.kind, n.language, n.file_path,
                         n.start_line, n.end_line, n.signature,
                         1 if is_test_node(n.file_path, n.qualified_name,
                                           config.test_globs, config.test_names,
                                           config.repo_path, n.decorators,
                                           config.test_decorators) else 0,
                         json.dumps(n.decorators)))
    conn.executemany(
        "INSERT INTO nodes(qualified_name,kind,language,file_path,start_line,"
        "end_line,signature,parent_id,is_test,decorators) VALUES(?,?,?,?,?,?,?,NULL,?,?)", rows)
    qname_to_id = {r["qualified_name"]: r["id"]
                   for r in conn.execute("SELECT id,qualified_name FROM nodes")}
    parent_updates = [
        (qname_to_id[n.parent_qname], qname_to_id[n.qualified_name])
        for n in inserted
        if n.parent_qname and n.parent_qname in qname_to_id]
    if parent_updates:
        conn.executemany(
            "UPDATE nodes SET parent_id=? WHERE id=?", parent_updates)
    index_fts(conn, inserted, qname_to_id)
    return len(rows)


def _insert_edges(conn, edges) -> None:
    conn.executemany(
        "INSERT INTO edges(source,target,kind,file_path,resolution)"
        " VALUES(?,?,?,?,?)",
        [(e.source, e.target, e.kind, e.file_path, e.resolution)
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


def needs_flows_update(config, conn, head=None) -> bool:
    """True when the stored flows_as_of_head differs from the given head
    (defaults to the current HEAD when head is None)."""
    row = conn.execute(
        "SELECT value FROM build_meta WHERE key='flows_as_of_head'").fetchone()
    stored = row["value"] if row else None
    if head is None:
        head = current_head(config)
    return stored != head


def _decorators(raw: str | None) -> list[str]:
    """Decode the decorators JSON column, tolerating NULL / empty / bad JSON."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def update_flows(config, conn) -> int:
    """Rebuild flows from the DB's nodes+edges. No-op when HEAD is unchanged,
    or when HEAD moved but the flow input (function/method nodes + resolved
    call edges) is structurally unchanged — e.g. a body-only edit or a
    non-source commit. The flows_as_of_head marker advances either way so the
    next sync no-ops."""
    head = current_head(config)
    if not needs_flows_update(config, conn, head):
        return 0
    input_hash = flow_input_hash(conn)
    stored = conn.execute(
        "SELECT value FROM build_meta WHERE key='flows_input_hash'").fetchone()
    if stored is not None and stored["value"] == input_hash:
        # HEAD moved but the call graph didn't structurally change: skip the
        # rebuild and just advance the marker.
        conn.execute(
            "INSERT OR REPLACE INTO build_meta(key,value) "
            "VALUES('flows_as_of_head',?)", (head or "",))
        return 0
    nodes = [NodeRow(r["id"], r["qualified_name"], r["file_path"], r["kind"],
                     _decorators(r["decorators"]))
             for r in conn.execute(
                 "SELECT id,qualified_name,file_path,kind,decorators "
                 "FROM nodes")]
    erows = [EdgeRow(r["source"], r["target"], r["resolution"])
             for r in conn.execute(
                 "SELECT source,target,resolution FROM edges WHERE kind='call'")]
    flows = build_flows(nodes, erows, config.entry_names,
                        config.entry_decorators)
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
            "VALUES('flows_as_of_head',?)", (head or "",))
        conn.execute(
            "INSERT OR REPLACE INTO build_meta(key,value) "
            "VALUES('flows_input_hash',?)", (input_hash,))
    return len(flows)


def update_communities(config, conn) -> int:
    """Rebuild communities from the DB's structural (non-call) resolved edges.
    Opt-in via config.community_detection; degrades gracefully if libs missing.
    No-op when communities_as_of_head already matches HEAD — a full rebuild
    stamps it when communities were actually produced."""
    if not config.community_detection:
        return 0
    head = current_head(config)
    marker = conn.execute(
        "SELECT value FROM build_meta "
        "WHERE key='communities_as_of_head'").fetchone()
    if marker is not None and marker["value"] == head:
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
        if communities:
            conn.execute(
                "INSERT OR REPLACE INTO build_meta(key,value) "
                "VALUES('communities_as_of_head',?)", (head or "",))
    return len(communities)


def _meta_changed(config, conn) -> bool:
    expected = {"config_hash": _config_hash(config),
                "index_version": str(INDEX_VERSION)}
    for key, value in expected.items():
        row = conn.execute(
            "SELECT value FROM build_meta WHERE key=?", (key,)).fetchone()
        if row is None or row["value"] != value:
            return True
    return False


def sync(config, conn) -> dict:
    """Bring the index current: config/version change -> full rebuild;
    otherwise incremental nodes/edges + flows + communities (each skips
    internally when up to date)."""
    if _meta_changed(config, conn):
        stats = rebuild(config, conn)
        return {"full_rebuild": True, "nodes": stats.node_count,
                "edges": stats.edge_count, "flows": stats.flow_count,
                "communities": stats.community_count}
    node_stats = update_nodes_edges(config, conn)
    flows = update_flows(config, conn)
    communities = update_communities(config, conn)
    return {"full_rebuild": False, "nodes": node_stats["nodes"],
            "edges": node_stats["edges"], "flows": flows,
            "communities": communities}
