# 死代码 / 孤儿符号检测（roadmap #6）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `find_dead_code` (MCP tool) + `dead-code` (CLI subcommand) that reports delete candidates: symbols with no static callers that aren't entry points, and whole files nothing imports.

**Architecture:** Parser captures decorators onto `ParsedNode` (consuming the previously-unused `entry_decorators` config). `nodes` gains a `decorators TEXT` JSON column via migration (INDEX_VERSION 3→4). A new `deadcode.py` query module runs SQL over `nodes.in_degree == 0` + Python-side entry filtering, mirroring the existing `testimpact.py` read-side pattern. Two thin frontends wire it up: one MCP tool, one CLI subcommand.

**Tech Stack:** Python 3.14, tree-sitter, sqlite3, fnmatch; tested with pytest (`uv run pytest`). No new dependencies.

## Global Constraints

- **判据 = `in_degree == 0` 且非入口**：无 flow / community 判据（现状核对见设计文档）。只读查询，不碰 Phase B/C 构建逻辑。
- **复用配置，不新增键**：入口判定用 `config.entry_names`（短名 fnmatch glob）与 `config.entry_decorators`（装饰器 fnmatch glob）。两个键都在 `_CONFIG_HASH_KEYS` 中，无需改动 config。
- **排除测试节点**：`is_test = 1` 的节点不是死代码（SQL 层过滤）。
- **`nodes.decorators` 列 + `INDEX_VERSION` 3→4**：schema + `_migrate_nodes` 迁移；版本 bump 触发老库全量重建。
- **两条写入路径都落 `decorators`**：`indexer._write_nodes`（全量）与 `update._insert_nodes`（增量）缺一不可，否则增量/全量索引不一致。
- **qname 一律走 `qname.py`**：`qname.join(module, name, scope_qname)` 拼接、`qname.short(qname)` 取短名；禁止手拼 `::`/`.`。
- **报告里的 `file` / `path` 是原始 `nodes.file_path`**（绝对路径，与 `testimpact` / `search_symbol` / `get_symbol_detail` 一致），不做相对化。
- **不删除任何代码**：输出是静态分析候选，`meta.note` 写明删除前需人工核对。
- **不做**：TS 顶层函数装饰器（grammar 层面 ERROR）、`__init__.py` 文件档候选、flow/community 判据、自动删除保证。
- 遵循项目代码规范：无单字母变量名、不用内置名当变量、函数体 ≤ 50 行、主控函数只编排。
- 测试约定：`from conftest import FIXTURES as FIX, Q`；`Q` = `qname.join`；`uv run pytest`。

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `code_review_ai/parser.py` | Modify | `ParsedNode.decorators` 字段 + `LANG.decorator_node` + `_walk_defs_typed` 装饰器捕获 + `_decorator_names`/`_decorator_name` 助手 |
| `code_review_ai/db.py` | Modify | `nodes.decorators TEXT` 列 + `_migrate_nodes` ALTER + `INDEX_VERSION = 4` |
| `code_review_ai/indexer.py` | Modify | `_write_nodes` 全量写入 `decorators` |
| `code_review_ai/update.py` | Modify | `_insert_nodes` 增量写入 `decorators` |
| `code_review_ai/deadcode.py` | Create | 只读查询模块 `find_dead_code(conn, config)` |
| `code_review_ai/mcp_server.py` | Modify | `find_dead_code()` MCP tool |
| `code_review_ai/cli.py` | Modify | `dead-code` 子命令（`--format json|text`） |
| `tests/test_parser.py` | Modify | 装饰器捕获测试 |
| `tests/test_db.py` | Modify | `INDEX_VERSION == 4` + decorators 列迁移测试 |
| `tests/test_incremental.py` | Modify | 全量/增量写入 decorators 的持久化测试 |
| `tests/test_deadcode.py` | Create | `find_dead_code` 行为测试 |
| `tests/test_mcp_server.py` | Modify | MCP tool 测试 |
| `tests/test_cli.py` | Modify | CLI 子命令测试 |

