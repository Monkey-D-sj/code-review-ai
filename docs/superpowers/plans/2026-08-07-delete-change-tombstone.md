# delete_change + tombstone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 增量 update 观察到删除时把被删文件/函数写成 tombstone（含一跳上游）；`get_change_summary` 的 diff 路径新增顶层 `delete_change` 字段输出这些删除及其影响者。

**Architecture:** 三层改动——db.py 加 `tombstones` 表；update.py 在 `_apply_nodes_edges_delta` 删除 loop **之前**收集并写 tombstone；changes.py 读 tombstone 组装 `delete_change`，并让 `_diff_coverage` 抑制被 delete_change 覆盖文件的 uncovered 条目。

**Tech Stack:** Python 3.14, tree-sitter, SQLite (WAL), git CLI (`git diff --unified=0`)。

## Global Constraints

- **tombstones 是追加式删除日志**：全量重建 `_clear_tables` **不清**它——这是它的价值（重建后图没了，删除细节还在）。
- **上游只取 `call` + `inherits` + `import`，不含 `contains`**（容器关系：删方法不影响类存在）。
- **写 tombstone 必须在删除 loop 之前**（此刻边还 resolved，`repair_resolutions` 尚未运行）。
- **上游排除本次同批被删的 source**（同文件一起删的内部调用者不算外部依赖）。
- **被删文件** → 该文件全部旧节点 tombstone，`file_deleted=1`；**存活文件重解析** → 旧节点 qname − 新 parse qname 的差集 tombstone，`file_deleted=0`。
- **`delete_change` 记录 shape**（diff 路径）：`{qname, kind, file, file_deleted, start_line, end_line, signature, is_test, upstream: [{source, kind, file}]}`。`record.file` 与 `upstream[].file` 都是 repo-relative（`_relative_to_repo`）。
- **`symbols=` 路径** → `delete_change: []`、`summary.delete_change: 0`。
- **无 tombstone 的删除不编造**：被删文件若没有 tombstone 记录 → 仍留在 `uncovered_changes` 为 `{file, hunks: [], deleted: true}`。
- **`file_path` 三处同一构造**：nodes（write）、tombstones（写自 node 行）、read 端查 tombstones 都用 `os.path.join(config.repo_path, rel)`，Windows 上保持混合分隔符一致，否则查不到 tombstone（退化为 uncovered）。
- **`deleted_at_head = current_head(config)`**（`update.py` 已导入 `current_head`），信息性，v1 不消费。
- **运行测试**：`uv run pytest` 可能被正在运行的 MCP server 锁住（venv pytest.exe 占用）→ 用 `./.venv/Scripts/python.exe -m pytest <路径> -v`。

---

### Task 1: db.py 增加 tombstones 表

**Files:**
- Modify: `code_review_ai/db.py`（SCHEMA 内、`files` 表之后、`CREATE INDEX` 之前）
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `SCHEMA` 里的 `tombstones` 表（列：`id, qname, kind, language, file_path, start_line, end_line, signature, is_test, decorators, deleted_at_head, file_deleted, upstream_json`）+ 索引 `idx_tombstones_file(file_path)`、`idx_tombstones_qname(qname)`。后续任务依赖此表与列名。

- [ ] **Step 1: 写失败的测试**

在 `tests/test_db.py` 末尾追加（复用现有 `connect, init_schema` import）：

```python
def test_init_schema_creates_tombstones_table(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    init_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tombstones)")}
    assert {"qname", "kind", "file_path", "start_line", "end_line",
            "signature", "is_test", "decorators", "deleted_at_head",
            "file_deleted", "upstream_json"} <= cols
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0
    init_schema(conn)  # 幂等
```

