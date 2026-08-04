# 设计：新增 `query_graph` 图邻域查询工具

日期：2026-08-04

## 目标

在 `code-review-ai` 中新增 `query_graph`（MCP tool + CLI `query-graph` 子命令）：
给定一个符号 qname，返回它的**图邻域**——「用了它的」节点（in）+「被它用的」
节点（out），边类型与方向可配置。默认边类型 CALL（`edge_kind="call"`）、
双向（`direction="both"`）。它补足 `get_symbol_detail` 的三处缺口：只回 qname、
未按边类型过滤（callers/callees 混入 contains/import/extends）、无方向控制。
`get_symbol_detail` 保持「单符号详情」职责不变。

## JSON 结构

```json
{
  "qname": "auth::login",
  "kind": "function",
  "file": "auth.py",
  "line": 6,
  "signature": "login(user, pw) -> str",
  "edge_kind": "call",
  "direction": "both",
  "in": [
    {"qname": "app::main", "kind": "function", "file": "app.py", "line": 5, "signature": "main() -> None"}
  ],
  "out": []
}
```

- `in` = 指向该节点的邻居（对 call：「用了它」/调用者）；`out` = 它指向的
  邻居（「被它用」/被调用者）。`direction` 限定时只返回对应一侧。
- 节点不存在 → `{"qname": ..., "found": false, "in": [], "out": []}`。
- 邻居只走 `resolution='resolved'` 的边（只有 resolved 另一端才是真实节点，才有
  详细对象）；dynamic/unresolved 边不出现。DISTINCT + 按 qname 去重、每方向限量
  （默认 50）。

## 边类型与方向语义

`edges.kind` 的取值（见 `resolver.py`）：`call`、`contains`（父→子，module→
function / class→method）、`import`（module→module）、`extends`、`implements`
（TS 继承）。`edge_kind` 参数取这些值之一，`all` = 不过滤 kind。

- `in`：`SELECT DISTINCT source FROM edges WHERE target=? AND resolution='resolved'
  [AND kind=?]`
- `out`：`SELECT DISTINCT target FROM edges WHERE source=? AND resolution='resolved'
  [AND kind=?]`

## 组件设计

### `graph.py` — 新模块（一模块一职责）

```python
def query_graph(conn, qualified_name: str, edge_kind: str = "call",
                direction: str = "both", max_per_dir: int = 50) -> dict
```

- 参数校验：`edge_kind ∈ {call, contains, import, extends, implements, all}`、
  `direction ∈ {in, out, both}`，非法抛 `ValueError`（消息带合法值列表）。
- 主控只做编排：查节点 → 查 in/out → 邻居转 detail → 组装返回。
- 本地小助手 `_node_brief(conn, qname)`（→ `{qname, kind, file, line,
  signature}`）与 `_dedup(items)`，与 `impact.py` 同款约 10 行；重复但不跨模块
  引用私有函数，保持模块解耦。邻居 qname 无对应节点时防御性跳过。
- conn 以参数注入，模块不持有连接。

### `mcp_server.py` — 新增 tool `query_graph`

```python
@mcp.tool()
def query_graph(qualified_name: str, edge_kind: str = "call",
                direction: str = "both") -> str:
    """图邻域查询：某符号通过指定边类型（call|contains|import|extends|implements|all，
    默认 call）的 resolved 边，in=用了它的节点，out=它用的节点。返回 JSON。"""
```

- import 用 `from code_review_ai.graph import query_graph as _query_graph`，避免
  与 tool 函数同名冲突。tool 委托 `_query_graph(conn, ...)`。

### `cli.py` — 新增 `query-graph` 子命令（避开已有的 `graph` 导出命令）

```bash
uv run code-review-ai query-graph <qualified_name> [--edge-kind call] [--direction both]
```

- 子解析器：`_add_common(s)`（`--repo`/`--db`）+ 位置参数 `qualified_name` +
  `--edge-kind`（默认 `call`）+ `--direction`（默认 `both`）。
- 分支调用 `graph.query_graph(conn, args.qualified_name, edge_kind=...,
  direction=...)`，print JSON；`ValueError`（非法参数）捕获后输出到 stderr 并
  返回 1（与 `summary` 子命令一致）。

## 错误处理 / 边界

- 非法 `edge_kind`/`direction` → `ValueError`，MCP 层透出、CLI 层转 stderr+exit 1。
- 邻居为 dynamic/unresolved 边 → 不出现（无真实节点）。
- 节点不在图上 → `found: false`，不报错。
- 每方向超限 → 截断到 `max_per_dir`（默认 50，与 `get_impact` 一致）。

## 测试

### `tests/test_graph.py` 新增

- fixture 验证（真实 resolver 输出）：
  - CALL：`auth::login` 的 `in` 含 `app::main`（`app.py` 里 `login("u","p")` 与
    `a.login` 两条 call 边，DISTINCT 后去重为 1）。
  - CONTAINS：`auth::UserService`、`direction="out"` 含 `auth::UserService.authenticate`。
  - IMPORT：`app`（module）、`direction="out"` 含 `auth`。
- 手工建表（in-memory/tmp DB 直接 INSERT nodes/edges）验证：
  - `extends`/`implements` 边按 kind 命中。
  - `edge_kind="all"` 返回混合 kind 邻居。
  - `direction="in"`/`"out"` 只返回对应一侧。
  - 去重（同一邻居多条边只出现一次）、限量截断。
  - 节点不存在 → `found: false`。
  - 非法 `edge_kind`/`direction` → `ValueError`。

### `tests/test_mcp_server.py` 新增

- `query_graph` tool 存在；`fn(qualified_name=Q("auth","login"))` 返回 JSON，含
  `qname`/`kind`/`file`/`line`/`signature`/`edge_kind`/`direction`/`in`/`out`，
  且 `in` 含 `app::main`。

### `tests/test_cli.py` 新增

- `query-graph auth::login` 子命令输出合法 JSON，`edge_kind=="call"` 且 `in` 含
  `app::main`（先 rebuild fixture）。

## 明确不做（YAGNI）

- 不做多跳 BFS/路径查询（`get_impact` 已覆盖 flow 纵向链）。
- 不改 `get_symbol_detail` 返回结构。
- `edge_kind` 不设 `inherits` 别名（真实 kind 是 `extends`/`implements`）。
