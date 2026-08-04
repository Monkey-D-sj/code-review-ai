# 分层增量更新（Incremental Update）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把索引更新从「每次全量重建」改为「分层增量」——watcher 只更新 nodes/edges，git hook 在 commit 时从 DB 重算 flows/communities，去掉内存 ParseCache。

**Architecture:** 便宜的层（nodes/edges/degrees）由 `update.update_nodes_edges` 增量 patch DB，改动文件才 re-parse，末尾跑 `repair_resolutions` 保证与全量重建逐行一致；贵重的层（flows/communities）由 `update_flows`/`update_communities` 从 DB 的 nodes+edges 全量重算（`build_flows`/`build_communities` 只吃 NodeRow/EdgeRow，无需 ParsedFile）。触发：watcher→nodes/edges；post-commit 等 git hook→`sync`；启动/MCP `rebuild_index`→`sync`；`rebuild` 保留全量。全局状态以 DB 为准（`files` manifest 表做变更检测，`build_meta` 存 `flows_as_of_head`/`config_hash`/`index_version`）。

**Tech Stack:** Python 3.14、sqlite3（WAL + busy_timeout）、tree-sitter、watchfiles、git subprocess。测试用 pytest（`uv run pytest`）。

## Global Constraints

- Qualified names 一律走 `qname.join` / `qname.short`，禁止手拼/手拆。
- 函数体 ≤ 50 行、类 ≤ 300 行；主控函数只做编排；禁止单字母变量名（数学索引除外）、禁止内置名作变量。
- 每次写入 DB 必须走 `db.transaction()`（原子；失败回滚，旧索引保留）。
- edges 的 `source`/`target` 存 qname 字符串（不是 id）；`nodes.file_path` / `edges.file_path` 是**绝对路径**；manifest 键是 **repo-relative**。
- 测试 fixture：`tests/fixtures/repo` 是父仓库内嵌目录，`git ls-files`（cwd=repo_path）返回 cwd-relative 路径；**新的增量测试**一律用 `_git_repo(tmp_path)` 复制出独立临时 git 仓库，禁止改共享 fixture；沿用既有"共享 fixture + 事后恢复"模式的测试（如 `test_is_stale_detects_mtime`）除外。
- `repair_resolutions` 规则（见 Task 4）必须保持：`kind='call'` 且 `target` 无 `::` 的边**不碰**；`dynamic` 边**不碰**。
- 现有 `indexer.is_stale` 及其测试**保持原样**（新路径用 `needs_*`，不删旧函数）。
- 每个 task 结束必须跑 `uv run pytest` 全量，全部通过才可进入下一个 task。

---

### Task 1: db.py — `files` manifest 表 + busy_timeout + INDEX_VERSION

**Files:**
- Modify: `code_review_ai/db.py`
- Test: `tests/test_db.py`（新增一个测试）

**Interfaces:**
- Consumes: 无
- Produces:
  - `db.SCHEMA` 含 `files(path TEXT PRIMARY KEY, mtime REAL, size INTEGER, file_hash TEXT)`
  - `db.INDEX_VERSION = 2`（模块常量）
  - `db.connect` 设置 `PRAGMA busy_timeout=5000`

- [ ] **Step 1: 写失败测试**

在 `tests/test_db.py` 追加：

```python
def test_files_table_and_busy_timeout(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    # files 表存在且可写
    conn.execute(
        "INSERT INTO files(path,mtime,size,file_hash) VALUES('a.py', 1.0, 3, 'x')")
    row = conn.execute("SELECT * FROM files").fetchone()
    assert row["path"] == "a.py" and row["size"] == 3
    assert INDEX_VERSION == 2
    # busy_timeout 生效（PRAGMA 返回毫秒）
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    conn.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_db.py::test_files_table_and_busy_timeout -v`
Expected: FAIL（`INDEX_VERSION` 未定义 / `no such table: files`）

- [ ] **Step 3: 实现**

在 `db.py` 顶部加 `INDEX_VERSION = 2`；`SCHEMA` 字符串末尾（`build_meta` 之后）追加：

```sql
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    mtime REAL,
    size INTEGER,
    file_hash TEXT
);
```

在 `connect()` 里 `PRAGMA journal_mode=WAL` 之后加 `conn.execute("PRAGMA busy_timeout=5000")`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_db.py::test_files_table_and_busy_timeout -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/db.py tests/test_db.py
git commit -m "feat(db): add files manifest table, busy_timeout, INDEX_VERSION"
```

---

### Task 2: parser.py — `list_source_files` 单次 git 调用

**Files:**
- Modify: `code_review_ai/parser.py:190-205`
- Test: `tests/test_parser.py`（新增一个测试）

**Interfaces:**
- Consumes: 无
- Produces: `list_source_files(repo_path, extensions=None) -> list[str]` 语义不变（返回 repo-relative 路径），但只 spawn **一次** git 子进程（传全部 extension glob）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_parser.py` 追加（用 `monkeypatch` 统计 git 调用次数）：

```python
def test_list_source_files_single_git_call(tmp_path, monkeypatch):
    import subprocess
    from code_review_ai import parser
    calls = {"n": 0}
    real_run = subprocess.run

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting)
    files = parser.list_source_files(FIXTURES)
    assert calls["n"] == 1          # 一次调用拿到所有扩展
    assert "app.py" in files and "ts/app.ts" in files
```

（在 `test_parser.py` 里确保已 `from conftest import FIXTURES`，若没有则补上。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_parser.py::test_list_source_files_single_git_call -v`
Expected: FAIL（`calls["n"] == 1` 断言失败，当前是 7）

- [ ] **Step 3: 实现**

把 `list_source_files` 主体改为一次调用：

```python
def list_source_files(repo_path: str, extensions: list[str] | None = None) -> list[str]:
    """Return sorted relative paths of source files from git.

    extensions: list of git ls-files globs like ["*.py", "*.ts"]. Default: ["*.py"]
    Single git call with all globs as pathspecs (was one subprocess per glob)."""
    if extensions is None:
        extensions = ["*.py"]
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", *extensions],
        cwd=repo_path, capture_output=True, text=True, check=True,
        encoding="utf-8", errors="replace",
    )
    return sorted(out.stdout.splitlines())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_parser.py::test_list_source_files_single_git_call tests/test_parser.py -v`
Expected: 新增测试 PASS，且 `tests/test_parser.py` 全绿（语义不变）

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/parser.py tests/test_parser.py
git commit -m "perf(parser): single git ls-files call for all source extensions"
```