- [ ] **Step 2: 运行确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_db.py::test_init_schema_creates_tombstones_table -v`
Expected: FAIL，`sqlite3.OperationalError: no such table: tombstones`

- [ ] **Step 3: 加表**

在 `SCHEMA` 的 `files` 表之后、`CREATE INDEX` 之前插入：

```sql
CREATE TABLE IF NOT EXISTS tombstones (
    id INTEGER PRIMARY KEY,
    qname TEXT NOT NULL,
    kind TEXT NOT NULL,
    language TEXT,
    file_path TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,
    is_test INTEGER NOT NULL DEFAULT 0,
    decorators TEXT,
    deleted_at_head TEXT,
    file_deleted INTEGER NOT NULL DEFAULT 0,
    upstream_json TEXT NOT NULL DEFAULT '[]'
);
```

在 SCHEMA 末尾的索引区追加：

```sql
CREATE INDEX IF NOT EXISTS idx_tombstones_file ON tombstones(file_path);
CREATE INDEX IF NOT EXISTS idx_tombstones_qname ON tombstones(qname);
```

`CREATE TABLE IF NOT EXISTS` + `init_schema` 在 cli/mcp_server/watcher 三入口都调用 → 存量 DB 下次启动即得新表，**无需 bump `INDEX_VERSION`**。

- [ ] **Step 4: 运行确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_db.py::test_init_schema_creates_tombstones_table -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/db.py tests/test_db.py
git commit -m "feat(db): tombstones table for deleted-file/function snapshots"
```

---

### Task 2: update.py 删除时写 tombstone

**Files:**
- Modify: `code_review_ai/update.py`（`_apply_nodes_edges_delta` 内、`_insert_nodes` 附近）
- Test: `tests/test_incremental.py`

**Interfaces:**
- Consumes: Task 1 的 `tombstones` 表列名；`current_head`（`update.py` 已 import）；`json`（已 import）；`os`（已 import）。
- Produces: `_collect_tombstones(conn, repo, parsed, changed_set, deleted_set, config) -> list[tuple]`（返回 `_insert_tombstones` 的列序行）、`_insert_tombstones(conn, rows) -> None`。Task 3 依赖这些行被写入。

- [ ] **Step 1: 写失败的测试**

在 `tests/test_incremental.py` 末尾追加（`json`、`upd`、`connect/init_schema`、`_git_repo`、`_init_and_build` 均已就绪）：

