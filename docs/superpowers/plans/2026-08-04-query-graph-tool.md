# query_graph 工具实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `query_graph` 图邻域查询：给定 qname，返回「用了它的」（in）+「被它用的」（out）邻居，边类型默认 call、方向默认 both。MCP tool + CLI `query-graph`。

**Architecture:** 逻辑集中在新模块 `graph.py`（`query_graph` 只编排：查节点 → `_neighbors` 查 in/out → 组装；conn 注入）；`mcp_server.py` 与 `cli.py` 各加薄前端。`get_symbol_detail` 不动。

**Tech Stack:** Python 3.14、`uv`、pytest、SQLite（`code_review_ai.db`）、`resolver.py` 的边模型。

## Global Constraints

- qname 一律走 `qname.join` / `qname.short`，禁止手工拼接。
- 禁止单字母变量名（数学索引除外）；循环变量用有意义的词。
- 函数体 ≤ 50 行；主控函数只做编排（参数准备/校验 → 调子函数 → 返回）。
- 业务/库模块不持有 DB 连接；DB 以 `conn` 参数注入。
- SQL 中 f-string 只允许插固定的列名字面量或常量子句（`" AND kind=?"` 或 `""`），**绝不**插用户输入。
- 测试用 `from conftest import Q, FIXTURES as FIX`；`tests/` 在 `sys.path` 上。
- fixture 事实：`app.py` 有 `from auth import login` + `import auth as a`，`app::main` 对 `auth::login` 产生 2 条 call 边（DISTINCT 后 1 个邻居）；`auth::UserService` CONTAINS `auth::UserService.authenticate`；`app`（module）IMPORT `auth`；fixture 无继承边。

---

### Task 1: `graph.py` — `query_graph` + 单测

**Files:**
- Create: `code_review_ai/graph.py`
- Test: `tests/test_graph.py`

**Interfaces:**
- Consumes: `code_review_ai.db.connect` / `init_schema`、`code_review_ai.indexer.rebuild`（仅测试）、fixture。
- Produces:
  - `graph.query_graph(conn, qualified_name: str, edge_kind: str = "call", direction: str = "both", max_per_dir: int = 50) -> dict`
  - `graph._node_brief(conn, qualified_name) -> dict`（`{qname, kind, file, line, signature}`）
  - `graph._neighbors(conn, select_column, where_column, match_qname, edge_kind, max_per_dir) -> list[dict]`
  - `graph._dedup(items) -> list[dict]`

- [ ] **Step 1: 写测试**（`tests/test_graph.py`）

