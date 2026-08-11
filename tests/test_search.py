import json
import shutil
import subprocess

from conftest import FIXTURES as FIX
from code_review_ai import update as upd
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.search import (deindex_fts, fts_search, index_fts,
                                   reindex_all)


class _FakeNode:
    """Minimal stand-in for a ParsedNode with just the fields index_fts reads."""

    def __init__(self, qualified_name, file_path, signature, decorators, end_line):
        self.qualified_name = qualified_name
        self.file_path = file_path
        self.signature = signature
        self.decorators = decorators
        self.end_line = end_line


def _seed(conn, *specs):
    """Insert (id, qname, kind, file, start, end, signature, decorators_list)
    rows into nodes, then index_fts over them. decorators_list is a Python list
    (the DB stores its json.dumps, index_fts re-dumps the same list)."""
    conn.executemany(
        "INSERT INTO nodes(id,qualified_name,kind,file_path,start_line,end_line,"
        "signature,decorators) VALUES(?,?,?,?,?,?,?,?)",
        [(s[0], s[1], s[2], s[3], s[4], s[5], s[6], json.dumps(s[7])) for s in specs])
    qname_to_id = {r["qualified_name"]: r["id"]
                   for r in conn.execute("SELECT id,qualified_name FROM nodes")}
    nodes = [_FakeNode(s[1], s[3], s[6], s[7], s[5]) for s in specs]
    index_fts(conn, nodes, qname_to_id)


def _conn(tmp_path):
    conn = connect(str(tmp_path / "fts.db"))
    init_schema(conn)
    return conn


def test_fts_prefix_expansion(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn,
          (1, "auth::login", "function", "auth.py", 6, 7, "def login(user, pw)", []),
          (2, "auth::login_user", "function", "auth.py", 9, 10, "def login_user(user)", []))
    hits = fts_search(conn, "login")
    assert {h["qname"] for h in hits} == {"auth::login", "auth::login_user"}
    hit = next(h for h in hits if h["qname"] == "auth::login")
    assert hit["kind"] == "function" and hit["file"] == "auth.py"
    assert hit["line"] == 6 and hit["end_line"] == 7
    assert hit["signature"] == "def login(user, pw)"
    assert "score" in hit


def test_fts_multi_word_and(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, (1, "auth::get_owner", "function", "auth.py", 1, 2,
                 "def get_owner(org)", []))
    assert fts_search(conn, "get owner")
    assert fts_search(conn, "get missing") == []


def test_like_infix_fallback(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, (1, "auth::UserService.authenticate", "method", "auth.py", 4, 5,
                 "def authenticate(user, pw)", []))
    # 'thent' 不是任何 FTS token 的前缀 -> 0 命中 -> LIKE 中缀兜底
    hits = fts_search(conn, "thent")
    assert [h["qname"] for h in hits] == ["auth::UserService.authenticate"]
    assert hits[0]["score"] is None


def test_glob_mode_backward_compat(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, (1, "auth::login", "function", "auth.py", 6, 7,
                 "def login(user, pw)", []))
    hits = fts_search(conn, "*login*")
    assert [h["qname"] for h in hits] == ["auth::login"]
    assert hits[0]["score"] is None


def test_bm25_sorts_more_relevant_first(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn,
          (1, "auth::login", "function", "auth.py", 6, 7, "def login(user, pw)", []),
          (2, "login::login", "function", "other.py", 1, 2, "def x()", []))
    # login::login 的 token 'login' 出现两次 -> bm25 更低 -> 排前
    hits = fts_search(conn, "login")
    assert hits[0]["qname"] == "login::login"


def test_limit_truncates(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn,
          (1, "util::hash_pw", "function", "util.py", 1, 2, "def hash_pw(pw)", []),
          (2, "util::helper", "function", "util.py", 5, 6, "def helper()", []),
          (3, "util::extra", "function", "util.py", 8, 9, "def extra()", []))
    assert len(fts_search(conn, "util", limit=2)) == 2


def test_deindex_removes_rows(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn,
          (1, "auth::login", "function", "auth.py", 6, 7, "def login(user, pw)", []),
          (2, "util::hash_pw", "function", "util.py", 1, 2, "def hash_pw(pw)", []))
    # deindex_fts 只清 FTS 索引；节点仍在 nodes 表时 LIKE 兜底仍会命中，
    # 所以直接断言 FTS 索引本身（不经过 fts_search 的 LIKE 兜底）。
    assert conn.execute(
        "SELECT count(*) FROM fts_nodes WHERE fts_nodes MATCH 'login'"
    ).fetchone()[0] == 1
    deindex_fts(conn, [1])
    assert conn.execute(
        "SELECT count(*) FROM fts_nodes WHERE fts_nodes MATCH 'login'"
    ).fetchone()[0] == 0
    # util::hash_pw 仍在索引
    assert conn.execute(
        "SELECT count(*) FROM fts_nodes WHERE fts_nodes MATCH 'hash_pw'"
    ).fetchone()[0] == 1


