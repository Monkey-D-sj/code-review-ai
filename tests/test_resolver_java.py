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


def test_local_var_method_resolves_via_declared_type():
    """svc is `UserService svc = ...`; type binding resolves svc.authenticate."""
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.foo::App.main", "com.foo::UserService.authenticate", "resolved") in by


def test_inherit_edges_resolved():
    edges = _resolve()
    by = {(e.source, e.target, e.kind, e.resolution) for e in edges}
    assert ("com.foo::UserService", "com.foo::BaseService", "extends", "resolved") in by
    assert ("com.foo::UserService", "com.foo::Auth", "implements", "resolved") in by


def _type_binding_repo(tmp_path):
    files = []
    for name, body in (
        ("Owner.java",
         "package com.example;\nclass Owner {}\n"),
        ("OwnerRepository.java",
         "package com.example;\n"
         "interface OwnerRepository {\n"
         "    Owner findByLastName(String lastName);\n"
         "}\n"),
        ("OwnerController.java",
         "package com.example;\n"
         "class OwnerController {\n"
         "    private final OwnerRepository owners;\n"
         "    OwnerController(OwnerRepository owners) { this.owners = owners; }\n"
         "    Owner find(String name) { return owners.findByLastName(name); }\n"
         "    Owner findThis(String name) { return this.owners.findByLastName(name); }\n"
         "    void unknown() { mysteryObj.call(); }\n"
         "}\n"),
    ):
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        files.append(parse_file(str(path), str(tmp_path)))
    return files


def test_receiver_type_binding_resolves(tmp_path):
    files = _type_binding_repo(tmp_path)
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    # 字段接收者 owners -> OwnerRepository.findByLastName
    assert ("com.example::OwnerController.find",
            "com.example::OwnerRepository.findByLastName", "resolved") in by
    # this. 前缀
    assert ("com.example::OwnerController.findThis",
            "com.example::OwnerRepository.findByLastName", "resolved") in by
    # 未知接收者仍 dynamic
    dyn = [e for e in edges if e.target == "mysteryObj.call"]
    assert dyn and dyn[0].resolution == "dynamic"