```python
def test_update_deletes_file_writes_tombstones(tmp_path):
    repo, cfg = _git_repo(tmp_path)
    conn = connect(cfg.db_path)
    _init_and_build(cfg, conn)
    # app.py 已调用 auth.login（call 边）且 import auth（import 边）
    (repo / "auth.py").unlink()
    upd.update_nodes_edges(cfg, conn, ["auth.py"])
    rows = conn.execute(
        "SELECT qname,kind,file_deleted FROM tombstones "
        "WHERE file_path LIKE '%auth.py'").fetchall()
    assert {r["qname"] for r in rows} == {
        "auth", "auth::login", "auth::UserService",
        "auth::UserService.authenticate"}
    assert all(r["file_deleted"] == 1 for r in rows)
    login_up = json.loads(conn.execute(
        "SELECT upstream_json FROM tombstones WHERE qname='auth::login'"
    ).fetchone()[0])
    assert any(u["source"] == "app::main" and u["kind"] == "call"
               for u in login_up)
    mod_up = json.loads(conn.execute(
        "SELECT upstream_json FROM tombstones WHERE qname='auth'"
    ).fetchone()[0])
    assert any(u["source"] == "app" and u["kind"] == "import" for u in mod_up)
    # 节点与边已清（原行为不变）
    assert conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE file_path LIKE '%auth.py'"
    ).fetchone()[0] == 0


def test_update_deletes_function_in_surviving_file_writes_tombstone(tmp_path):
    repo, cfg = _git_repo(tmp_path)
    conn = connect(cfg.db_path)
    _init_and_build(cfg, conn)
    # 移除 login()，保留 UserService
    (repo / "auth.py").write_text(
        "class UserService:\n    def authenticate(self, user, pw) -> bool:\n"
        "        return check(pw)\n", encoding="utf-8")
    upd.update_nodes_edges(cfg, conn, ["auth.py"])
    row = conn.execute(
        "SELECT * FROM tombstones WHERE qname='auth::login'").fetchone()
    assert row is not None and row["file_deleted"] == 0
    upstream = json.loads(row["upstream_json"])
    assert any(u["source"] == "app::main" and u["kind"] == "call"
               for u in upstream)
    assert conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE qualified_name='auth::UserService'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE qualified_name='auth::login'"
    ).fetchone()[0] == 0


def test_tombstones_survive_rebuild(tmp_path):
    repo, cfg = _git_repo(tmp_path)
    conn = connect(cfg.db_path)
    _init_and_build(cfg, conn)
    (repo / "auth.py").unlink()
    upd.update_nodes_edges(cfg, conn, ["auth.py"])
    before = conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0]
    assert before > 0
    from code_review_ai.indexer import rebuild
    rebuild(cfg, conn)
    after = conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0]
    assert after == before          # 全量重建不清 tombstone


def test_tombstone_upstream_excludes_same_batch_sources(tmp_path):
    repo, cfg = _git_repo(tmp_path)
    (repo / "mod.py").write_text(
        "def inner():\n    pass\n\n\ndef outer():\n    inner()\n",
        encoding="utf-8")
    conn = connect(cfg.db_path)
    _init_and_build(cfg, conn)
    (repo / "mod.py").unlink()
    upd.update_nodes_edges(cfg, conn, ["mod.py"])
    inner = conn.execute(
        "SELECT upstream_json FROM tombstones WHERE qname='mod::inner'"
    ).fetchone()
    assert inner is not None
    assert json.loads(inner[0]) == []   # mod::outer 同批被删，排除
```

- [ ] **Step 2: 运行确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_incremental.py -v`
Expected: 4 个新测试 FAIL（`no such table: tombstones` 或 `no such column`），既有测试仍 PASS。

- [ ] **Step 3: 写最小实现**

在 `update.py` 的 `_apply_nodes_edges_delta` 之前添加两个函数：

```python
def _tombstone_upstream(conn, qname: str, deleted_qnames: set[str]) -> list[dict]:
    """One-hop upstream (call/inherits/import) of a deleted qname, excluding
    sources being deleted in this same batch. Runs before the delete loop so
    the edges are still the resolved pre-deletion state."""
    rows = conn.execute(
        "SELECT source, kind, file_path FROM edges "
        "WHERE target=? AND kind IN ('call','inherits','import')",
        (qname,)).fetchall()
    return [{"source": r["source"], "kind": r["kind"], "file": r["file_path"]}
            for r in rows if r["source"] not in deleted_qnames]


def _collect_tombstones(conn, repo, parsed, changed_set: set[str],
                        deleted_set: set[str], config) -> list[tuple]:
    """Tombstone rows for deletions in this batch, captured BEFORE the delete
    loop (edges still resolved). Whole-file deletions tombstone every old node
    (file_deleted=1); deletions inside a re-parsed surviving file are the
    old−new qname delta (file_deleted=0). Returns rows in _insert_tombstones
    column order."""
    parsed_qnames = {n.qualified_name for pf in parsed for n in pf.nodes}
    deleted_qnames: set[str] = set()
    pending: list[tuple] = []   # (old_node_row, file_deleted)
    for rel in sorted(changed_set | deleted_set):
        abs_path = os.path.join(repo, rel)
        rows = conn.execute(
            "SELECT * FROM nodes WHERE file_path=?", (abs_path,)).fetchall()
        if rel in deleted_set:
            pending.extend((r, 1) for r in rows)
            deleted_qnames.update(r["qualified_name"] for r in rows)
        else:
            delta = [r for r in rows if r["qualified_name"] not in parsed_qnames]
            pending.extend((r, 0) for r in delta)
            deleted_qnames.update(r["qualified_name"] for r in delta)
    head = current_head(config)
    out: list[tuple] = []
    for row, file_deleted in pending:
        upstream = _tombstone_upstream(conn, row["qualified_name"],
                                       deleted_qnames)
        out.append((row["qualified_name"], row["kind"], row["language"],
                    row["file_path"], row["start_line"], row["end_line"],
                    row["signature"], row["is_test"], row["decorators"],
                    head, file_deleted, json.dumps(upstream)))
    return out