---

### Task 3: manifest.py + update.changed_files — 变更检测

**Files:**
- Create: `code_review_ai/manifest.py`
- Create: `code_review_ai/update.py`（本 task 只放变更检测部分）
- Test: `tests/test_incremental.py`（新建，含 `_git_repo` 帮助函数）

**Interfaces:**
- Consumes: Task 1（`files` 表）、Task 2（`list_source_files`）、`parser.filter_excluded`、`parser.SOURCE_GLOBS`
- Produces:
  - `manifest.hash_file(path: str) -> str`（sha256 hex）
  - `manifest.read(conn) -> dict[str, tuple[float, int, str]]`（path → (mtime, size, file_hash)）
  - `manifest.update(conn, entries: dict[str, tuple[float,int,str]]) -> None`（`INSERT OR REPLACE`）
  - `manifest.remove(conn, paths: list[str]) -> None`
  - `update.changed_files(config, conn) -> tuple[set[str], set[str], set[str]]`（changed, added, deleted，repo-relative）
  - `update.needs_nodes_update(config, conn) -> bool`

- [ ] **Step 1: 写 `_git_repo` 帮助 + 失败测试**

在 `tests/test_incremental.py` 写入（该帮助被后续所有 task 复用）：

```python
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
```

（每次 `_git_repo(tmp_path)` 都是独立拷贝，无需恢复现场。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_incremental.py::test_changed_files_detects_modify_add_delete -v`
Expected: FAIL（`from code_review_ai import update as upd` 等 ImportError）

- [ ] **Step 3: 实现**

`code_review_ai/manifest.py`：

```python
"""Per-file manifest: rel_path -> (mtime, size, file_hash), persisted in the
`files` table. Used to detect changed/added/deleted files for incremental
node/edge updates without re-parsing everything."""

import hashlib


def hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read(conn) -> dict[str, tuple[float, int, str]]:
    return {r["path"]: (r["mtime"], r["size"], r["file_hash"])
            for r in conn.execute("SELECT path,mtime,size,file_hash FROM files")}


def update(conn, entries: dict[str, tuple[float, int, str]]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO files(path,mtime,size,file_hash) VALUES(?,?,?,?)",
        [(path, mtime, size, file_hash)
         for path, (mtime, size, file_hash) in entries.items()])


def remove(conn, paths: list[str]) -> None:
    if paths:
        conn.executemany("DELETE FROM files WHERE path=?", [(p,) for p in paths])
```

`code_review_ai/update.py`（本 task 只放变更检测；后续 task 追加）：

```python
"""Incremental index updates.

Watcher keeps nodes/edges fresh (update_nodes_edges); git hooks and startup
recompute flows/communities from the DB (update_flows/update_communities).
The DB is the source of truth — no in-memory parse cache."""

import os

from code_review_ai.parser import (SOURCE_GLOBS, filter_excluded,
                                   list_source_files)
from code_review_ai import manifest


def changed_files(config, conn) -> tuple[set[str], set[str], set[str]]:
    """(changed, added, deleted) repo-relative paths vs the `files` manifest."""
    repo = config.repo_path
    current = set(filter_excluded(
        list_source_files(repo, SOURCE_GLOBS), config.exclude))
    manifest_entries = manifest.read(conn)
    changed: set[str] = set()
    added: set[str] = set()
    for rel in current:
        abs_path = os.path.join(repo, rel)
        try:
            st = os.stat(abs_path)
        except OSError:
            continue  # listed by git but gone from disk -> falls through to deleted
        entry = manifest_entries.get(rel)
        if entry is None:
            added.add(rel)
            continue
        mtime, size, file_hash = entry
        if st.st_mtime == mtime and st.st_size == size:
            continue  # fast path: unchanged
        if manifest.hash_file(abs_path) == file_hash:
            continue  # touch-only; content identical
        changed.add(rel)
    deleted = set(manifest_entries) - current
    return changed, added, deleted


def needs_nodes_update(config, conn) -> bool:
    changed, added, deleted = changed_files(config, conn)
    return bool(changed or added or deleted)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_incremental.py -v`
Expected: PASS（若 mtime 浮点相等性有问题，把 `changed_files` 里的 `st.st_mtime == mtime` 放宽为 `abs(st.st_mtime - mtime) < 1e-6`）

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/manifest.py code_review_ai/update.py tests/test_incremental.py
git commit -m "feat(update): file-hash manifest change detection"
```

---

### Task 4: update.repair_resolutions — resolution 修复 pass

**Files:**
- Modify: `code_review_ai/update.py`
- Test: `tests/test_incremental.py`

**Interfaces:**
- Consumes: `nodes`/`edges` 表已存在（任意来源）
- Produces: `update.repair_resolutions(conn) -> int`（翻转的边数）

- [ ] **Step 1: 写失败测试**

