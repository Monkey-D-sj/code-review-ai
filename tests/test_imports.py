"""Import-binding persistence + alias surfacing.

The parser has always captured import aliases (`ImportEntry.local_name`) and the
resolver resolves calls through them, but the binding itself was never written
to the DB — so a name like ``decrypt_storage_password`` (bound by
``from lib import decrypt_password as decrypt_storage_password``) was invisible
to search_symbol / get_impact and forced a grep. These
tests guard the new `imports` table + `fts_imports` search index + the inverse
alias lookup on the search / impact surfaces.
"""

import subprocess

from code_review_ai import update as upd
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.impact import get_impact, get_symbol_aliases
from code_review_ai.search import fts_search

from conftest import Q


def _git(tmp_path):
    """Init a git repo (list_source_files reads `git ls-files`)."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)


def _alias_repo(tmp_path, mod_body):
    (tmp_path / "lib.py").write_text(
        "def decrypt_password():\n    return 1\n", encoding="utf-8")
    (tmp_path / "mod.py").write_text(mod_body, encoding="utf-8")
    _git(tmp_path)
    cfg = load_config(str(tmp_path))
    cfg.repo_path = str(tmp_path)
    cfg.db_path = str(tmp_path / "i.db")
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return cfg, conn


ALIASED = "from lib import decrypt_password as decrypt_storage_password\n"
PLAIN = "from lib import decrypt_password\n"


def test_rebuild_persists_import_aliases(tmp_path):
    cfg, conn = _alias_repo(tmp_path, ALIASED)

    row = conn.execute(
        "SELECT local_name, imported_name, module, resolved_target, "
        "file_path, line FROM imports").fetchone()
    assert row is not None
    assert row["local_name"] == "decrypt_storage_password"
    assert row["imported_name"] == "decrypt_password"
    assert row["module"] == "lib"
    assert row["resolved_target"] == Q("lib", "decrypt_password")
    assert row["file_path"] == str(tmp_path / "mod.py")  # absolute, like nodes
    assert row["line"] == 1

    # The alias is searchable by its bound name, and the hit lands on the
    # real symbol while file/line point at the import site (file is absolute,
    # relativized at the MCP boundary like every other surface).
    hits = fts_search(conn, "decrypt_storage_password")
    assert hits, "alias name must be searchable"
    hit = hits[0]
    assert hit["qname"] == Q("lib", "decrypt_password")
    assert hit["file"] == str(tmp_path / "mod.py")
    assert hit["line"] == 1
    assert hit["signature"] == (
        "imported as decrypt_storage_password from lib")


def test_search_ignores_non_aliased_imports(tmp_path):
    cfg, conn = _alias_repo(tmp_path, PLAIN)
    # The binding row is persisted (inverse lookup may still use it)...
    assert conn.execute("SELECT count(*) FROM imports").fetchone()[0] == 1
    # ...but a plain import is NOT alias-searchable: searching the imported
    # name surfaces the defining node (lib::decrypt_password), never an
    # "imported as" hit, and the (fabricated) alias name finds nothing.
    # NB: a bare `count(*) FROM fts_imports` would read the content table for
    # external-content FTS5, so assert through MATCH / fts_search instead.
    hits = fts_search(conn, "decrypt_password")
    assert any(h["qname"] == Q("lib", "decrypt_password") for h in hits)
    assert not any(h["signature"].startswith("imported as") for h in hits)
    assert fts_search(conn, "decrypt_storage_password") == []
    # Same predicate on the inverse lookup: a plain import (local == imported)
    # is not an alias and must not pad the impact aliases payload.
    assert get_symbol_aliases(conn, Q("lib", "decrypt_password")) == []


def test_get_symbol_aliases_and_impact_surface_alias(tmp_path):
    cfg, conn = _alias_repo(tmp_path, ALIASED)

    aliases = get_symbol_aliases(conn, Q("lib", "decrypt_password"))
    assert aliases == [{"name": "decrypt_storage_password",
                        "file": str(tmp_path / "mod.py"), "line": 1}]

    # get_impact's main channel carries the alias, non-empty only.
    result = get_impact(conn, [Q("lib", "decrypt_password")])[0]
    assert result["aliases"] == aliases
    # A symbol with no aliases omits the key entirely (no payload bloat).
    result = get_impact(conn, [Q("lib", "missing")])[0]
    assert "aliases" not in result


def test_incremental_sync_maintains_imports(tmp_path):
    cfg, conn = _alias_repo(tmp_path, ALIASED)
    assert conn.execute(
        "SELECT count(*) FROM fts_imports WHERE fts_imports MATCH "
        "'decrypt_storage_password'").fetchone()[0] == 1

    # Rename the alias on disk; the incremental path must drop the old row
    # (backing + fts) and index the new one.
    (tmp_path / "mod.py").write_text(
        "from lib import decrypt_password as decrypt_storage_alias\n",
        encoding="utf-8")
    upd.update_nodes_edges(cfg, conn, ["mod.py"])

    assert conn.execute(
        "SELECT count(*) FROM fts_imports WHERE fts_imports MATCH "
        "'decrypt_storage_password'").fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM fts_imports WHERE fts_imports MATCH "
        "'decrypt_storage_alias'").fetchone()[0] == 1
    row = conn.execute(
        "SELECT local_name, resolved_target FROM imports").fetchone()
    assert row["local_name"] == "decrypt_storage_alias"
    assert row["resolved_target"] == Q("lib", "decrypt_password")
    assert fts_search(conn, "decrypt_storage_alias")[0]["qname"] == Q(
        "lib", "decrypt_password")


def test_delete_file_drops_imports(tmp_path):
    cfg, conn = _alias_repo(tmp_path, ALIASED)
    (tmp_path / "mod.py").unlink()
    upd.update_nodes_edges(cfg, conn, ["mod.py"])
    assert conn.execute("SELECT count(*) FROM imports").fetchone()[0] == 0
    # The FTS index must be clean too — check via MATCH (bare count would read
    # the now-empty content table and mask a stale index entry).
    assert conn.execute(
        "SELECT count(*) FROM fts_imports WHERE fts_imports MATCH "
        "'decrypt_storage_password'").fetchone()[0] == 0
    assert fts_search(conn, "decrypt_storage_password") == []
