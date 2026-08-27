"""Impact contract — Python (Phase 1).

End-to-end contract (parse -> resolve -> rebuild -> get_impact /
get_test_impact): direct + cross-file + transitive calls, multiple callers,
cycle/diamond, direct + transitive test impact, not-found / no coverage, diff
modify/add/delete, and incremental sync == full rebuild.
"""

from __future__ import annotations

from code_review_ai.impact import get_impact
from code_review_ai.qname import join as Q
from code_review_ai.testimpact import get_test_impact

from helpers import assert_incremental_equals_rebuild, build_index, qname_set


GRAPH = {
    "service.py": (
        "def find(user_id):\n"
        "    return user_id\n"
        "\n"
        "\n"
        "def unused():\n"
        "    pass\n"
    ),
    "controller.py": (
        "from service import find\n"
        "\n"
        "def get(user_id):\n"
        "    return find(user_id)\n"
    ),
    "app.py": (
        "from controller import get\n"
        "\n"
        "def main():\n"
        "    return get(1)\n"
    ),
    "admin.py": (
        "from service import find\n"
        "\n"
        "def main():\n"
        "    return find(1)\n"
    ),
    "api.py": (
        "from service import find\n"
        "\n"
        "def direct(user_id):\n"
        "    return find(user_id)\n"
    ),
    "test_service.py": (
        "from service import find\n"
        "\n"
        "def test_find():\n"
        "    assert find(1) == 1\n"
    ),
}


def test_python_caller_recall(tmp_path):
    """Direct, cross-file, transitive and multiple-caller recall on a changed symbol."""
    _, conn = build_index(tmp_path,GRAPH)
    res = get_impact(conn, [Q("service", "find")])[0]
    assert res["found"]
    upstream = qname_set(res["upstream"])
    # cross-file direct callers (one of them an entry point)
    assert Q("controller", "get") in upstream
    assert Q("admin", "main") in upstream
    # transitive caller (app::main -> controller::get -> service::find)
    assert Q("app", "main") in upstream
    # every direct caller of the changed symbol is recalled, incl. off-entry api::direct
    assert Q("api", "direct") in upstream
    # every entry point that (transitively) reaches the symbol is affected
    assert {Q("app", "main"), Q("admin", "main")} <= set(res["affected_entries"])


def test_python_max_level_returns_direct_and_depth(tmp_path):
    """max_level=1 keeps only direct neighbors plus a depth summary."""
    _, conn = build_index(tmp_path, GRAPH)
    res = get_impact(conn, [Q("service", "find")], max_level=1)[0]
    assert res["found"]
    upstream = qname_set(res["upstream"])
    # direct callers only: controller::get, admin::main, api::direct
    assert Q("controller", "get") in upstream
    assert Q("admin", "main") in upstream
    assert Q("api", "direct") in upstream
    # app::main is 2 hops away (app -> controller -> service) and is dropped
    assert Q("app", "main") not in upstream
    # depth tells the reviewer the deeper chain still exists
    assert res["depth"]["upstream_max"] == 2
    assert res["depth"]["upstream_total"] == 4
    # every returned node is a direct neighbor (level 1)
    assert {n["level"] for n in res["upstream"]} == {1}
    # default (max_level=0) still returns the full transitive closure
    full = get_impact(conn, [Q("service", "find")])[0]
    assert Q("app", "main") in qname_set(full["upstream"])
    assert "depth" not in full


def test_python_test_impact_direct(tmp_path):
    """A test calling the changed symbol directly is selected."""
    _, conn = build_index(tmp_path,GRAPH)
    res = get_test_impact(conn, [Q("service", "find")])
    assert res["not_found"] == []
    assert res["test_count"] == 1
    test = res["affected_tests"][0]
    assert test["qname"] == Q("test_service", "test_find")
    assert test["file"].replace("\\", "/").endswith("test_service.py")
    assert test["covers"] == [Q("service", "find")]


def test_python_test_impact_transitive(tmp_path):
    """A test reaching the symbol through a helper is selected (2-hop)."""
    _, conn = build_index(tmp_path,{
        "prod.py": (
            "def hash_pw(pw):\n"
            "    return pw\n"
            "\n"
            "\n"
            "def login(user, pw):\n"
            "    return hash_pw(pw)\n"
        ),
        "test_prod.py": (
            "from prod import login, hash_pw\n"
            "\n"
            "def test_login():\n"
            "    login('u', 'p')\n"
            "\n"
            "\n"
            "def test_hash():\n"
            "    hash_pw('p')\n"
        ),
    })
    res = get_test_impact(conn, [Q("prod", "hash_pw")])
    qns = {test["qname"] for test in res["affected_tests"]}
    assert qns == {Q("test_prod", "test_login"), Q("test_prod", "test_hash")}


def test_python_not_found_and_no_coverage(tmp_path):
    """Uncovered symbols report cleanly; unknown symbols are flagged not_found."""
    _, conn = build_index(tmp_path,GRAPH)
    # exists but nothing covers it
    uncovered = get_test_impact(conn, [Q("service", "unused")])
    assert uncovered["affected_tests"] == []
    assert uncovered["not_found"] == []
    # unknown symbol
    missing = get_test_impact(conn, [Q("service", "nope")])
    assert missing["not_found"] == [Q("service", "nope")]
    impact = get_impact(conn, [Q("service", "nope")])[0]
    assert impact["found"] is False


def test_python_cycle_and_diamond(tmp_path):
    """A diamond with a cycle terminates; the shared entry is reported once."""
    _, conn = build_index(tmp_path,{
        "graph.py": (
            "def main():\n"
            "    b()\n"
            "    c()\n"
            "\n"
            "\n"
            "def b():\n"
            "    d()\n"
            "\n"
            "\n"
            "def c():\n"
            "    d()\n"
            "\n"
            "\n"
            "def d():\n"
            "    b()\n"
        ),
    })
    res = get_impact(conn, [Q("graph", "d")])[0]
    assert res["found"]
    assert Q("graph", "main") in qname_set(res["upstream"])
    assert [node["qname"] for node in res["upstream"]].count(Q("graph", "main")) == 1
    assert res["affected_entries"] == [Q("graph", "main")]


def test_python_incremental_equals_rebuild(tmp_path):
    """modify + add + delete: incremental sync leaves impact identical to rebuild."""
    cfg, conn = build_index(tmp_path,GRAPH)
    symbols = [Q("service", "find"), Q("service", "new_helper"), Q("api", "direct")]

    def apply_changes(repo):
        # modify: service.find now delegates to a new helper
        (repo / "service.py").write_text(
            "def find(user_id):\n"
            "    return new_helper(user_id)\n"
            "\n"
            "\n"
            "def new_helper(user_id):\n"
            "    return user_id\n"
            "\n"
            "\n"
            "def unused():\n"
            "    pass\n", encoding="utf-8")
        # add: a new module calling find
        (repo / "extra.py").write_text(
            "from service import find\n"
            "\n"
            "def extra():\n"
            "    return find(1)\n", encoding="utf-8")
        # delete: api.py goes away entirely
        (repo / "api.py").unlink()

    assert_incremental_equals_rebuild(cfg, conn, apply_changes, symbols)