在 `tests/test_incremental.py` 追加：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_incremental.py::test_repair_resolutions_flips_by_global_set -v`
Expected: FAIL（`upd.repair_resolutions` AttributeError）

- [ ] **Step 3: 实现**

在 `update.py` 追加：

```python
def repair_resolutions(conn) -> int:
    """Re-evaluate non-dynamic edge labels against the current node qname set.

    Matches what a full rebuild would resolve: for unchanged files, target is
    derived from stable imports, so only existence changes. Call edges whose
    target has no '::' are raw/unresolvable in a full rebuild too — skipped."""
    qnames = {r["qualified_name"]
              for r in conn.execute("SELECT qualified_name FROM nodes")}
    rows = conn.execute(
        "SELECT id,kind,target,resolution FROM edges").fetchall()
    updates: list[tuple[str, int]] = []
    for row in rows:
        resolution = row["resolution"]
        if resolution == "dynamic":
            continue
        target = row["target"]
        if row["kind"] == "call" and "::" not in target:
            continue
        new_label = "resolved" if target in qnames else "unresolved"
        if new_label != resolution:
            updates.append((new_label, row["id"]))
    if updates:
        conn.executemany("UPDATE edges SET resolution=? WHERE id=?", updates)
    return len(updates)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_incremental.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/update.py tests/test_incremental.py
git commit -m "feat(update): resolution repair pass for incremental edges"
```

---

### Task 5: indexer.recompute_degrees + update.update_nodes_edges

**Files:**
- Modify: `code_review_ai/indexer.py`（加 `recompute_degrees`；`_write_degrees` 删除、`rebuild` 改调 `recompute_degrees`）
- Modify: `code_review_ai/update.py`（`update_nodes_edges` + 私有 helpers）
- Test: `tests/test_incremental.py` + 既有 `test_indexer.py`（degree 断言即验证）

**Interfaces:**
- Consumes: Task 2/3/4；`indexer.recompute_degrees(conn)`；`resolver.resolve_edges`；`parser.parse_file`；`db.transaction`
- Produces:
  - `indexer.recompute_degrees(conn) -> None`（从 edges 表全量重算 nodes.in_degree/out_degree）
  - `update.update_nodes_edges(config, conn, changed_paths: list[str] | None = None) -> dict`
    返回 `{"nodes": int, "edges": int, "parsed_files": int, "changed": list[str], "deleted": list[str]}`
  - 私有 `_classify_hint`、`_apply_nodes_edges_delta`、`_delete_memberships`、`_sync_manifest`

- [ ] **Step 1: 写失败测试（watcher 只动 nodes/edges + 删文件清理 + 只 parse 改动）**

在 `tests/test_incremental.py` 追加：

```python
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
    repo, cfg = _git_repo(tmp_path)
    conn = connect(cfg.db_path)
    _init_and_build(cfg, conn)
    node_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM nodes WHERE file_path LIKE '%util.py'")]
    assert node_ids
    placeholders = ",".join("?" for _ in node_ids)
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_incremental.py -k update_nodes_edges -v`
Expected: FAIL（`update_nodes_edges` AttributeError）

- [ ] **Step 3: 实现 recompute_degrees（indexer.py）**

在 `indexer.py` 加 import `from collections import defaultdict`，新增：

```python
def recompute_degrees(conn: sqlite3.Connection) -> None:
    """Recompute in_degree/out_degree for every node from the resolved call
    edges in the DB (DISTINCT callers/callees). Run after edges are written."""
    callers: dict[str, set[str]] = defaultdict(set)
    callees: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute(
            "SELECT source,target FROM edges "
            "WHERE kind='call' AND resolution='resolved'"):
        callers[r["target"]].add(r["source"])
        callees[r["source"]].add(r["target"])
    conn.execute("UPDATE nodes SET in_degree=0, out_degree=0")
    updates = [(len(callers[q]), len(callees[q]), nid)
               for q, nid in conn.execute("SELECT qualified_name,id FROM nodes")
               if q in callers or q in callees]
    if updates:
        conn.executemany(
            "UPDATE nodes SET in_degree=?, out_degree=? WHERE id=?", updates)
```

删除 `_write_degrees`（连同其调用处），`rebuild` 里 `_write_degrees(conn, all_edges, qname_to_id)` 替换为 `recompute_degrees(conn)`。此时跑既有 degree 测试验证：

Run: `uv run pytest tests/test_indexer.py::test_rebuild_writes_node_degrees -v`
Expected: PASS

- [ ] **Step 4: 实现 update_nodes_edges（update.py）**

在 `update.py` 顶部补 import：`os`、`json`、`from code_review_ai import qname`、`from code_review_ai.db import transaction`、`from code_review_ai.indexer import recompute_degrees, _stamp_built_at`、`from code_review_ai.parser import parse_file`、`from code_review_ai.resolver import resolve_edges`。追加：

```python
def update_nodes_edges(config, conn, changed_paths: list[str] | None = None) -> dict:
    """Incremental nodes/edges/degrees update. With changed_paths (watcher
    events, repo-relative) re-parse exactly those; without, scan the manifest
    for changes. Always ends with the resolution repair pass."""
    repo = config.repo_path
    if changed_paths is not None:
        changed, added, deleted = _classify_hint(config, conn, changed_paths)
    else:
        changed, added, deleted = changed_files(config, conn)
    if not (changed or added or deleted):
        repair_resolutions(conn)
        return {"nodes": 0, "edges": 0, "parsed_files": 0,
                "changed": [], "deleted": []}
    parse_paths = sorted(added | changed)
    parsed = [parse_file(os.path.join(repo, rel), repo) for rel in parse_paths]
    with transaction(conn):
        nodes, edges = _apply_nodes_edges_delta(
            conn, repo, parsed, changed | added, deleted)
        repair_resolutions(conn)
        _sync_manifest(conn, repo, parse_paths, deleted)
        _stamp_built_at(conn)
    return {"nodes": nodes, "edges": edges, "parsed_files": len(parse_paths),
            "changed": sorted(changed | added), "deleted": sorted(deleted)}


def _classify_hint(config, conn, changed_paths: list[str]):
    """Split watcher event paths into (changed, added, deleted) by disk+manifest."""
    repo = config.repo_path
    manifest_entries = manifest.read(conn)
    present: set[str] = set()
    deleted: set[str] = set()
    for rel in changed_paths:
        if os.path.isfile(os.path.join(repo, rel)):
            present.add(rel)
        else:
            deleted.add(rel)
    added = present - set(manifest_entries)
    changed = present & set(manifest_entries)
    return changed, added, deleted


