import json

from conftest import FIXTURES as FIX, Q

from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.deadcode import find_dead_code


def _built(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "dc.db")
    cfg.repo_path = FIX
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return cfg, conn


def test_find_dead_code_fixture(tmp_path):
    cfg, conn = _built(tmp_path)
    payload = find_dead_code(conn, cfg)
    symbols = {s["qname"] for s in payload["symbols"]}
    # 无 caller 且非入口 -> 检出
    assert Q("util", "hash_pw") in symbols
    assert Q("util", "helper") in symbols
    assert Q("auth", "UserService") in symbols
    assert Q("auth", "authenticate", Q("auth", "UserService")) in symbols
    # 入口 / 有 caller -> 不检出
    assert Q("app", "main") not in symbols
    assert Q("auth", "login") not in symbols
    # 文件档：util.py（无入口、无人 import）；app.py（含入口）与 auth.py（被 import）不进
    file_qnames = {f["qname"] for f in payload["files"]}
    assert "util" in file_qnames
    assert "app" not in file_qnames
    assert "auth" not in file_qnames
    # rollup：util 文件聚合其死符号
    util_file = next(f for f in payload["files"] if f["qname"] == "util")
    assert util_file["symbol_count"] == 2
    assert set(util_file["symbols"]) == {Q("util", "hash_pw"), Q("util", "helper")}
    assert payload["meta"]["symbol_count"] == len(symbols)


def test_find_dead_code_excludes_entry_decorator(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "x.db")
    cfg.repo_path = str(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,file_path,start_line,signature,"
        "decorators,in_degree,is_test) VALUES(?,?,?,?,?,?,0,0)",
        (Q("web", "index"), "function", "web.py", 1,
         "def index():", json.dumps(["app.route"])))
    conn.commit()
    payload = find_dead_code(conn, cfg)
    assert all(s["qname"] != Q("web", "index") for s in payload["symbols"])


def test_find_dead_code_excludes_spring_mapping(tmp_path):
    """A Spring @GetMapping handler has no static callers (in_degree=0) but the
    default entry_decorators now include the mapping annotations, so it must
    not be reported as a dead-code candidate."""
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "x.db")
    cfg.repo_path = str(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,file_path,start_line,signature,"
        "decorators,in_degree,is_test) VALUES(?,?,?,?,?,?,0,0)",
        (Q("com.example", "list"), "method", "HomeController.java", 1,
         "public String list()", json.dumps(["GetMapping"])))
    conn.commit()
    payload = find_dead_code(conn, cfg)
    assert all(s["qname"] != Q("com.example", "list")
               for s in payload["symbols"])


def test_find_dead_code_excludes_test_and_entry_name(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "x.db")
    cfg.repo_path = str(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,file_path,start_line,signature,"
        "decorators,in_degree,is_test) VALUES(?,?,?,?,?,?,0,?)",
        (Q("t", "test_login"), "function", "test_t.py", 1,
         "def test_login():", "[]", 1))
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,file_path,start_line,signature,"
        "decorators,in_degree,is_test) VALUES(?,?,?,?,?,?,0,0)",
        (Q("t", "main"), "function", "t.py", 1, "def main():", "[]"))
    conn.commit()
    payload = find_dead_code(conn, cfg)
    assert all(s["qname"] != Q("t", "test_login") for s in payload["symbols"])
    assert all(s["qname"] != Q("t", "main") for s in payload["symbols"])


def test_find_dead_code_tolerates_bad_decorators_json(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "x.db")
    cfg.repo_path = str(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,file_path,start_line,signature,"
        "decorators,in_degree,is_test) VALUES(?,?,?,?,?,?,0,0)",
        (Q("m", "f"), "function", "m.py", 1, "def f():", "not-json{"))
    conn.commit()
    payload = find_dead_code(conn, cfg)
    record = next(s for s in payload["symbols"] if s["qname"] == Q("m", "f"))
    assert record["decorators"] == []


def test_find_dead_code_file_with_internal_callers(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "x.db")
    cfg.repo_path = str(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,file_path,start_line,signature,"
        "decorators,in_degree,is_test) VALUES(?,?,?,?,?,?,?,0)",
        ("m", "module", "m.py", 1, "", "[]", 0))
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,file_path,start_line,signature,"
        "decorators,in_degree,is_test) VALUES(?,?,?,?,?,?,?,0)",
        (Q("m", "a"), "function", "m.py", 1, "def a():", "[]", 1))
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,file_path,start_line,signature,"
        "decorators,in_degree,is_test) VALUES(?,?,?,?,?,?,?,0)",
        (Q("m", "b"), "function", "m.py", 1, "def b():", "[]", 1))
    conn.commit()
    payload = find_dead_code(conn, cfg)
    # nothing imports m.py, and it holds no entry/test -> file IS a candidate
    file_qnames = {f["qname"] for f in payload["files"]}
    assert "m" in file_qnames
    # but its functions call each other (in_degree > 0) -> no dead-symbol rollup
    m_file = next(f for f in payload["files"] if f["qname"] == "m")
    assert m_file["symbol_count"] == 0
    symbols = {s["qname"] for s in payload["symbols"]}
    assert Q("m", "a") not in symbols
    assert Q("m", "b") not in symbols
