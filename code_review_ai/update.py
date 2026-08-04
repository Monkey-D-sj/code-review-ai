"""Incremental index updates.

Watcher keeps nodes/edges fresh (update_nodes_edges); git hooks and startup
recompute flows/communities from the DB (update_flows/update_communities).
The DB is the source of truth — no in-memory parse cache."""

import os

from code_review_ai.parser import (SOURCE_GLOBS, filter_excluded,
                                   list_source_files)
from code_review_ai import manifest


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
