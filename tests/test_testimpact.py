import subprocess

from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.impact import get_impact
from code_review_ai.testimpact import get_test_impact


def _build_repo(tmp_path):
    """Isolated repo: prod.py (login -> hash_pw, plus unused) + test_prod.py
    (test_login -> login, test_hash -> hash_pw). git-init'd so ls-files sees
    both files; test_prod.py is indexed (not excluded) and tagged is_test=1."""
    (tmp_path / "prod.py").write_text(
        "def hash_pw(pw):\n    return pw\n\n"
        "def login(user, pw):\n    return hash_pw(pw)\n\n"
        "def unused():\n    pass\n", encoding="utf-8")
    (tmp_path / "test_prod.py").write_text(
        "from prod import login, hash_pw\n\n"
        "def test_login():\n    login('u', 'p')\n\n"
        "def test_hash():\n    hash_pw('p')\n", encoding="utf-8")
    for cmd in (["git", "init"], ["git", "add", "-A"],
                ["git", "commit", "-m", "fixture"]):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
    cfg = load_config(str(tmp_path))
    cfg.db_path = str(tmp_path / "i.db")
    cfg.repo_path = str(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return conn


def _is_test(conn, qname) -> bool:
    row = conn.execute("SELECT is_test FROM nodes WHERE qualified_name=?",
                       (qname,)).fetchone()
    return bool(row and row["is_test"])


def _norm(p: str) -> str:
    return p.replace("\\", "/")


def test_is_test_tagging(tmp_path):
    conn = _build_repo(tmp_path)
    assert _is_test(conn, "test_prod::test_login")
    assert _is_test(conn, "test_prod::test_hash")
    assert not _is_test(conn, "prod::login")
    assert not _is_test(conn, "prod::hash_pw")
    assert not _is_test(conn, "prod::unused")


def test_test_impact_direct(tmp_path):
    conn = _build_repo(tmp_path)
    res = get_test_impact(conn, ["prod::login"])
    assert res["test_count"] == 1
    test = res["affected_tests"][0]
    assert test["qname"] == "test_prod::test_login"
    assert test["name"] == "test_login"
    assert _norm(test["file"]).endswith("test_prod.py")
    assert test["covers"] == ["prod::login"]
    assert any(_norm(p).endswith("test_prod.py") for p in res["test_files"])
    assert res["not_found"] == []


def test_test_impact_transitive(tmp_path):
    conn = _build_repo(tmp_path)
    # hash_pw is reached directly by test_hash AND transitively by test_login
    # (test_login -> login -> hash_pw) via the BFS flow -> both affected.
    res = get_test_impact(conn, ["prod::hash_pw"])
    qnames = {t["qname"] for t in res["affected_tests"]}
    assert qnames == {"test_prod::test_login", "test_prod::test_hash"}
    assert res["test_count"] == 2
    for test in res["affected_tests"]:
        assert test["covers"] == ["prod::hash_pw"]


def test_test_impact_no_coverage(tmp_path):
    conn = _build_repo(tmp_path)
    # prod::unused exists and is found, but no test reaches it. That is a
    # *complete* analysis — no breakpoint hides any edge — so no fallback.
    res = get_test_impact(conn, ["prod::unused"])
    assert res["affected_tests"] == []
    assert res["test_count"] == 0
    assert res["not_found"] == []
    assert res["complete"] is True
    assert res["fallback_recommended"] is False
    assert res["fallback_reasons"] == []


def test_test_impact_unknown_symbol(tmp_path):
    conn = _build_repo(tmp_path)
    res = get_test_impact(conn, ["prod::nope"])
    assert res["affected_tests"] == []
    assert res["not_found"] == ["prod::nope"]
    # an unknown changed symbol makes the result untrustworthy (guide §5.4)
    assert res["complete"] is False
    assert res["fallback_recommended"] is True
    assert "changed symbol not found in index" in res["fallback_reasons"]


def test_test_impact_multiple_changed_symbols_merge_covers(tmp_path):
    conn = _build_repo(tmp_path)
    res = get_test_impact(conn, ["prod::login", "prod::hash_pw"])
    by_q = {t["qname"]: t for t in res["affected_tests"]}
    # test_login reaches both login (direct) and hash_pw (transitive)
    assert by_q["test_prod::test_login"]["covers"] == ["prod::hash_pw", "prod::login"]
    # test_hash reaches only hash_pw
    assert by_q["test_prod::test_hash"]["covers"] == ["prod::hash_pw"]


def _build_breakpoint_repo(tmp_path):
    """prod.py has `dispatch` with a dynamic outgoing edge (handler.handle()).
    test_prod.py tests only `other`, which dispatch is not reachable from."""
    (tmp_path / "prod.py").write_text(
        "def dispatch(handler):\n"
        "    handler.handle()\n"
        "\n"
        "def other():\n"
        "    return 1\n", encoding="utf-8")
    (tmp_path / "test_prod.py").write_text(
        "from prod import other\n\n"
        "def test_other():\n"
        "    other()\n", encoding="utf-8")
    for cmd in (["git", "init"], ["git", "add", "-A"],
                ["git", "commit", "-m", "fixture"]):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
    cfg = load_config(str(tmp_path))
    cfg.db_path = str(tmp_path / "i.db")
    cfg.repo_path = str(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return conn


def _build_breakpoint_with_coverage_repo(tmp_path):
    """A test calls `dispatch` directly, so there IS coverage even though the
    symbol has a dynamic outgoing edge — no fallback should be advised."""
    (tmp_path / "prod.py").write_text(
        "def dispatch(handler):\n"
        "    handler.handle()\n", encoding="utf-8")
    (tmp_path / "test_prod.py").write_text(
        "from prod import dispatch\n\n"
        "def test_dispatch():\n"
        "    dispatch(None)\n", encoding="utf-8")
    for cmd in (["git", "init"], ["git", "add", "-A"],
                ["git", "commit", "-m", "fixture"]):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
    cfg = load_config(str(tmp_path))
    cfg.db_path = str(tmp_path / "i.db")
    cfg.repo_path = str(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return conn


def test_test_impact_breakpoint_no_coverage_suggests_fallback(tmp_path):
    """A zero-test result is only trustworthy when the graph around the changed
    symbol is fully resolved; a dynamic/candidate breakpoint forces fallback."""
    conn = _build_breakpoint_repo(tmp_path)
    res = get_test_impact(conn, ["prod::dispatch"])
    assert res["affected_tests"] == []
    assert res["test_count"] == 0
    assert res["not_found"] == []
    assert res["complete"] is False
    assert res["fallback_recommended"] is True
    assert any("dynamic/candidate" in r for r in res["fallback_reasons"])


def test_test_impact_breakpoint_with_coverage_no_fallback(tmp_path):
    """Coverage defeats the breakpoint: dispatch has a dynamic outgoing edge,
    but test_dispatch reaches it, so "run only these tests" is trustworthy."""
    conn = _build_breakpoint_with_coverage_repo(tmp_path)
    res = get_test_impact(conn, ["prod::dispatch"])
    assert res["test_count"] == 1
    assert res["not_found"] == []
    assert res["complete"] is True
    assert res["fallback_recommended"] is False
    assert res["fallback_reasons"] == []


def test_get_impact_exclude_vs_only(tmp_path):
    conn = _build_repo(tmp_path)
    # hash_pw is called by prod::login (business) and test_prod::test_hash (test)
    excluded = get_impact(conn, ["prod::hash_pw"], tests="exclude")[0]
    only_tests = get_impact(conn, ["prod::hash_pw"], tests="only")[0]
    excl_qn = {n["qname"] for n in excluded["upstream"]}
    only_qn = {n["qname"] for n in only_tests["upstream"]}
    assert "prod::login" in excl_qn and "test_prod::test_hash" not in excl_qn
    assert "test_prod::test_hash" in only_qn and "prod::login" not in only_qn


def test_get_impact_default_is_exclude(tmp_path):
    conn = _build_repo(tmp_path)
    default = get_impact(conn, ["prod::hash_pw"])[0]
    explicit = get_impact(conn, ["prod::hash_pw"], tests="exclude")[0]
    assert ({n["qname"] for n in default["upstream"]}
            == {n["qname"] for n in explicit["upstream"]})
    assert ({n["qname"] for n in default["upstream"]}
            .isdisjoint({"test_prod::test_hash", "test_prod::test_login"}))
