"""Impact contract — Java (Phase 1).

Mirrors the contract on Java sources: receiver-type binding resolves
``service.save(...)`` through a local variable's declared type, ``main``
matches ``entry_names``, JUnit ``@Test`` / ``*Test.java`` tag test methods,
and incremental sync == full rebuild.
"""

from __future__ import annotations

from code_review_ai.impact import get_impact
from code_review_ai.testimpact import get_test_impact

from helpers import assert_incremental_equals_rebuild, build_index, qname_set, norm


SERVICE = "com.example::Service.save"
CONTROLLER = "com.example::Controller.create"
APP_MAIN = "com.example::App.main"
ADMIN_MAIN = "com.example::Admin.main"
API_DIRECT = "com.example::Api.direct"

GRAPH = {
    "com/example/Service.java": (
        "package com.example;\n"
        "public class Service {\n"
        "    public String save(String name) {\n"
        "        return name;\n"
        "    }\n"
        "    public String unused() {\n"
        "        return \"\";\n"
        "    }\n"
        "}\n"
    ),
    "com/example/Controller.java": (
        "package com.example;\n"
        "public class Controller {\n"
        "    public String create(String name) {\n"
        "        Service service = new Service();\n"
        "        return service.save(name);\n"
        "    }\n"
        "}\n"
    ),
    "com/example/App.java": (
        "package com.example;\n"
        "public class App {\n"
        "    public static void main(String[] args) {\n"
        "        Controller controller = new Controller();\n"
        "        controller.create(\"x\");\n"
        "    }\n"
        "}\n"
    ),
    "com/example/Admin.java": (
        "package com.example;\n"
        "public class Admin {\n"
        "    public static void main(String[] args) {\n"
        "        Service service = new Service();\n"
        "        service.save(\"y\");\n"
        "    }\n"
        "}\n"
    ),
    "com/example/Api.java": (
        "package com.example;\n"
        "public class Api {\n"
        "    public String direct(String name) {\n"
        "        Service service = new Service();\n"
        "        return service.save(name);\n"
        "    }\n"
        "}\n"
    ),
    "com/example/ServiceTest.java": (
        "package com.example;\n"
        "public class ServiceTest {\n"
        "    @Test\n"
        "    public void testSave() {\n"
        "        Service service = new Service();\n"
        "        service.save(\"a\");\n"
        "    }\n"
        "}\n"
    ),
}


def test_java_caller_recall(tmp_path):
    """Receiver-type binding + cross-file/transitive/multiple-caller recall."""
    _, conn = build_index(tmp_path,GRAPH)
    res = get_impact(conn, [SERVICE])[0]
    assert res["found"]
    upstream = qname_set(res["upstream"])
    assert CONTROLLER in upstream
    assert ADMIN_MAIN in upstream
    assert APP_MAIN in upstream
    assert API_DIRECT in upstream
    assert {APP_MAIN, ADMIN_MAIN} <= set(res["affected_entries"])


def test_java_test_impact_direct(tmp_path):
    """A @Test method calling the changed symbol is selected."""
    _, conn = build_index(tmp_path,GRAPH)
    res = get_test_impact(conn, [SERVICE])
    assert res["not_found"] == []
    assert res["test_count"] == 1
    test = res["affected_tests"][0]
    assert test["qname"] == "com.example::ServiceTest.testSave"
    assert norm(test["file"]).endswith("com/example/ServiceTest.java")
    assert test["covers"] == [SERVICE]


def test_java_test_impact_transitive(tmp_path):
    """Two @Test methods — one direct, one via a helper — both reach the symbol."""
    _, conn = build_index(tmp_path,{
        "com/example/UserService.java": (
            "package com.example;\n"
            "public class UserService {\n"
            "    public String login(String user, String pw) {\n"
            "        return hashPw(pw);\n"
            "    }\n"
            "    public String hashPw(String pw) {\n"
            "        return pw;\n"
            "    }\n"
            "}\n"
        ),
        "com/example/UserServiceTest.java": (
            "package com.example;\n"
            "public class UserServiceTest {\n"
            "    @Test\n"
            "    public void testLogin() {\n"
            "        UserService userService = new UserService();\n"
            "        userService.login(\"u\", \"pw\");\n"
            "    }\n"
            "    @Test\n"
            "    public void testHash() {\n"
            "        UserService userService = new UserService();\n"
            "        userService.hashPw(\"pw\");\n"
            "    }\n"
            "}\n"
        ),
    })
    res = get_test_impact(conn, ["com.example::UserService.hashPw"])
    by_qname = {test["qname"]: test for test in res["affected_tests"]}
    assert set(by_qname) == {
        "com.example::UserServiceTest.testLogin",
        "com.example::UserServiceTest.testHash",
    }
    assert all(test["covers"] == ["com.example::UserService.hashPw"]
               for test in by_qname.values())


def test_java_not_found_and_no_coverage(tmp_path):
    """Uncovered symbols report cleanly; unknown symbols are flagged not_found."""
    _, conn = build_index(tmp_path,GRAPH)
    uncovered = get_test_impact(conn, ["com.example::Service.unused"])
    assert uncovered["affected_tests"] == []
    assert uncovered["not_found"] == []
    missing = get_test_impact(conn, ["com.example::Service.nope"])
    assert missing["not_found"] == ["com.example::Service.nope"]
    impact = get_impact(conn, ["com.example::Service.nope"])[0]
    assert impact["found"] is False


def test_java_cycle_and_diamond(tmp_path):
    """A diamond with a cycle terminates; the shared entry is reported once."""
    _, conn = build_index(tmp_path,{
        "com/example/Graph.java": (
            "package com.example;\n"
            "public class Graph {\n"
            "    public static void main(String[] args) {\n"
            "        b();\n"
            "        c();\n"
            "    }\n"
            "    public static void b() {\n"
            "        d();\n"
            "    }\n"
            "    public static void c() {\n"
            "        d();\n"
            "    }\n"
            "    public static void d() {\n"
            "        b();\n"
            "    }\n"
            "}\n"
        ),
    })
    res = get_impact(conn, ["com.example::Graph.d"])[0]
    assert res["found"]
    assert "com.example::Graph.main" in qname_set(res["upstream"])
    assert [node["qname"] for node in res["upstream"]].count(
        "com.example::Graph.main") == 1
    assert res["affected_entries"] == ["com.example::Graph.main"]


def test_java_incremental_equals_rebuild(tmp_path):
    """modify + add + delete: incremental sync leaves impact identical to rebuild."""
    cfg, conn = build_index(tmp_path,GRAPH)
    symbols = [SERVICE, "com.example::Service.saveV2", API_DIRECT]

    def apply_changes(repo):
        (repo / "com/example/Service.java").write_text(
            "package com.example;\n"
            "public class Service {\n"
            "    public String save(String name) {\n"
            "        return saveV2(name);\n"
            "    }\n"
            "    public String saveV2(String name) {\n"
            "        return name;\n"
            "    }\n"
            "    public String unused() {\n"
            "        return \"\";\n"
            "    }\n"
            "}\n", encoding="utf-8")
        (repo / "com/example/Extra.java").write_text(
            "package com.example;\n"
            "public class Extra {\n"
            "    public String extra(String name) {\n"
            "        Service service = new Service();\n"
            "        return service.save(name);\n"
            "    }\n"
            "}\n", encoding="utf-8")
        (repo / "com/example/Api.java").unlink()

    assert_incremental_equals_rebuild(cfg, conn, apply_changes, symbols)
