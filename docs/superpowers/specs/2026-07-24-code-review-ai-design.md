# Code Review AI — 代码影响链路分析工具设计

- **日期**: 2026-07-24
- **状态**: 设计已确认，待实现
- **语言**: Python 3.14（uv 管理）

## 1. 目标

用 tree-sitter 解析 Python 代码库的 AST，落库 SQLite，构建调用图（nodes / edges）与调用链路（flows），通过 MCP Server 供 AI 在 code review 时按需查询「某函数改动涉及整条链路是否受影响」。AI 只拉取相关链路而非逐文件阅读，从而节省 token。

## 2. 关键决策

| 维度 | 决策 |
|---|---|
| 交付形态 | MCP Server（核心为库，MCP 为薄前端） |
| 目标语言 | Python 单语言 |
| 链路语义 | 双向：上游调用方（blast radius）+ 下游被调方，传递闭包 |
| 调用解析 | 导入感知：`module.func()` + 同模块直接调用可解析；`obj.method()` 标 `dynamic` |
| 改动入口 | 多模态：无参 git diff / 传文件清单 / 传 symbols(qualified_name) |
| 索引策略 | 全量重建；`watchfiles` 监听改动自动重建，review 时不重建 |
| flow 模型 | 从指定入口 BFS 最短路径，每个可达节点一条 flow（非全路径，防菱形爆炸） |

## 3. 数据模型（SQLite schema）

库走 WAL 模式，重建在事务内完成以支持原子切换。

### 3.1 `nodes` — 符号节点

```sql
CREATE TABLE nodes (
    id INTEGER PRIMARY KEY,
    qualified_name TEXT UNIQUE,   -- 冒号连接作用域：auth:login / pkg.auth:UserService:authenticate
    kind TEXT,                    -- module / class / function / method
    language TEXT,                -- python（v1，预留多语言）
    file_path TEXT,               -- 相对仓库根
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,               -- def 行原文，如 def login(user, pw) -> Token
    parent_id INTEGER REFERENCES nodes(id)  -- 方法所属类 / 嵌套函数所属函数，可空
);
```

- `qualified_name`：模块路径保留 Python 点号，`:` 连接作用域层级，与 Python 自身 `.` 属性访问区分。
- 节点含四类：`module`（每文件一个，作锚点与未来 import 边）、`class`、`function`、`method`（类内函数）。

### 3.2 `edges` — 直接调用边

```sql
CREATE TABLE edges (
    id INTEGER PRIMARY KEY,
    source TEXT,                  -- 调用方 qualified_name
    target TEXT,                  -- 被调方 qualified_name；未解析/动态时存原始表达式（如 obj.method）
    kind TEXT,                    -- 关系类型：call（后续可扩 import / inherit）
    file_path TEXT,               -- 调用点文件
    call_line INTEGER,
    resolution TEXT               -- resolved / dynamic / unresolved
);
```

- `source` / `target` 存 `qualified_name` 文本（非 FK id），便于 edges 自包含、并能存未解析的原始表达式。
- `resolution` 是 `target` 的可信度信号（合并 `target_name` 后唯一区分真 qname 与原始表达式的方式）：
  - `resolved`：target 是真 qname，在 `nodes` 中可对上，可进 flow 遍历。
  - `dynamic`：`obj.method()` 形式，绑不定具体类/方法，target 存原始表达式。
  - `unresolved`：名字查不到（builtin / 外部库 / `from m import *`），target 存原始名。
- dynamic / unresolved 边**保留 target 字符串但无对应节点**，AI 能看到解析缺口，不会误判链路完整。

### 3.3 `flows` — 调用链路（物化）

```sql
CREATE TABLE flows (
    id INTEGER PRIMARY KEY,
    name TEXT,                    -- 入口函数名
    entry_point_id INTEGER,       -- 入口节点 id
    depth INTEGER,                -- 该 flow 终点距入口的深度（边数）
    node_count INTEGER,           -- 链上节点数
    file_count INTEGER,           -- 涉及文件数
    criticality REAL,             -- 0.0 ~ 1.0（v1 留空 NULL，算法后定）
    path_json TEXT                -- 有序节点 ID 数组，如 "[102,103,104,105]"
);
```

