import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import FIXTURES
from code_review_ai.config import load_config
from code_review_ai.changes import current_head
from code_review_ai.db import connect, init_schema
from code_review_ai import update as upd
from code_review_ai import manifest as mf


def _git_repo(tmp_path):
    """Copy the shared fixture into an isolated temp git repo."""
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES, repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        cwd=repo, check=True)
    cfg = load_config(str(repo))
    cfg.repo_path = str(repo)
    cfg.db_path = str(tmp_path / "index.db")
    return repo, cfg


def test_changed_files_detects_modify_add_delete(tmp_path):
    repo, cfg = _git_repo(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    import os
    from code_review_ai.parser import list_source_files, SOURCE_GLOBS
    # 灌入当前树作为初始 manifest
    rels = list_source_files(cfg.repo_path, SOURCE_GLOBS)
    def _entry(rel):
        abs_path = os.path.join(cfg.repo_path, rel)
        st = os.stat(abs_path)
        return (st.st_mtime, st.st_size, mf.hash_file(abs_path))
    mf.update(conn, {rel: _entry(rel) for rel in rels})
    # modify：改 util.py 内容
    p = repo / "util.py"
    p.write_text(p.read_text(encoding="utf-8") + "\ndef new_helper():\n    pass\n",
                 encoding="utf-8")
    changed, added, deleted = upd.changed_files(cfg, conn)
    assert "util.py" in changed and "app.py" not in changed
    # touch-only：mtime 变但内容不变 -> hash 判定未变（避免误报）
    app = repo / "app.py"
    st = app.stat()
    os.utime(app, (st.st_atime + 5, st.st_mtime + 5))
    changed, added, deleted = upd.changed_files(cfg, conn)
    assert "app.py" not in changed
    # delete：删 auth.py（仍在 git 索引中）
    (repo / "auth.py").unlink()
    changed, added, deleted = upd.changed_files(cfg, conn)
    assert "auth.py" in deleted
    # add：新建 extra.py
    (repo / "extra.py").write_text("def x():\n    pass\n", encoding="utf-8")
    changed, added, deleted = upd.changed_files(cfg, conn)
    assert "extra.py" in added
    assert upd.needs_nodes_update(cfg, conn) is True


def test_repair_resolutions_flips_by_global_set(tmp_path):
    repo, cfg = _git_repo(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.execute("INSERT INTO nodes(qualified_name,kind) VALUES('m::User','function')")
    # 类型一 unresolved 边：target 是含 :: 的 qname
    conn.execute(
        "INSERT INTO edges(source,target,kind,resolution) VALUES('f','m::User','call','unresolved')")
    # 类型二 unresolved 边：target 无 ::（裸名）即使命中单段 module 也不碰
    conn.execute(
        "INSERT INTO edges(source,target,kind,resolution) VALUES('g','login','call','unresolved')")
    # dynamic 边不碰
    conn.execute(
        "INSERT INTO edges(source,target,kind,resolution) VALUES('h','a.login','call','dynamic')")
    # 反向：resolved 边 target 已不在全集 -> 翻 unresolved
    conn.execute(
        "INSERT INTO edges(source,target,kind,resolution) VALUES('i','gone::x','call','resolved')")
    # import 边：target 命中 module -> resolved
    conn.execute("INSERT INTO nodes(qualified_name,kind) VALUES('login','module')")
    conn.execute(
        "INSERT INTO edges(source,target,kind,resolution) VALUES('a','login','import','unresolved')")

    flipped = upd.repair_resolutions(conn)

    def label_of(source):
        return conn.execute(
            "SELECT resolution FROM edges WHERE source=?", (source,)
        ).fetchone()[0]

    assert label_of("f") == "resolved"        # 类型一：新增方向修复
    assert label_of("g") == "unresolved"      # 类型二裸名（无 ::）不动
    assert label_of("h") == "dynamic"         # dynamic 不动
    assert label_of("i") == "unresolved"      # 反向修复（target 已不在全集）
    assert label_of("a") == "resolved"        # import 边：target 命中 module
    assert flipped == 3


def _init_and_build(cfg, conn):
    init_schema(conn)
    from code_review_ai.indexer import rebuild
    rebuild(cfg, conn)


def test_update_nodes_edges_touches_only_changed(tmp_path, monkeypatch):
    repo, cfg = _git_repo(tmp_path)
    conn = connect(cfg.db_path)
    _init_and_build(cfg, conn)
    flows_before = conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0]

    # 改 util.py 内容，走 watcher hint 路径（changed_paths）-> 只 re-parse util.py
    calls = {"n": 0}
    real_parse = upd.parse_file

    def counting(*a, **k):
        calls["n"] += 1
        return real_parse(*a, **k)

    monkeypatch.setattr(upd, "parse_file", counting)
    p = repo / "util.py"
    p.write_text(p.read_text(encoding="utf-8") + "\ndef new_helper():\n    pass\n",
                 encoding="utf-8")
    result = upd.update_nodes_edges(cfg, conn, ["util.py"])
    assert calls["n"] == 1                          # 只 parse 了 util.py
    assert result["parsed_files"] == 1
    # flows 表未动（nodes/edges 更新不触碰 flows）
    assert conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0] == flows_before
    # 新符号已入库
    assert conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE qualified_name='util::new_helper'"
    ).fetchone()[0] == 1


