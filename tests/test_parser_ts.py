"""Tests for TypeScript/JavaScript parsing."""

import os

from conftest import FIXTURES as FIX, Q
from code_review_ai.parser import (parse_file, _esm_relative_module,
                                   _lang_for_path)


def _parse(fname: str):
    return parse_file(os.path.join(FIX, "ts", fname), FIX)


def test_parse_ts_auth():
    pf = _parse("auth.ts")
    assert pf.language == "typescript"

    kinds = {n.qualified_name: n.kind for n in pf.nodes}
    assert kinds["ts.auth"] == "module"
    assert kinds[Q("ts.auth", "UserService")] == "class"
    assert kinds[Q("ts.auth", "login")] == "function"
    assert kinds[Q("ts.auth", "authenticate", "ts.auth::UserService")] == "method"

    # Verify parent relationship for method
    method = next(n for n in pf.nodes if n.kind == "method")
    assert method.parent_qname == Q("ts.auth", "UserService")


def test_parse_ts_app():
    pf = _parse("app.ts")
    assert pf.language == "typescript"

    # Imports - relative specifiers are canonicalized to module qnames
    import_map = {i.local_name: i for i in pf.imports}
    assert import_map["login"].module == "ts.auth"
    assert import_map["login"].imported_name == "login"
    assert import_map["a"].module == "ts.auth"
    assert import_map["a"].imported_name is None  # namespace import
    assert import_map["hashPw"].module == "ts.util"

    # Calls
    calls = {(c.target_expr, c.call_form) for c in pf.raw_calls}
    assert ("login", "simple") in calls
    assert ("a.login", "attribute") in calls
    assert ("obj.run", "attribute") in calls

    # All calls inside main()
    for c in pf.raw_calls:
        assert c.source_qname == Q("ts.app", "main")


def test_esm_relative_module_normalization():
    """Relative specifiers canonicalize to module qnames with the same
    conventions as _module_qname (src/ stripped, suffix dropped)."""
    # ./sibling from ts/app.ts -> ts/auth
    assert _esm_relative_module("./auth", f"{FIX}/ts/app.ts", FIX) == "ts.auth"
    # explicit extension is stripped so it agrees with the bare form
    assert _esm_relative_module("./auth.ts", f"{FIX}/ts/app.ts", FIX) == "ts.auth"
    # ../ hops up out of the directory
    assert _esm_relative_module("../lib/api", f"{FIX}/ts/app.ts", FIX) == "lib.api"
    # a leading src/ segment is dropped like any indexed module
    assert _esm_relative_module("./auth", f"{FIX}/src/app.ts", FIX) == "auth"
    assert _esm_relative_module("../root-mod", f"{FIX}/src/a/b.ts", FIX) == "root-mod"
    # non-relative / escaping specifiers are left alone (-> None)
    assert _esm_relative_module("vue", f"{FIX}/ts/app.ts", FIX) is None
    assert _esm_relative_module("@/hooks/x", f"{FIX}/ts/app.ts", FIX) is None
    assert _esm_relative_module("../../outside", f"{FIX}/ts/app.ts", FIX) is None


def test_parse_ts_util_arrow():
    pf = _parse("util.ts")
    assert pf.language == "typescript"

    qnames = {n.qualified_name for n in pf.nodes}
    assert Q("ts.util", "helper") in qnames  # arrow function assigned to const

    # helper should be a function
    helper = next(n for n in pf.nodes if n.qualified_name == Q("ts.util", "helper"))
    assert helper.kind == "function"


def test_lang_for_path():
    assert _lang_for_path("foo.py")[0] == "python"
    assert _lang_for_path("foo.ts")[0] == "typescript"
    assert _lang_for_path("foo.tsx")[0] == "typescript"
    assert _lang_for_path("foo.js")[0] == "javascript"
    assert _lang_for_path("foo.jsx")[0] == "javascript"
    assert _lang_for_path("foo.mjs")[0] == "javascript"
    assert _lang_for_path("foo.cjs")[0] == "javascript"


def test_parse_vue_sfc():
    pf = parse_file(os.path.join(FIX, "HelloWorld.vue"), FIX)
    assert pf.language == "typescript"

    qnames = {n.qualified_name for n in pf.nodes}
    assert "HelloWorld" in qnames
    assert Q("HelloWorld", "greet") in qnames

    # should pick up import from script block
    imps = {i.local_name: i for i in pf.imports}
    assert "ref" in imps
    assert imps["ref"].module == "vue"


def test_parse_vue_sfc_lang_selection(tmp_path):
    """§5.4: a .vue script's dialect follows the block's `lang` attribute —
    lang="ts" → typescript, plain `<script>` / lang="js" → javascript (Vue's
    plain-<script> default is JS, not TS)."""
    ts = tmp_path / "Ts.vue"
    ts.write_text(
        '<template><div/></template>\n'
        '<script setup lang="ts">\n'
        "const n: number = 1;\n"
        "function greet(name: string): string { return name; }\n"
        "</script>\n",
        encoding="utf-8",
    )
    assert parse_file(str(ts), str(tmp_path)).language == "typescript"

    js = tmp_path / "Js.vue"
    js.write_text(
        '<template><div/></template>\n'
        '<script setup>\n'
        "const n = 1;\n"
        "</script>\n",
        encoding="utf-8",
    )
    assert parse_file(str(js), str(tmp_path)).language == "javascript"

    jsx = tmp_path / "Jsx.vue"
    jsx.write_text(
        '<template><div/></template>\n'
        '<script lang="js">\n'
        "const n = 1;\n"
        "</script>\n",
        encoding="utf-8",
    )
    assert parse_file(str(jsx), str(tmp_path)).language == "javascript"