```python
import pytest

from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.graph import query_graph
from code_review_ai.indexer import rebuild

from conftest import FIXTURES as FIX, Q


def _built_conn(tmp_path):
    cfg = load_config(FIX)
    cfg.repo_path = FIX
    conn = connect(str(tmp_path / "g.db"))
    init_schema(conn)
    rebuild(cfg, conn)
    return conn


def _hand_built_conn(tmp_path):
    conn = connect(str(tmp_path / "h.db"))
    init_schema(conn)
    return conn


def _insert_node(conn, qualified_name, kind="function", file_path="x.py"):
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,language,file_path,start_line,end_line,signature) "
        "VALUES (?,?,?,?,?,?,?)",
        (qualified_name, kind, "python", file_path, 1, 2, "sig"))


def _insert_edge(conn, source, target, kind="call", resolution="resolved"):
    conn.execute(
        "INSERT INTO edges(source,target,kind,file_path,call_line,resolution) "
        "VALUES (?,?,?,?,0,?)",
        (source, target, kind, "x.py", resolution))


def test_call_neighbors_both_directions(tmp_path):
    conn = _built_conn(tmp_path)
    out = query_graph(conn, Q("auth", "login"))
    assert out["qname"] == Q("auth", "login")
    assert out["edge_kind"] == "call" and out["direction"] == "both"
    assert [n["qname"] for n in out["in"]] == [Q("app", "main")]
    assert out["out"] == []


def test_contains_out(tmp_path):
    conn = _built_conn(tmp_path)
    out = query_graph(conn, Q("auth", "UserService"), edge_kind="contains", direction="out")
    assert [n["qname"] for n in out["out"]] == [Q("auth", "authenticate", Q("auth", "UserService"))]


def test_import_out_for_module(tmp_path):
    conn = _built_conn(tmp_path)
    out = query_graph(conn, "app", edge_kind="import", direction="out")
    assert [n["qname"] for n in out["out"]] == ["auth"]


def test_extends_and_implements_kinds(tmp_path):
    conn = _hand_built_conn(tmp_path)
    _insert_node(conn, "a::Base", kind="class")
    _insert_node(conn, "a::Iface", kind="class")
    _insert_node(conn, "b::Sub", kind="class")
    _insert_edge(conn, "b::Sub", "a::Base", kind="extends")
    _insert_edge(conn, "b::Sub", "a::Iface", kind="implements")
    out = query_graph(conn, "b::Sub", edge_kind="extends", direction="out")
    assert [n["qname"] for n in out["out"]] == ["a::Base"]
    all_out = query_graph(conn, "b::Sub", edge_kind="all", direction="out")
    assert {n["qname"] for n in all_out["out"]} == {"a::Base", "a::Iface"}


def test_direction_filters(tmp_path):
    conn = _hand_built_conn(tmp_path)
    _insert_node(conn, "a::caller")
    _insert_node(conn, "a::mid")
    _insert_node(conn, "a::callee")
    _insert_edge(conn, "a::caller", "a::mid")
    _insert_edge(conn, "a::mid", "a::callee")
    assert [n["qname"] for n in query_graph(conn, "a::mid", direction="in")["in"]] == ["a::caller"]
    assert [n["qname"] for n in query_graph(conn, "a::mid", direction="out")["out"]] == ["a::callee"]


def test_neighbors_dedup(tmp_path):
    conn = _hand_built_conn(tmp_path)
    _insert_node(conn, "a::caller")
    _insert_node(conn, "a::mid")
    _insert_edge(conn, "a::caller", "a::mid")
    _insert_edge(conn, "a::caller", "a::mid")
    assert len(query_graph(conn, "a::mid", direction="in")["in"]) == 1


def test_max_per_dir_truncates(tmp_path):
    conn = _hand_built_conn(tmp_path)
    _insert_node(conn, "a::mid")
    for index in range(3):
        _insert_node(conn, f"a::c{index}")
        _insert_edge(conn, f"a::c{index}", "a::mid")
    assert len(query_graph(conn, "a::mid", direction="in", max_per_dir=2)["in"]) == 2


def test_node_not_found(tmp_path):
    conn = _hand_built_conn(tmp_path)
    out = query_graph(conn, "nope::missing")
    assert out["found"] is False
    assert out["in"] == [] and out["out"] == []


def test_invalid_edge_kind_and_direction(tmp_path):
    conn = _hand_built_conn(tmp_path)
    with pytest.raises(ValueError, match="edge_kind"):
        query_graph(conn, "x", edge_kind="bogus")
    with pytest.raises(ValueError, match="direction"):
        query_graph(conn, "x", direction="sideways")
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_graph.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'code_review_ai.graph'`）

- [ ] **Step 3: 实现 `code_review_ai/graph.py`**

