"""Impact contract — TypeScript (Phase 1).

Mirrors the Python contract on ESM/TS sources: relative named imports resolve
across files, ``main`` short-name entries anchor flows, ``*.test.ts`` files tag
test module nodes, and incremental sync == full rebuild.
"""

from __future__ import annotations

from code_review_ai.impact import get_impact
from code_review_ai.qname import join as Q
from code_review_ai.testimpact import get_test_impact

from helpers import assert_incremental_equals_rebuild, build_index, qname_set, norm


GRAPH = {
    "src/service.ts": (
        "export function find(userId: number): number {\n"
        "  return userId;\n"
        "}\n"
        "\n"
        "export function unused(): void {}\n"
    ),
    "src/controller.ts": (
        "import { find } from './service';\n"
        "\n"
        "export function get(userId: number): number {\n"
        "  return find(userId);\n"
        "}\n"
    ),
    "src/app.ts": (
        "import { get } from './controller';\n"
        "\n"
        "export function main(): number {\n"
        "  return get(1);\n"
        "}\n"
    ),
    "src/admin.ts": (
        "import { find } from './service';\n"
        "\n"
        "export function main(): number {\n"
        "  return find(1);\n"
        "}\n"
    ),
    "src/api.ts": (
        "import { find } from './service';\n"
        "\n"
        "export function direct(userId: number): number {\n"
        "  return find(userId);\n"
        "}\n"
    ),
    "src/service.test.ts": (
        "import { find } from './service';\n"
        "\n"
        "test('find', () => {\n"
        "  find(1);\n"
        "});\n"
    ),
}


def test_typescript_caller_recall(tmp_path):
    """Direct, cross-file, transitive and multiple-caller recall."""
    _, conn = build_index(tmp_path,GRAPH)
    res = get_impact(conn, [Q("service", "find")])[0]
    assert res["found"]
    upstream = qname_set(res["upstream"])
    assert Q("controller", "get") in upstream
    assert Q("admin", "main") in upstream
    assert Q("app", "main") in upstream
    assert Q("api", "direct") in upstream
    assert {Q("app", "main"), Q("admin", "main")} <= set(res["affected_entries"])


def test_typescript_test_impact_direct(tmp_path):
    """A test module calling the changed symbol is selected as the test node."""
    _, conn = build_index(tmp_path,GRAPH)
    res = get_test_impact(conn, [Q("service", "find")])
    assert res["not_found"] == []
    assert res["test_count"] == 1
    test = res["affected_tests"][0]
    assert test["qname"] == "service.test"
    assert norm(test["file"]).endswith("src/service.test.ts")
    assert test["covers"] == [Q("service", "find")]


def test_typescript_test_impact_transitive(tmp_path):
    """Named test functions (is_test via the file glob) anchor flows, so a test
    reaching the symbol through a helper is selected (2-hop) alongside a direct
    one. Module-scope `test('...', () => ...)` calls are only 1-hop via the
    edges fallback — transitive reachability needs a named function node."""
    _, conn = build_index(tmp_path,{
        "src/prod.ts": (
            "export function hashPw(pw: string): string {\n"
            "  return pw;\n"
            "}\n"
            "\n"
            "export function login(user: string, pw: string): string {\n"
            "  return hashPw(pw);\n"
            "}\n"
        ),
        "src/a.test.ts": (
            "import { login } from './prod';\n"
            "\n"
            "function testLogin(): void {\n"
            "  login('u', 'pw');\n"
            "}\n"
        ),
        "src/b.test.ts": (
            "import { hashPw } from './prod';\n"
            "\n"
            "function testHash(): void {\n"
            "  hashPw('pw');\n"
            "}\n"
        ),
    })
    res = get_test_impact(conn, [Q("prod", "hashPw")])
    by_qname = {test["qname"]: test for test in res["affected_tests"]}
    assert set(by_qname) == {"a.test::testLogin", "b.test::testHash"}
    assert all(test["covers"] == [Q("prod", "hashPw")] for test in by_qname.values())