- 一个 flow = 一条有序调用路径（入口到某可达节点的 BFS 最短路径）。
- `depth` 字段澄清：表示该 flow 自身深度（入口到终点的边数）；构建时的全局深度上限由 config `max_depth` 控制（非每行重复存的 cap）。

### 3.4 `flow_memberships` — flow 成员

```sql
CREATE TABLE flow_memberships (
    flow_id INTEGER,
    node_id INTEGER,
    position INTEGER,             -- 在流中的顺序（0=入口）
    PRIMARY KEY (flow_id, node_id)
);
```

- `path_json`（给 AI 直接读）与 `flow_memberships`（带 position，用于反查「哪些 flow 经过节点 X」）冗余存储，各司其职。

### 3.5 查询语义（统一走 memberships，不分正反向）

改动函数 F → 查 `flow_memberships WHERE node_id = F` 的所有 flow → 每个 flow 内：
- `position < F` 的前缀 = 上游调用方（向入口）
- `position > F` 的后缀 = 下游被调方
- 该 flow 的 `entry_point` = 受影响的业务入口

F 若是入口本身，前缀为空、后缀即其调用链。无需 `direction` 列。

## 4. 解析与调用解析

### 4.1 Parser（tree-sitter-python）

- 文件清单：`git ls-files --cached --others --exclude-standard "*.py"`（已跟踪 + 未跟踪新文件，尊重 .gitignore）。
- 逐文件解析为 AST，提取：
  - **节点**：`function_definition` / `class_definition`，外加每文件一个 `module` 节点。
    - `qualified_name`：模块路径(点) + 包含类链 + 名字，`:` 连接；`__init__.py` 模块名取包名。
    - `kind`：module / class / function / method（类内函数 = method）。
    - `file_path`、`start_line`/`end_line`、`signature`（def 行原文）、`parent_id`、`language=python`。
  - **原始调用**：`call` 节点，判定形式：
    - `foo()` → simple，target = `foo`
    - `a.b()` → attribute，target = `a.b`（含链式 `a.b.c`）
    - `f()()` / `a[b]()` 等 → other
    - 记 `source`（最近包含的 function/method；无则 module）、`call_file`、`call_line`、原始 target 表达式。

### 4.2 Resolver（导入感知 → edges）

每模块建两张表：
- **导入表**：`import m` / `import m as n` / `from m import x as y` / 相对导入 `from . import x`（按当前包解析为绝对模块）；`from m import *` 标星号导入。
- **本地符号表**：本模块 function / class，按名索引。

解析每个原始调用（模块 M、source S、target T）：
- `foo()`：本地符号表命中 → resolved（target = 该节点 qname）；否则导入表 `from m import foo` → 查全局 `m:foo`，命中 resolved 否则 unresolved；都没有 → unresolved。
- `a.b()`：`a` 是导入模块 → 查 `a:b`；`a` 是本地类 → 查 `a:b`（方法）；`a` 是 self/cls 或变量 → dynamic（target 存 `a.b`）；链式同理。
- other 形式 → unresolved。

**全局解析顺序**：先解析所有文件得全部节点 → 建全局 `qname -> node_id` 映射 → 再解析调用成 edges（跨模块查找需要）。

### 4.3 入口点识别（config + 启发式）

- 配置（`pyproject.toml` 的 `[tool.code-review-ai]` 或独立 `cr-ai.toml`）：
  - `entry_names`：如 `["main", "run", "handle_*"]`
  - `entry_decorators`：如 `["app.route", "click.command", "router.get", "router.post", "celery.task"]`
- 默认启发式：名为 `main` 的函数。
- 匹配到的函数节点即入口；flow 生成时用其 `node_id` 作 `entry_point_id`。无需改 schema。

### 4.4 重建编排（Phase A + Phase B）

**Phase A（解析落库）**：
1. `git ls-files ...` 拿文件。
2. 逐文件解析 → 节点 + 原始调用 + 每模块导入表。
3. 建全局 `qname -> node_id` 映射。
4. 解析调用 → edges。
5. 识别入口点。
6. 事务清空 + 写 `nodes` / `edges`（Phase A 完）。