def test_update_nodes_edges_deletes_file_cleans_memberships(tmp_path):
    pytest.importorskip("leidenalg")
    repo, cfg = _git_repo(tmp_path)
    cfg.community_detection = True
    # 让 util.py 符号真正挂到 flow 与 community 上，否则后续清理断言是空转：
    # - 追加一个根函数 run_util()（无入边 -> 成为 flow 入口）调用 util.helper()
    # - import util 产生 app -> util 结构边，使 util 模块进入 community
    app = repo / "app.py"
    app.write_text(app.read_text(encoding="utf-8")
                   + "\nimport util\n\n\ndef run_util():\n    util.helper()\n",
                   encoding="utf-8")
    conn = connect(cfg.db_path)
    _init_and_build(cfg, conn)
    node_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM nodes WHERE file_path LIKE '%util.py'")]
    assert node_ids
    placeholders = ",".join("?" for _ in node_ids)
    # 前置断言：删除前 util.py 节点确实在 flow/community memberships 中
    assert conn.execute(
        f"SELECT COUNT(*) FROM flow_memberships WHERE node_id IN ({placeholders})",
        node_ids).fetchone()[0] > 0
    assert conn.execute(
        f"SELECT COUNT(*) FROM community_memberships WHERE node_id IN ({placeholders})",
        node_ids).fetchone()[0] > 0
    # 删除 util.py，走 watcher hint 路径
    (repo / "util.py").unlink()
    upd.update_nodes_edges(cfg, conn, ["util.py"])
    # 节点与边已清
    assert conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE file_path LIKE '%util.py'"
    ).fetchone()[0] == 0
    # flow/community memberships 无悬空
    assert conn.execute(
        f"SELECT COUNT(*) FROM flow_memberships WHERE node_id IN ({placeholders})",
        node_ids).fetchone()[0] == 0
    assert conn.execute(
        f"SELECT COUNT(*) FROM community_memberships WHERE node_id IN ({placeholders})",
        node_ids).fetchone()[0] == 0


