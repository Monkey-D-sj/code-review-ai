# FTS 全文搜索实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给符号检索加 SQLite FTS5 全文搜索（索引 qname / file_path / signature / decorators / end_line），升级 MCP `search_symbol` 与 CLI `search` 两个现有入口。

**Architecture:** `fts_nodes` 是 nodes 表的 FTS5 **外部内容表**（`content='nodes'`, `rowid=nodes.id`），单一事实来源、无第二份 content 字符串。写路径：全量 rebuild 与增量 update 统一用 `index_fts` 逐行 INSERT（新符号）/ `deindex_fts` 按 rowid DELETE（删除符号）；`fts_nodes` 作为外部内容表仍保留 `reindex_all`（`'rebuild'` 命令）作恢复工具——但该命令自开事务，只能事务外调用，**不作为 rebuild 事务内的灌入机制**（见 Task 3）。查询：含 `*`/`?` → 短名 glob（向后兼容）；纯词 → FTS token 前缀展开 + bm25 排名；0 命中 → 大小写不敏感 LIKE 中缀兜底。

**Tech Stack:** Python 3.14（uv 管理）、内置 sqlite3 3.50.4（FTS5 可用，`pragma compile_options` 不显示但实测 `CREATE VIRTUAL TABLE USING fts5` 与 MATCH/rebuild/bm25 均正常）。

## Global Constraints

- **INDEX_VERSION 必须从 5 升到 6**（`code_review_ai/db.py:8`）：schema 变更按既有惯例由 `_meta_changed` 门控触发旧库全量重建，**不写一次性 backfill**。
- `fts_nodes` 是**外部内容表**：`content='nodes', content_rowid='id'`，索引列名必须与 nodes 表列名一致。`kind` 不进 FTS，查询时 JOIN nodes 过滤 `kind IN ('function','method','class')`。
- 明确不做：源码全文（函数体）、Spring 路由（`ParsedNode.mappings`）。
- 查询语义（spec §查询语义）：含 `*`/`?` → glob 短名（`score=null`）；纯词 → 清洗 token（只留 `[A-Za-z0-9_]`，全符号输入返回空）→ 逐词前缀展开 `token*` → `AND` 连接 → `MATCH` + `bm25` 升序（低分靠前）取 top-N；FTS 0 命中 → LIKE 兜底（`lower(...||COALESCE(decorators,'')||...) LIKE '%q%' ESCAPE '\'`）。
- 结果形状固定：`{qname, kind, file, line, end_line, signature, score}`；glob 模式 `score=None`。
- 项目惯例：qname 一律走 `code_review_ai.qname`（`qname.short` 用于 glob 短名），禁止手拼；一模块一职责；禁止单字母变量；主控函数只编排。
- 测试惯例：单元测试直接 `connect` + `init_schema` + 手插 nodes；全量集成用 `rebuild(cfg, conn)`；增量集成用 `_git_repo(tmp_path)`（copy FIXTURES + git init/commit，见 `tests/test_incremental.py:16`）。fixtures：`auth.py`（`auth::login`, `auth::UserService.authenticate`）、`util.py`（`util::hash_pw`）、`ts/auth.ts`（`ts.auth::login`）。
- 既有测试必须同步：`tests/test_db.py:61` 的 `assert INDEX_VERSION == 5` 改 6。

---

### Task 1: db.py — fts_nodes schema + INDEX_VERSION 升 6

**Files:**
- Modify: `code_review_ai/db.py:8`（INDEX_VERSION）、`code_review_ai/db.py:10-98`（SCHEMA 末尾）
- Modify: `tests/test_db.py:61`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: 无（首个任务）。
- Produces: `init_schema(conn)` 执行后存在 `fts_nodes` 虚拟表；`INDEX_VERSION == 6`。

- [ ] **Step 1: 更新既有版本断言为红**

`tests/test_db.py:61` 改为 `assert INDEX_VERSION == 6`，并新增一个 schema 存在性测试（追加到该文件末尾）：

