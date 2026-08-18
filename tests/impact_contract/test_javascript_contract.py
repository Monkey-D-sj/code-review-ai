"""Impact contract — JavaScript (Phase 1).

The JS contract runs on real ``.js`` ESM sources (the guide explicitly forbids
reusing ``.ts`` for it), plus a dialect check that ``.mjs`` / ``.cjs`` files
are indexed as ``javascript`` modules.
"""

from __future__ import annotations

from code_review_ai.impact import get_impact
from code_review_ai.qname import join as Q
from code_review_ai.testimpact import get_test_impact

from helpers import assert_incremental_equals_rebuild, build_index, qname_set, norm


GRAPH = {
    "src/service.js": (
        "export function find(userId) {\n"
        "  return userId;\n"
        "}\n"
        "\n"
        "export function unused() {}\n"
    ),
    "src/controller.js": (
        "import { find } from './service.js';\n"
        "\n"
        "export function get(userId) {\n"
        "  return find(userId);\n"
        "}\n"
    ),
    "src/app.js": (
        "import { get } from './controller.js';\n"
        "\n"
        "export function main() {\n"
        "  return get(1);\n"
        "}\n"
    ),
    "src/admin.js": (
        "import { find } from './service.js';\n"
        "\n"
        "export function main() {\n"
        "  return find(1);\n"
        "}\n"
    ),
    "src/api.js": (
        "import { find } from './service.js';\n"
        "\n"
        "export function direct(userId) {\n"
        "  return find(userId);\n"
        "}\n"
    ),
    "src/service.test.js": (
        "import { find } from './service.js';\n"
        "\n"
        "test('find', () => {\n"
        "  find(1);\n"
        "});\n"
    ),
}


def test_javascript_caller_recall(tmp_path):
    """Direct, cross-file, transitive and multiple-caller recall on .js sources."""
    _, conn = build_index(tmp_path,GRAPH)
    res = get_impact(conn, [Q("service", "find")])[0]
    assert res["found"]
    upstream = qname_set(res["upstream"])
    assert Q("controller", "get") in upstream
    assert Q("admin", "main") in upstream
    assert Q("app", "main") in upstream
    assert Q("api", "direct") in upstream
    assert {Q("app", "main"), Q("admin", "main")} <= set(res["affected_entries"])


def test_javascript_test_impact_direct(tmp_path):
    """A module-scope test calling the changed symbol is selected (edges fallback)."""
    _, conn = build_index(tmp_path,GRAPH)
    res = get_test_impact(conn, [Q("service", "find")])
    assert res["not_found"] == []
    assert res["test_count"] == 1
    test = res["affected_tests"][0]
    assert test["qname"] == "service.test"
    assert norm(test["file"]).endswith("src/service.test.js")
    assert test["covers"] == [Q("service", "find")]


def test_javascript_test_impact_transitive(tmp_path):
    """Named test functions anchor flows, so a 2-hop test is selected too."""
    _, conn = build_index(tmp_path,{
        "src/prod.js": (
            "export function hashPw(pw) {\n"
            "  return pw;\n"
            "}\n"
            "\n"
            "export function login(user, pw) {\n"
            "  return hashPw(pw);\n"
            "}\n"
        ),
        "src/a.test.js": (
            "import { login } from './prod.js';\n"
            "\n"
            "function testLogin() {\n"
            "  login('u', 'pw');\n"
            "}\n"
        ),
        "src/b.test.js": (
            "import { hashPw } from './prod.js';\n"
            "\n"
            "function testHash() {\n"
            "  hashPw('pw');\n"
            "}\n"
        ),
    })
    res = get_test_impact(conn, [Q("prod", "hashPw")])
    by_qname = {test["qname"]: test for test in res["affected_tests"]}
    assert set(by_qname) == {"a.test::testLogin", "b.test::testHash"}
    assert all(test["covers"] == [Q("prod", "hashPw")] for test in by_qname.values())


def test_javascript_not_found_and_no_coverage(tmp_path):
    """Uncovered symbols report cleanly; unknown symbols are flagged not_found."""
    _, conn = build_index(tmp_path,GRAPH)
    uncovered = get_test_impact(conn, [Q("service", "unused")])
    assert uncovered["affected_tests"] == []
    assert uncovered["not_found"] == []
    missing = get_test_impact(conn, [Q("service", "nope")])
    assert missing["not_found"] == [Q("service", "nope")]
    impact = get_impact(conn, [Q("service", "nope")])[0]
    assert impact["found"] is False


def test_javascript_cycle_and_diamond(tmp_path):
    """A diamond with a cycle terminates; the shared entry is reported once."""
    _, conn = build_index(tmp_path,{
        "src/graph.js": (
            "function main() {\n"
            "  b();\n"
            "  c();\n"
            "}\n"
            "\n"
            "function b() {\n"
            "  d();\n"
            "}\n"
            "\n"
            "function c() {\n"
            "  d();\n"
            "}\n"
            "\n"
            "function d() {\n"
            "  b();\n"
            "}\n"
        ),
    })
    res = get_impact(conn, [Q("graph", "d")])[0]
    assert res["found"]
    assert Q("graph", "main") in qname_set(res["upstream"])
    assert [node["qname"] for node in res["upstream"]].count(Q("graph", "main")) == 1
    assert res["affected_entries"] == [Q("graph", "main")]


def test_javascript_mjs_and_cjs_dialects(tmp_path):
    """Both dialects index to javascript-language module nodes (CJS resolution
    is intentionally out of scope today — only module presence is asserted)."""
    _, conn = build_index(tmp_path,{
        "src/mod.mjs": (
            "export function greet(name) {\n"
            "  return 'hi ' + name;\n"
            "}\n"
        ),
        "src/legacy.cjs": (
            "module.exports = function greet(name) {\n"
            "  return name;\n"
            "};\n"
        ),
    })
    modules = {row["qualified_name"]: row["language"] for row in conn.execute(
        "SELECT qualified_name, language FROM nodes WHERE kind = 'module'")}
    assert modules.get("mod") == "javascript"
    assert modules.get("legacy") == "javascript"


def test_javascript_incremental_equals_rebuild(tmp_path):
    """modify + add + delete: incremental sync leaves impact identical to rebuild."""
    cfg, conn = build_index(tmp_path,GRAPH)
    symbols = [Q("service", "find"), Q("service", "newHelper"), Q("api", "direct")]

    def apply_changes(repo):
        (repo / "src/service.js").write_text(
            "export function find(userId) {\n"
            "  return newHelper(userId);\n"
            "}\n"
            "\n"
            "export function newHelper(userId) {\n"
            "  return userId;\n"
            "}\n"
            "\n"
            "export function unused() {}\n", encoding="utf-8")
        (repo / "src/extra.js").write_text(
            "import { find } from './service.js';\n"
            "\n"
            "export function extra() {\n"
            "  return find(1);\n"
            "}\n", encoding="utf-8")
        (repo / "src/api.js").unlink()

    assert_incremental_equals_rebuild(cfg, conn, apply_changes, symbols)
