"""Tests for TypeScript/JavaScript parsing."""

import os

from conftest import FIXTURES as FIX, Q
from code_review_ai.parser import parse_file, _lang_for_path


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

    # Imports
    import_map = {i.local_name: i for i in pf.imports}
    assert import_map["login"].module == "./auth"
    assert import_map["login"].imported_name == "login"
    assert import_map["a"].module == "./auth"
    assert import_map["a"].imported_name is None  # namespace import
    assert import_map["hashPw"].module == "./util"

    # Calls
    calls = {(c.target_expr, c.call_form) for c in pf.raw_calls}
    assert ("login", "simple") in calls
    assert ("a.login", "attribute") in calls
    assert ("obj.run", "attribute") in calls

    # All calls inside main()
    for c in pf.raw_calls:
        assert c.source_qname == Q("ts.app", "main")


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
