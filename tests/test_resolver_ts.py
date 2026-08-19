"""Phase 3 — TS/JS ESM module-resolution closure (guide §6, JS-M02/M05/M06/
M07/M09/M10): default exports, `export *` barrels, local re-exports, and
extension / directory-index probing all close previously-unresolved edges.
"""
from code_review_ai.parser import parse_file
from code_review_ai.resolver import resolve_calls

from conftest import Q


def _edges(tmp_path, names):
    parsed = [parse_file(str(tmp_path / name), str(tmp_path)) for name in names]
    qnames = {node.qualified_name for pf in parsed for node in pf.nodes}
    return resolve_calls(parsed, qnames)


# ── JS-M02: `export default` + default import ────────────────────────


def test_ts_default_function_import_resolves(tmp_path):
    """`import login from './auth'` binds to the module's `export default`."""
    (tmp_path / "auth.ts").write_text(
        "export default function login() {\n"
        "    return true;\n"
        "}\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text(
        "import login from './auth';\n"
        "export function main() {\n"
        "    login();\n"
        "}\n", encoding="utf-8")
    edges = _edges(tmp_path, ("auth.ts", "app.ts"))
    assert any(e.source == Q("app", "main")
               and e.target == Q("auth", "login")
               and e.resolution == "resolved" for e in edges)


def test_ts_default_class_static_call_resolves(tmp_path):
    """A default-imported class receiver resolves attribute calls onto it."""
    (tmp_path / "helper.ts").write_text(
        "export default class Helper {\n"
        "    static build() {\n"
        "        return 1;\n"
        "    }\n"
        "}\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text(
        "import Helper from './helper';\n"
        "export function main() {\n"
        "    Helper.build();\n"
        "}\n", encoding="utf-8")
    edges = _edges(tmp_path, ("helper.ts", "app.ts"))
    assert any(e.source == Q("app", "main")
               and e.target == Q("helper", "build", "helper::Helper")
               and e.resolution == "resolved" for e in edges)


# ── JS-M07: `export * from` barrels ──────────────────────────────────


def test_ts_export_star_barrel_resolves(tmp_path):
    """A named import from a barrel that re-exports via `export *` chains to
    the real definition module."""
    (tmp_path / "b.ts").write_text(
        "export function helper() {\n"
        "    return 1;\n"
        "}\n", encoding="utf-8")
    (tmp_path / "a.ts").write_text(
        "export * from './b';\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text(
        "import { helper } from './a';\n"
        "export function main() {\n"
        "    helper();\n"
        "}\n", encoding="utf-8")
    edges = _edges(tmp_path, ("b.ts", "a.ts", "app.ts"))
    assert any(e.source == Q("app", "main")
               and e.target == Q("b", "helper")
               and e.resolution == "resolved" for e in edges)


def test_ts_multi_barrel_conflict_produces_candidates(tmp_path):
    """冲突负例: two barrels both export `helper` — the named import becomes two
    candidate edges sharing one site_id, not a silently-picked winner."""
    (tmp_path / "b1.ts").write_text(
        "export function helper() {\n    return 1;\n}\n", encoding="utf-8")
    (tmp_path / "b2.ts").write_text(
        "export function helper() {\n    return 2;\n}\n", encoding="utf-8")
    (tmp_path / "a.ts").write_text(
        "export * from './b1';\n"
        "export * from './b2';\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text(
        "import { helper } from './a';\n"
        "export function main() {\n"
        "    helper();\n"
        "}\n", encoding="utf-8")
    edges = _edges(tmp_path, ("b1.ts", "b2.ts", "a.ts", "app.ts"))
    candidates = [e for e in edges if e.resolution == "candidate"]
    assert len(candidates) == 2
    assert {e.target for e in candidates} == {Q("b1", "helper"), Q("b2", "helper")}
    # both alternatives share the one call site's site_id, and the evidence
    # records the full candidate list
    assert {e.site_id for e in candidates} == {candidates[0].site_id}
    assert set(candidates[0].evidence_json["candidates"]) == {
        Q("b1", "helper"), Q("b2", "helper")}


# ── JS-M05/M06: local + external re-exports ──────────────────────────


def test_ts_local_reexport_alias_resolves(tmp_path):
    """`export { y as x }` (no from) — a local re-export alias — resolves
    through the module's own binding."""
    (tmp_path / "m.ts").write_text(
        "export function y() {\n    return 1;\n}\n"
        "export { y as x };\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text(
        "import { x } from './m';\n"
        "export function main() {\n"
        "    x();\n"
        "}\n", encoding="utf-8")
    edges = _edges(tmp_path, ("m.ts", "app.ts"))
    assert any(e.source == Q("app", "main")
               and e.target == Q("m", "y")
               and e.resolution == "resolved" for e in edges)


def test_ts_reexport_with_source_resolves(tmp_path):
    """`export { x } from './b'` (JS-M06) — already an import binding; a named
    import from the re-exporting module chains to the source definition."""
    (tmp_path / "b.ts").write_text(
        "export function x() {\n    return 1;\n}\n", encoding="utf-8")
    (tmp_path / "a.ts").write_text(
        "export { x } from './b';\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text(
        "import { x } from './a';\n"
        "export function main() {\n"
        "    x();\n"
        "}\n", encoding="utf-8")
    edges = _edges(tmp_path, ("b.ts", "a.ts", "app.ts"))
    assert any(e.source == Q("app", "main")
               and e.target == Q("b", "x")
               and e.resolution == "resolved" for e in edges)


# ── JS-M09/M10: extension & directory-index probing ──────────────────


def test_ts_bare_specifier_resolves_via_probe(tmp_path):
    """`import { helper } from './util'` (no extension) resolves to
    util::helper — the filesystem probe and the suffix strip agree."""
    (tmp_path / "util.ts").write_text(
        "export function helper() {\n    return 1;\n}\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text(
        "import { helper } from './util';\n"
        "export function main() {\n"
        "    helper();\n"
        "}\n", encoding="utf-8")
    edges = _edges(tmp_path, ("util.ts", "app.ts"))
    assert any(e.source == Q("app", "main")
               and e.target == Q("util", "helper")
               and e.resolution == "resolved" for e in edges)


def test_js_directory_index_resolves(tmp_path):
    """`import { helper } from './lib'` where only lib/index.ts exists resolves
    to lib.index::helper — the index probe closes the directory-import gap."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "index.ts").write_text(
        "export function helper() {\n    return 1;\n}\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text(
        "import { helper } from './lib';\n"
        "export function main() {\n"
        "    helper();\n"
        "}\n", encoding="utf-8")
    edges = _edges(tmp_path, ("lib/index.ts", "app.ts"))
    assert any(e.source == Q("app", "main")
               and e.target == Q("lib.index", "helper")
               and e.resolution == "resolved" for e in edges)