def test_update_flows_rebuilds_from_db_and_skips_when_head_unchanged(tmp_path):
    repo, cfg = _git_repo(tmp_path)
    conn = connect(cfg.db_path)
    _init_and_build(cfg, conn)      # rebuild 已 stamp flows_as_of_head=HEAD
    before = conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
    assert before > 0
    # HEAD 未变 -> no-op
    assert upd.update_flows(cfg, conn) == 0
    # 改一个文件、commit（HEAD 变）-> 重算，flow 结构应随新符号变化
    (repo / "util.py").write_text(
        (repo / "util.py").read_text(encoding="utf-8")
        + "\ndef new_helper():\n    pass\n",
        encoding="utf-8")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-aqm", "add helper"], cwd=repo, check=True)
    n = upd.update_flows(cfg, conn)
    assert n > 0
    assert conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0] == n
    assert conn.execute(
        "SELECT value FROM build_meta WHERE key='flows_as_of_head'"
    ).fetchone()[0] == current_head(cfg)


def test_update_communities_when_enabled(tmp_path):
    pytest.importorskip("leidenalg")
    repo, cfg = _git_repo(tmp_path)
    cfg.community_detection = True
    conn = connect(cfg.db_path)
    _init_and_build(cfg, conn)
    # 默认 fixture 里 auth 模块与 auth::UserService 类之间没有结构边，社区
    # 划分后不存在跨社区边，community_edges 恒为空。补一条连接这两者的
    # resolved 结构边，让 update_communities 真正写出 community_edges。
    conn.execute(
        "INSERT INTO edges(source,target,kind,resolution) "
        "VALUES('auth','auth::UserService','import','resolved')")
    conn.commit()
    n = upd.update_communities(cfg, conn)
    assert n > 0
    members = conn.execute(
        "SELECT COUNT(*) FROM community_memberships").fetchone()[0]
    total = conn.execute(
        "SELECT SUM(node_count) FROM communities").fetchone()[0]
    assert members == total
    assert conn.execute(
        "SELECT COUNT(*) FROM community_edges").fetchone()[0] > 0


def test_sync_config_change_triggers_full_rebuild(tmp_path):
    repo, cfg = _git_repo(tmp_path)
    conn = connect(cfg.db_path)
    _init_and_build(cfg, conn)
    from code_review_ai.db import INDEX_VERSION
    # 配置变更（entry_names 不同）-> sync 应全量重建
    cfg.entry_names = ["different_entry"]
    result = upd.sync(cfg, conn)
    assert result["full_rebuild"] is True
    assert result["flows"] > 0
    # rebuild 已 stamp 新 meta
    assert conn.execute(
        "SELECT value FROM build_meta WHERE key='index_version'"
    ).fetchone()[0] == str(INDEX_VERSION)
    # manifest 已填充
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] > 0


def test_sync_nothing_changed_is_noop(tmp_path):
    repo, cfg = _git_repo(tmp_path)
    conn = connect(cfg.db_path)
    _init_and_build(cfg, conn)
    flows_before = conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
    result = upd.sync(cfg, conn)
    assert result["full_rebuild"] is False
    assert result["flows"] == 0
    assert conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0] == flows_before


def _edge_set(conn):
    return {tuple(r) for r in conn.execute(
        "SELECT source,target,kind,resolution,file_path,call_line FROM edges")}


def _flow_set(conn):
    out = set()
    for f in conn.execute("SELECT id,entry_point_id FROM flows").fetchall():
        entry = conn.execute(
            "SELECT qualified_name FROM nodes WHERE id=?",
            (f["entry_point_id"],)).fetchone()
        path = tuple(r[0] for r in conn.execute(
            "SELECT n.qualified_name FROM flow_memberships m "
            "JOIN nodes n ON n.id=m.node_id WHERE m.flow_id=? ORDER BY m.position",
            (f["id"],)).fetchall())
        out.add((entry["qualified_name"] if entry else None, path))
    return out


