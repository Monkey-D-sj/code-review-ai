# 设计：死代码 / 孤儿符号检测（roadmap #6）

日期：2026-08-06

## 目标

在 `code-review-ai` 中新增一个**只读**工具 `find_dead_code`（MCP）+ `dead-code`
子命令（CLI），输出「可安全删除的候选清单」：

1. **符号档**：无静态 caller、且不是入口的 function / method / class。
2. **文件档**：从未被任何模块导入的整文件（module 节点无 resolved import 入边）。

判据经现状核对**简化为一句话**：**`nodes.in_degree == 0`（无 resolved call
入度）且不是入口**。flow / community **不作为判据**（见「现状核对」）。

## 现状核对 —— 为什么只要 caller + 入口

roadmap 原文写的是「无 caller、无 flow、无 community」，但对照当前管线：

| 判据 | 实际含义 | 结论 |
|---|---|---|
| 无 caller | `nodes.in_degree` = DISTINCT resolved call 入度（`recompute_degrees` 已算好） | ✅ **真实判别信号** |
| 无 flow | 当前平铺流模型下 `build_flows` 把**每个无 caller 的 function/method 都当作自己的 flow root** → 对函数 `on_flow` 恒真 | ❌ 不能判别，去掉 |
| 无 community | 社区由结构边（contains/import/inherits）构建，module 通过 `contains` 边锚定自己的函数 → 任何函数都在社区里 | ❌ 不能判别，去掉 |

另外两个必须排除的「无 caller 但非死代码」：

- **入口函数**（`entry_names` 短名 glob，默认 `main`）：业务入口，无 caller 正常。
- **框架装饰器注册的函数**（`entry_decorators`：`@app.route` / `@click.command` /
  `@router.get` / `@celery.task`）：由框架在运行时注册，无静态 caller 但不是死代码。
  解析器目前**不记录装饰器**，`entry_decorators` 是「已加载未消费」的配置键 —— 本
  改动消费它。
- **测试函数**：测试文件在索引里且节点标记 `is_test = 1`，pytest 无静态 caller
  但不是死代码 → 排除 `is_test` 节点。

## JSON 结构

```json
{
  "symbols": [
    {"qname": "util::hash_pw", "kind": "function", "file": "util.py",
     "line": 1, "signature": "def hash_pw(pw) -> str:", "decorators": []},
    {"qname": "util::helper", "kind": "function", "file": "util.py",
     "line": 6, "signature": "def helper():", "decorators": []}
  ],
  "files": [
    {"path": "util.py", "qname": "util", "symbol_count": 2,
     "symbols": ["util::hash_pw", "util::helper"]}
  ],
  "meta": {
    "symbol_count": 2,
    "file_count": 1,
    "note": "候选是静态分析的删码候选，不是自动删除令：动态调用、反射、多态覆盖与框架魔法不可见，删除前请人工核对。"
  }
}
```

- `symbols[]`：符号档候选，含 `qname`（`module::scope.scope.name`）、`kind`
  （function/method/class）、`file`（仓库相对路径）、`line`（start_line）、
  `signature`、`decorators`（解析器捕获的装饰器名数组，供调用方判断为什么被排除/
  为何可疑）。
- `files[]`：文件档候选（符号档的 rollup），含整文件路径、module qname、内部死符号
  数量与 qname 列表。
- `meta`：两档计数 + 静态分析免责声明。

## 组件设计

### `parser.py` — 捕获装饰器（数据驱动）

- `ParsedNode` 新增字段 `decorators: list[str]`（默认 `[]`）。
- 捕获机制（已用 tree-sitter AST 实测确认）：
  - **Python**：`@deco` 的 def 被包在 `decorated_definition` 容器里（`decorator`
    子节点 + 内层 `function_definition`/`class_definition`）。`_walk_defs_typed`
    命中 `decorated_definition` 时收集其 `decorator` 子节点的装饰器名，再递归进
    内层 def 节点携带。方法装饰器（类内 `@staticmethod def f()`）同样是
    `decorated_definition`，天然覆盖。
  - **TS/JS**：`decorator` 是 `class_declaration` / `method_definition` 的**直接
    子节点**。def 分支里扫子节点收集即可。顶层函数装饰器在 tree-sitter-typescript
    grammar 中是 `ERROR`，无法可靠捕获 —— 已知限制，不做。
- 装饰器名提取：取 `decorator` 节点里 `@` 之后第一个子节点（表达式）；若表达式是
  call（`@app.route("/")`），取 callee 的文本（`app.route`）—— 复用
  `_call_target`；否则取表达式文本（`@staticmethod` → `staticmethod`）。链式
  `@a @b` 收集为列表。
- LANG 条目数据驱动：`python` 增加 `decorated_node="decorated_definition"`；各语言
  增加 `decorator_node="decorator"`。某语言不配置则为 no-op。

### `db.py` — schema + 迁移

- `nodes` 表新增 `decorators TEXT`（JSON 数组）。
- `_migrate_nodes` 增加 `ALTER TABLE nodes ADD COLUMN decorators TEXT`（沿用
  `in_degree`/`is_test` 的迁移模式，老库平滑升级）。