```python
def test_init_schema_creates_fts_nodes(tmp_path):
    conn = connect(str(tmp_path / "fts.db"))
    init_schema(conn)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fts_nodes'"
    ).fetchone()
    assert row is not None
    conn.close()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL —— `INDEX_VERSION == 5` 断言失败（`assert 5 == 6`）。

- [ ] **Step 3: 升版本 + 建表**

`code_review_ai/db.py:8`：
```python
INDEX_VERSION = 6
```

在 `code_review_ai/db.py` SCHEMA 字符串**末尾**（`CREATE INDEX ... idx_tombstones_qname` 之后、闭合 `"""` 之前）追加：

```python
CREATE VIRTUAL TABLE IF NOT EXISTS fts_nodes USING fts5(
    qualified_name, file_path, signature, decorators, end_line,
    content='nodes', content_rowid='id'
);
```

> 说明：外部内容表在 CREATE 时**不校验** content 表列是否存在，所以 `tests/test_db.py` 里那些先建无 decorators 的 legacy nodes 表再跑 `init_schema` 的迁移测试仍安全（`_migrate_nodes` 会补列）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS（含新的 `test_init_schema_creates_fts_nodes`）。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/db.py tests/test_db.py
git commit -m "feat(db): fts_nodes external-content table; INDEX_VERSION 5->6"
```

---

### Task 2: search.py — FTS 读写模块 + 单元测试

**Files:**
- Create: `code_review_ai/search.py`
- Test: `tests/test_search.py`

**Interfaces:**
- Consumes: Task 1 的 `init_schema(conn)` 建出的 `fts_nodes` 表。
- Produces（后序任务依赖的确切签名）:
  - `index_fts(conn, parsed_nodes, qname_to_id: dict[str, int]) -> None` —— 增量插；`parsed_nodes` 元素需有 `.qualified_name / .file_path / .signature / .decorators(列表) / .end_line`（与 `ParsedNode` 一致）；`qname_to_id` 映射 qualified_name → nodes.id。
  - `deindex_fts(conn, node_ids: list[int]) -> None` —— 增量删，空列表为 no-op。
  - `reindex_all(conn) -> None` —— 全量 `'rebuild'` 命令。
  - `fts_search(conn, query: str, limit: int = 50) -> list[dict]` —— 返回 `[{qname, kind, file, line, end_line, signature, score}]`。

- [ ] **Step 1: 写失败的单元测试**

新建 `tests/test_search.py`（`from conftest import Q` 可用但本文件用字面 qname 更直白）：

```python
import json

from code_review_ai.db import connect, init_schema
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_search.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'code_review_ai.search'`（红在导入）。

- [ ] **Step 3: 写最小实现 `code_review_ai/search.py`**

```python
"""Full-text search over indexed symbols via an FTS5 external-content table.