**Phase B（flow 生成，读库不重解析）**：
1. 全量查 `nodes` + `edges` 入内存。
2. 建邻接表：
   - 只取 `resolution = 'resolved'` 的边。
   - 建 `qname -> node_id` 映射，邻接表以 `node_id` 为键：`adj[source_id] = [target_id, ...]`。
   - 方向：正向（caller → callee）；反向查询走 memberships，不需要反向邻接表。
3. 从每个指定入口 BFS：visited 集合（处理递归/环）、parent 指针 reconstruct 最短路径、`max_depth` 上限。
4. 每个可达节点产出一条 flow：`path_json` = 重建路径；写 `flow_memberships`（position = 路径中的下标）；填 `node_count` / `file_count` / `depth`；`criticality = NULL`。

flow 生成与解析解耦——`nodes`/`edges` 落库后单独跑，算法变了能不重解析地重算 flow。

## 5. 改动检测与 MCP 工具接口

### 5.1 改动检测（多模态 + changed-symbol 提取）

三种入口：
- 无参 → `git diff <base>` 全量。
- 传文件清单 → `git diff <base> -- <files>`。
- 传 `symbols` → 直接用作改动符号集，跳过 diff。

`base` 可配（默认 `origin/main`，fallback `main`），config `diff_base`。

**changed-symbol 提取**（从 diff）：
- 逐改动文件取 diff 的增删行范围。
- 解析该文件**当前** AST → 函数行范围 `[start, end]`。
- 函数与增删行有交集 → 改动符号。
- 未跟踪新文件（file 模式）→ 其所有函数均算改动。
- v1 聚焦修改/新增函数；删除的符号单独报告但不深追（当前图已无该节点）。

三种入口最终都归约成「一组改动的 qname」，只是来源不同。

### 5.2 MCP 工具集（stdio 本地服务）

| 工具 | 入参 | 返回 |
|---|---|---|
| `rebuild_index` | 无（或 `force`） | build 统计：node/edge/flow 计数、built_at |
| `get_impact` | `symbols`[] 或 `files`[] 或无（git diff） | 每个改动符号的受影响链路（见 5.3） |
| `search_symbol` | `query`（名字/glob） | 匹配节点：qname、kind、file、line |
| `get_symbol_detail` | `qualified_name` | 节点详情 + 直接调用/被调边 |
| `list_entry_points` | 无 | 所有指定入口（qname、file） |

### 5.3 影响查询流程（`get_impact` 内部）

对每个改动符号 s：
1. `SELECT flow_id, position FROM flow_memberships WHERE node_id = s` → 含 s 的所有 flow。
2. 每个 flow（按 `path_json` / memberships 排序）：
   - `position < s` 的前缀 = 上游调用方。
   - `position > s` 的后缀 = 下游被调方。
   - `entry_point` = 该 flow 的入口（受影响的业务链路）。
3. **回退**：s 不在任何 flow 上（从入口不可达）→ 直查 edges：`target = s` 得调用方，`source = s` 得被调方。
4. 汇总受影响入口集合 = 含 s 的 flow 的 `entry_point` 去重。

### 5.4 输出格式与 token 控制

- 紧凑 JSON，每节点仅 `qname + file:line + signature`，不含源码。
- 链路去重；每方向节点数上限可配（默认 50），超出截断并提示。
- 直接给出「受影响入口」清单，让 AI 一眼看到波及哪些业务链路。

### 5.5 生命周期（watcher + catch-up + 原子切换）

- **启动补建**：MCP server 启动时，若索引缺失或文件 mtime 晚于 `built_at` → 先重建一次（catch-up），保证一开就是新的。
- **监听**：`watchfiles` 监听 `.py`，改动防抖（~500ms 批处理）→ 后台全量重建。
- **原子切换**：SQLite WAL，重建在事务内；`get_impact` 重建期间读上次已提交的旧索引，提交后原子切到新索引，永不见半成品。
- **`rebuild_index`** 降级为手动 force（可选），正常情况不用调。