def test_reindex_all_rebuilds_from_nodes(tmp_path):
    conn = _conn(tmp_path)
    # 手插 nodes 但不走 index_fts —— 模拟索引缺失/陈旧；rebuild 命令从 nodes 内容整体重建
    conn.execute(
        "INSERT INTO nodes(id,qualified_name,kind,file_path,start_line,end_line,"
        "signature,decorators) VALUES(1,'auth::login','function','auth.py',6,7,"
        "'def login(user, pw)','[]')")
    reindex_all(conn)
    assert any(h["qname"] == "auth::login" for h in fts_search(conn, "login"))


def test_all_punctuation_query_returns_empty(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, (1, "auth::login", "function", "auth.py", 6, 7,
                 "def login(user, pw)", []))
    assert fts_search(conn, "::") == []
    assert fts_search(conn, "") == []


def test_rebuild_populates_fts(tmp_path):
    cfg = load_config(FIX)
    cfg.repo_path = FIX
    conn = connect(str(tmp_path / "r.db"))
    init_schema(conn)
    rebuild(cfg, conn)
    # 直接断言 FTS 索引本身（不经过 fts_search 的 LIKE 兜底）——否则空索引
    # 也会被 nodes 表的 LIKE 中缀兜底命中，掩盖 fts_nodes 从未填充的事实。
    assert conn.execute(
        "SELECT count(*) FROM fts_nodes WHERE fts_nodes MATCH 'login'"
    ).fetchone()[0] >= 2
    hits = fts_search(conn, "login")
    assert any(h["qname"] == "auth::login" for h in hits)
    assert any(h["qname"] == "ts.auth::login" for h in hits)
    # glob 模式向后兼容
    hits = fts_search(conn, "*login*")
    assert any(h["qname"] == "auth::login" for h in hits)
    # 二次 rebuild 不产生重复 FTS 行
    rebuild(cfg, conn)
    hits = fts_search(conn, "login")
    assert len([h for h in hits if h["qname"] == "auth::login"]) == 1


def _git_repo(tmp_path):
    """Copy the shared fixture into an isolated temp git repo."""
    repo = tmp_path / "repo"
    shutil.copytree(FIX, repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        cwd=repo, check=True)
    return repo


def test_incremental_add_indexes_new_symbol(tmp_path):
    repo = _git_repo(tmp_path)
    cfg = load_config(str(repo))
    cfg.repo_path = str(repo)
    conn = connect(str(tmp_path / "i.db"))
    init_schema(conn)
    rebuild(cfg, conn)
    repo.joinpath("util.py").write_text(
        "def hash_pw(pw):\n    return pw\n\n\ndef brand_new():\n    pass\n",
        encoding="utf-8")
    upd.update_nodes_edges(cfg, conn, ["util.py"])
    hits = fts_search(conn, "brand_new")
    assert any(h["qname"] == "util::brand_new" for h in hits)
    # 直接断言 FTS 索引本身——fts_search 有 nodes 表 LIKE 兜底，空索引也会被
    # 中缀命中，掩盖 fts_nodes 从未填充的事实（同 test_deindex_removes_rows 手法）。
    assert conn.execute(
        "SELECT count(*) FROM fts_nodes WHERE fts_nodes MATCH 'brand_new'"
    ).fetchone()[0] == 1


def test_incremental_delete_deindexes_symbol(tmp_path):
    repo = _git_repo(tmp_path)
    cfg = load_config(str(repo))
    cfg.repo_path = str(repo)
    conn = connect(str(tmp_path / "d.db"))
    init_schema(conn)
    rebuild(cfg, conn)
    auth_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM nodes WHERE file_path LIKE '%auth.py'")]
    assert auth_ids
    repo.joinpath("auth.py").unlink()
    upd.update_nodes_edges(cfg, conn, ["auth.py"])
    hits = fts_search(conn, "login")
    assert not any(h["qname"] == "auth::login" for h in hits)
    # ts/auth.ts 的 login 不受影响
    assert any(h["qname"] == "ts.auth::login" for h in hits)
    # 直接断言 FTS 索引本身——fts_search 的 MATCH 会 JOIN nodes 表，auth.py
    # 的内容行已删，陈旧 FTS 行会被 JOIN 过滤掉而发现不了泄漏；须直接查
    # fts_nodes（外部内容表 MATCH 只走索引，陈旧行仍会计数）。
    placeholders = ",".join("?" for _ in auth_ids)
    stale = conn.execute(
        "SELECT count(*) FROM fts_nodes WHERE fts_nodes MATCH 'login' "
        f"AND rowid IN ({placeholders})", auth_ids).fetchone()[0]
    assert stale == 0