**Task dependency order:** Task 1 (parser) and Task 2 (db) are independent. Task 3 (persist) consumes both. Task 4 (deadcode query) consumes Task 3. Tasks 5 (MCP) and 6 (CLI) each consume Task 4. Task 7 is final integration.

---

### Task 1: Parser 捕获装饰器

**Files:**
- Modify: `code_review_ai/parser.py` (ParsedNode `~174-183`, LANG python `44-61` / typescript `62-80` / javascript `81-99`, `_walk_defs_typed` `358-384`)
- Test: `tests/test_parser.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ParsedNode.decorators: list[str]`（默认 `[]`）；LANG 新增键 `decorator_node="decorator"`（python/typescript/javascript）。后续 Task 3 读 `n.decorators`，Task 4 用配置匹配它。

- [ ] **Step 1: 写失败测试**

在 `tests/test_parser.py` 顶部 imports 加 `from code_review_ai import qname`，然后追加四个测试：

```python
def test_parse_captures_module_decorators(tmp_path):
    mod = tmp_path / "web.py"
    mod.write_text(
        'import flask\n\n'
        '@app.route("/")\n'
        'def index():\n'
        '    return "ok"\n',
        encoding="utf-8")
    pf = parse_file(str(mod), str(tmp_path))
    index = next(n for n in pf.nodes if n.qualified_name == Q("web", "index"))
    assert index.decorators == ["app.route"]


def test_parse_captures_chained_and_arg_decorators(tmp_path):
    mod = tmp_path / "cli.py"
    mod.write_text(
        'import click\n\n'
        '@click.command()\n'
        '@click.option("--x")\n'
        'def run():\n'
        '    pass\n',
        encoding="utf-8")
    pf = parse_file(str(mod), str(tmp_path))
    run = next(n for n in pf.nodes if n.qualified_name == Q("cli", "run"))
    assert run.decorators == ["click.command", "click.option"]


def test_parse_captures_method_decorators(tmp_path):
    mod = tmp_path / "svc.py"
    mod.write_text(
        'class Svc:\n'
        '    @staticmethod\n'
        '    def ping():\n'
        '        return 1\n',
        encoding="utf-8")
    pf = parse_file(str(mod), str(tmp_path))
    ping = next(n for n in pf.nodes
                if n.qualified_name == Q("svc", "ping", Q("svc", "Svc")))
    assert ping.decorators == ["staticmethod"]


def test_parse_ts_captures_class_and_method_decorators(tmp_path):
    mod = tmp_path / "pets.ts"
    mod.write_text(
        '@Controller("x")\n'
        'export class Pets {\n'
        '  @Get()\n'
        '  list() { return [] }\n'
        '}\n',
        encoding="utf-8")
    pf = parse_file(str(mod), str(tmp_path))
    by_short = {qname.short(n.qualified_name): n for n in pf.nodes}
    assert by_short["Pets"].decorators == ["Controller"]
    assert by_short["list"].decorators == ["Get"]
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_parser.py -k decorator -v`
Expected: FAIL — `AttributeError: 'ParsedNode' object has no attribute 'decorators'`（或空列表断言失败）。

- [ ] **Step 3: 实现**

**a. `ParsedNode` 加字段**（`parser.py` `~174-183`）：

```python
@dataclass
class ParsedNode:
    qualified_name: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    signature: str
    parent_qname: str | None
    language: str = "python"
    decorators: list[str] = field(default_factory=list)
```

**b. LANG 三个语言加 `decorator_node`**（java 不加，其注解是不同节点类型，超出范围）：

- python dict（`class_extends: "superclasses"` 之后）：`"decorator_node": "decorator",`
- typescript dict（`class_implements: "implements_clause"` 之后）：`"decorator_node": "decorator",`
- javascript dict（同位置）：`"decorator_node": "decorator",`

**c. 重写 `_walk_defs_typed`**（`parser.py` `358-384`），并在其上方加两个助手。实现用「pending 兄弟收集 + 直接子节点收集」双机制统一处理 Python 的 `decorated_definition` 容器（装饰器与内层 def 是同层兄弟）和 TS 的类/方法装饰器（是 def 的直接子节点或紧邻兄弟）。设计文档里的 `decorated_node` 键因此不需要 —— 本实现用兄弟收集自然覆盖，行为一致（这是对设计的一个等价简化）。

