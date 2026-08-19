"""Phase 3 — TS/JS ESM + CommonJS module-resolution closure (guide §6,
JS-M02/M05/M06/M07/M09/M10 ESM, JS-M13/M15/M18/M19 CJS + tsconfig): default
exports, barrels, extension/index probing, require/module.exports bindings,
and baseUrl/extends resolution.
"""
import json

from code_review_ai.config import load_config
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


# ── JS-M18: require() / module.exports ───────────────────────────────


def test_cjs_require_attribute_call_resolves(tmp_path):
    """`const util = require('./util'); util.helper()` binds through the local
    require alias."""
    (tmp_path / "util.js").write_text(
        "function helper() {\n    return 1;\n}\n"
        "module.exports = { helper };\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "const util = require('./util');\n"
        "function main() {\n"
        "    util.helper();\n"
        "}\n", encoding="utf-8")
    edges = _edges(tmp_path, ("util.js", "app.js"))
    assert any(e.source == Q("app", "main")
               and e.target == Q("util", "helper")
               and e.resolution == "resolved" for e in edges)


def test_cjs_direct_require_attribute_resolves(tmp_path):
    """JS-M18: `require('./util').helper()` — the require expression is the
    receiver (rpartition picks the last dot, past the specifier's own)."""
    (tmp_path / "util.js").write_text(
        "function helper() {\n    return 1;\n}\n"
        "module.exports = { helper };\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "function main() {\n"
        "    require('./util').helper();\n"
        "}\n", encoding="utf-8")
    edges = _edges(tmp_path, ("util.js", "app.js"))
    assert any(e.source == Q("app", "main")
               and e.target == Q("util", "helper")
               and e.resolution == "resolved" for e in edges)


def test_cjs_destructured_require_resolves(tmp_path):
    """`const { login } = require('./auth')` binds the named export directly."""
    (tmp_path / "auth.js").write_text(
        "function login() {\n    return true;\n}\n"
        "exports.login = login;\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "const { login } = require('./auth');\n"
        "function main() {\n"
        "    login();\n"
        "}\n", encoding="utf-8")
    edges = _edges(tmp_path, ("auth.js", "app.js"))
    assert any(e.source == Q("app", "main")
               and e.target == Q("auth", "login")
               and e.resolution == "resolved" for e in edges)


def test_cjs_default_export_import_resolves(tmp_path):
    """JS-M19: `module.exports = login` (identifier) lets a default import of
    the CJS module resolve to the referenced local."""
    (tmp_path / "auth.js").write_text(
        "function login() {\n    return true;\n}\n"
        "module.exports = login;\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text(
        "import login from './auth';\n"
        "export function main() {\n"
        "    login();\n"
        "}\n", encoding="utf-8")
    edges = _edges(tmp_path, ("auth.js", "app.ts"))
    assert any(e.source == Q("app", "main")
               and e.target == Q("auth", "login")
               and e.resolution == "resolved" for e in edges)


def test_cjs_reexport_through_local_alias_resolves(tmp_path):
    """`exports.wrap = helper.run` where `helper = require('./util')` chains
    through the local require alias to util::run."""
    (tmp_path / "util.js").write_text(
        "function run() {\n    return 1;\n}\n"
        "module.exports = { run };\n", encoding="utf-8")
    (tmp_path / "auth.js").write_text(
        "const helper = require('./util');\n"
        "exports.wrap = helper.run;\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "const { wrap } = require('./auth');\n"
        "function main() {\n"
        "    wrap();\n"
        "}\n", encoding="utf-8")
    edges = _edges(tmp_path, ("util.js", "auth.js", "app.js"))
    assert any(e.source == Q("app", "main")
               and e.target == Q("util", "run")
               and e.resolution == "resolved" for e in edges)


def test_cjs_missing_module_stays_unresolved(tmp_path):
    """`require('fs')` (external) keeps the raw name on an unresolved edge."""
    (tmp_path / "app.js").write_text(
        "const fs = require('fs');\n"
        "function main() {\n"
        "    fs.readFileSync('x');\n"
        "}\n", encoding="utf-8")
    edges = _edges(tmp_path, ("app.js",))
    main_edges = [e for e in edges if e.source == Q("app", "main")]
    assert any(e.resolution == "unresolved" for e in main_edges)


# ── JS-M13/M15: tsconfig baseUrl + extends ───────────────────────────


def _resolve_with_cfg(tmp_path, tsconfig, names):
    """Parse files with a tsconfig-driven config (baseUrl/path_aliases)."""
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.code-review-ai]\nrepo_path = "{tmp_path.as_posix()}"\n',
        encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        json.dumps(tsconfig), encoding="utf-8")
    cfg = load_config(str(tmp_path))
    parsed = [parse_file(str(tmp_path / name), str(tmp_path)) for name in names]
    qnames = {node.qualified_name for pf in parsed for node in pf.nodes}
    return cfg, resolve_calls(parsed, qnames, cfg.path_aliases, None, cfg.base_url)


