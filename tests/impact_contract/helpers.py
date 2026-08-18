"""Shared building blocks for the cross-language Impact contract suite (Phase 1).

The contract under test: parse -> resolve -> rebuild -> get_impact /
get_test_impact must work end-to-end for each supported language, and an
incremental sync must leave the index identical to a fresh full rebuild.

Test modules import from this file as ``from helpers import ...`` — pytest's
``importmode=prepend`` puts the containing directory on ``sys.path`` for us.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from code_review_ai import update as upd
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.impact import get_impact
from code_review_ai.indexer import rebuild
from code_review_ai.testimpact import get_test_impact


def _git(repo_path: str, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo_path, check=True, capture_output=True,
    )


def write_files(repo_path: Path, files: dict[str, str]) -> None:
    """Write ``files`` (relative path -> content) under ``repo_path``."""
    for rel, content in files.items():
        path = repo_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def commit_all(repo_path: Path, message: str = "fixture") -> None:
    _git(repo_path, "init", "-q")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-qm", message)


def build_index(repo_path: Path, files: dict[str, str], config: dict | None = None):
    """Write ``files``, git-init/commit, full rebuild.

    Returns ``(cfg, conn)``. ``config`` keys override the loaded config (e.g.
    ``{"entry_names": [...]}``) so a test can pin non-default entry points.
    """
    write_files(repo_path, files)
    commit_all(repo_path)
    cfg = load_config(str(repo_path))
    if config:
        for key, value in config.items():
            setattr(cfg, key, value)
    cfg.db_path = str(repo_path / "index.db")
    cfg.repo_path = str(repo_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return cfg, conn


def qname_set(nodes: list[dict]) -> set[str]:
    return {node["qname"] for node in nodes}


def norm(path: str) -> str:
    return path.replace("\\", "/")


def _edge_snapshot(conn) -> set[tuple]:
    return {tuple(r) for r in conn.execute(
        "SELECT source, target, kind, resolution, file_path FROM edges")}


def _flow_snapshot(conn) -> set[tuple]:
    """{(entry_qname, (member_qnames in position order))}."""
    out: set[tuple] = set()
    for flow in conn.execute("SELECT id, entry_point_id FROM flows").fetchall():
        entry = conn.execute(
            "SELECT qualified_name FROM nodes WHERE id = ?",
            (flow["entry_point_id"],)).fetchone()
        path = tuple(
            row[0] for row in conn.execute(
                "SELECT n.qualified_name FROM flow_memberships m "
                "JOIN nodes n ON n.id = m.node_id "
                "WHERE m.flow_id = ? ORDER BY m.position",
                (flow["id"],)))
        out.add((entry["qualified_name"] if entry else None, path))
    return out


def _impact_snapshot(conn, symbols: list[str]) -> tuple:
    """(get_impact results, get_test_impact results) — serialisable for equality."""
    impact = get_impact(conn, symbols)
    for result in impact:
        # node briefs are dicts -> tuples so both snapshots compare equal
        result["upstream"] = [tuple(sorted(item.items())) for item in result["upstream"]]
        result["downstream"] = [tuple(sorted(item.items())) for item in result["downstream"]]
    test_impact = get_test_impact(conn, symbols)
    for test in test_impact["affected_tests"]:
        test["covers"] = tuple(test["covers"])
    return impact, test_impact


def assert_incremental_equals_rebuild(
    cfg, conn, apply_changes, symbols: list[str],
) -> None:
    """Apply changes -> incremental sync -> snapshot -> rebuild -> compare.

    ``apply_changes(Path(cfg.repo_path))`` mutates the working tree
    (edit/add/delete). The two snapshots must be identical: edges, flows,
    impact, test impact.
    """
    repo_path = Path(cfg.repo_path)
    apply_changes(repo_path)
    upd.update_nodes_edges(cfg, conn)
    commit_all(repo_path, message="change batch")
    upd.sync(cfg, conn)

    incr = (_edge_snapshot(conn), _flow_snapshot(conn), _impact_snapshot(conn, symbols))

    rebuild(cfg, conn)
    full = (_edge_snapshot(conn), _flow_snapshot(conn), _impact_snapshot(conn, symbols))

    assert incr[0] == full[0], "edge sets differ"
    assert incr[1] == full[1], "flow sets differ"
    assert incr[2][0] == full[2][0], "get_impact output differs"
    assert incr[2][1] == full[2][1], "get_test_impact output differs"
