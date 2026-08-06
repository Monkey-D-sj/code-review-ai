"""Tests for Java call resolution."""
import os

from conftest import FIXTURES as FIX
from code_review_ai.parser import parse_file
from code_review_ai.resolver import resolve_edges


def _java_files():
    names = (
        "java/com/foo/UserService.java",
        "java/com/foo/App.java",
        "java/com/foo/PasswordChecker.java",
        "java/com/foo/BaseService.java",
        "java/com/foo/Auth.java",
        "java/com/foo/util/Util.java",
    )
    return [parse_file(os.path.join(FIX, name), FIX) for name in names]


def _resolve():
    files = _java_files()
    qnames = {n.qualified_name for f in files for n in f.nodes}
    return resolve_edges(files, qnames)


def test_new_construct_resolves_to_class():
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.foo::App.main", "com.foo::UserService", "resolved") in by


def test_import_class_attribute_call_resolves():
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.foo::App.main", "com.foo::PasswordChecker.check", "resolved") in by


def test_static_import_resolves():
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.foo::App.main", "com.foo.util::Util.compute", "resolved") in by


def test_bare_same_class_method_resolves():
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.foo::UserService.authenticate", "com.foo::UserService.check", "resolved") in by


def test_fqcn_resolves():
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.foo::UserService.authenticate", "com.foo::BaseService.boot", "resolved") in by


def test_local_var_method_stays_dynamic():
    edges = _resolve()
    dyn = [e for e in edges if e.target == "svc.authenticate"]
    assert dyn and dyn[0].resolution == "dynamic"


def test_inherit_edges_resolved():
    edges = _resolve()
    by = {(e.source, e.target, e.kind, e.resolution) for e in edges}
    assert ("com.foo::UserService", "com.foo::BaseService", "extends", "resolved") in by
    assert ("com.foo::UserService", "com.foo::Auth", "implements", "resolved") in by