- `INDEX_VERSION` 3 → 4：索引内容语义变化（节点带装饰器），强制全量重建，否则老
  索引会把框架注册函数误报为死代码。

### `indexer.py` / `update.py` — 两条写入路径都落列

- `indexer._write_nodes` 与 `update._insert_nodes` 的 `INSERT INTO nodes(...)` 都
  带上 `decorators`（JSON 序列化）。任何一条漏写都会导致增量/全量索引结果不一致。

### `deadcode.py` — 新查询模块（只读，`conn` 参数注入）

`find_dead_code(conn, config) -> dict`，主控只做编排，子查询拆小函数：

1. **符号档**：
   ```sql
   SELECT n.qualified_name, n.kind, n.file_path, n.start_line, n.signature, n.decorators
   FROM nodes n
   WHERE n.kind IN ('function','method','class')
     AND n.is_test = 0
     AND n.in_degree = 0
   ```
   然后在 Python 侧过滤入口（与 `flow_builder` 同款 fnmatch 语义）：
   - 短名命中任一 `entry_names` glob → 排除；
   - `decorators`（JSON 解码，容错 `[]`）中任一命中任一 `entry_decorators` fnmatch
     → 排除。

2. **文件档**：
   ```sql
   SELECT n.qualified_name, n.file_path
   FROM nodes n
   WHERE n.kind = 'module'
     AND n.file_path NOT LIKE '%__init__.py'
     AND NOT EXISTS (
       SELECT 1 FROM edges e
       WHERE e.kind = 'import' AND e.resolution = 'resolved'
         AND e.target = n.qualified_name)
   ```
   再过滤：文件内含入口符号（entry_names 命中或装饰器命中）、或含 `is_test` 节点
   的文件 → 排除。文件内死符号列表 = 符号档中 `file` 等于该路径的符号（rollup）。

`config` 只读 `entry_names` / `entry_decorators`（已在 `_CONFIG_HASH_KEYS` 中，**不
新增配置键**）。

### `mcp_server.py` / `cli.py` — 前端

- MCP tool `find_dead_code()` → `json.dumps(find_dead_code(conn, config))`。与
  `get_communities` 一样无参数、读当前索引。
- CLI `dead-code` 子命令 → 默认 print JSON（与 `query`/`summary` 一致）；
  `--format text` 输出紧凑表格（每行一个符号/文件）。索引时效由既有机制保证
  （MCP server 启动 catch-up + watcher；CLI 读当前 DB，先 `rebuild` 或用 MCP）。

## 错误处理

- 纯 DB 读查询，无 git diff / 网络等失败路径。
- `decorators` 列解析容错：`NULL` / 空 / 非法 JSON → `[]`（不因一个坏值炸掉整个
  报告）。
- 老库：`INDEX_VERSION` 4 与 `build_meta.index_version` 不符 → `sync` 自动全量重建。

## 测试（TDD）

### `tests/test_parser.py` 新增

- 模块级 `@app.route("/")` 装饰函数 → `decorators == ["app.route"]`。
- 类内 `@staticmethod` 方法 → 捕获 `["staticmethod"]`。
- 链式 `@a @b` → `["a", "b"]`。
- 带参数装饰器（`@click.command()` / `@cli.option("--x")`）→ 剥参后取 callee 名。
- TS 类/方法装饰器（`@Controller("x")` / `@Get()`）→ 捕获（复用 `parse_file`）。

### `tests/test_db.py` 新增

- 迁移后 `nodes` 表含 `decorators` 列（沿用 `is_test` 迁移断言模式）。

### `tests/test_deadcode.py` 新增

- 走 fixture 全量 rebuild：
  - `util::hash_pw` / `util::helper` 检出；`app::main` 是入口不检出；
    `auth::UserService.authenticate` 无 caller 检出。
  - `util.py` 进文件档；`app.py`（含入口）与 `auth.py`（被导入）不进。
- 手建库：装饰器节点 + `entry_decorators` 命中 → 排除；`is_test=1` 节点 → 排除；
  `entry_names` glob 命中 → 排除；`decorators` 坏 JSON → 容错为 `[]`。

### `tests/test_mcp_server.py` / `tests/test_cli.py` 新增

- MCP tool `find_dead_code` 返回合法 JSON 且 `meta.symbol_count` 与 fixture 一致。
- CLI `dead-code` 输出合法 JSON；`--format text` 输出含 qname 的行。

## 明确不做（YAGNI）

- **不做 flow / community 判据**：现状核对已证明它们对函数/模块恒真，加了只会虚增
  复杂度和误导（与设计文档 §4.4 的偏差已在 CLAUDE.md 注明）。
- **不做 TS 顶层函数装饰器**：grammar 层面是 `ERROR`，不可靠。
- **不做 `__init__.py` 文件档候选**：import 解析对包入口不可靠（`import pkg` 与
  `from pkg.sub import x` 的 target 不同），避免误报。
- **不做「自动删除」/ 强安全保证**：输出是静态分析候选，动态调用、反射、多态覆盖
  不可见，`meta.note` 写明删除前需人工核对。
- **不新增配置键**：复用 `entry_names` / `entry_decorators`。
- **不改 flow / community 构建逻辑**：只读查询，不碰 Phase B/C。