def test_typescript_not_found_and_no_coverage(tmp_path):
    """Uncovered symbols report cleanly; unknown symbols are flagged not_found."""
    _, conn = build_index(tmp_path,GRAPH)
    uncovered = get_test_impact(conn, [Q("service", "unused")])
    assert uncovered["affected_tests"] == []
    assert uncovered["not_found"] == []
    missing = get_test_impact(conn, [Q("service", "nope")])
    assert missing["not_found"] == [Q("service", "nope")]
    impact = get_impact(conn, [Q("service", "nope")])[0]
    assert impact["found"] is False


def test_typescript_cycle_and_diamond(tmp_path):
    """A diamond with a cycle terminates; the shared entry is reported once."""
    _, conn = build_index(tmp_path,{
        "src/graph.ts": (
            "function main(): void {\n"
            "  b();\n"
            "  c();\n"
            "}\n"
            "\n"
            "function b(): void {\n"
            "  d();\n"
            "}\n"
            "\n"
            "function c(): void {\n"
            "  d();\n"
            "}\n"
            "\n"
            "function d(): void {\n"
            "  b();\n"
            "}\n"
        ),
    })
    res = get_impact(conn, [Q("graph", "d")])[0]
    assert res["found"]
    assert Q("graph", "main") in qname_set(res["upstream"])
    assert [node["qname"] for node in res["upstream"]].count(Q("graph", "main")) == 1
    assert res["affected_entries"] == [Q("graph", "main")]


def test_typescript_default_and_barrel_flow(tmp_path):
    """JS-M02/M07 through the impact layer: a default import and a `export *`
    barrel both land on the real callee, so flows reach the business entry."""
    _, conn = build_index(tmp_path, {
        "src/auth.ts": (
            "export default function login(): boolean {\n"
            "  return true;\n"
            "}\n"
            "export function helper(): boolean {\n"
            "  return true;\n"
            "}\n"
        ),
        "src/barrel.ts": (
            "export * from './auth';\n"
        ),
        "src/app.ts": (
            "import login from './auth';\n"
            "import { helper } from './barrel';\n"
            "export function main(): boolean {\n"
            "  return login() && helper();\n"
            "}\n"
        ),
    })
    # the default-imported callee reaches main directly
    default_impact = get_impact(conn, [Q("auth", "login")])[0]
    assert default_impact["found"]
    assert Q("app", "main") in qname_set(default_impact["upstream"])
    assert default_impact["affected_entries"] == [Q("app", "main")]
    # the barrel re-export reaches main too
    barrel_impact = get_impact(conn, [Q("auth", "helper")])[0]
    assert barrel_impact["found"]
    assert Q("app", "main") in qname_set(barrel_impact["upstream"])
    assert barrel_impact["affected_entries"] == [Q("app", "main")]


def test_typescript_incremental_equals_rebuild(tmp_path):
    """modify + add + delete: incremental sync leaves impact identical to rebuild."""
    cfg, conn = build_index(tmp_path,GRAPH)
    symbols = [Q("service", "find"), Q("service", "newHelper"), Q("api", "direct")]

    def apply_changes(repo):
        (repo / "src/service.ts").write_text(
            "export function find(userId: number): number {\n"
            "  return newHelper(userId);\n"
            "}\n"
            "\n"
            "export function newHelper(userId: number): number {\n"
            "  return userId;\n"
            "}\n"
            "\n"
            "export function unused(): void {}\n", encoding="utf-8")
        (repo / "src/extra.ts").write_text(
            "import { find } from './service';\n"
            "\n"
            "export function extra(): number {\n"
            "  return find(1);\n"
            "}\n", encoding="utf-8")
        (repo / "src/api.ts").unlink()

    assert_incremental_equals_rebuild(cfg, conn, apply_changes, symbols)