def _apply_nodes_edges_delta(conn, repo, parsed, changed_set: set[str],
                             deleted_set: set[str]) -> tuple[int, int]:
    touch = [os.path.join(repo, rel) for rel in changed_set | deleted_set]
    removed_ids: list[int] = []
    for abs_path in touch:
        removed_ids += [r["id"] for r in conn.execute(
            "SELECT id FROM nodes WHERE file_path=?", (abs_path,))]
        conn.execute("DELETE FROM edges WHERE file_path=?", (abs_path,))
        conn.execute("DELETE FROM nodes WHERE file_path=?", (abs_path,))
    if removed_ids:
        _delete_memberships(conn, removed_ids)
    remaining = {r["qualified_name"]
                 for r in conn.execute("SELECT qualified_name FROM nodes")}
    new_qnames = {n.qualified_name for pf in parsed for n in pf.nodes}
    global_set = remaining | new_qnames
    node_count = _insert_nodes(conn, parsed)
    edges = resolve_edges(parsed, global_set)
    _insert_edges(conn, edges)
    recompute_degrees(conn)
    return node_count, len(edges)


def _insert_nodes(conn, parsed) -> int:
    rows = [(n.qualified_name, n.kind, n.language, n.file_path,
             n.start_line, n.end_line, n.signature)
            for pf in parsed for n in pf.nodes]
    conn.executemany(
        "INSERT INTO nodes(qualified_name,kind,language,file_path,start_line,"
        "end_line,signature,parent_id) VALUES(?,?,?,?,?,?,?,NULL)", rows)
    qname_to_id = {r["qualified_name"]: r["id"]
                   for r in conn.execute("SELECT id,qualified_name FROM nodes")}
    parent_updates = [
        (qname_to_id[n.parent_qname], qname_to_id[n.qualified_name])
        for pf in parsed for n in pf.nodes
        if n.parent_qname and n.parent_qname in qname_to_id]
    if parent_updates:
        conn.executemany(
            "UPDATE nodes SET parent_id=? WHERE id=?", parent_updates)
    return len(rows)


def _insert_edges(conn, edges) -> None:
    conn.executemany(
        "INSERT INTO edges(source,target,kind,file_path,call_line,resolution)"
        " VALUES(?,?,?,?,?,?)",
        [(e.source, e.target, e.kind, e.file_path, e.call_line, e.resolution)
         for e in edges])


def _delete_memberships(conn, node_ids: list[int]) -> None:
    placeholders = ",".join("?" for _ in node_ids)
    conn.execute(
        f"DELETE FROM flow_memberships WHERE node_id IN ({placeholders})",
        node_ids)
    conn.execute(
        f"DELETE FROM community_memberships WHERE node_id IN ({placeholders})",
        node_ids)


def _sync_manifest(conn, repo, parse_paths: list[str], deleted: set[str]) -> None:
    entries: dict[str, tuple[float, int, str]] = {}
    for rel in parse_paths:
        abs_path = os.path.join(repo, rel)
        st = os.stat(abs_path)
        entries[rel] = (st.st_mtime, st.st_size, manifest.hash_file(abs_path))
    manifest.update(conn, entries)
    manifest.remove(conn, sorted(deleted))
```

注意：`update.py` 需 `import os`；`_apply_nodes_edges_delta` 的 `parsed` 来自 `parse_file(绝对路径, repo)`，节点 `file_path` 存绝对路径（与全量一致）。

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_incremental.py -k update_nodes_edges tests/test_indexer.py -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add code_review_ai/indexer.py code_review_ai/update.py tests/test_incremental.py
git commit -m "feat(update): incremental nodes/edges update with repair pass"
```

---

### Task 6: changes.current_head + update_flows / update_communities

**Files:**
- Modify: `code_review_ai/changes.py`（加 `current_head`）
- Modify: `code_review_ai/update.py`（`update_flows`、`update_communities`、`needs_flows_update`）
- Test: `tests/test_incremental.py`

**Interfaces:**
- Consumes: Task 5（DB 中已更新的 nodes/edges）、`flow_builder.build_flows`、`community.build_communities`/`inter_community_edges`/`WeightMode`
- Produces:
  - `changes.current_head(config) -> str | None`（`git rev-parse HEAD`，失败返回 None）
  - `update.needs_flows_update(config, conn) -> bool`（`build_meta.flows_as_of_head` != 当前 HEAD）
  - `update.update_flows(config, conn) -> int`（重算 flow 数；HEAD 未变返回 0）
  - `update.update_communities(config, conn) -> int`

- [ ] **Step 1: 写失败测试**

在 `tests/test_incremental.py` 追加：

```python
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
    n = upd.update_communities(cfg, conn)
    assert n > 0
    members = conn.execute(
        "SELECT COUNT(*) FROM community_memberships").fetchone()[0]
    total = conn.execute(
        "SELECT SUM(node_count) FROM communities").fetchone()[0]
    assert members == total
```

