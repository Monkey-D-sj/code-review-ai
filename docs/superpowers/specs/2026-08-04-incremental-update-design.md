# 设计：分层增量更新（incremental update）

日期：2026-08-04

## 目标

把 `code-review-ai` 的索引更新从「每次全量重建」改为「分层增量」：

- **便宜的层**（nodes / edges / degrees）：改动文件才 re-parse，直接 patch DB —— 由
  **watcher** 在保存时触发，未提交改动实时进图。
- **贵重的层**（flows / communities）：从 DB 的 nodes+edges **全量重算**，不碰解析 ——
  由 **git hook** 在 commit / merge / checkout / rewrite 时触发。
- 去掉内存 `ParseCache`，全局状态以 DB 为准。
- 全量 `rebuild` 保留：首次建索引、强制重建、配置/版本变更兜底。

**正确性目标**：在「相同文件集 + 相同代码版本」前提下，增量路径最终产生的
nodes/edges/flows 与全量重建**严格一致**。解析器对调用边「是否存在」的判定只依赖全局
qname 集，而该集合在每次增量后已是最新；配合末尾的 **resolution 修复 pass**（见 ②），
可达逐行一致。已知残余差异仅限 wildcard import（`from x import *`）——全量重建同样
解析不了，属两路径一致的固有行为，不算漂移。

## 触发与数据总览

| 触发 | 动作 | 数据 |
|---|---|---|
| watcher（保存） | `update_nodes_edges` | nodes/edges/degrees + manifest |
| git hook（post-commit/merge/checkout/rewrite） | `sync` | 上面 + flows/communities + `flows_as_of_head` |
| 启动 / MCP `rebuild_index` | `sync`（内部按需跳过） | 同上 |
| CLI `rebuild`（手动强制） | `rebuild` | 全量清表重建 |

派生关键：`build_flows` / `build_communities` 只吃 `NodeRow`/`EdgeRow`，从 DB 即可重算、
无需 ParsedFile —— 这是能去掉缓存的原因。

## ① 增量 nodes/edges 更新 — `update.update_nodes_edges`

```
update_nodes_edges(config, conn, changed_paths=None):
    改动集:
      changed_paths 给定（watcher 事件）→ 分类 add/modify/delete，转 repo-relative
      未给定（启动/hook 兜底）→ manifest 对比
    一个事务内:
      parse 改动+新增文件 → ParsedFile 列表
      新全局 qname 集 = (DB 旧集 − 删除文件节点) ∪ (改动文件新节点)
      DELETE edges/nodes WHERE file_path IN (改动+删除文件的绝对路径)
      DELETE flow_memberships/community_memberships WHERE node_id IN (被删节点)  ← 防悬空
      INSERT 新 nodes（回填 parent_id）+ 新 edges（对新 ParsedFile resolve，用新全集）
      重算 degrees（edges 表全量 GROUP BY）
      _repair_resolutions(conn)            ← 见 ②
      更新 manifest（upsert 改动/新增的 hash，删移除项）
      stamp built_at
```

- manifest 键 = repo-relative；DB `file_path` = 绝对路径（既有约定），按绝对路径删。
- 改动文件只删它自己的旧行再插新行；未变文件的 nodes/edges 一行不动。
- degrees 全量重算（edges 表扫描 + 计数，~19k 行 ≈ 10ms）。
- 文件发现：`list_source_files` 改为**单次** `git ls-files` 传全部 extension glob
  （当前 7 个子进程 → 1 个，458ms → ~60ms）+ 逐文件 `isfile` 判删除（tracked 但已删的
  文件 → delete）。

## ② resolution 修复 pass — `update._repair_resolutions`

原理：未变文件的边，其 `target` 由该文件**不变**的 import/local 映射推出，唯一可变的是
「全局 qname 集」的存在性 → 只差存在性，可全表修复。

规则（对 `resolution != 'dynamic'` 的边）：

- `kind='call'`：仅当 `target` 含 `::`（类型一：已算出 `module::name` 的 qname）才按
  `target ∈ 全集` 翻转；无 `::` 的 raw 目标（裸名、CALL_OTHER）**跳过**——它们不是合法
  qname，且**全量重建同样解析不了**；若翻转反而会误判（裸名撞单段 module）。