```python
def _decorator_names(node, lang) -> list[str]:
    """Collect decorator names from a node's direct ``decorator`` children.
    A lang without ``decorator_node`` configured is a no-op."""
    deco_type = lang.get("decorator_node")
    if not deco_type:
        return []
    return [_decorator_name(c) for c in node.children if c.type == deco_type]


def _decorator_name(deco_node) -> str:
    """Extract the decorator's name: '@app.route("/")' -> 'app.route',
    '@staticmethod' -> 'staticmethod'. A call decorator strips its arguments to
    the callee (the same field _call_target reads), so entry_decorators globs
    match on the name a user would write."""
    for child in deco_node.children:
        if child.type in ("identifier", "attribute", "member_expression",
                          "scoped_identifier"):
            return child.text.decode("utf-8")
        if child.type in ("call", "call_expression"):
            func = child.child_by_field_name("function")
            if func is not None:
                return func.text.decode("utf-8")
    return ""


def _walk_defs_typed(node, source, module_qname, scope_qname, parent_kind, lang, output):
    """Walk AST for def nodes, capturing decorators.

    Decorators precede their def as siblings (Python wraps decorated defs in a
    ``decorated_definition`` container whose ``decorator`` children sit next to
    the inner def; TS/JS put a ``decorator`` node directly before a class or
    method, or as a child of it). Both shapes are handled: decorator siblings
    accumulate into ``pending`` and are consumed by the next def node, and a
    def node's own direct ``decorator`` children are collected too. A lang
    without ``decorator_node`` configured is a no-op.
    """
    deco_type = lang.get("decorator_node")
    pending: list[str] = []
    for child in node.children:
        t = child.type
        if deco_type and t == deco_type:
            pending.append(_decorator_name(child))
            continue
        if t in lang["def_nodes"]:
            # method_definition outside a class is just an object-literal
            # shorthand — not a real definition
            if t == "method_definition" and parent_kind != "class":
                _walk_defs_typed(child, source, module_qname, scope_qname, parent_kind, lang, output)
                continue
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue  # anonymous function/class — skip
            name = name_node.text.decode("utf-8")
            qn = qname.join(module_qname, name, scope_qname)
            kind = lang["def_nodes"][t]
            if kind == "function" and parent_kind == "class":
                kind = "method"
            decorators = list(pending)
            if deco_type:
                decorators.extend(_decorator_names(child, lang))
            output.append(ParsedNode(
                qualified_name=qn, kind=kind, file_path="",
                start_line=child.start_point[0] + 1, end_line=child.end_point[0] + 1,
                signature=_sig(source, child), parent_qname=scope_qname,
                decorators=decorators,
            ))
            pending = []
            _walk_defs_typed(child, source, module_qname, qn, kind, lang, output)
        elif lang.get("detect_arrow_in_vars") and t == "variable_declarator":
            pending = []
            _maybe_arrow_def(child, source, module_qname, scope_qname, parent_kind, lang, output)
        else:
            # Leaf tokens (TS `export`/`default` keywords can sit between a
            # decorator and its class inside export_statement) are part of the
            # same declaration — don't let them wipe pending. A real statement
            # boundary is a non-leaf node (e.g. a TS property decorator's
            # public_field_definition), which still clears pending.
            if child.children:
                pending = []
            _walk_defs_typed(child, source, module_qname, scope_qname, parent_kind, lang, output)
```

> 说明：`else` 分支清空 `pending` 是刻意的 —— 装饰器只修饰紧邻的 def；若下一个兄弟不是我们跟踪的 def（如 TS 属性装饰器），pending 不应泄漏到后面的 def。Python `decorated_definition` 保证装饰器与 def 同层相邻，`else` 清空不会误伤。唯一例外是叶子 token（TS `export`/`default` 关键字位于装饰器与其 class 之间），它们不构成语句边界，不触发清空 —— 否则计划自带测试 `test_parse_ts_captures_class_and_method_decorators` 会失败（tree-sitter-typescript 0.23.2 中 `@Controller("x") export class Pets` 的 decorator 与 class 是 export_statement 内的兄弟，中间隔一个 export 关键字叶子）。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_parser.py -k decorator -v`
Expected: PASS（4 个装饰器测试全绿）。

Run: `uv run pytest tests/test_parser.py -v`
Expected: PASS（原有测试不受影响）。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/parser.py tests/test_parser.py
git commit -m "feat(parser): capture decorators on def/class nodes"
```

