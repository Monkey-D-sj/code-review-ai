"""Tests for Java call resolution."""
import os

from conftest import FIXTURES as FIX
from code_review_ai.flow_builder import EdgeRow, NodeRow, build_flows
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


def test_new_construct_also_links_constructor():
    """The P1 audit gap: `new Foo()` must also edge to the real Java
    constructor (Foo.Foo), not to a never-existing Foo.__init__."""
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.foo::App.main", "com.foo::UserService.UserService",
            "resolved") in by


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


def _di_repo(tmp_path):
    files = []
    for name, body in (
        ("OwnerRepository.java",
         "package com.example;\n"
         "interface OwnerRepository {\n"
         "    Owner findByLastName(String lastName);\n"
         "}\n"),
        ("AuditService.java",
         "package com.example;\n"
         "class AuditService {}\n"),
        ("OwnerController.java",
         "package com.example;\n"
         "class OwnerController {\n"
         "    @Autowired\n"
         "    private OwnerRepository owners;\n"
         "    @SuppressWarnings(\"unused\")\n"
         "    private AuditService audit;\n"
         "    private String unused;\n"
         "    OwnerController(OwnerRepository owners) { this.owners = owners; }\n"
         "}\n"),
    ):
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        files.append(parse_file(str(path), str(tmp_path)))
    return files


def test_di_field_injection_resolves(tmp_path):
    """The P1 audit gap: @Autowired fields must yield a class -> dependency
    edge when the annotation matches di_annotations."""
    files = _di_repo(tmp_path)
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames, None, None, ["Autowired"])
    by = {(e.source, e.target, e.kind, e.resolution) for e in edges}
    assert ("com.example::OwnerController", "com.example::OwnerRepository",
            "call", "resolved") in by
    # an annotation not in di_annotations -> no DI edge for that field
    assert not any(e.source == "com.example::OwnerController"
                   and e.target == "com.example::AuditService" for e in edges)


def test_di_field_injection_off_without_matching_annotation(tmp_path):
    files = _di_repo(tmp_path)
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames)  # no di_annotations configured
    assert not any(e.target == "com.example::OwnerRepository"
                   and e.source == "com.example::OwnerController" for e in edges)


def test_di_constructor_param_resolves(tmp_path):
    """Constructor params are unconditional injection points (Spring injects
    single-constructor params unannotated); the edge chains off the
    constructor qname reached via `new Foo()`."""
    files = _di_repo(tmp_path)
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.example::OwnerController.OwnerController",
            "com.example::OwnerRepository", "resolved") in by


def _ctor_chain_repo(tmp_path):
    """A class whose constructor calls an internal method, instantiated from
    App.main - the full P1 chain: instantiation -> constructor -> the
    constructor's own callee."""
    files = []
    for name, body in (
        ("Service.java",
         "package com.example;\n"
         "class Service {\n"
         "    private int seed;\n"
         "    Service(int seed) {\n"
         "        this.seed = seed;\n"
         "        helper();\n"
         "    }\n"
         "    void helper() {}\n"
         "}\n"),
        ("App.java",
         "package com.example;\n"
         "class App {\n"
         "    void main() {\n"
         "        new Service(1);\n"
         "    }\n"
         "}\n"),
    ):
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        files.append(parse_file(str(path), str(tmp_path)))
    return files


def test_instantiation_to_constructor_internal_call_end_to_end(tmp_path):
    """The P1 audit gap, end to end: `new Foo()` must reach the real Java
    constructor (Foo.Foo), and the constructor's own internal calls must then
    land as resolved edges - and in the entry-point flow - so impact can walk
    instantiation -> constructor -> what the constructor calls. This is the
    '实例化 → 构造函数 → 构造函数内部调用' chain STATIC_ANALYSIS_COVERAGE §8 #3
    flagged as needing an end-to-end test."""
    files = _ctor_chain_repo(tmp_path)
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    # instantiation reaches the class and the real constructor (Class.Class)
    assert ("com.example::App.main", "com.example::Service", "resolved") in by
    assert ("com.example::App.main",
            "com.example::Service.Service", "resolved") in by
    # the constructor's internal bare call resolves to the same class's method
    assert ("com.example::Service.Service",
            "com.example::Service.helper", "resolved") in by

    # full chain shows up in App.main's flow: ctor + the ctor's callee
    all_nodes = sorted({n.qualified_name: n for f in files for n in f.nodes}.values(),
                       key=lambda n: n.qualified_name)
    id_of = {n.qualified_name: i for i, n in enumerate(all_nodes)}
    nodes = [NodeRow(id=id_of[n.qualified_name], qualified_name=n.qualified_name,
                     file_path=n.file_path, kind=n.kind) for n in all_nodes]
    flows = build_flows(nodes, [EdgeRow(e.source, e.target, e.resolution)
                                for e in edges], ["main"])
    flow = next(f for f in flows
                if f.entry_point_id == id_of["com.example::App.main"])
    reachable = {all_nodes[i].qualified_name for i in flow.path}
    assert "com.example::Service.Service" in reachable
    assert "com.example::Service.helper" in reachable
