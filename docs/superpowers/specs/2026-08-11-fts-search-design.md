# 设计：FTS 全文搜索

日期：2026-08-11

## 目标

给 `code-review-ai` 加 SQLite FTS5 全文搜索，覆盖符号级内容：qname、file_path、
signature、decorators、end_line。**升级现有入口**（MCP `search_symbol` + CLI `search`），
不做新工具：带 `*`/`?` 的查询保持 glob 语义（向后兼容），纯词查询走 FTS token
匹配 + bm25 排名，0 命中时回退大小写不敏感 LIKE 中缀匹配。

当前 `search_symbol` 是 O(n) 全表扫描 + `fnmatch` 短名 glob（`mcp_server.py:105`、
`cli.py:222`）：无索引、无排名、匹配不到签名/路径/decorator、`login` 找不到
`login_user`。本设计用 FTS5 索引补上这些缺口。

明确不做：源码全文（函数体/文件内容）、Spring 路由（`ParsedNode.mappings` 暂不
持久化，留给后续）。

## 数据模型：外部内容表 `fts_nodes`

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS fts_nodes USING fts5(
    qualified_name, file_path, signature, decorators, end_line,
    content='nodes', content_rowid='id'
);
```

- `content='nodes'`：**FTS 索引内容直接取自 nodes 表本身**（单一事实来源），不存在
  第二份 content 字符串、无拼写漂移。索引列是 nodes 的可搜索列子集。
- `rowid ↔ nodes.id`：删除/JOIN 都走 rowid。
- `end_line` 纳入索引列与查询结果（评审需要完整函数范围）。
- kind 不进 FTS（查询时 JOIN nodes 过滤）。
- unicode61 分词天然适合代码：`auth::UserService.authenticate` → `auth` /
  `UserService` / `authenticate`；下划线是 token 字符，`login_user` 保持整体。
- `INDEX_VERSION` 5→6（`db.py`）：schema 变更，按既有惯例由 `_meta_changed`
  门控触发旧库全量重建。

## 写路径维护（新建 `search.py`）

`search.py` 一个模块负责 FTS 读写（一模块一职责），`indexer` / `update` 共用。

```python
def index_fts(conn, parsed_nodes, qname_to_id) -> None   # 增量：逐行 DELETE+INSERT
def deindex_fts(conn, node_ids) -> None                   # 增量删除
def reindex_all(conn) -> None                             # 全量：rebuild 命令
def fts_search(conn, query, limit=50) -> list[dict]       # 查询
```

- **全量**（`indexer.py`）：
  - `_clear_tables` 加 `DELETE FROM fts_nodes`。
  - `_write_nodes` 插完 nodes 后一行 `INSERT INTO fts_nodes(fts_nodes) VALUES('rebuild')`
    —— 从 nodes 表整体重建索引，零逐行代码。
- **增量**（`update.py`）：
  - `_apply_nodes_edges_delta` 删除路径：对已收集的 `removed_ids` 调 `deindex_fts`
    （`DELETE FROM fts_nodes WHERE rowid IN (...)`）。
  - `_insert_nodes`：逐行 `INSERT INTO fts_nodes(rowid, qualified_name, file_path,
    signature, decorators, end_line) VALUES(?,?,?,?,?,?)`（值就是刚写进 nodes 的那批）。

## 迁移

`INDEX_VERSION` 5→6。旧库缺 `fts_nodes` 表：`init_schema` 的 `CREATE ... IF NOT
EXISTS` 建表；下一次 `sync`（MCP 启动 `startup_sync` / 钩子 / `rebuild_index`）的
`_meta_changed` 检测到版本不符 → 全量 rebuild → `_write_nodes` 经 `reindex_all`
灌 FTS。走既有「schema 变更即重建」惯例，不写一次性 backfill。代价：旧库首次
启动全量重解析（与其它 schema 变更等价）。

## 查询语义（`fts_search`）

| 输入 | 行为 |
|---|---|
| 含 `*`/`?`（如 `*login*`） | **glob 模式**：`fnmatch` 短名，向后兼容现有行为，`score=null` |
| 纯词（如 `login`、`get owner`） | **FTS 模式**：清洗 token（剥 FTS 操作符）→ 逐词前缀展开 `login*` → `AND` 连接 → `MATCH` + `bm25` 排名，取 top-N |
| FTS 命中 0 条 | **LIKE 兜底**：`lower(qname\|\|' '\|\|file_path\|\|' '\|\|signature\|\|' '\|\|COALESCE(decorators,'')\|\|' '\|\|end_line) LIKE '%q%'`（COALESCE 防 decorators 为 NULL 使整体拼接失效），覆盖中缀（`user` → `login_user`） |

- FTS 模式大小写不敏感（unicode61 默认），优于现在 glob 的大小写敏感。
- glob 模式保持短名 contract；FTS/LIKE 覆盖全部索引列文本。

### 查询 SQL（FTS 模式）

```sql
SELECT n.qualified_name, n.kind, n.file_path, n.start_line, n.end_line,
       n.signature, bm25(fts_nodes) AS score
FROM fts_nodes JOIN nodes n ON n.id = fts_nodes.rowid
WHERE fts_nodes MATCH ? AND n.kind IN ('function','method','class')
ORDER BY bm25(fts_nodes) LIMIT ?
```

## JSON 结果形状

```json
[
  {"qname": "auth::login", "kind": "function", "file": "code_review_ai/auth.py",
   "line": 12, "end_line": 30, "signature": "def login(...)", "score": -3.2}
]
```

glob 模式 `score` 为 `null`；FTS 模式返回 bm25 分（越低越相关，升序）。

## 组件设计

### `search.py` — 新模块

`index_fts` / `deindex_fts` / `reindex_all` / `fts_search` 四个函数 + glob/FTS/LIKE
分支的私有辅助。`fts_search` 只编排：判断模式 → 组装查询 → 返回结果列表。

### `mcp_server.py` — 升级 `search_symbol`

```python
@mcp.tool()
def search_symbol(query: str, limit: int = 50) -> str:
    """按 glob（含 * / ?）或全文检索查找符号；纯词走 FTS token 匹配 + 排名，
    0 命中时回退中缀子串。返回 JSON 列表，含 score。"""
    return json.dumps(fts_search(conn, query, limit=limit))
```

### `cli.py` — 升级 `search`

`search` 子命令改调 `fts_search`，加 `--limit`（默认 50），保持
`file:line<TAB>kind<TAB>qname` 输出格式（追加 signature 列）。

## 测试

新 `tests/test_search.py`：

- 全量 rebuild 后能搜到符号；`*login*` glob 兼容不变。
- FTS token：`login` → `auth::login`；前缀命中 `login_user`。
- 多词 AND；bm25 排序 + limit 截断。
- LIKE 中缀兜底：`user` → `login_user`。
- 增量：改文件 → sync → 新符号可搜；删文件 → tombstone → 不再命中。
- 结果含 `end_line`。

既有测试更新：

- `tests/test_mcp_server.py`：`search_symbol` 加 `limit` 参数与 FTS 命中断言。
- `tests/test_cli.py`：`search` 断言适配新输出（+ signature 列 / `--limit`）。

## 文档

- `CLAUDE.md`：`search` 命令说明补全文检索 + `--limit`。
- `README.md`：工具列表补 `search_symbol` 全文检索说明。
- 本 spec。