---

### Task 2: DB schema —— `decorators` 列 + `INDEX_VERSION` 4

**Files:**
- Modify: `code_review_ai/db.py` (INDEX_VERSION `8`, SCHEMA nodes `11-24`, `_migrate_nodes` `103-115`)
- Test: `tests/test_db.py`（`test_files_table_and_busy_timeout` 里的 `assert INDEX_VERSION == 3` 也要改）

**Interfaces:**
- Consumes: nothing.
- Produces: `nodes.decorators TEXT` 列（新库经 SCHEMA、老库经 ALTER）；`INDEX_VERSION = 4`。Task 3 的 INSERT 依赖此列。

- [ ] **Step 1: 写失败测试**

在 `tests/test_db.py` 追加：

```python
def test_init_schema_migrates_decorators_column(tmp_path):
    """An index.db from before decorators existed gains the column via ALTER
    TABLE (CREATE TABLE IF NOT EXISTS won't touch it)."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE nodes ("
        "id INTEGER PRIMARY KEY, qualified_name TEXT UNIQUE, kind TEXT,"
        "language TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        "signature TEXT, parent_id INTEGER REFERENCES nodes(id),"
        "in_degree INTEGER NOT NULL DEFAULT 0,"
        "out_degree INTEGER NOT NULL DEFAULT 0,"
        "is_test INTEGER NOT NULL DEFAULT 0);"
    )
    conn.execute("INSERT INTO nodes(qualified_name) VALUES('mod::old')")
    conn.commit()
    conn.close()

    conn = connect(str(db))
    init_schema(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    assert "decorators" in cols
    row = conn.execute(
        "SELECT decorators FROM nodes WHERE qualified_name='mod::old'"
    ).fetchone()
    assert row["decorators"] is None
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_db.py::test_init_schema_migrates_decorators_column -v`
Expected: FAIL — `KeyError: 'decorators'`（`in_degree` 断言也会在其它测试里先炸）。

- [ ] **Step 3: 实现**

**a. `db.py` `8` 行版本号：**

```python
INDEX_VERSION = 4
```

**b. SCHEMA 的 nodes 表**（`11-24`，`is_test` 之后加一列）：

```sql
    is_test INTEGER NOT NULL DEFAULT 0,
    decorators TEXT
);
```

**c. `_migrate_nodes`**（`115` 后追加一个分支）：

```python
    if "decorators" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN decorators TEXT")
```

**d. 更新既有断言**：`tests/test_db.py` 里 `assert INDEX_VERSION == 3` → `assert INDEX_VERSION == 4`。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS（含新迁移测试与版本断言）。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/db.py tests/test_db.py
git commit -m "feat(db): add nodes.decorators column, bump INDEX_VERSION to 4"
```

---

### Task 3: 两条写入路径都持久化 `decorators`

**Files:**
- Modify: `code_review_ai/indexer.py` (`_write_nodes` `93-115`)
- Modify: `code_review_ai/update.py` (`_insert_nodes` `152-171`)
- Test: `tests/test_incremental.py`（顶部 imports 加 `import json`）

**Interfaces:**
- Consumes: Task 1 的 `n.decorators` 字段；Task 2 的 `nodes.decorators` 列。
- Produces: 全量（`indexer._write_nodes`）与增量（`update._insert_nodes`）都写入 `json.dumps(n.decorators)`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_incremental.py` 追加（复用已有的 `_git_repo` / `_init_and_build` 助手，文件顶部加 `import json`）：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_incremental.py::test_decorators_persisted_on_full_and_incremental -v`
Expected: FAIL — `OperationalError: table nodes has no column named decorators`（`_write_nodes` 的 INSERT 还没带新列）。

- [ ] **Step 3: 实现**

**a. `indexer.py` `_write_nodes`**（`96-104`）—— INSERT 加列、tuple 加值：

```python
    conn.executemany(
        "INSERT INTO nodes(qualified_name,kind,language,file_path,"
        "start_line,end_line,signature,parent_id,is_test,decorators) "
        "VALUES(?,?,?,?,?,?,?,NULL,?,?)",
        [(n.qualified_name, n.kind, n.language, n.file_path,
          n.start_line, n.end_line, n.signature,
          1 if is_test_node(n.file_path, n.qualified_name,
                            config.test_globs, config.test_names,
                            config.repo_path) else 0,
          json.dumps(n.decorators))
         for pf in parsed for n in pf.nodes],
    )