- `kind IN ('import','extends','implements','contains')`：`target ∈ 全集` 即解析规则
  本身（`_build_imports` / `_build_inherits` / `_build_contains` 的判定），直接翻转。

```
new_label = 'resolved' if target ∈ 当前全集 else 'unresolved'
只 UPDATE 标签变化的行
```

- 两个方向都修：unresolved→resolved（新增符号）、resolved→unresolved（删除符号）。
  后者在 flows 构建时本会因 target 缺节点被跳过，此处让标签本身也对齐全量。
- 位置：`update_nodes_edges` 事务末尾、删除节点之后、插入节点之后（保证全集是「当前」的）。
  读一次 `SELECT qualified_name FROM nodes` 进 set + 扫一遍 edges（~19k 行 ≈ 几 ms）。
- 效果：edges 表与全量重建逐行一致；commit 时 flows/communities 从修复后的 edges 重算
  → 全量也一致。

## ③ flows / communities 从 DB 重算 — `update.update_flows` / `update_communities`

```
update_flows(config, conn):
    if 未过期(HEAD == flows_as_of_head): return      # 启动快速短路
    nodes = SELECT id,qualified_name,file_path,kind FROM nodes
    edges = SELECT source,target,resolution FROM edges WHERE kind='call'
    flows = build_flows(nodes, edges, config.entry_names)
    事务内: DELETE flows+flow_memberships → 重插
    stamp flows_as_of_head = git rev-parse HEAD
```

- `update_communities` 同理（仅 `community_detection` 开启时）：structural（非 call）
  resolved 边 → `build_communities` → 替换 communities/memberships/`community_edges`。
- flows 表示「**最后提交状态**」（commit 时构建）；commit 间的 watcher 改动不进 flows，
  `get_impact` 对新符号走 edges fallback（优雅降级）。

## ④ 触发源

- **watcher**（`watcher.py`）：事件 → `update_nodes_edges(config, conn, changed_paths)`，
  不再全量 rebuild。
- **git hook**：`post-commit` / `post-merge` / `post-checkout` / `post-rewrite` 各写一份，
  调用 `code-review-ai sync --repo <abs> --db <abs>`。`sync` = nodes 补（manifest 差集，
  无变化即 no-op）→ flows → communities。post-commit 后 HEAD 已变，flows 必然重算。
- **启动**（`watcher.startup_sync`）：`sync`，但 ③ 用 `flows_as_of_head == HEAD` 短路——
  只有「服务关闭期间有过 commit（hook 没跑）」才重算 flows。
- **MCP `rebuild_index`**：改跑 `sync`（「立即最新」语义不变；config/版本变更由 ⑤ 兜底
  转全量）。
- **CLI**：新增 `update`（只 ①）、`sync`、`install-hooks`；`rebuild` 保留全量。

## ⑤ 元数据与变更检测

- 新表 `files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, file_hash TEXT)`：
  - 快速路径：mtime+size 匹配 → 未变（跳过 hashing）；否则 re-hash 对比判定变更。
  - hash 为权威信号（内容变了 hash 必变）。
- `build_meta` 新增键：
  - `flows_as_of_head` — flows/communities 的 as-of HEAD；
  - `config_hash` — `exclude`/`entry_names`/`entry_decorators`/`community_*`/`diff_base`
    等的哈希；
  - `index_version` — 算法/schema 版本。
- **兜底全量**：`sync`/`update_nodes_edges` 检测到 `config_hash` 或 `index_version` 与
  build_meta 不一致 → 走 `rebuild` 全量。
- `is_stale` 由新的两问替代：`needs_nodes_update`（manifest 差集）+ `needs_flows_update`
  （HEAD 差）；`built_at` 保留为信息性（watcher 测试仍依赖它变化）。

## ⑥ hook 安装 — `install-hooks`

- 新 CLI 子命令 `install-hooks --repo <path>`：向目标仓库 `.git/hooks/` 写 4 个钩子
  （post-commit / post-merge / post-checkout / post-rewrite），脚本 =
  `code-review-ai sync --repo ... --db ...`。launch 命令在安装时捕获（与 MCP 安装同机制：
  `uvx --from <git-url>` 或本地 `uv run --project`）。
