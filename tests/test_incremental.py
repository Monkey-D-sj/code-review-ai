import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import FIXTURES
from code_review_ai.config import load_config
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