```

**b. `update.py` `_insert_nodes`**（`152-162`）——同样处理：

```python
    rows = [(n.qualified_name, n.kind, n.language, n.file_path,
             n.start_line, n.end_line, n.signature,
             1 if is_test_node(n.file_path, n.qualified_name,
                               config.test_globs, config.test_names,
                               config.repo_path) else 0,
             json.dumps(n.decorators))
            for pf in parsed for n in pf.nodes]
    conn.executemany(
        "INSERT INTO nodes(qualified_name,kind,language,file_path,start_line,"
        "end_line,signature,parent_id,is_test,decorators) VALUES(?,?,?,?,?,?,?,NULL,?,?)", rows)
```

`indexer.py` 顶部已 `import json`（`3` 行）、`update.py` 顶部已 `import json`（`7` 行），无需新增。

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_incremental.py::test_decorators_persisted_on_full_and_incremental -v`
Expected: PASS（两条路径都写入了 decorators）。

Run: `uv run pytest tests/test_incremental.py -v`
Expected: PASS（原有增量测试不受影响）。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/indexer.py code_review_ai/update.py tests/test_incremental.py
git commit -m "feat(indexer): persist decorators in full and incremental write paths"
```

---

### Task 4: `deadcode.py` 查询模块

**Files:**
- Create: `code_review_ai/deadcode.py`
- Test: `tests/test_deadcode.py`（Create）

**Interfaces:**
- Consumes: `nodes.decorators` 列（Task 2/3）、`config.entry_names` / `config.entry_decorators`。
- Produces: `find_dead_code(conn, config) -> {"symbols": [...], "files": [...], "meta": {...}}`。Task 5 / 6 直接调用它。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_deadcode.py`：

```python
import json

from conftest import FIXTURES as FIX, Q

from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.deadcode import find_dead_code


def _built(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "dc.db")
    cfg.repo_path = FIX
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return cfg, conn


def test_find_dead_code_fixture(tmp_path):
    cfg, conn = _built(tmp_path)
    payload = find_dead_code(conn, cfg)
    symbols = {s["qname"] for s in payload["symbols"]}
    # 无 caller 且非入口 -> 检出
    assert Q("util", "hash_pw") in symbols
    assert Q("util", "helper") in symbols
    assert Q("auth", "UserService") in symbols
    assert Q("auth", "authenticate", Q("auth", "UserService")) in symbols
    # 入口 / 有 caller -> 不检出
    assert Q("app", "main") not in symbols
    assert Q("auth", "login") not in symbols
    # 文件档：util.py（无入口、无人 import）；app.py（含入口）与 auth.py（被 import）不进
    file_qnames = {f["qname"] for f in payload["files"]}
    assert "util" in file_qnames
    assert "app" not in file_qnames
    assert "auth" not in file_qnames
    # rollup：util 文件聚合其死符号
    util_file = next(f for f in payload["files"] if f["qname"] == "util")
    assert util_file["symbol_count"] == 2
    assert set(util_file["symbols"]) == {Q("util", "hash_pw"), Q("util", "helper")}
    assert payload["meta"]["symbol_count"] == len(symbols)


def test_find_dead_code_excludes_entry_decorator(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "x.db")
    cfg.repo_path = str(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,file_path,start_line,signature,"
        "decorators,in_degree,is_test) VALUES(?,?,?,?,?,?,0,0)",
        (Q("web", "index"), "function", "web.py", 1,
         "def index():", json.dumps(["app.route"])))
    conn.commit()
    payload = find_dead_code(conn, cfg)
    assert all(s["qname"] != Q("web", "index") for s in payload["symbols"])


def test_find_dead_code_excludes_test_and_entry_name(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "x.db")
    cfg.repo_path = str(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,file_path,start_line,signature,"
        "decorators,in_degree,is_test) VALUES(?,?,?,?,?,?,0,?)",
        (Q("t", "test_login"), "function", "test_t.py", 1,
         "def test_login():", "[]", 1))
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,file_path,start_line,signature,"
        "decorators,in_degree,is_test) VALUES(?,?,?,?,?,?,0,0)",
        (Q("t", "main"), "function", "t.py", 1, "def main():", "[]"))
    conn.commit()
    payload = find_dead_code(conn, cfg)
    assert all(s["qname"] != Q("t", "test_login") for s in payload["symbols"])
    assert all(s["qname"] != Q("t", "main") for s in payload["symbols"])


def test_find_dead_code_tolerates_bad_decorators_json(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "x.db")
    cfg.repo_path = str(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO nodes(qualified_name,kind,file_path,start_line,signature,"
        "decorators,in_degree,is_test) VALUES(?,?,?,?,?,?,0,0)",
        (Q("m", "f"), "function", "m.py", 1, "def f():", "not-json{"))
    conn.commit()
    payload = find_dead_code(conn, cfg)
    record = next(s for s in payload["symbols"] if s["qname"] == Q("m", "f"))
    assert record["decorators"] == []
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_deadcode.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'code_review_ai.deadcode'`。