（测试里补 `from code_review_ai.changes import current_head`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_incremental.py -k 'update_flows or update_communities' -v`
Expected: FAIL（ImportError / AttributeError）

- [ ] **Step 3: 实现**

`changes.py` 追加：

```python
def current_head(config: Config) -> str | None:
    """Current git HEAD sha, or None if unresolvable (no commits / not a repo)."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=config.repo_path,
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace")
    except OSError:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()
```

`update.py` 追加 import：`import json`、`from code_review_ai.changes import current_head`、`from code_review_ai.community import WeightMode, build_communities, inter_community_edges`、`from code_review_ai.flow_builder import EdgeRow, NodeRow, build_flows`、`from code_review_ai import qname`。追加：

```python
def needs_flows_update(config, conn) -> bool:
    row = conn.execute(
        "SELECT value FROM build_meta WHERE key='flows_as_of_head'").fetchone()
    stored = row["value"] if row else None
    return stored != current_head(config)


def update_flows(config, conn) -> int:
    """Rebuild flows from the DB's nodes+edges. No-op if HEAD is unchanged
    (flows represent the last committed state)."""
    if not needs_flows_update(config, conn):
        return 0
    nodes = [NodeRow(r["id"], r["qualified_name"], r["file_path"], r["kind"])
             for r in conn.execute(
                 "SELECT id,qualified_name,file_path,kind FROM nodes")]
    erows = [EdgeRow(r["source"], r["target"], r["resolution"])
             for r in conn.execute(
                 "SELECT source,target,resolution FROM edges WHERE kind='call'")]
    flows = build_flows(nodes, erows, config.entry_names)
    id_to_qname = {n.id: n.qualified_name for n in nodes}
    with transaction(conn):
        conn.execute("DELETE FROM flow_memberships")
        conn.execute("DELETE FROM flows")
        membership_rows: list[tuple[int, int, int]] = []
        for f in flows:
            name = qname.short(id_to_qname.get(f.entry_point_id, ""))
            cur = conn.execute(
                "INSERT INTO flows(name,entry_point_id,depth,node_count,"
                "file_count,criticality,path_json) VALUES(?,?,?,?,?,?,?)",
                (name, f.entry_point_id, f.depth, f.node_count, f.file_count,
                 None, json.dumps(f.path)))
            fid = cur.lastrowid
            membership_rows.extend((fid, nid, pos) for pos, nid in enumerate(f.path))
        if membership_rows:
            conn.executemany(
                "INSERT INTO flow_memberships(flow_id,node_id,position) "
                "VALUES(?,?,?)", membership_rows)
        conn.execute(
            "INSERT OR REPLACE INTO build_meta(key,value) "
            "VALUES('flows_as_of_head',?)", (current_head(config) or "",))
    return len(flows)


def update_communities(config, conn) -> int:
    """Rebuild communities from the DB's structural (non-call) resolved edges.
    Opt-in via config.community_detection; degrades gracefully if libs missing."""
    if not config.community_detection:
        return 0
    nodes = [NodeRow(r["id"], r["qualified_name"], r["file_path"], r["kind"])
             for r in conn.execute(
                 "SELECT id,qualified_name,file_path,kind FROM nodes")]
    erows = [EdgeRow(r["source"], r["target"], "resolved")
             for r in conn.execute(
                 "SELECT source,target FROM edges "
                 "WHERE kind!='call' AND resolution='resolved'")]
    try:
        communities = build_communities(
            nodes, erows,
            weight_mode=WeightMode.parse(config.community_weight))
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "leidenalg/igraph not installed; skipping community detection")
        return 0
    qname_to_id = {n.qualified_name: n.id for n in nodes}
    membership_rows: list[tuple[int, int]] = []
    with transaction(conn):
        conn.execute("DELETE FROM community_edges")
        conn.execute("DELETE FROM community_memberships")
        conn.execute("DELETE FROM communities")
        for c in communities:
            cur = conn.execute(
                "INSERT INTO communities(label,node_count,modularity) "
                "VALUES(?,?,?)", (c.label, len(c.members), c.modularity))
            cid = cur.lastrowid
            membership_rows.extend((cid, nid) for nid in c.members)
        if membership_rows:
            conn.executemany(
                "INSERT INTO community_memberships(community_id,node_id) "
                "VALUES(?,?)", membership_rows)
            node_to_comm = {nid: cid for cid, nid in membership_rows}
            comm_edges = inter_community_edges(erows, qname_to_id, node_to_comm)
            if comm_edges:
                conn.executemany(
                    "INSERT INTO community_edges(community_id_a,community_id_b,"
                    "weight) VALUES(?,?,?)",
                    [(a, b, w) for (a, b), w in comm_edges.items()])
    return len(communities)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_incremental.py tests/test_changes.py -v`
Expected: PASS（`tests/test_changes.py` 确保 changes.py 改动不回归）

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/changes.py code_review_ai/update.py tests/test_incremental.py
git commit -m "feat(update): rebuild flows/communities from the DB"
```

---

### Task 7: config_hash + rebuild 元数据落盘 + sync

**Files:**
- Modify: `code_review_ai/config.py`（加 `config_hash`）
- Modify: `code_review_ai/indexer.py`（`_stamp_meta` 替代 `_stamp_built_at`，rebuild 里落 `config_hash`/`index_version`/`flows_as_of_head`/manifest）
- Modify: `code_review_ai/update.py`（`sync`、`_meta_changed`）
- Test: `tests/test_incremental.py`

**Interfaces:**
- Consumes: Task 5/6、`config.INDEX_VERSION`（用 db 的）
- Produces:
  - `config.config_hash(config: Config) -> str`
  - `indexer.rebuild` 额外在 build_meta 落 `config_hash`、`index_version`（=db.INDEX_VERSION）、`flows_as_of_head`，并填充 `files` 表
  - `update.sync(config, conn) -> dict`（`{"full_rebuild": bool, "nodes": int, "edges": int, "flows": int, "communities": int}`）

- [ ] **Step 1: 写失败测试**

在 `tests/test_incremental.py` 追加：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_incremental.py -k sync -v`
Expected: FAIL（`sync` AttributeError / `files` 空）

- [ ] **Step 3: 实现**

`config.py` 追加：

```python
import hashlib
import json

_CONFIG_HASH_KEYS = ("diff_base", "entry_names", "entry_decorators", "exclude",
                     "community_detection", "community_weight")


def config_hash(config: Config) -> str:
    """Stable hash of the config keys that affect index shape. On change the
    incremental paths fall back to a full rebuild."""
    payload = {key: getattr(config, key) for key in _CONFIG_HASH_KEYS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
```

（`config.py` 顶部已有 `import os`/`tomllib`；补 `hashlib`/`json`。）

`indexer.py`：**保留** `_stamp_built_at`（`update.update_nodes_edges` 仍 import 它），新增 `_stamp_meta` 并在 `rebuild` 里把 `built_at = _stamp_built_at(conn)` 替换为 `built_at = _stamp_meta(config, conn)`：

```python
def _stamp_meta(config: Config, conn: sqlite3.Connection) -> str:
    """Stamp build metadata after a full rebuild: built_at (via
    _stamp_built_at), config_hash, index_version, flows_as_of_head, and the
    file manifest (so the next incremental update can diff against it)."""
    from code_review_ai.changes import current_head
    from code_review_ai.config import config_hash as _config_hash
    from code_review_ai.db import INDEX_VERSION
    from code_review_ai import manifest
    built_at = _stamp_built_at(conn)
    conn.executemany(
        "INSERT OR REPLACE INTO build_meta(key,value) VALUES(?,?)",
        [("config_hash", _config_hash(config)),
         ("index_version", str(INDEX_VERSION)),
         ("flows_as_of_head", current_head(config) or "")])
    # populate file manifest so incremental updates can diff
    repo = config.repo_path
    files = filter_excluded(
        list_source_files(repo, SOURCE_GLOBS), config.exclude)
    entries = {}
    for rel in files:
        abs_path = os.path.join(repo, rel)
        try:
            st = os.stat(abs_path)
        except OSError:
            continue
        entries[rel] = (st.st_mtime, st.st_size, manifest.hash_file(abs_path))
    manifest.update(conn, entries)
    return built_at
```

注意：`rebuild` 里的 `files` 变量已被 `_stamp_meta` 重新计算——把原 `rebuild` 里 `files` 定义保留（parse 用），`_stamp_meta` 内部重新 `list_source_files` 一次（可接受；或在 `rebuild` 内把 `files` 传给 `_stamp_meta`，二选一，测试只断言结果）。

`update.py` 追加 import：`from code_review_ai.config import config_hash as _config_hash`、`from code_review_ai.db import INDEX_VERSION`、`from code_review_ai.indexer import rebuild`。追加：

```python
def _meta_changed(config, conn) -> bool:
    expected = {"config_hash": _config_hash(config),
                "index_version": str(INDEX_VERSION)}
    for key, value in expected.items():
        row = conn.execute(
            "SELECT value FROM build_meta WHERE key=?", (key,)).fetchone()
        if row is None or row["value"] != value:
            return True
    return False


def sync(config, conn) -> dict:
    """Bring the index current: config/version change -> full rebuild;
    otherwise incremental nodes/edges + flows + communities (each skips
    internally when up to date)."""
    if _meta_changed(config, conn):
        stats = rebuild(config, conn)
        return {"full_rebuild": True, "nodes": stats.node_count,
                "edges": stats.edge_count, "flows": stats.flow_count,
                "communities": stats.community_count}
    node_stats = update_nodes_edges(config, conn)
    flows = update_flows(config, conn)
    communities = update_communities(config, conn)
    return {"full_rebuild": False, "nodes": node_stats["nodes"],
            "edges": node_stats["edges"], "flows": flows,
            "communities": communities}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_incremental.py -k sync tests/test_config.py tests/test_indexer.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/config.py code_review_ai/indexer.py code_review_ai/update.py tests/test_incremental.py
git commit -m "feat(update): sync orchestration with config/version full-rebuild guard"
```

---

### Task 8: watcher.py + mcp_server.py 接线

**Files:**
- Modify: `code_review_ai/watcher.py`
- Modify: `code_review_ai/mcp_server.py`
- Modify: `tests/test_watcher.py`
- Modify: `tests/test_mcp_server.py`（如有断言 rebuild_index 全量重建，改断言）

**Interfaces:**
- Consumes: Task 5（`update_nodes_edges`）、Task 7（`sync`）
- Produces:
  - `watcher.startup_sync(config, conn, lock=None) -> bool`
  - `watcher.run_watcher(config, lock, stop_event=None) -> None`（`cache` 参数移除）
  - `mcp_server` 的 `rebuild_index` tool → `sync`；`mcp._cache` 移除，保留 `_conn`/`_lock`

- [ ] **Step 1: 改失败测试（watcher 不再全量 rebuild，启动走 sync）**

`tests/test_watcher.py` 改写为：

```python
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
```

注意：watcher 触发 `update_nodes_edges` 会 stamp built_at，故 `after != before` 成立；`_source_file` 过滤 `.git` 内非源文件，watchfiles 在临时仓库上安全。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_watcher.py -v`
Expected: FAIL（ImportError：`startup_sync` 不存在 / `run_watcher` 参数不匹配）

- [ ] **Step 3: 实现 watcher.py**

```python
import logging
import os
import sqlite3
import threading
from contextlib import nullcontext

from code_review_ai.config import Config
from code_review_ai.db import connect, init_schema
from code_review_ai.parser import SOURCE_SUFFIXES
from code_review_ai.update import sync, update_nodes_edges

log = logging.getLogger(__name__)


def startup_sync(config: Config, conn: sqlite3.Connection,
                 lock: threading.Lock | None = None) -> bool:
    """Bring the index current at startup. Returns True if anything was updated."""
    with (lock or nullcontext()):
        result = sync(config, conn)
    if result.get("full_rebuild"):
        return True
    return bool(result["nodes"] or result["edges"] or result["flows"]
                or result["communities"])


def run_watcher(config: Config, lock: threading.Lock | None,
                stop_event: threading.Event | None = None) -> None:
    """Watch source files; on change, update nodes/edges incrementally.
    Blocks until stop_event set. Uses its own DB connection."""
    from watchfiles import FileChange, watch
    conn = connect(config.db_path)
    init_schema(conn)
    stop_event = stop_event or threading.Event()
    debounce = max(config.watch_debounce_ms, 50)
    try:
        for changes in watch(config.repo_path, debounce=debounce,
                             watch_filter=_source_file, stop_event=stop_event):
            if stop_event.is_set():
                break
            paths = _relative_paths(config, changes)
            log.info("detected %d changes; updating nodes/edges", len(paths))
            try:
                with (lock or nullcontext()):
                    update_nodes_edges(config, conn, paths)
            except Exception:
                log.exception("update failed; keeping old index")
    except Exception:
        log.exception("watcher stopped unexpectedly")
        return


def _relative_paths(config: Config, changes) -> list[str]:
    out = []
    for _change, path in changes:
        rel = os.path.relpath(path, config.repo_path).replace("\\", "/")
        out.append(rel)
    return out


def _source_file(change, path):
    if not path.endswith(SOURCE_SUFFIXES):
        return False
    if change == FileChange.deleted:
        return True                      # deletes must reach the updater
    return os.path.isfile(path)
```

删除原 `startup_rebuild` 与对 `ParseCache`/`is_stale`/`rebuild` 的 import。

- [ ] **Step 4: 改 mcp_server.py**

- 顶部 import：`from code_review_ai.indexer import rebuild` → `from code_review_ai.update import sync`；删 `ParseCache`。
- `create_server`：删 `cache = ParseCache()`；`rebuild_index` tool 主体改为：

```python
    @mcp.tool()
    def rebuild_index() -> str:
        """Refresh the code graph index from the working tree (incremental
        nodes/edges, then flows/communities; full rebuild on config/version
        change). Returns a JSON object with counts. Normally the watcher keeps
        nodes/edges current and git hooks keep flows current; call only when
        you need fresh data right now."""
        with lock:
            result = sync(config, conn)
        return json.dumps({"nodes": result["nodes"], "edges": result["edges"],
                           "flows": result["flows"],
                           "communities": result["communities"],
                           "full_rebuild": result["full_rebuild"]})
```

- 底部 attach 改 `mcp._conn = conn; mcp._lock = lock`（删 `mcp._cache`）。
- `main()`：

```python
def main():
    import logging
    import threading
    from code_review_ai.config import load_config
    from code_review_ai.watcher import run_watcher, startup_sync
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    server = create_server(config)
    startup_sync(config, server._conn, server._lock)
    t = threading.Thread(target=run_watcher, args=(config, server._lock), daemon=True)
    t.start()
    server.run()
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_watcher.py tests/test_mcp_server.py -v`
Expected: 全部 PASS（如 `test_mcp_server.py` 有断言 `rebuild_index` 返回 `built_at`/`timings_ms`，同步更新断言为 `nodes`/`flows`/`full_rebuild`）

- [ ] **Step 6: Commit**

```bash
git add code_review_ai/watcher.py code_review_ai/mcp_server.py tests/test_watcher.py tests/test_mcp_server.py
git commit -m "feat(watcher): incremental nodes/edges on save; startup sync"
```

---

### Task 9: hooks.py + cli.py 子命令（update / sync / install-hooks）

**Files:**
- Create: `code_review_ai/hooks.py`
- Modify: `code_review_ai/cli.py`
- Test: `tests/test_cli.py` + `tests/test_hooks.py`

**Interfaces:**
- Consumes: Task 7（`sync`）、Task 5（`update_nodes_edges`）
- Produces:
  - `hooks.install_hooks(repo: str, db: str, launch: str = "code-review-ai") -> list[str]`（写 4 个 post-* 钩子，返回写入路径）
  - CLI 子命令 `update` / `sync` / `install-hooks`

- [ ] **Step 1: 写失败测试**

`tests/test_hooks.py`：

```python
from pathlib import Path
from code_review_ai.hooks import HOOK_NAMES, install_hooks


def test_install_hooks_writes_sync_scripts(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    written = install_hooks(str(repo), str(tmp_path / "index.db"), launch="code-review-ai")
    assert len(written) == len(HOOK_NAMES)
    for name in HOOK_NAMES:
        p = Path(written[HOOK_NAMES.index(name)])
        content = p.read_text(encoding="utf-8")
        assert "sync --repo" in content
        assert "--db" in content


def test_install_hooks_idempotent(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    first = install_hooks(str(repo), str(tmp_path / "i.db"))
    second = install_hooks(str(repo), str(tmp_path / "i.db"))
    assert first == second
    assert Path(first[0]).read_text(encoding="utf-8") == Path(second[0]).read_text(encoding="utf-8")
```

`tests/test_cli.py` 追加：

```python
def test_cli_update_and_sync(tmp_path, capsys):
    from conftest import FIXTURES as FIX
    from code_review_ai import cli
    db = str(tmp_path / "cli.db")
    # sync 空库 -> 全量
    assert cli.main(["sync", "--repo", FIX, "--db", db]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["full_rebuild"] is True and payload["flows"] > 0
    # update 无变化 -> 0 parse
    assert cli.main(["update", "--repo", FIX, "--db", db]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["parsed_files"] == 0
```

（`tests/test_cli.py` 顶部需 `import json`。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_hooks.py tests/test_cli.py -v`
Expected: FAIL（ImportError：`hooks` / CLI 子命令不存在）

- [ ] **Step 3: 实现 hooks.py**

```python
"""Install post-* git hooks that keep flows/communities fresh at commit time.