```python
import sqlite3

VALID_KINDS = ("call", "contains", "import", "extends", "implements", "all")
VALID_DIRECTIONS = ("in", "out", "both")


def _node_brief(conn: sqlite3.Connection, qualified_name: str) -> dict:
    row = conn.execute(
        "SELECT qualified_name,kind,file_path,start_line,signature "
        "FROM nodes WHERE qualified_name=?", (qualified_name,),
    ).fetchone()
    if row is None:
        return {"qname": qualified_name, "kind": None, "file": "",
                "line": 0, "signature": ""}
    return {"qname": row["qualified_name"], "kind": row["kind"],
            "file": row["file_path"], "line": row["start_line"],
            "signature": row["signature"]}


def _dedup(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for item in items:
        if item["qname"] not in seen:
            seen.add(item["qname"])
            out.append(item)
    return out


def _neighbors(conn: sqlite3.Connection, select_column: str, where_column: str,
               match_qname: str, edge_kind: str, max_per_dir: int) -> list[dict]:
    """Resolved-edge neighbors of match_qname. select_column/where_column are
    fixed literals ("source"/"target"), never user input."""
    kind_clause = "" if edge_kind == "all" else " AND kind=?"
    params = [match_qname, "resolved"] + ([edge_kind] if edge_kind != "all" else [])
    rows = conn.execute(
        f"SELECT DISTINCT {select_column} FROM edges WHERE {where_column}=? "
        f"AND resolution=?{kind_clause}", params)
    briefs = (_node_brief(conn, row[select_column]) for row in rows)
    return _dedup([brief for brief in briefs if brief["kind"] is not None])[:max_per_dir]


def query_graph(conn: sqlite3.Connection, qualified_name: str,
                edge_kind: str = "call", direction: str = "both",
                max_per_dir: int = 50) -> dict:
    """Neighbors of one symbol via resolved edges: `in` = nodes pointing to it,
    `out` = nodes it points to. Raises ValueError on invalid edge_kind/direction."""
    if edge_kind not in VALID_KINDS:
        raise ValueError(
            f"invalid edge_kind {edge_kind!r}; expected one of {', '.join(VALID_KINDS)}")
    if direction not in VALID_DIRECTIONS:
        raise ValueError(
            f"invalid direction {direction!r}; expected one of {', '.join(VALID_DIRECTIONS)}")
    brief = _node_brief(conn, qualified_name)
    if brief["kind"] is None:
        return {"qname": qualified_name, "found": False, "in": [], "out": []}
    result = {"qname": qualified_name, "kind": brief["kind"],
              "file": brief["file"], "line": brief["line"],
              "signature": brief["signature"],
              "edge_kind": edge_kind, "direction": direction, "in": [], "out": []}
    if direction in ("in", "both"):
        result["in"] = _neighbors(conn, "source", "target", qualified_name,
                                  edge_kind, max_per_dir)
    if direction in ("out", "both"):
        result["out"] = _neighbors(conn, "target", "source", qualified_name,
                                   edge_kind, max_per_dir)
    return result
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_graph.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/graph.py tests/test_graph.py
git commit -m "feat(graph): add query_graph neighborhood query"
```

---

### Task 2: MCP tool `query_graph`

**Files:**
- Modify: `code_review_ai/mcp_server.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: Task 1 的 `graph.query_graph`、`create_server` 闭包内 `conn`。
- Produces: MCP tool `query_graph(qualified_name, edge_kind="call", direction="both") -> str`。

- [ ] **Step 1: 写失败测试**（`tests/test_mcp_server.py` 追加）

```python
def test_query_graph_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    tools = server._tool_manager._tools
    assert "query_graph" in tools
    out = json.loads(tools["query_graph"].fn(qualified_name=Q("auth", "login")))
    assert out["qname"] == Q("auth", "login")
    assert out["edge_kind"] == "call" and out["direction"] == "both"
    assert [n["qname"] for n in out["in"]] == [Q("app", "main")]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_mcp_server.py::test_query_graph_tool -v`
Expected: FAIL（`KeyError: 'query_graph'`）

- [ ] **Step 3: 最小实现**

`code_review_ai/mcp_server.py` import 区加 `from code_review_ai.graph import query_graph as _query_graph`，并在 `get_change_summary` tool 后新增：

```python
    @mcp.tool()
    def query_graph(qualified_name: str, edge_kind: str = "call",
                    direction: str = "both") -> str:
        """图邻域查询：某符号通过指定边类型（call|contains|import|extends|
        implements|all，默认 call）的 resolved 边，in=用了它的节点，out=它用的
        节点。返回 JSON 对象。"""
        return json.dumps(_query_graph(conn, qualified_name,
                                       edge_kind=edge_kind, direction=direction))
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(mcp): add query_graph tool"
```

---

### Task 3: CLI `query-graph` 子命令

**Files:**
- Modify: `code_review_ai/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1 的 `graph.query_graph`、`cli` 的 `conn`。
- Produces: `uv run code-review-ai query-graph <qualified_name> [--edge-kind call] [--direction both]` 打印 JSON；非法参数输出 stderr 并返回 1。

