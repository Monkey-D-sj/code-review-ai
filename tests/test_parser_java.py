"""Tests for Java parsing."""
import os

from conftest import FIXTURES as FIX
from code_review_ai.parser import (parse_file, _lang_for_path, CALL_ATTRIBUTE,
                                   CALL_CONSTRUCT, CALL_SIMPLE)


def _parse(rel: str):
    return parse_file(os.path.join(FIX, "java", rel), FIX)


def test_lang_for_path_java():
    assert _lang_for_path("Foo.java")[0] == "java"


def test_module_from_package_declaration():
    pf = _parse("com/foo/UserService.java")
    assert pf.language == "java"
    assert pf.module_qname == "com.foo"


def test_parse_class_methods_constructor():
    pf = _parse("com/foo/UserService.java")
    kinds = {n.qualified_name: n.kind for n in pf.nodes}
    assert kinds["com.foo::UserService"] == "class"
    assert kinds["com.foo::UserService.authenticate"] == "method"
    assert kinds["com.foo::UserService.check"] == "method"
    assert kinds["com.foo::UserService.UserService"] == "method"  # constructor
    method = next(n for n in pf.nodes
                  if n.qualified_name == "com.foo::UserService.authenticate")
    assert method.parent_qname == "com.foo::UserService"


def test_parse_interface_is_class_kind():
    pf = _parse("com/foo/Auth.java")
    kinds = {n.qualified_name: n.kind for n in pf.nodes}
    assert kinds["com.foo::Auth"] == "class"
    assert kinds["com.foo::Auth.run"] == "method"


def test_parse_calls_method_invocation_and_construct():
    pf = _parse("com/foo/App.java")
    calls = {(c.target_expr, c.call_form) for c in pf.raw_calls}
    assert ("UserService", CALL_CONSTRUCT) in calls
    assert ("PasswordChecker.check", CALL_ATTRIBUTE) in calls
    assert ("svc.authenticate", CALL_ATTRIBUTE) in calls
    assert ("compute", CALL_SIMPLE) in calls
    for c in pf.raw_calls:
        assert c.source_qname == "com.foo::App.main"
        assert c.language == "java"


def test_parse_bare_and_dotted_call_targets():
    pf = _parse("com/foo/UserService.java")
    calls = {(c.target_expr, c.call_form) for c in pf.raw_calls}
    assert ("check", CALL_SIMPLE) in calls        # bare method call
    assert ("BaseService.boot", CALL_ATTRIBUTE) in calls


def test_module_fallback_path_when_no_package(tmp_path):
    src = tmp_path / "src" / "main" / "java" / "App.java"
    src.parent.mkdir(parents=True)
    src.write_text("class App {}\n", encoding="utf-8")
    pf = parse_file(str(src), str(tmp_path))
    assert pf.module_qname == "App"