def test_ts_base_url_bare_import_resolves(tmp_path):
    """JS-M13: a bare specifier resolves under tsconfig baseUrl when the module
    exists there (`lib/helper` -> src/lib/helper.ts -> lib.helper)."""
    (tmp_path / "src" / "lib").mkdir(parents=True)
    (tmp_path / "src" / "lib" / "helper.ts").write_text(
        "export function run() {\n    return 1;\n}\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text(
        "import { run } from 'lib/helper';\n"
        "export function main() {\n"
        "    run();\n"
        "}\n", encoding="utf-8")
    cfg, edges = _resolve_with_cfg(
        tmp_path, {"compilerOptions": {"baseUrl": "src"}},
        ("app.ts", "src/lib/helper.ts"))
    assert cfg.base_url == "src"
    assert any(e.source == Q("app", "main")
               and e.target == Q("lib.helper", "run")
               and e.resolution == "resolved" for e in edges)


def test_ts_base_url_missing_module_keeps_raw_specifier(tmp_path):
    """A bare specifier with no module under baseUrl degrades to unresolved —
    no phantom module qname is invented."""
    (tmp_path / "app.ts").write_text(
        "import { run } from 'lib/nope';\n"
        "export function main() {\n"
        "    run();\n"
        "}\n", encoding="utf-8")
    cfg, edges = _resolve_with_cfg(
        tmp_path, {"compilerOptions": {"baseUrl": "src"}}, ("app.ts",))
    main_edges = [e for e in edges if e.source == Q("app", "main")]
    assert main_edges and all(e.resolution == "unresolved" for e in main_edges)


def test_ts_extends_chain_inherits_base_url(tmp_path):
    """JS-M15: a child tsconfig extends a base — the inherited baseUrl resolves
    a bare import end-to-end."""
    (tmp_path / "src" / "lib").mkdir(parents=True)
    (tmp_path / "src" / "lib" / "helper.ts").write_text(
        "export function run() {\n    return 1;\n}\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text(
        "import { run } from 'lib/helper';\n"
        "export function main() {\n"
        "    run();\n"
        "}\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.code-review-ai]\nrepo_path = "{tmp_path.as_posix()}"\n',
        encoding="utf-8")
    (tmp_path / "tsconfig.base.json").write_text(
        json.dumps({"compilerOptions": {"baseUrl": "src"}}), encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"extends": "./tsconfig.base.json"}), encoding="utf-8")
    cfg = load_config(str(tmp_path))
    parsed = [parse_file(str(tmp_path / n), str(tmp_path))
              for n in ("app.ts", "src/lib/helper.ts")]
    qnames = {node.qualified_name for pf in parsed for node in pf.nodes}
    edges = resolve_calls(parsed, qnames, cfg.path_aliases, None, cfg.base_url)
    assert cfg.base_url == "src"
    assert any(e.source == Q("app", "main")
               and e.target == Q("lib.helper", "run")
               and e.resolution == "resolved" for e in edges)