- 每仓库一次；未装 hook 的仓库 flows 靠启动 `HEAD` 对比兜底，功能不坏。
- Windows 下 git 经 sh 执行钩子：脚本用 POSIX sh，路径引号/转义按实现细节处理。

## 并发与错误处理

- hook 进程与 watcher 进程同时写同一 SQLite：WAL 已开，另加 `busy_timeout`；每次写入走
  `transaction()`（原子，失败回滚，旧索引保留）。
- hook 失败不影响 commit（git 的 post-* 钩子语义保证）；watcher 失败仅记录日志（保持现行为）。
- parse 单文件异常：沿用现全量语义——异常上抛、事务回滚，由 watcher 层捕获记日志，不
  留部分状态。

## 组件设计（一模块一职责）

- **`update.py`（新）**：增量路径编排——`update_nodes_edges`、`_repair_resolutions`、
  `update_flows`、`update_communities`、`sync`、manifest 读写、`needs_*` 检查。复用
  `parser.parse_file`、`resolver.resolve_edges`、`flow_builder.build_flows`、
  `community.build_communities`、`db.transaction`；不反向依赖 `indexer`（避免环），
  需要全量兜底时由调用方决定。
- **`indexer.py`**：保留全量 `rebuild` 及 `_write_*`；抽出共享的 `recompute_degrees(conn)`
  供 `update` 复用（避免重复）。
- **`parser.py`**：`list_source_files` 改为单次 git 调用。
- **`db.py`**：`SCHEMA` 加 `files` 表；`_migrate_*` 补老库。
- **`watcher.py`**：`run_watcher` 调 `update_nodes_edges`；`startup_rebuild` → `startup_sync`
  （内部 `sync`）。去掉 `ParseCache` 参数。
- **`mcp_server.py`**：`rebuild_index` → `sync`；去掉 `ParseCache`。
- **`cli.py`**：加 `update` / `sync` / `install-hooks` 子命令。

## 测试

### `tests/test_incremental.py`（新）

- **watcher 只动 nodes/edges**：改 A 文件 → `update_nodes_edges` 后 A 的 nodes/edges 更新、
  B 未动、flows 表行数不变。
- **修复（新增方向）**：F 调 `from models import User`（当时 unresolved）→ models.py 加
  User → `update_nodes_edges`（只 re-parse models.py）→ F 那条边翻 resolved，且不 re-parse F。
- **反向修复**：删 User → 翻回 unresolved。
- **类型二不误判**：裸名 `login()`（未 import）+ 存在单段 module `login` → 修复 pass 不动它。
- **删文件**：nodes/edges 清除、flow_memberships/community_memberships 无悬空。
- **等价性**：对同一棵树，`sync`（多次增量累积）后的 edges+flows == 一次 `rebuild`
  的结果（边按 (source,target,kind) 比较）。
- **manifest 变更检测**：modify/add/delete 三态；mtime+size 未变但内容变 → hash 判定。
- **flows 短路**：`flows_as_of_head == HEAD` → `update_flows` no-op。
- **配置变更**：改 `entry_names` → 触发全量 rebuild。

### 既有测试改动

- `test_indexer.py::test_rebuild_cache_skips_unchanged_files`：ParseCache 移除 → 改写为
  「manifest 无变化时 `update_nodes_edges` 零 parse 调用」。
- `test_watcher.py`：`startup_rebuild` → `startup_sync`；`run_watcher` 触发改为
  `update_nodes_edges`（断言 built_at 变化仍成立）。

## 明确不做（YAGNI）

- **不持久化 raw_calls/imports 到 DB**（importers 重 resolve 依赖 re-parse）——repair
  pass 已在不 re-parse 前提下达成严格一致。
- **不做增量 flows**（只重算受影响 entry）——flows 从 DB 全量重算已足够快（实测 1573
  flows 在 write_db 287ms 内），等 benchmark 显示瓶颈再优化。
- **不做跨进程解析缓存**——冷启动仍全量 parse（~0.7s），但只写变化的行；manifest 保证
  DB 写入是增量的。
- **不做增量 communities**（Leiden 全局分区，重算即正确）；community 默认关闭，仅在开启时
  随 commit 重算。
- **wildcard import 解析**：两路径一致地不支持，不修。