- [ ] **Step 1: 写失败测试**（`tests/test_cli.py` 追加）

```python
def test_cli_query_graph(tmp_path, capsys):
    code = main(["rebuild", "--repo", FIX, "--db", str(tmp_path / "c.db")])
    assert code == 0
    _ = capsys.readouterr()
    code = main(["query-graph", Q("auth", "login"),
                 "--repo", FIX, "--db", str(tmp_path / "c.db")])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["edge_kind"] == "call"
    assert [n["qname"] for n in data["in"]] == [Q("app", "main")]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cli.py::test_cli_query_graph -v`
Expected: FAIL（argparse `invalid choice: 'query-graph'`）

- [ ] **Step 3: 最小实现**

`code_review_ai/cli.py`：
- import 区加 `from code_review_ai.graph import query_graph`
- 在 `summary` 子解析器后加：

```python
    s = sub.add_parser("query-graph")
    _add_common(s)
    s.add_argument("qualified_name")
    s.add_argument("--edge-kind", default="call")
    s.add_argument("--direction", default="both")
```

- 在 `main` 的 `summary` 分支后加：

```python
    elif args.cmd == "query-graph":
        try:
            payload = query_graph(conn, args.qualified_name,
                                  edge_kind=args.edge_kind, direction=args.direction)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(payload))
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/cli.py tests/test_cli.py
git commit -m "feat(cli): add query-graph subcommand mirroring query_graph"
```

---

### Task 4: 文档同步（CLAUDE.md / AGENTS.md）

**Files:**
- Modify: `CLAUDE.md`、`AGENTS.md`

**Interfaces:**
- Consumes: Task 2/3 产出的工具名与命令名。
- Produces: 文档列出 `query_graph` 工具与 `query-graph` 命令。

- [ ] **Step 1: Commands 段加 `query-graph`**

两文件 Commands 代码块 `summary` 两行后加：

```bash
uv run code-review-ai query-graph auth::login               # graph neighborhood (in/out via resolved edges)
uv run code-review-ai query-graph auth::login --edge-kind call --direction both
```

- [ ] **Step 2: Frontends 段工具列表加 `query_graph`**

两文件 `tools rebuild_index, get_impact, get_change_summary, ...` 列表在
`get_change_summary` 后加 `query_graph`。

- [ ] **Step 3: 模块职责行加 graph.py**

两文件模块职责行在 `impact.py ...` 前加 `graph.py resolved-edge neighborhood query → in/out neighbors ·`。

- [ ] **Step 4: 提交**

```bash
git add CLAUDE.md AGENTS.md
git commit -m "docs: document query_graph tool and query-graph CLI subcommand"
```

---

### Task 5: 全量验证

- [ ] **Step 1: 全量测试**

Run: `uv run pytest`
Expected: 全部 PASS（含既有用例，无回归）

- [ ] **Step 2: 手动冒烟（可选）**

Run: `uv run code-review-ai query-graph code_review_ai.changes::build_change_summary --repo . --db .code-review-ai/index.db`
Expected: 打印含 `in`/`out` 与邻居详细对象的 JSON