代价/边界：
- server 得在跑才监听（Claude Code 开着时）；关掉期间在 IDE 改的代码，下次启动靠 catch-up 补建。
- 想让索引常驻新鲜（不依赖 session），可拆独立 daemon 跑 watcher、MCP 只读 DB（v1 不做）。
- 全量重建对中小型够快；规模涨上去再上真增量（仅重解析改动文件 + 其导入方 + 仅重算受影响 flow）。

## 6. 错误处理、测试与项目结构

### 6.1 项目结构（src 布局，每模块单一职责，无环依赖）

```
src/code_review_ai/
  config.py        # 配置加载（[tool.code-review-ai] / cr-ai.toml + env）
  db.py            # SQLite 连接、schema、WAL、事务
  parser.py        # tree-sitter -> 节点 + 原始调用 + 导入表（每文件）
  resolver.py      # 导入感知 -> edges（带 resolution）
  flow_builder.py  # 邻接表 + BFS -> flows/memberships
  indexer.py       # Phase A/B 重建编排
  changes.py       # git diff / files / symbols -> 改动符号集
  impact.py        # memberships 切片 + edges 回退 -> 影响面
  watcher.py       # watchfiles 防抖 -> 触发重建
  mcp_server.py    # MCP 工具注册/分发
  cli.py           # 可选 CLI（手动 rebuild/query/search）
tests/
  fixtures/        # 合成 Python 仓库（main、handler、跨模块、菱形、环、off-flow 工具）
  test_*.py        # 每模块单测
```

依赖：`tree-sitter`、`tree-sitter-python`、`mcp` SDK、`watchfiles`；dev `pytest`。git 走 subprocess（不引 pygit2）。SQLite / tomllib 走标准库。

配置示例：

```toml
[tool.code-review-ai]
repo_path = "."
db_path = ".code-review-ai/index.db"
diff_base = "origin/main"
max_depth = 10
watch_debounce_ms = 500
entry_names = ["main", "run", "handle_*"]
entry_decorators = ["app.route", "click.command", "router.get", "router.post", "celery.task"]
```

### 6.2 错误处理

- **语法错误文件**：tree-sitter ERROR 节点，部分解析，记 warning 不中断。
- **文件读取**：缺失 / 编码异常 → 跳过 + warning（UTF-8 优先）。
- **git 失败**：非仓库或 base ref 缺失 → 明确报错 + config 提示；diff 模式不可用时引导用 `symbols` 模式。
- **重建失败**：事务回滚，旧索引保留（WAL 原子性），上报错误。
- **watcher 崩溃**：不拖垮 MCP server，下次访问触发 catch-up。
- **MCP 工具**：返回结构化错误（符号未找到 / 索引重建中），不抛崩。
- **重建期间查询**：读旧已提交索引（原子），响应可选带 rebuilding 标记。
- 原则：server 永不崩；优雅降级（部分结果 + warning）；重建失败保旧索引。

### 6.3 测试策略（pytest）

- **parser**：节点 qname/kind/行范围/signature、嵌套/方法/类/module 节点、原始调用形式判定。
- **resolver**：直接调用、`module.func`、`Cls.method`、`obj.method`(dynamic)、`from m import x`、相对导入、`import *`(unresolved)、别名。
- **flow_builder**：线性链、分支、环（递归）、菱形（每目标一条路径）、depth 上限、多入口。
- **indexer**：全量重建计数/内容、事务原子性（注入失败保旧）。
- **changes**：修改函数(行重叠)、新文件(全函数)、删除(报告)、files 模式、symbols 模式。
- **impact**：上下游切片、受影响入口、off-flow 回退 edges。
- **mcp_server**：工具分发、三种入参模式、输出格式、错误响应。
- **集成**：端到端——索引 fixtures → 改文件 → watcher 触发重建 → `get_impact` 正确返回。
- **爆炸防护**：菱形图断言 flow 数有界（每 entry-target 一条，非全路径）。

## 7. 范围外 / 未来

- 多语言（grammar 插件机制）。
- 类型感知调用解析（Pyright / LSP，解析 `obj.method()` 动态分派）。
- 真增量索引（仅重解析改动文件 + 导入方 + 受影响 flow）。
- `criticality` 评分算法。
- `import` / `inherit` 边（`edges.kind` 扩展）。
- 独立 watcher daemon（不依赖 MCP session）。