def _insert_tombstones(conn, rows: list[tuple]) -> None:
    conn.executemany(
        "INSERT INTO tombstones(qname,kind,language,file_path,start_line,"
        "end_line,signature,is_test,decorators,deleted_at_head,file_deleted,"
        "upstream_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
```

在 `_apply_nodes_edges_delta` 的函数体开头（`touch = [...]` 之前）插入两行：

```python
    tombstone_rows = _collect_tombstones(
        conn, repo, parsed, changed_set, deleted_set, config)
    _insert_tombstones(conn, tombstone_rows)
```

注意：`_apply_nodes_edges_delta` 收到的 `changed_set` 实参是 `changed | added`（来自 `update_nodes_edges`）——added 文件无旧节点，delta 为空，天然安全；`deleted_set` 的文件不会出现在 `parsed` 里（无新 parse 内容）。

- [ ] **Step 4: 运行确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_incremental.py -v`
Expected: 4 个新测试 PASS + 既有测试全绿。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/update.py tests/test_incremental.py
git commit -m "feat(update): write tombstones with one-hop upstream on delete"
```

---

### Task 3: changes.py 读 tombstone → delete_change

**Files:**
- Modify: `code_review_ai/changes.py`（`_diff_coverage`、`build_change_summary`、`_symbols_summary`；顶部加 `import json`、`import os`）
- Modify: `code_review_ai/mcp_server.py:83-90`（`get_change_summary` docstring）
- Test: `tests/test_changes.py`、`tests/test_cli.py:31`、`tests/test_mcp_server.py:97`

**Interfaces:**
- Consumes: Task 1 的 `tombstones` 表 + Task 2 写入的 `upstream_json` 行；现有 `_relative_to_repo(config, file_path)`（changes.py 内）。
- Produces: `_delete_change(config, conn, deleted_files: set[str], numstat: dict[str, tuple[int,int]]) -> tuple[list[dict], set[str]]`（records + covered_files）；`_diff_coverage(..., covered_files=None)` 新增参。

- [ ] **Step 1: 更新既有 shape 断言（红）**

`tests/test_changes.py::test_build_change_summary_diff_path`（约 178 行）的 summary 断言加 `"delete_change": 0`，并加 `assert out["delete_change"] == []`：

```python
    assert out["summary"] == {"files_changed": 2, "lines_added": 10,
                              "lines_removed": 2, "changed_functions": 1,
                              "uncovered_changes": 1, "delete_change": 0}
    ...
    assert out["delete_change"] == []
```

`tests/test_cli.py:31` 与 `tests/test_mcp_server.py:97` 的 `set(data)` 都加 `"delete_change"`：

```python
    assert set(data) == {"summary", "changed_functions", "uncovered_changes",
                         "delete_change"}
```

`tests/test_changes.py::test_build_change_summary_symbols_path` 末尾加：

```python
    assert out["delete_change"] == []
    assert out["summary"]["delete_change"] == 0
```

- [ ] **Step 2: 运行确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_changes.py tests/test_cli.py tests/test_mcp_server.py -v`
Expected: 上述 3 个既有测试 FAIL（缺 `delete_change` 键），其余 PASS。

- [ ] **Step 3: 新增测试（红）**

在 `tests/test_changes.py` 顶部补 `import json`；末尾追加 helper 与测试（`_conn`、`load_config`、`FIX`、`os` 已就绪）：

```python
def _seed_tombstone(conn, qname, kind, rel_file, file_deleted, upstream):
    conn.execute(
        "INSERT INTO tombstones(qname,kind,file_path,file_deleted,upstream_json)"
        " VALUES(?,?,?,?,?)",
        (qname, kind, os.path.join(FIX, rel_file),
         1 if file_deleted else 0, json.dumps(upstream)))


def test_delete_change_from_tombstone_deleted_file(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    conn = _conn(tmp_path)
    _seed_tombstone(conn, "auth", "module", "auth.py", True,
                    [{"source": "app", "kind": "import",
                      "file": os.path.join(FIX, "app.py")}])
    _seed_tombstone(conn, "auth::login", "function", "auth.py", True,
                    [{"source": "app::main", "kind": "call",
                      "file": os.path.join(FIX, "app.py")}])
    conn.execute("DELETE FROM nodes WHERE file_path LIKE '%auth.py'")  # watcher 已清
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({}, {"auth.py"}))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (0, 3)})
    out = build_change_summary(cfg, conn)
    assert out["summary"]["delete_change"] == 2
    by_qname = {r["qname"]: r for r in out["delete_change"]}
    assert set(by_qname) == {"auth", "auth::login"}
    assert by_qname["auth"]["kind"] == "module"
    assert by_qname["auth"]["file_deleted"] is True
    assert by_qname["auth"]["file"] == "auth.py"
    assert by_qname["auth"]["upstream"] == [
        {"source": "app", "kind": "import", "file": "app.py"}]
    assert by_qname["auth::login"]["upstream"] == [
        {"source": "app::main", "kind": "call", "file": "app.py"}]
    assert out["uncovered_changes"] == []   # 被 delete_change 覆盖，不进 uncovered


def test_delete_change_from_tombstone_surviving_file(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    conn = _conn(tmp_path)
    _seed_tombstone(conn, "auth::login", "function", "auth.py", False,
                    [{"source": "app::main", "kind": "call",
                      "file": os.path.join(FIX, "app.py")}])
    conn.execute("DELETE FROM nodes WHERE qualified_name='auth::login'")
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({}, set()))  # 纯删除 -> 无 hunk
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (0, 2)})
    out = build_change_summary(cfg, conn)
    assert out["summary"]["delete_change"] == 1
    record = out["delete_change"][0]
    assert record["qname"] == "auth::login"
    assert record["file_deleted"] is False
    assert record["file"] == "auth.py"
    assert out["uncovered_changes"] == []   # 空 hunk uncovered 条目被抑制


def test_delete_change_ignores_reaadded_qname(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    conn = _conn(tmp_path)
    _seed_tombstone(conn, "auth::login", "function", "auth.py", False, [])
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({}, set()))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (0, 2)})
    out = build_change_summary(cfg, conn)
    assert out["delete_change"] == []       # qname 仍在活图 -> 不是当前删除
    assert out["uncovered_changes"] == [{"file": "auth.py", "hunks": []}]
```

- [ ] **Step 4: 运行确认失败**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_changes.py -v`
Expected: 3 个新测试 FAIL（`KeyError: 'delete_change'` 或 `delete_change` 为空），既有 `test_deleted_file_uncovered` 仍 PASS（无 tombstone → 仍走 uncovered）。

- [ ] **Step 5: 实现 `_delete_change`**

`changes.py` 顶部加 `import json` 与 `import os`。在 `_diff_coverage` 之后、`_changed_functions` 之前添加：

```python
def _delete_change(config: Config, conn, deleted_files: set[str],
                   numstat: dict[str, tuple[int, int]],
                   ) -> tuple[list[dict], set[str]]:
    """Deleted-function records for the current diff, from tombstones.

    Candidate files: deleted files + surviving files that removed lines.
    Tombstones are filtered to qnames no longer in the live graph (a tombstone
    whose qname was re-added isn't a current deletion), deduped per
    (file_path, qname) keeping the latest, and each becomes one delete_change
    record with its one-hop upstream. Returns (records, covered_files);
    covered_files is the set of files whose deletions delete_change reports so
    _diff_coverage suppresses their uncovered entries instead of double
    reporting them."""
    live = {r["qualified_name"]
            for r in conn.execute("SELECT qualified_name FROM nodes")}
    records: list[dict] = []
    covered: set[str] = set()
    candidates = set(deleted_files) | {
        rel for rel, (_, removed) in numstat.items() if removed > 0}
    for rel in sorted(candidates):
        abs_path = os.path.join(config.repo_path, rel)
        rows = conn.execute(
            "SELECT * FROM tombstones WHERE file_path=?", (abs_path,)).fetchall()
        latest: dict = {}
        for row in rows:
            if row["qualified_name"] in live:
                continue
            key = (row["file_path"], row["qualified_name"])
            if key not in latest or row["id"] > latest[key]["id"]:
                latest[key] = row
        file_records = [{
            "qname": row["qualified_name"], "kind": row["kind"], "file": rel,
            "file_deleted": bool(row["file_deleted"]),
            "start_line": row["start_line"], "end_line": row["end_line"],
            "signature": row["signature"], "is_test": row["is_test"],
            "upstream": [{"source": u["source"], "kind": u["kind"],
                          "file": _relative_to_repo(config, u["file"])}
                         for u in json.loads(row["upstream_json"] or "[]")],
        } for row in latest.values()]
        if file_records:
            file_records.sort(key=lambda r: r["start_line"])
            records.extend(file_records)
            covered.add(rel)
    return records, covered
```

- [ ] **Step 6: `_diff_coverage` 加 covered_files 抑制**

签名改为：

```python
def _diff_coverage(config: Config, diff_ranges: dict[str, list[tuple[int, int]]],
                   numstat: dict[str, tuple[int, int]], deleted: set[str],
                   kinds: tuple[str, ...] = ("function", "method", "class"),
                   covered_files: set[str] | None = None,
                   ) -> tuple[list[dict], list[dict]]:
```

函数体开头加 `covered_files = covered_files or set()`。删除文件分支改为：

```python
        if rel in deleted:
            if rel not in covered_files:
                uncovered.append({"file": rel, "hunks": [], "deleted": True})
            continue
```

空 hunk 分支（`if not hunks:`）改为：

```python
        if not hunks:
            if rel not in covered_files:
                uncovered.append({"file": rel, "hunks": []})
            continue
```

`_changed_functions` 的调用 `_diff_coverage(config, diff_ranges, {}, set(), kinds=kinds)` 不用改（`covered_files` 默认 `None` → `set()`）。

- [ ] **Step 7: 组装 `build_change_summary` + `_symbols_summary` + docstring**

`build_change_summary` diff 路径改为：

```python
    diff, deleted = _git_diff(base, files, config.repo_path)
    numstat = _git_numstat(base, files, config.repo_path)
    delete_change, covered_files = _delete_change(config, conn, deleted, numstat)
    functions, uncovered = _diff_coverage(config, diff, numstat, deleted,
                                          covered_files=covered_files)
    return {"summary": {"files_changed": len(numstat),
                        "lines_added": sum(added for added, _ in numstat.values()),
                        "lines_removed": sum(removed for _, removed in numstat.values()),
                        "changed_functions": len(functions),
                        "uncovered_changes": len(uncovered),
                        "delete_change": len(delete_change)},
            "changed_functions": functions,
            "uncovered_changes": uncovered,
            "delete_change": delete_change}
```

`_symbols_summary` 返回体改为：

```python
    return {"summary": {"files_changed": len(files), "lines_added": 0,
                        "lines_removed": 0, "changed_functions": len(symbols),
                        "uncovered_changes": 0, "delete_change": 0},
            "changed_functions": records,
            "uncovered_changes": [],
            "delete_change": []}
```

`code_review_ai/mcp_server.py:83-90` docstring 改为：

```python
        """Change summary: from the git diff (diff_base) compute `summary`
        (diff stats incl. uncovered_changes + delete_change counts) +
        `changed_functions` (changed function/method/class detail) +
        `uncovered_changes` (files whose changes no function/class covers —
        module-level hunks, unsupported extensions, binary, and deleted files
        without a tombstone) + `delete_change` (deleted functions/modules with
        their one-hop upstream, from tombstones written at update time). Pass
        explicit `symbols` to resolve those qnames from the graph instead of
        the diff. Returns a JSON object."""
```

- [ ] **Step 8: 运行确认通过**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_changes.py tests/test_cli.py tests/test_mcp_server.py -v`
Expected: 全部 PASS（含 3 个新测试 + 3 个更新过的既有测试）。

- [ ] **Step 9: 提交**

```bash
git add code_review_ai/changes.py code_review_ai/mcp_server.py \
        tests/test_changes.py tests/test_cli.py tests/test_mcp_server.py
git commit -m "feat(summary): delete_change field from tombstones with one-hop upstream"
```

---

### Task 4: 全量回归 + 真实 diff 验收

**Files:**
- Test: 全测试套件

**Interfaces:**
- Consumes: Task 1-3 全部改动。

- [ ] **Step 1: 全量测试**

Run: `./.venv/Scripts/python.exe -m pytest -v`
Expected: 全绿（现有 224 项 + 新增 ~10 项）。若有失败，先修后继续。

- [ ] **Step 2: 真实 diff 验收**

在某个含删除的工作树/分支上跑（用当前仓库自己的 diff）：

```bash
./.venv/Scripts/python.exe -m code_review_ai.cli summary --repo . --db .code-review-ai/index.db > _tombstone_check.json
```

Expected: 输出含 `delete_change` 数组与 `summary.delete_change` 计数；对其中每个记录，用 `git diff` 核对文件确已删除 / 函数确已不在，且 `upstream` 的 source 与实际调用/导入关系一致；被 delete_change 覆盖的文件不出现在 `uncovered_changes`。

- [ ] **Step 3: 提交（如验收发现 spec 偏差则回修并提交）**

若一切正常，无额外提交；如有修正，`git commit -m "fix(summary): ..."`。

---

## Self-Review

**Spec 覆盖：**
- tombstones 表 + 追加式不清 → Task 1 + Task 2 的 `test_tombstones_survive_rebuild`。✅
- 写 tombstone 在删除 loop 之前、上游排除同批 source → Task 2 实现 + `test_tombstone_upstream_excludes_same_batch_sources`。✅
- 整文件删（file_deleted=1）/ 存活文件差集（file_deleted=0）→ Task 2 两个测试。✅
- 上游只取 call/inherits/import、不含 contains → `_tombstone_upstream` SQL。✅
- delete_change shape、record.file/upstream.file repo-relative → Task 3 `_delete_change`。✅
- 无 tombstone 删除留 uncovered → `_diff_coverage` covered_files 分支 + 既有 `test_deleted_file_uncovered`。✅
- symbols 路径空 delete_change → `_symbols_summary`。✅
- 已删又加回忽略 → `test_delete_change_ignores_reaadded_qname`。✅
- 已知限制写进 spec（重建后上游未知、fallback 不解析旧文件等）→ spec 文档已含，本计划不做，属非目标。✅

**占位符扫描：** 无 TBD/TODO；每个 Step 都含真实代码与确切命令。

**类型一致性：** `_collect_tombstones` 返回列序与 `_insert_tombstones` SQL 列序一致（12 列：qname..upstream_json）；`_delete_change` 读取的 `upstream_json` 由 `_tombstone_upstream` 的 `{source, kind, file}` dict 序列化而来，Task 3 反序列化读取同一组键。`_diff_coverage` 新参 `covered_files` 在 Task 3 内同时改签名、调用处与分支，无残留旧调用。