def test_parse_vue_sfc_multiple_script_blocks(tmp_path):
    """§5.4: a .vue with several <script> blocks — a lang="ts" block anywhere
    wins the dialect; both blocks' code is concatenated and parsed, and the
    line numbers of nodes in the later block are preserved (the join must not
    shift them)."""
    f = tmp_path / "Mix.vue"
    f.write_text(
        '<template><div/></template>\n'
        '<script>\n'
        "function plain() {\n"
        "  return 1;\n"
        "}\n"
        "</script>\n"
        '<script setup lang="ts">\n'
        "function greet(name: string): string {\n"
        "  return name;\n"
        "}\n"
        "</script>\n",
        encoding="utf-8",
    )
    pf = parse_file(str(f), str(tmp_path))
    assert pf.language == "typescript"  # lang="ts" block wins over plain
    lines = {n.qualified_name: n.start_line for n in pf.nodes}
    # plain block parses too; both land on their original .vue line numbers
    assert lines["Mix::plain"] == 3
    assert lines["Mix::greet"] == 8


def test_parse_vue_sfc_template_only(tmp_path):
    """A .vue with only a <template> has no script dialect — parsing falls back
    to the path default (typescript) without crashing."""
    f = tmp_path / "TemplateOnly.vue"
    f.write_text("<template><div/></template>\n", encoding="utf-8")
    pf = parse_file(str(f), str(tmp_path))
    assert pf.language == "typescript"


def test_parse_vue_sfc_lang_case_insensitive(tmp_path):
    """lang=\"TS\" (upper) is normalized to the typescript dialect like lang=\"ts\"."""
    f = tmp_path / "Upper.vue"
    f.write_text(
        '<template><div/></template>\n'
        '<script setup lang="TS">\n'
        "function greet(name: string): string { return name; }\n"
        "</script>\n",
        encoding="utf-8",
    )
    pf = parse_file(str(f), str(tmp_path))
    assert pf.language == "typescript"
    assert any(n.qualified_name == "Upper::greet" for n in pf.nodes)


def test_parse_ts_method_in_class():
    """Verify method_definition inside class_declaration gets kind='method'."""
    pf = _parse("auth.ts")

    method = next(
        n for n in pf.nodes
        if n.qualified_name == Q("ts.auth", "authenticate", "ts.auth::UserService")
    )
    assert method.kind == "method"
    assert method.parent_qname == Q("ts.auth", "UserService")
    assert method.signature.startswith("authenticate")


# ── CommonJS extraction (JS-M18/M19) ─────────────────────────────────


def test_parse_cjs_require_bindings(tmp_path):
    """`const m = require(...)`, destructured require, and bare side-effect
    require each key the import map; the require expression is also keyed as
    its own receiver-alias so `require('mod').foo()` binds."""
    f = tmp_path / "app.js"
    f.write_text(
        "const util = require('./util');\n"
        "const { helper, wrap: w } = require('./more');\n"
        "require('./side');\n",
        encoding="utf-8",
    )
    pf = parse_file(str(f), str(tmp_path))
    imps = {i.local_name: i for i in pf.imports}
    assert imps["util"].module == "util"
    assert imps["require('./util')"].module == "util"
    assert imps["helper"].module == "more" and imps["helper"].imported_name == "helper"
    assert imps["w"].module == "more" and imps["w"].imported_name == "wrap"
    assert imps["require('./side')"].module == "side"


def test_parse_cjs_export_bindings(tmp_path):
    """`exports.foo = bar`, `module.exports.foo = bar` and the object barrel
    each produce a re-export binding the resolver chains through."""
    f = tmp_path / "auth.js"
    f.write_text(
        "function login() {\n    return true;\n}\n"
        "const helper = require('./util');\n"
        "exports.login = login;\n"
        "module.exports.wrap = helper.run;\n"
        "module.exports = { login, run: helper.run };\n",
        encoding="utf-8",
    )
    pf = parse_file(str(f), str(tmp_path))
    imps = {i.local_name: i for i in pf.imports}
    assert imps["login"].module == "auth" and imps["login"].imported_name == "login"
    assert imps["wrap"].module == "helper" and imps["wrap"].imported_name == "run"
    assert imps["run"].module == "helper" and imps["run"].imported_name == "run"


def test_parse_cjs_default_export_function_expression(tmp_path):
    """`module.exports = function login(){}` names the default export qname."""
    f = tmp_path / "auth.js"
    f.write_text(
        "module.exports = function login() {\n    return true;\n}\n",
        encoding="utf-8",
    )
    assert parse_file(str(f), str(tmp_path)).default_export == Q("auth", "login")


def test_parse_cjs_default_export_identifier(tmp_path):
    """`module.exports = login` (reference to a local) names that local."""
    f = tmp_path / "auth.js"
    f.write_text(
        "function login() {\n    return true;\n}\n"
        "module.exports = login;\n",
        encoding="utf-8",
    )
    assert parse_file(str(f), str(tmp_path)).default_export == Q("auth", "login")


def test_parse_cjs_default_export_absent(tmp_path):
    """An object-literal `module.exports = {...}` has no single default qname."""
    f = tmp_path / "auth.js"
    f.write_text("module.exports = { a: 1 };\n", encoding="utf-8")
    assert parse_file(str(f), str(tmp_path)).default_export is None