`fts_nodes` mirrors the `nodes` table (content='nodes', rowid = nodes.id). This
module owns the write-side helpers — index_fts (incremental insert), deindex_fts
(incremental delete), reindex_all (full 'rebuild' command) — and the query,
fts_search. Query semantics: wildcard queries (* / ?) keep the legacy short-name
glob; plain words run FTS token match with per-token prefix expansion + bm25
ranking; 0 hits fall back to a case-insensitive LIKE infix over the node's
searchable columns.
"""

import fnmatch
import json
import sqlite3

from code_review_ai import qname

_INDEX_COLUMNS = ("qualified_name", "file_path", "signature", "decorators", "end_line")
_SEARCH_KINDS = "('function','method','class')"


def _fts_insert_sql() -> str:
    columns = ",".join(_INDEX_COLUMNS)
    placeholders = ",".join("?" for _ in _INDEX_COLUMNS)
    return f"INSERT INTO fts_nodes(rowid,{columns}) VALUES(?,{placeholders})"


def _node_fts_row(node, qname_to_id: dict[str, int]) -> tuple:
    """One fts row per parsed node; values mirror what a full 'rebuild' would
    read from the nodes table so the index stays consistent either way."""
    return (qname_to_id[node.qualified_name], node.qualified_name,
            node.file_path, node.signature, json.dumps(node.decorators),
            node.end_line)


def index_fts(conn, parsed_nodes, qname_to_id: dict[str, int]) -> None:
    """Index nodes just written to `nodes` into fts_nodes (incremental path)."""
    rows = [_node_fts_row(node, qname_to_id) for node in parsed_nodes
            if node.qualified_name in qname_to_id]
    if rows:
        conn.executemany(_fts_insert_sql(), rows)


def deindex_fts(conn, node_ids: list[int]) -> None:
    """Remove fts rows for deleted node ids (fts rowid = nodes.id). No-op on
    an empty list."""
    if not node_ids:
        return
    placeholders = ",".join("?" for _ in node_ids)
    conn.execute(
        f"DELETE FROM fts_nodes WHERE rowid IN ({placeholders})", node_ids)


def reindex_all(conn) -> None:
    """Rebuild the FTS index from the nodes content table (external-content
    'rebuild' command). Called by the full-rebuild path after nodes are
    written."""
    conn.execute("INSERT INTO fts_nodes(fts_nodes) VALUES('rebuild')")


def fts_search(conn, query: str, limit: int = 50) -> list[dict]:
    """Search indexed symbols.

    Returns [{qname, kind, file, line, end_line, signature, score}] sorted by
    relevance (score = bm25 in FTS mode, None in glob mode). `query` containing
    `*`/`?` runs the legacy short-name glob; plain words run FTS with a LIKE
    infix fallback when nothing matches."""
    if any(ch in query for ch in "*?"):
        return _glob_search(conn, query, limit)
    match_expr = _match_expr(query)
    if match_expr is None:
        return []
    rows = _fts_match(conn, match_expr, limit)
    if rows:
        return rows
    return _like_fallback(conn, query, limit)


def _match_expr(query: str) -> str | None:
    """Build an FTS5 MATCH expression from a plain-word query: sanitize each
    whitespace token to [A-Za-z0-9_], prefix-expand, AND-join. None when no
    usable token survives (e.g. all-punctuation input)."""
    tokens = []
    for word in query.split():
        token = "".join(ch for ch in word if ch.isalnum() or ch == "_")
        if token and any(ch.isalnum() for ch in token):
            tokens.append(f"{token}*")
    return " AND ".join(tokens) if tokens else None


def _fts_match(conn, match_expr: str, limit: int) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT n.qualified_name AS qname, n.kind, n.file_path AS file, "
        "n.start_line AS line, n.end_line, n.signature, bm25(fts_nodes) AS score "
        "FROM fts_nodes JOIN nodes n ON n.id = fts_nodes.rowid "
        f"WHERE fts_nodes MATCH ? AND n.kind IN {_SEARCH_KINDS} "
        "ORDER BY bm25(fts_nodes) LIMIT ?", (match_expr, limit))]


def _like_fallback(conn, query: str, limit: int) -> list[dict]:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    return [dict(row) for row in conn.execute(
        "SELECT qualified_name AS qname, kind, file_path AS file, "
        "start_line AS line, end_line, signature, NULL AS score FROM nodes "
        f"WHERE kind IN {_SEARCH_KINDS} AND "
        "lower(qualified_name||' '||file_path||' '||signature||' '||"
        "COALESCE(decorators,'')||' '||end_line) LIKE ? ESCAPE '\\' "
        "LIMIT ?", (pattern.lower(), limit))]


def _glob_search(conn, query: str, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT qualified_name,kind,file_path,start_line,end_line,signature "
        f"FROM nodes WHERE kind IN {_SEARCH_KINDS}").fetchall()
    out: list[dict] = []
    for row in rows:
        if fnmatch.fnmatch(qname.short(row["qualified_name"]), query):
            out.append({"qname": row["qualified_name"], "kind": row["kind"],
                        "file": row["file_path"], "line": row["start_line"],
                        "end_line": row["end_line"], "signature": row["signature"],
                        "score": None})
            if len(out) >= limit:
                break
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_search.py -v`
Expected: PASS（10 个测试全绿）。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/search.py tests/test_search.py
git commit -m "feat(search): FTS5 index_fts/deindex_fts/reindex_all/fts_search"
```

---

### Task 3: indexer.py — 全量 rebuild 写 FTS

**Files:**
- Modify: `code_review_ai/indexer.py:90-98`（`_clear_tables`）、`code_review_ai/indexer.py:66-67`（`rebuild`）
- Test: `tests/test_search.py`（追加集成测试）

**Interfaces:**
- Consumes: Task 2 的 `index_fts(conn, inserted, qname_to_id)`。
- Produces: `_write_nodes` 返回 `(qname_to_id, inserted)` 元组；全量 rebuild 后 `fts_nodes` 与 nodes 内容一致（可被 `fts_search` 命中）。

- [ ] **Step 1: 写失败的集成测试**

`tests/test_search.py` 追加（头部补 import）：

```python
from code_review_ai.config import load_config
from code_review_ai.indexer import rebuild
from conftest import FIXTURES as FIX


def test_rebuild_populates_fts(tmp_path):
    cfg = load_config(FIX)
    cfg.repo_path = FIX
    conn = connect(str(tmp_path / "r.db"))
    init_schema(conn)
    rebuild(cfg, conn)
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_search.py::test_rebuild_populates_fts -v`
Expected: FAIL —— rebuild 后 `fts_search("login")` 返回空（`fts_nodes` 从未填充）。

- [ ] **Step 3: 实现**

`code_review_ai/indexer.py:90-98` 的 `_clear_tables`，在 `DELETE FROM nodes` 之后加一行：

```python
def _clear_tables(conn: sqlite3.Connection) -> None:
    """Delete every table, child-first for FK safety on nodes.parent_id."""
    conn.execute("DELETE FROM flow_memberships")
    conn.execute("DELETE FROM flows")
    conn.execute("DELETE FROM community_memberships")
    conn.execute("DELETE FROM community_edges")
    conn.execute("DELETE FROM communities")
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM nodes")
    conn.execute("DELETE FROM fts_nodes")
```

`code_review_ai/indexer.py:101-136` 的 `_write_nodes`：返回类型改为 `tuple[dict[str, int], list]`，末尾 `return qname_to_id, inserted`（docstring 补一句：返回值含本次去重后插入的节点列表，rebuild 用它灌 FTS）。

`code_review_ai/indexer.py:66-68` 的 `rebuild`，解包 + 调 `index_fts`：

```python
    with transaction(conn):
        _clear_tables(conn)
        qname_to_id, inserted = _write_nodes(conn, parsed, config)
        index_fts(conn, inserted, qname_to_id)
        _write_edges(conn, all_edges)
```

`code_review_ai/indexer.py` 头部 import 区加：

```python
from code_review_ai.search import index_fts
```

> 说明：**不能在这里用 `reindex_all`（`'rebuild'` 命令）**——它内部自开事务，在 `with transaction(conn)` 显式事务内会抛 `OperationalError: cannot start a transaction within a transaction`（已实测，`/tmp/fts_txn.py`）。改用与增量路径同一 `index_fts` 逐行灌入，保持事务原子性（失败整体回滚）。`_clear_tables` 的 `DELETE FROM fts_nodes` 必须保留：nodes 全量重插后 rowid 是新的，不清会留下指向旧 rowid 的孤儿 FTS 行。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_search.py -v`
Expected: PASS（含新集成测试；此文件 11 个测试全绿）。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/indexer.py tests/test_search.py
git commit -m "feat(indexer): rebuild reindexes fts_nodes after writing nodes"
```

---

### Task 4: update.py — 增量 update 维护 FTS

**Files:**
- Modify: `code_review_ai/update.py:194-195`（delta 删除路径）、`code_review_ai/update.py:229-238`（`_insert_nodes`）
- Test: `tests/test_search.py`（追加集成测试）

**Interfaces:**
- Consumes: Task 2 的 `index_fts(conn, inserted, qname_to_id)` 与 `deindex_fts(conn, removed_ids)`。
- Produces: 增量 update 后新符号可搜、删除符号不再命中。

- [ ] **Step 1: 写失败的集成测试**

`tests/test_search.py` 追加（头部补 import：`shutil`、`subprocess`、`from code_review_ai import update as upd`），并加 `_git_repo` 辅助（与 `tests/test_incremental.py:16` 一致，返回 `tmp_path / "repo"` 这个 Path）：

```python
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


def test_incremental_delete_deindexes_symbol(tmp_path):
    repo = _git_repo(tmp_path)
    cfg = load_config(str(repo))
    cfg.repo_path = str(repo)
    conn = connect(str(tmp_path / "d.db"))
    init_schema(conn)
    rebuild(cfg, conn)
    repo.joinpath("auth.py").unlink()
    upd.update_nodes_edges(cfg, conn, ["auth.py"])
    hits = fts_search(conn, "login")
    assert not any(h["qname"] == "auth::login" for h in hits)
    # ts/auth.ts 的 login 不受影响
    assert any(h["qname"] == "ts.auth::login" for h in hits)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_search.py -k incremental -v`
Expected: FAIL —— 增量后 `brand_new` 搜不到 / 删除后 `auth::login` 仍命中（增量路径未维护 FTS）。

- [ ] **Step 3: 实现**

`code_review_ai/update.py:188-193` 的 `_apply_nodes_edges_delta` 删除路径：逐路径先收集 ids、**`deindex_fts` 必须在 `DELETE FROM nodes` 之前**（外部内容表的 FTS DELETE 在对应内容行已删时会静默 no-op、泄漏陈旧索引条目，已实测 `/tmp/fts_delete2.py`）：

```python
    removed_ids: list[int] = []
    for abs_path in touch:
        path_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM nodes WHERE file_path=?", (abs_path,))]
        removed_ids += path_ids
        deindex_fts(conn, path_ids)   # 必须在 DELETE nodes 之前
        conn.execute("DELETE FROM edges WHERE file_path=?", (abs_path,))
        conn.execute("DELETE FROM nodes WHERE file_path=?", (abs_path,))
    if removed_ids:
        _delete_memberships(conn, removed_ids)
```

`code_review_ai/update.py:229-238` 的 `_insert_nodes`，`parent_updates` 回填之后、`return len(rows)` 之前加：

```python
    if parent_updates:
        conn.executemany(
            "UPDATE nodes SET parent_id=? WHERE id=?", parent_updates)
    index_fts(conn, inserted, qname_to_id)
    return len(rows)
```

`code_review_ai/update.py` 头部 import 区加：

```python
from code_review_ai.search import deindex_fts, index_fts
```

> 说明：`_apply_nodes_edges_delta` 里 `removed_ids` 是按 file_path 收集的整文件旧节点 id（含被改文件里删掉的符号），全部 deindex，**且必须发生在 `DELETE FROM nodes` 之前**（FTS5 外部内容表的 DELETE 需要对应内容行存在才能算出要删的 token，内容行已删则静默 no-op）。`_insert_nodes` 的 `inserted` 是该批次实际插入的 ParsedNode 列表，`qname_to_id` 是全库映射（含去重），二者组合精确维护 FTS。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_search.py -v`
Expected: PASS（全部 13 个测试绿）。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/update.py tests/test_search.py
git commit -m "feat(update): keep fts_nodes in sync on incremental add/delete"
```

---

### Task 5: mcp_server.py — search_symbol 升级

**Files:**
- Modify: `code_review_ai/mcp_server.py:1`（删 import）、`:3`（删 import）、import 区（加）、`:104-115`（search_symbol）
- Test: `tests/test_mcp_server.py:48-52`

**Interfaces:**
- Consumes: Task 2 的 `fts_search(conn, query, limit=50)`。
- Produces: `search_symbol(query: str, limit: int = 50) -> str`，返回 JSON 列表，元素含 `qname/kind/file/line/end_line/signature/score`。

- [ ] **Step 1: 更新工具测试为红**

`tests/test_mcp_server.py:48-52` 替换为：

```python
def test_search_symbol_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    tools = server._tool_manager._tools
    out = tools["search_symbol"].fn(query="login", limit=10)
    data = json.loads(out)
    hit = next(d for d in data if d["qname"] == Q("auth", "login"))
    assert hit["kind"] == "function"
    assert hit["file"] == "auth.py"
    assert hit["end_line"] >= hit["line"]
    assert "signature" in hit and "score" in hit


def test_search_symbol_glob_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    out = server._tool_manager._tools["search_symbol"].fn(query="*login*")
    data = json.loads(out)
    assert any(d["qname"] == Q("auth", "login") for d in data)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_mcp_server.py::test_search_symbol_tool tests/test_mcp_server.py::test_search_symbol_glob_tool -v`
Expected: FAIL —— `search_symbol` 拒绝 `limit` 关键字参数（`unexpected keyword argument 'limit'`），且返回形状缺 `end_line`/`score`。

- [ ] **Step 3: 实现**

`code_review_ai/mcp_server.py`：
- 删 `:1` `from code_review_ai import qname` 与 `:3` `import fnmatch`。
- import 区（`from code_review_ai.deadcode import ...` 之后）加 `from code_review_ai.search import fts_search`。
- `:104-115` 的 `search_symbol` 整体替换为：

```python
    @mcp.tool()
    def search_symbol(query: str, limit: int = 50) -> str:
        """Discover symbols by full-text search or glob on their short name
        (e.g. "*login*", "UserService", "login"). Pure-word queries run FTS
        token match + bm25 ranking, falling back to substring on 0 hits.
        Returns a JSON list of {qname, kind, file, line, end_line, signature,
        score}. Use to find qualified names before get_symbol_detail /
        get_impact."""
        return json.dumps(fts_search(conn, query, limit=limit))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: PASS（两个新测试 + 既有工具测试全绿）。同时 `uv run pytest tests/test_search.py -q` 不受影响。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(mcp): search_symbol full-text via fts_search with limit"
```

---

### Task 6: cli.py — search 子命令升级

**Files:**
- Modify: `code_review_ai/cli.py:1`（删 import）、import 区（加）、`:91-93`（parser）、`:222-230`（search 分支）
- Test: `tests/test_cli.py:9-20`

**Interfaces:**
- Consumes: Task 2 的 `fts_search(conn, query, limit)`。
- Produces: `search` 子命令接受 `--limit`（默认 50），输出保持 `qname  kind  file:start-end` 行格式并追加 signature。

- [ ] **Step 1: 更新 CLI 测试为红**

`tests/test_cli.py:9-20` 替换为：

```python
def test_cli_search(tmp_path, capsys):
    # rebuild first, then search
    code = main(["rebuild", "--repo", FIX,
                 "--db", str(tmp_path / "c.db")])
    assert code == 0
    _ = capsys.readouterr()  # discard rebuild output

    code = main(["search", "login", "--limit", "5", "--repo", FIX,
                 "--db", str(tmp_path / "c.db")])
    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    hit = next(line for line in lines if Q("auth", "login") in line)
    assert "function" in hit and "auth.py" in hit
    assert "def login" in hit  # signature 列
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_cli.py::test_cli_search -v`
Expected: FAIL —— `search` 子命令拒绝 `--limit`（`unrecognized arguments`）。

- [ ] **Step 3: 实现**

`code_review_ai/cli.py`：
- 删 `:1` `from code_review_ai import qname`。
- import 区加 `from code_review_ai.search import fts_search`（放 `from code_review_ai.indexer import rebuild` 之后）。
- `:91-93` 的 parser 追加 `--limit`：

```python
    sp = sub.add_parser("search")
    _add_common(sp)
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=50,
                    help="max results (default: 50)")
```

- `:222-230` 的 search 分支替换为：

```python
    elif args.cmd == "search":
        for r in fts_search(conn, args.query, limit=args.limit):
            signature = f"  {r['signature']}" if r.get("signature") else ""
            print(f"{r['qname']}  {r['kind']}  {r['file']}:{r['line']}-{r['end_line']}{signature}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_cli.py::test_cli_search -v`
Expected: PASS（行含 `auth::login`、`function`、`auth.py`、`def login`）。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/cli.py tests/test_cli.py
git commit -m "feat(cli): search full-text with --limit and signature column"
```

---

### Task 7: 文档

**Files:**
- Modify: `CLAUDE.md`、`README.md`

**Interfaces:**
- Consumes: 前 6 个任务的新行为。
- Produces: 文档反映全文检索语义与 `--limit`。

- [ ] **Step 1: 更新 CLAUDE.md**

`CLAUDE.md` 的 Commands 一节，把 `search` 相关行补全文检索语义。原文：

```
uv run code-review-ai search  "login"                       # glob-match symbol short names
```

改为：

```
uv run code-review-ai search  "login" [--limit 50]          # full-text (FTS) or glob (*login*) symbol search
```

- [ ] **Step 2: 更新 README.md**

README 工具列表里 `search_symbol` 描述补一句全文检索：纯词走 FTS token 匹配 + bm25 排名，0 命中回退中缀子串；含 `*`/`?` 保持短名 glob。

- [ ] **Step 3: 全量回归**

Run: `uv run pytest -q`
Expected: PASS（全部既有 + 新增测试）。

- [ ] **Step 4: 提交**

```bash
git add CLAUDE.md README.md
git commit -m "docs: document full-text search semantics and --limit"
```
