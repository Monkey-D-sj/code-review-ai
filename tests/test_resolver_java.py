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


def test_var_constructor_inference_resolves_receiver(tmp_path):
    owner = tmp_path / "Owner.java"
    owner.write_text(
        "package com.example;\n"
        "class Owner { void run() {} }\n",
        encoding="utf-8",
    )
    caller = tmp_path / "Caller.java"
    caller.write_text(
        "package com.example;\n"
        "class Caller { void use() { var owner = new Owner(); owner.run(); } }\n",
        encoding="utf-8",
    )
    files = [parse_file(str(owner), str(tmp_path)),
             parse_file(str(caller), str(tmp_path))]
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.example::Caller.use", "com.example::Owner.run", "resolved") in by


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


def test_di_edges_carry_type_rule_provenance(tmp_path):
    """Phase 2: DI edges carry origin='type', the JAVA-F04/F05 rule id, and
    structured evidence (mechanism, dep_type, annotations)."""
    files = _di_repo(tmp_path)
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames, None, None, ["Autowired"])
    by_rule = {e.rule_id: e for e in edges
               if e.origin == "type" and e.rule_id is not None}
    field = by_rule["JAVA-F05"]
    assert field.source == "com.example::OwnerController"
    assert field.target == "com.example::OwnerRepository"
    assert field.evidence_json == {
        "mechanism": "field", "dep_type": "OwnerRepository",
        "annotations": ["Autowired"]}
    ctor = by_rule["JAVA-F04"]
    assert ctor.source == "com.example::OwnerController.OwnerController"
    assert ctor.evidence_json["mechanism"] == "constructor"
    assert ctor.evidence_json["dep_type"] == "OwnerRepository"


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


def _wildcard_repo(tmp_path):
    """A consumer importing two other packages via `import a.b.*;` — Widget in
    com.widgets, Base in com.base — so every Java resolution channel can be
    exercised: construct, bare-class-head attribute, declared-type receiver,
    DI field/ctor, and inheritance, all through the wildcard channel."""
    files = []
    for name, body in (
        ("com/widgets/Widget.java",
         "package com.widgets;\n"
         "public class Widget {\n"
         "    public void run() {}\n"
         "}\n"),
        ("com/base/Base.java",
         "package com.base;\n"
         "public class Base {\n"
         "    public void boot() {}\n"
         "}\n"),
        ("com/app/App.java",
         "package com.app;\n"
         "import com.widgets.*;\n"
         "import com.base.*;\n"
         "public class App extends Base {\n"
         "    @Autowired private Widget field;\n"
         "    App(Widget ctorDep) { this.field = ctorDep; }\n"
         "    public void main() {\n"
         "        Widget w = new Widget();\n"
         "        w.run();\n"
         "        Widget.run();\n"
         "        new Widget();\n"
         "    }\n"
         "}\n"),
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        files.append(parse_file(str(path), str(tmp_path)))
    return files


def _wildcard_edges(tmp_path, di=None):
    files = _wildcard_repo(tmp_path)
    qnames = {n.qualified_name for f in files for n in f.nodes}
    return resolve_edges(files, qnames, None, None, di)


def test_wildcard_import_construct_resolves(tmp_path):
    """JAVA-M04: `new Widget()` where Widget comes from `import com.widgets.*`."""
    edges = _wildcard_edges(tmp_path)
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.app::App.main", "com.widgets::Widget", "resolved") in by


def test_wildcard_import_class_head_attribute_resolves(tmp_path):
    """`Widget.run()` — the bare class head resolves via the wildcard import."""
    edges = _wildcard_edges(tmp_path)
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.app::App.main", "com.widgets::Widget.run", "resolved") in by


def test_wildcard_import_receiver_type_binding_resolves(tmp_path):
    """`Widget w = ...; w.run()` — the declared receiver type resolves via the
    wildcard import."""
    edges = _wildcard_edges(tmp_path)
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.app::App.main", "com.widgets::Widget.run", "resolved") in by


def test_wildcard_import_inherit_resolves(tmp_path):
    """COM-M05-java: `import com.base.*; class App extends Base` — the base
    comes from a wildcard import, not a named one."""
    edges = _wildcard_edges(tmp_path)
    by = {(e.source, e.target, e.kind, e.resolution) for e in edges}
    assert ("com.app::App", "com.base::Base", "extends", "resolved") in by


def test_wildcard_import_di_resolves(tmp_path):
    """DI field + ctor param of a wildcard-imported type yield resolved edges."""
    edges = _wildcard_edges(tmp_path, ["Autowired"])
    by = {(e.source, e.target, e.kind, e.resolution) for e in edges}
    assert ("com.app::App", "com.widgets::Widget", "call", "resolved") in by
    assert ("com.app::App.App", "com.widgets::Widget", "call", "resolved") in by


def test_wildcard_conflict_produces_candidates(tmp_path):
    """冲突负例: two wildcard-imported packages both define Widget — `new
    Widget()` becomes two candidate edges sharing one site_id, not a silently
    picked winner."""
    files = []
    for name, body in (
        ("a/pkg/Widget.java", "package a.pkg;\npublic class Widget {}\n"),
        ("b/pkg/Widget.java", "package b.pkg;\npublic class Widget {}\n"),
        ("app/App.java",
         "package app;\n"
         "import a.pkg.*;\n"
         "import b.pkg.*;\n"
         "public class App {\n"
         "    void main() {\n"
         "        Widget w = new Widget();\n"
         "    }\n"
         "}\n"),
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        files.append(parse_file(str(path), str(tmp_path)))
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames)
    candidates = [e for e in edges if e.resolution == "candidate"]
    assert {e.target for e in candidates} == {"a.pkg::Widget", "b.pkg::Widget"}
    assert {e.site_id for e in candidates} == {candidates[0].site_id}
    assert set(candidates[0].evidence_json["candidates"]) == {
        "a.pkg::Widget", "b.pkg::Widget"}


def test_wildcard_di_conflict_produces_candidates(tmp_path):
    """A DI dep resolved via two wildcard packages emits candidate DI edges."""
    files = []
    for name, body in (
        ("a/pkg/Repo.java", "package a.pkg;\npublic class Repo {}\n"),
        ("b/pkg/Repo.java", "package b.pkg;\npublic class Repo {}\n"),
        ("app/Ctl.java",
         "package app;\n"
         "import a.pkg.*;\n"
         "import b.pkg.*;\n"
         "public class Ctl {\n"
         "    @Autowired private Repo repo;\n"
         "}\n"),
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        files.append(parse_file(str(path), str(tmp_path)))
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames, None, None, ["Autowired"])
    candidates = [e for e in edges if e.resolution == "candidate"]
    assert {e.target for e in candidates} == {"a.pkg::Repo", "b.pkg::Repo"}
    assert {e.site_id for e in candidates} == {candidates[0].site_id}
    # the DI provenance survives on the candidate edges
    assert candidates[0].rule_id == "JAVA-F05"
    assert candidates[0].origin == "type"


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
