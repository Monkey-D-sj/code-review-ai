import shutil
import subprocess
import threading
import time
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.watcher import startup_sync, run_watcher

from conftest import FIXTURES


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
    cfg.db_path = str(tmp_path / "w.db")
    cfg.watch_debounce_ms = 100
    return repo, cfg


def _built_at(db_path):
    c = connect(db_path)
    row = c.execute("SELECT value FROM build_meta WHERE key='built_at'").fetchone()
    c.close()
    return row[0] if row else None


def test_startup_sync_rebuilds_empty_db(tmp_path):
    _, cfg = _git_repo(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    assert startup_sync(cfg, conn) is True     # 空库 -> 全量


def test_run_watcher_updates_nodes_edges_on_change(tmp_path):
    repo, cfg = _git_repo(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    lock = threading.Lock()
    startup_sync(cfg, conn, lock)
    before = _built_at(cfg.db_path)

    stop = threading.Event()
    t = threading.Thread(target=run_watcher, args=(cfg, lock, stop), daemon=True)
    t.start()
    after = before
    try:
        time.sleep(0.5)                       # 让 watchfiles 建立基线
        p = repo / "util.py"
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n# touch\n")
        deadline = time.time() + 8
        while time.time() < deadline and after == before:
            time.sleep(0.1)
            after = _built_at(cfg.db_path)
    finally:
        stop.set()
    t.join(timeout=8)
    assert not t.is_alive()
    assert after != before                   # watcher 的 update_nodes_edges 已 stamp built_at
