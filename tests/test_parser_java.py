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


def test_extract_imports_regular_wildcard_static(tmp_path):
    src = tmp_path / "S.java"
    src.write_text(
        "package a.b;\n"
        "import com.foo.UserService;\n"
        "import java.util.*;\n"
        "import static com.foo.util.Util.compute;\n"
        "class S { void m() { compute(); } }\n",
        encoding="utf-8",
    )
    pf = parse_file(str(src), str(tmp_path))
    imp = {i.local_name: i for i in pf.imports}
    assert imp["UserService"].module == "com.foo"
    assert imp["UserService"].imported_name == "UserService"
    assert imp["*"].module == "java.util"
    assert imp["*"].is_star is True
    static = imp["compute"]
    assert static.module == "com.foo.util::Util"  # 静态 import:module 是类 qname
    assert static.imported_name == "compute"


def test_extract_inherits_extends_implements():
    pf = _parse("com/foo/UserService.java")
    ih = {(i.relation, i.base_expr) for i in pf.inherits}
    assert ("extends", "BaseService") in ih
    assert ("implements", "Auth") in ih


def test_extract_inherits_interface_extends_type_list():
    # interface extends 走 extends_interfaces 字段(包着 type_list)——验证下钻
    pf = _parse("com/foo/Auth.java")
    ih = {(i.relation, i.base_expr) for i in pf.inherits}
    assert ("extends", "Marker") in ih


def test_java_var_types_collected(tmp_path):
    src = tmp_path / "OwnerController.java"
    src.write_text(
        "package com.example;\n"
        "class OwnerController {\n"
        "    private final OwnerRepository owners;\n"
        "    private String a, b;\n"
        "    public OwnerController(OwnerRepository clinicService) {\n"
        "        this.owners = clinicService;\n"
        "    }\n"
        "    public String show(int ownerId, Model model) {\n"
        "        Owner owner = new Owner();\n"
        "        var repo = owners;\n"
        "        return owners.findByLastName(ownerId, model);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    pf = parse_file(str(src), str(tmp_path))
    show = pf.var_types["com.example::OwnerController.show"]
    assert show["owners"] == "OwnerRepository"            # 类字段
    assert show["a"] == "String" and show["b"] == "String"  # 多 declarator
    assert show["model"] == "Model"                        # 参数
    assert show["owner"] == "Owner"                        # 局部变量
    assert "ownerId" not in show                           # 基元类型跳过
    assert "repo" not in show                              # var 跳过
    ctor = pf.var_types["com.example::OwnerController.OwnerController"]
    assert ctor["clinicService"] == "OwnerRepository"      # 构造器参数


def test_java_mockmvc_request_capture(tmp_path):
    src = tmp_path / "HomeControllerTests.java"
    src.write_text(
        "package com.example;\n"
        "class HomeControllerTests {\n"
        "    void listOk() {\n"
        "        mockMvc.perform(get(\"/owners?page=1\"));\n"
        "    }\n"
        "    void createOk() {\n"
        "        mockMvc.perform(post(\"/owners/new\").param(\"x\", \"y\"));\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    pf = parse_file(str(src), str(tmp_path))
    by = {n.qualified_name: n.mockmvc_requests for n in pf.nodes}
    assert by["com.example::HomeControllerTests.listOk"] == [("GET", "/owners?page=1")]
    # 链式 .param(...) 也能取到根请求构建器
    assert by["com.example::HomeControllerTests.createOk"] == [("POST", "/owners/new")]


def test_java_annotations_capture_mappings(tmp_path):
    src = tmp_path / "HomeController.java"
    src.write_text(
        "package com.example;\n"
        "@Controller\n"
        "class HomeController {\n"
        "    @GetMapping(\"/owners\")\n"
        "    public String list() { return null; }\n"
        "    @GetMapping(\"/owners/{ownerId}\")\n"
        "    public String show(int ownerId) { return null; }\n"
        "    @RequestMapping(value=\"/r\", method=RequestMethod.POST)\n"
        "    public String rm() { return null; }\n"
        "}\n",
        encoding="utf-8",
    )
    pf = parse_file(str(src), str(tmp_path))
    by = {n.qualified_name: n.mappings for n in pf.nodes}
    assert by["com.example::HomeController.list"] == [("GET", "/owners")]
    assert by["com.example::HomeController.show"] == [("GET", "/owners/{ownerId}")]
    # RequestMapping 带 method 元素 -> 具体方法;无则 ANY
    assert by["com.example::HomeController.rm"] == [("POST", "/r")]
    cls = next(n for n in pf.nodes if n.qualified_name == "com.example::HomeController")
    assert "Controller" in cls.decorators