def test_sync_accumulation_equals_full_rebuild(tmp_path):
    repo, cfg = _git_repo(tmp_path)
    conn = connect(cfg.db_path)
    _init_and_build(cfg, conn)
    from code_review_ai.indexer import rebuild

    # 一连串增量改动 + 提交
    (repo / "util.py").write_text(
        (repo / "util.py").read_text(encoding="utf-8") + "\ndef new_helper():\n    pass\n",
        encoding="utf-8")
    upd.update_nodes_edges(cfg, conn)          # manifest 扫描路径
    (repo / "auth.py").write_text(
        (repo / "auth.py").read_text(encoding="utf-8") + "\ndef logout(u):\n    return u\n",
        encoding="utf-8")
    upd.update_nodes_edges(cfg, conn)
    (repo / "extra.py").write_text("from auth import logout\ndef x():\n    logout('a')\n",
                                   encoding="utf-8")
    upd.update_nodes_edges(cfg, conn)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-aqm", "edits"], cwd=repo, check=True)
    upd.sync(cfg, conn)

    incr_edges = _edge_set(conn)
    incr_flows = _flow_set(conn)

    rebuild(cfg, conn)
    full_edges = _edge_set(conn)
    full_flows = _flow_set(conn)

    assert incr_edges == full_edges
    assert incr_flows == full_flows


def test_repair_new_direction_no_reparse_of_importer(tmp_path, monkeypatch):
    """F 调 from m import User（当时 unresolved）；m 加 User -> F 边翻 resolved，
    且 F 不被 re-parse（验证修复 pass 的 importers 场景）。"""
    repo, cfg = _git_repo(tmp_path)
    conn = connect(cfg.db_path)
    _init_and_build(cfg, conn)
    # F = app.py 已 import auth.login 且 resolved；构造一个 unresolved importer 场景：
    # 改 auth.py 加 User，app.py 不 import User —— 用手工边验证不 re-parse F
    calls = {"n": 0}
    real = upd.parse_file

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(upd, "parse_file", counting)
    # 直接注入一条类型一 unresolved 边（模拟 F 曾 import auth::User 而未存在）。
    # 真实历史里这条边由前一次 update_nodes_edges 提交（transaction() 已 COMMIT）；
    # 这里手工 INSERT 会留下 sqlite 隐式事务，需 COMMIT 掉，否则下面
    # update_nodes_edges 的 transaction() 无法 BEGIN。
    conn.execute(
        "INSERT INTO edges(source,target,kind,resolution) "
        "VALUES('app::main','auth::User','call','unresolved')")
    conn.commit()
    # 改 auth.py 加 User
    (repo / "auth.py").write_text(
        (repo / "auth.py").read_text(encoding="utf-8") + "\ndef User():\n    pass\n",
        encoding="utf-8")
    upd.update_nodes_edges(cfg, conn)          # manifest 路径：只 re-parse auth.py
    assert calls["n"] == 1                      # F（app.py）未被 re-parse
    row = conn.execute(
        "SELECT resolution FROM edges WHERE target='auth::User'").fetchone()
    assert row is not None and row["resolution"] == "resolved"


def test_decorators_persisted_on_full_and_incremental(tmp_path):
    repo, cfg = _git_repo(tmp_path)
    (repo / "web.py").write_text(
        'from flask import Flask\napp = Flask(__name__)\n\n'
        '@app.route("/")\ndef index():\n    return "ok"\n',
        encoding="utf-8")
    conn = connect(cfg.db_path)
    _init_and_build(cfg, conn)
    # 全量路径（indexer._write_nodes）
    row = conn.execute(
        "SELECT decorators FROM nodes WHERE qualified_name='web::index'"
    ).fetchone()
    assert row is not None and json.loads(row["decorators"]) == ["app.route"]
    # 增量路径（update._insert_nodes）：改文件 -> watcher hint 只 re-parse web.py
    (repo / "web.py").write_text(
        'from flask import Flask\napp = Flask(__name__)\n\n'
        '@app.route("/")\n@cache\ndef index():\n    return "ok"\n',
        encoding="utf-8")
    upd.update_nodes_edges(cfg, conn, ["web.py"])
    row = conn.execute(
        "SELECT decorators FROM nodes WHERE qualified_name='web::index'"
    ).fetchone()
    assert json.loads(row["decorators"]) == ["app.route", "cache"]
