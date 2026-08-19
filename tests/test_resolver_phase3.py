"""Phase 3 — 模块解析闭合: star-import closure, __all__, cross-module inheritance,
and the .pyi stub degradation contract (guide §6 gate: 正例 / 冲突负例 / 缺失模块
degrade / Impact contract / 增量等价).

These tests run at the resolver / indexer level, the same layer Phase 2's
candidate-model tests live at.
"""
import subprocess

from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.parser import parse_file
from code_review_ai.resolver import resolve_calls, resolve_edges

from conftest import Q


def _edges(tmp_path, names):
    parsed = [parse_file(str(tmp_path / name), str(tmp_path)) for name in names]
    qnames = {node.qualified_name for pf in parsed for node in pf.nodes}
    return resolve_calls(parsed, qnames)


# ── PY-M08: `from m import *` star import closure ────────────────────


def test_star_import_unique_hit_resolves(tmp_path):
    (tmp_path / "pkg.py").write_text(
        "def helper():\n"
        "    return 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from pkg import *\n"
        "def entry():\n"
        "    helper()\n", encoding="utf-8")
    edges = _edges(tmp_path, ("pkg.py", "app.py"))
    # helper() resolves to pkg::helper through the star import — was unresolved
    assert any(e.source == Q("app", "entry")
               and e.target == Q("pkg", "helper")
               and e.resolution == "resolved" for e in edges)


def test_star_import_multi_hit_produces_candidates_with_site_id(tmp_path):
    """冲突负例: two modules both named helper — the call becomes two candidate
    edges sharing one site_id (the first real producer of the Phase 2 slot)."""
    (tmp_path / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from a import *\n"
        "from b import *\n"
        "def entry():\n"
        "    helper()\n", encoding="utf-8")
    edges = _edges(tmp_path, ("a.py", "b.py", "app.py"))
    candidates = [e for e in edges if e.resolution == "candidate"]
    assert len(candidates) == 2
    assert {e.target for e in candidates} == {Q("a", "helper"), Q("b", "helper")}
    # both alternatives share the one call site's site_id, and the evidence
    # records the full candidate list
    assert {e.site_id for e in candidates} == {candidates[0].site_id}
    assert set(candidates[0].evidence_json["candidates"]) == {
        Q("a", "helper"), Q("b", "helper")}
    assert candidates[0].evidence_json["truncated"] is False


def test_star_import_honors_all(tmp_path):
    """A module's __all__ gates what a star import can see."""
    (tmp_path / "pkg.py").write_text(
        "__all__ = ['public_fn', 'helper']\n"
        "def public_fn():\n    pass\n"
        "def helper():\n    pass\n"
        "def _internal():\n    pass\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from pkg import *\n"
        "def entry():\n"
        "    helper()\n"
        "    public_fn()\n"
        "    _internal()\n", encoding="utf-8")
    edges = _edges(tmp_path, ("pkg.py", "app.py"))
    by = {(e.source, e.target, e.resolution) for e in edges}
    # names in __all__ resolve through the star import
    assert (Q("app", "entry"), Q("pkg", "helper"), "resolved") in by
    assert (Q("app", "entry"), Q("pkg", "public_fn"), "resolved") in by
    # a symbol excluded by __all__ stays unresolved
    assert (Q("app", "entry"), "_internal", "unresolved") in by


def test_star_import_attribute_call_resolves(tmp_path):
    (tmp_path / "pkg.py").write_text(
        "class Helper:\n"
        "    def build(self):\n"
        "        pass\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from pkg import *\n"
        "def entry():\n"
        "    Helper.build()\n", encoding="utf-8")
    edges = _edges(tmp_path, ("pkg.py", "app.py"))
    # Helper.build() binds to pkg::Helper.build through the star-imported class
    assert any(e.source == Q("app", "entry")
               and e.target == Q("pkg", "build", "pkg::Helper")
               and e.resolution == "resolved" for e in edges)


def test_star_reexport_barrel_resolves(tmp_path):
    """`from a import helper` where a's body is `from b import *` — the resolver
    follows the star re-export chain (the _resolve_reexport star branch)."""
    (tmp_path / "b.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("from b import *\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from a import helper\n"
        "def entry():\n"
        "    helper()\n", encoding="utf-8")
    edges = _edges(tmp_path, ("b.py", "a.py", "app.py"))
    assert any(e.source == Q("app", "entry")
               and e.target == Q("b", "helper")
               and e.resolution == "resolved" for e in edges)


# ── COM-M05: cross-module inheritance through imports ────────────────


def test_cross_module_inherit_via_module_alias(tmp_path):
    (tmp_path / "base.py").write_text("class Base:\n    pass\n", encoding="utf-8")
    (tmp_path / "user.py").write_text(
        "import base\n"
        "class User(base.Base):\n"
        "    pass\n", encoding="utf-8")
    parsed = [parse_file(str(tmp_path / name), str(tmp_path))
              for name in ("base.py", "user.py")]
    qnames = {node.qualified_name for pf in parsed for node in pf.nodes}
    edges = resolve_edges(parsed, qnames)
    by = {(e.source, e.target, e.kind, e.resolution) for e in edges}
    # class User(base.Base) resolves to base::Base — previously unresolved
    assert (Q("user", "User"), Q("base", "Base"), "extends", "resolved") in by


def test_cross_module_inherit_via_from_import(tmp_path):
    (tmp_path / "base.py").write_text("class Base:\n    pass\n", encoding="utf-8")
    (tmp_path / "user.py").write_text(
        "from base import Base\n"
        "class User(Base):\n"
        "    pass\n", encoding="utf-8")
    parsed = [parse_file(str(tmp_path / name), str(tmp_path))
              for name in ("base.py", "user.py")]
    qnames = {node.qualified_name for pf in parsed for node in pf.nodes}
    edges = resolve_edges(parsed, qnames)
    by = {(e.source, e.target, e.kind, e.resolution) for e in edges}
    assert (Q("user", "User"), Q("base", "Base"), "extends", "resolved") in by


# ── PY-M10: .pyi stubs degrade cleanly (never indexed) ───────────────


def _pyi_repo(tmp_path):
    (tmp_path / "x.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "x.pyi").write_text(
        "def f() -> int: ...\n"
        "def g() -> str: ...\n", encoding="utf-8")
    for cmd in (["git", "init"], ["git", "add", "-A"],
                ["git", "commit", "-m", "fixture"]):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
    cfg = load_config(str(tmp_path))
    cfg.db_path = str(tmp_path / "i.db")
    cfg.repo_path = str(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return conn


def test_pyi_stub_not_indexed_no_phantom_nodes(tmp_path):
    """PY-M10: .pyi stubs are excluded by the *.py source glob. They must not
    produce phantom nodes, edges, or symbol conflicts — x.py is the only source
    of truth for module x."""
    conn = _pyi_repo(tmp_path)
    qnames = {row["qualified_name"]
              for row in conn.execute("SELECT qualified_name FROM nodes")}
    assert "x" in qnames
    assert Q("x", "f") in qnames
    # the .pyi-only stub symbol never appears (the degradation contract)
    assert Q("x", "g") not in qnames