- [ ] **Step 3: 实现**

创建 `code_review_ai/deadcode.py`：

```python
"""Dead-code / orphan symbol detection: candidates for safe removal.

Pure read-side query over the index (mirrors testimpact.py). A symbol is a
dead-code candidate when it has no resolved callers (``nodes.in_degree == 0``)
and is not an entry point — an ``entry_names`` short-name glob match or an
``entry_decorators`` decorator match. A file is a candidate when no other
module imports it (no resolved import edge into its module node) and it
contains no entry or test symbol. flow/community are deliberately NOT
criteria: under the current flat-flow / structural-community model every
callerless function is its own flow root and every function is anchored into a
community, so both are vacuous (see the design spec).
"""

import fnmatch
import json
import sqlite3

from code_review_ai import qname


def find_dead_code(conn: sqlite3.Connection, config) -> dict:
    """Return the dead-code candidate report: {"symbols", "files", "meta"}.

    ``symbols`` — function/method/class with in_degree == 0 and not an entry.
    ``files``  — whole files nothing imports, rolled up with their dead symbols.
    ``meta``   — counts plus a static-analysis disclaimer.
    """
    symbols = _dead_symbols(conn, config)
    files = _dead_files(conn, config, symbols)
    return {
        "symbols": symbols,
        "files": files,
        "meta": {
            "symbol_count": len(symbols),
            "file_count": len(files),
            "note": ("候选是静态分析的删码候选，不是自动删除令：动态调用、反射、"
                     "多态覆盖与框架魔法不可见，删除前请人工核对。"),
        },
    }


def _dead_symbols(conn: sqlite3.Connection, config) -> list[dict]:
    """Symbol-tier candidates: function/method/class nodes with no resolved
    callers (in_degree == 0), excluding test nodes and entry points."""
    rows = conn.execute(
        "SELECT qualified_name, kind, file_path, start_line, signature, decorators "
        "FROM nodes "
        "WHERE kind IN ('function','method','class') "
        "AND is_test = 0 AND in_degree = 0"
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        decorators = _decorators(row["decorators"])
        if _is_entry(row["qualified_name"], decorators, config):
            continue
        out.append({
            "qname": row["qualified_name"],
            "kind": row["kind"],
            "file": row["file_path"],
            "line": row["start_line"],
            "signature": row["signature"],
            "decorators": decorators,
        })
    return out


def _dead_files(conn: sqlite3.Connection, config,
                symbols: list[dict]) -> list[dict]:
    """File-tier candidates: module nodes nothing imports (no resolved import
    edge targeting them), excluding __init__.py and files holding an entry or
    test symbol. Dead symbols inside each file are rolled up."""
    rows = conn.execute(
        "SELECT n.qualified_name, n.file_path "
        "FROM nodes n "
        "WHERE n.kind = 'module' "
        "AND n.file_path NOT LIKE '%__init__.py' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM edges e "
        "  WHERE e.kind = 'import' AND e.resolution = 'resolved' "
        "    AND e.target = n.qualified_name)"
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        file_path = row["file_path"]
        if _file_has_entry_or_test(conn, file_path, config):
            continue
        inner = [s for s in symbols if s["file"] == file_path]
        out.append({
            "path": file_path,
            "qname": row["qualified_name"],
            "symbol_count": len(inner),
            "symbols": [s["qname"] for s in inner],
        })
    return out


def _file_has_entry_or_test(conn: sqlite3.Connection, file_path: str,
                            config) -> bool:
    """True when a file holds an entry symbol or any test node — such a file
    is reachable/runnable without a static importer, so it is not a candidate."""
    rows = conn.execute(
        "SELECT qualified_name, decorators, is_test FROM nodes WHERE file_path = ?",
        (file_path,),
    ).fetchall()
    return any(
        row["is_test"] or _is_entry(row["qualified_name"],
                                    _decorators(row["decorators"]), config)
        for row in rows
    )


def _is_entry(qualified_name: str, decorators: list[str], config) -> bool:
    """True when a symbol is an entry point: short-name glob match on
    entry_names, or any decorator matching an entry_decorators glob."""
    if any(fnmatch.fnmatch(qname.short(qualified_name), pat)
           for pat in config.entry_names):
        return True
    return any(
        fnmatch.fnmatch(decorator, pat)
        for decorator in decorators
        for pat in config.entry_decorators
    )


def _decorators(raw: str | None) -> list[str]:
    """Decode the decorators JSON column, tolerating NULL / empty / bad JSON."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_deadcode.py -v`