The hooks call `code-review-ai sync` (nodes catch-up + flows + communities) so
the index reflects the last commit exactly. Per-repo setup; repos without hooks
still self-heal at startup via the flows_as_of_head check."""

from pathlib import Path

HOOK_NAMES = ("post-commit", "post-merge", "post-checkout", "post-rewrite")


def install_hooks(repo: str, db: str, launch: str = "code-review-ai") -> list[str]:
    """Write the four post-* hooks under <repo>/.git/hooks. Returns paths."""
    repo_abs = str(Path(repo).resolve())
    db_abs = str(Path(db).resolve()).replace("\\", "/")
    hooks_dir = Path(repo) / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    script = (
        "#!/bin/sh\n"
        "# code-review-ai: rebuild flows/communities at commit time\n"
        f"{launch} sync --repo '{repo_abs}' --db '{db_abs}'\n"
    )
    written: list[str] = []
    for name in HOOK_NAMES:
        path = hooks_dir / name
        path.write_text(script, encoding="utf-8")
        try:
            path.chmod(0o755)
        except OSError:
            pass
        written.append(str(path))
    return written
```

- [ ] **Step 4: 实现 cli.py**

顶部 import 加 `from code_review_ai.update import sync, update_nodes_edges`。`main` 里子命令注册：

```python
    up = sub.add_parser("update")
    _add_common(up)
    sp = sub.add_parser("sync")
    _add_common(sp)
    hp = sub.add_parser("install-hooks")
    _add_common(hp)
    hp.add_argument("--launch", default="code-review-ai")
```

dispatch（`rebuild` 分支之后追加）：

```python
    elif args.cmd == "update":
        print(json.dumps(update_nodes_edges(cfg, conn)))
    elif args.cmd == "sync":
        print(json.dumps(sync(cfg, conn)))
    elif args.cmd == "install-hooks":
        from code_review_ai.hooks import install_hooks
        for path in install_hooks(cfg.repo_path, cfg.db_path, args.launch):
            print(f"installed {path}")
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_hooks.py tests/test_cli.py -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add code_review_ai/hooks.py code_review_ai/cli.py tests/test_hooks.py tests/test_cli.py
git commit -m "feat(cli): update/sync/install-hooks subcommands + git hook installer"
```

---

### Task 10: 等价性 + 残余测试收尾

**Files:**
- Modify: `tests/test_indexer.py`（`test_rebuild_cache_skips_unchanged_files` 改写）
- Modify: `tests/test_incremental.py`（等价性 + 修复方向 + 类型二测试）
- Test: 全量

**Interfaces:**
- Consumes: Task 7 全部函数

- [ ] **Step 1: 移除 indexer 的 ParseCache + 改写缓存测试**

先删 `indexer.py` 里的 `_CacheEntry`、`ParseCache` 类、`_parse_files` 的 cache 分支，并把签名收敛为 `_parse_files(files, repo)`、`rebuild(config, conn)`；顶部 import 清理。此时 `tests/test_indexer.py` 顶部 `from code_review_ai.indexer import ParseCache, rebuild, is_stale` 需去掉 `ParseCache`（`test_is_stale_detects_mtime` 不受影响）。

`test_rebuild_cache_skips_unchanged_files` 改写为（manifest 驱动）：

```python
def test_rebuild_then_update_parses_only_changed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    from code_review_ai import update as upd
    rebuild(cfg, conn)                          # 全量，填充 manifest（Task 7）
    calls = {"n": 0}
    real = upd.parse_file

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(upd, "parse_file", counting)
    # 无变化 -> 不 parse 任何文件
    upd.update_nodes_edges(cfg, conn)
    assert calls["n"] == 0
    # 只改 util.py -> 只 parse 一个
    p = "tests/fixtures/repo/util.py"
    orig = open(p, encoding="utf-8").read()
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n# x\n")
        calls["n"] = 0
        upd.update_nodes_edges(cfg, conn)
        assert calls["n"] == 1
    finally:
        with open(p, "w", encoding="utf-8") as f:
            f.write(orig)
```

（此测试沿用 `test_is_stale_detects_mtime` 的"共享 fixture + 事后恢复"模式，Global Constraints 已豁免；实现者若想规避共享改动，可复制 `_git_repo` 帮助到 `test_indexer.py`。）

- [ ] **Step 2: 加等价性测试（验收核心）**

在 `tests/test_incremental.py` 追加：

```python
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
    # 直接注入一条类型一 unresolved 边（模拟 F 曾 import auth::User 而未存在）
    conn.execute(
        "INSERT INTO edges(source,target,kind,resolution) "
        "VALUES('app::main','auth::User','call','unresolved')")
    # 改 auth.py 加 User
    (repo / "auth.py").write_text(
        (repo / "auth.py").read_text(encoding="utf-8") + "\ndef User():\n    pass\n",
        encoding="utf-8")
    upd.update_nodes_edges(cfg, conn)          # manifest 路径：只 re-parse auth.py
    assert calls["n"] == 1                      # F（app.py）未被 re-parse
    row = conn.execute(
        "SELECT resolution FROM edges WHERE target='auth::User'").fetchone()
    assert row is not None and row["resolution"] == "resolved"
```

- [ ] **Step 3: 全量跑测试**

Run: `uv run pytest -v`
Expected: 全部 PASS（若 `_git_repo` 内 `auth.py` 恢复逻辑繁琐，等价性测试可用独立临时 repo，每次 `_git_repo` 即新拷贝，无需恢复）

- [ ] **Step 4: Commit**

```bash
git add tests/test_indexer.py tests/test_incremental.py
git commit -m "test(update): equivalence with full rebuild + importer repair"
```

---

## Self-Review 对照

- **spec ① update_nodes_edges** → Task 5（delta + repair + manifest + degrees）。
- **spec ② repair pass** → Task 4。
- **spec ③ update_flows/update_communities 从 DB** → Task 6。
- **spec ④ 触发源**（watcher→nodes/edges；hook→sync；启动→sync；MCP rebuild_index→sync；CLI update/sync/install-hooks）→ Task 8（watcher/mcp）、Task 9（cli/hooks）。
- **spec ⑤ 元数据**（files 表 / flows_as_of_head / config_hash / index_version / 兜底全量 / needs_* 替代 is_stale）→ Task 1、Task 7；is_stale 保留（Global Constraints）。
- **spec ⑥ install-hooks** → Task 9。
- **spec 并发**（busy_timeout、transaction、hook 失败不影响 commit）→ Task 1、Global Constraints、hooks 脚本（git 保证）。
- **spec 测试节**（watcher 只动 nodes/edges、修复方向、删文件清理、等价性、manifest、flows 短路、配置变更）→ Task 3/5/6/7/10。
- **spec「不做」**：raw_calls 不持久化、增量 flows 不做、跨进程缓存不做、增量 communities 不做、wildcard 不修 —— 计划均未实现。
