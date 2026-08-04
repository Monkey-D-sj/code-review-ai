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