Expected: PASS（4 个测试全绿）。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/deadcode.py tests/test_deadcode.py
git commit -m "feat(deadcode): dead-code/orphan detection over the index"
```

---

### Task 5: MCP tool `find_dead_code`

**Files:**
- Modify: `code_review_ai/mcp_server.py`（import `16` 行后、tool 注册 `68` 行 get_test_impact 之后）
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: Task 4 的 `find_dead_code(conn, config)`。
- Produces: `find_dead_code()` MCP tool（无参数，返回 JSON 字符串）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_mcp_server.py` 追加（复用已有 `_server` 助手）：

```python
def test_find_dead_code_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    tools = server._tool_manager._tools
    assert "find_dead_code" in tools
    data = json.loads(tools["find_dead_code"].fn())
    assert set(data) == {"symbols", "files", "meta"}
    qnames = {s["qname"] for s in data["symbols"]}
    assert Q("util", "hash_pw") in qnames
    assert Q("app", "main") not in qnames
    assert any(f["qname"] == "util" for f in data["files"])
    assert data["meta"]["symbol_count"] == len(data["symbols"])
    assert data["meta"]["file_count"] == len(data["files"])
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_mcp_server.py::test_find_dead_code_tool -v`
Expected: FAIL — `assert 'find_dead_code' in tools`。

- [ ] **Step 3: 实现**

**a. import**（`mcp_server.py` `16` 行 `from code_review_ai.testimpact import ...` 之后）：

```python
from code_review_ai.deadcode import find_dead_code as _find_dead_code
```

**b. tool**（`get_test_impact` tool 块结束 `68` 行之后插入）：

```python
    @mcp.tool()
    def find_dead_code() -> str:
        """Dead-code / orphan detection: symbols with no static callers that
        are not entry points (entry_names glob / entry_decorators decorator),
        plus whole files nothing imports. Returns a JSON candidate list —
        symbols + files — with a note that these are static-analysis
        candidates, not deletion orders."""
        return json.dumps(_find_dead_code(conn, config))
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_mcp_server.py::test_find_dead_code_tool -v`
Expected: PASS。

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: PASS（原有 tool 测试不受影响）。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(mcp): expose find_dead_code tool"
```

---

### Task 6: CLI `dead-code` 子命令

**Files:**
- Modify: `code_review_ai/cli.py`（import `13` 行后、subparser `57-60` test-impact 之后、dispatch `137-143` test-impact elif 之后）
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 4 的 `find_dead_code(cfg, conn)`。
- Produces: `dead-code [--format json|text]` 子命令；默认打印 JSON，`--format text` 打印紧凑表格。

- [ ] **Step 1: 写失败测试**

在 `tests/test_cli.py` 追加：

```python
def test_cli_dead_code_json(tmp_path, capsys):
    assert main(["rebuild", "--repo", FIX, "--db", str(tmp_path / "dc.db")]) == 0
    _ = capsys.readouterr()
    code = main(["dead-code", "--repo", FIX, "--db", str(tmp_path / "dc.db")])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"symbols", "files", "meta"}
    assert any(s["qname"] == Q("util", "hash_pw") for s in data["symbols"])
    assert not any(s["qname"] == Q("app", "main") for s in data["symbols"])


def test_cli_dead_code_text(tmp_path, capsys):
    assert main(["rebuild", "--repo", FIX, "--db", str(tmp_path / "dc.db")]) == 0
    _ = capsys.readouterr()
    code = main(["dead-code", "--format", "text",
                 "--repo", FIX, "--db", str(tmp_path / "dc.db")])
    assert code == 0
    out = capsys.readouterr().out
    assert Q("util", "hash_pw") in out
    assert "FILE" in out
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cli.py::test_cli_dead_code_json tests/test_cli.py::test_cli_dead_code_text -v`
Expected: FAIL — `error: argument cmd: invalid choice: 'dead-code'`。

- [ ] **Step 3: 实现**

**a. import**（`cli.py` `13` 行 `from code_review_ai.testimpact import get_test_impact` 之后）：

```python
from code_review_ai.deadcode import find_dead_code
```

**b. subparser**（`test-impact` subparser 块 `57-60` 之后）：

```python
    s = sub.add_parser("dead-code")
    _add_common(s)
    s.add_argument("--format", choices=["json", "text"], default="json",
                   help="output format (default: json)")
```

**c. dispatch**（`elif args.cmd == "test-impact":` 块 `143` 之后）：

```python
    elif args.cmd == "dead-code":
        payload = find_dead_code(cfg, conn)
        if args.format == "text":
            for s in payload["symbols"]:
                print(f"{s['file']}:{s['line']}\t{s['kind']}\t{s['qname']}")
            for f in payload["files"]:
                print(f"FILE\t{f['path']}\t{f['qname']}\t{f['symbol_count']} symbols")
        else:
            print(json.dumps(payload))
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_cli.py::test_cli_dead_code_json tests/test_cli.py::test_cli_dead_code_text -v`
Expected: PASS。

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS（原有 CLI 测试不受影响）。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/cli.py tests/test_cli.py
git commit -m "feat(cli): add dead-code subcommand"
```

---

### Task 7: 全量回归 + 冒烟验证

**Files:** none（验证步骤）

**Interfaces:** Consumes: 全部前述改动。

- [ ] **Step 1: 全量测试**

Run: `uv run pytest`
Expected: PASS（全部测试，包括本次新增的 4 组）。

- [ ] **Step 2: 冒烟 —— CLI 在真实仓库上跑**

Run: `uv run code-review-ai rebuild --repo . --db .code-review-ai/index.db` 然后 `uv run code-review-ai dead-code --repo . --db .code-review-ai/index.db --format text`
Expected: 输出死符号/死文件候选表格，`meta` 计数与符号一致，无异常。（注意：本仓库配置的 `exclude` 会过滤测试目录等，属预期。）

- [ ] **Step 3: 冒烟 —— MCP 索引版本迁移**

Run: `uv run code-review-ai sync --repo . --db .code-review-ai/index.db`
Expected: `{"full_rebuild": true, ...}` —— `INDEX_VERSION` 4 触发一次全量重建（老库平滑升级）。

---

Plan complete and saved to `docs/superpowers/plans/2026-08-06-dead-code-detection.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
