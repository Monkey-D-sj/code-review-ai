# delete_change + tombstone Design

**日期**:2026-08-07
**范围**:update 阶段把被删的文件/函数存成 tombstone（含一跳上游），summary 阶段新增顶层 `delete_change` 字段输出这些删除及其影响者，让 AI 审查者看到「谁在依赖被删的东西」。
**前置**:`uncovered_changes` 特性已合入——`_git_diff` 产出 `(diff_ranges, deleted)`，`_diff_coverage` / `_symbols_summary` / `build_change_summary` 已就位。

## 背景

删除在 call-graph 里的困境:**增量 update 观察到一个文件被删时，`_apply_nodes_edges_delta` 把该文件的所有节点 + 边一起 `DELETE` 掉**。之后 `summary` diff 只能看到 `{file, hunks: [], deleted: true}`，既不知道被删了哪些函数，也不知道谁在调用它们。全量重建后旧节点/旧边彻底消失，届时「依赖 DB 查旧节点」不可靠（`uncovered-changes` spec 的非目标已注明）。

**目标**:被删函数的一跳上游——caller / subclass / importer——回答「被删除的函数对上游有啥影响」。文件被删同样要追踪 import 它的模块。tombstone 在**删除发生的时刻**把节点快照 + 上游持久化下来，这样无论之后增量清理还是全量重建，summary 都能读回删除细节。

## 数据形状

### 新表 `tombstones`（db.py SCHEMA）

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
CREATE INDEX IF NOT EXISTS idx_tombstones_file ON tombstones(file_path);
CREATE INDEX IF NOT EXISTS idx_tombstones_qname ON tombstones(qname);
```

- 在 `SCHEMA` 里 `CREATE TABLE IF NOT EXISTS` 即够——`init_schema` 在 cli / mcp_server / watcher 三个入口都会调用，存量 DB 在任一前端下次启动时获得新表；**无需 bump `INDEX_VERSION`**。
- 字段全部来自被删节点的旧行（`file_path` 与 `nodes.file_path` 同一格式，见限制 7）；`upstream_json` 是 `[{"source": qname, "kind": "call"|"inherits"|"import", "file": 绝对路径}]`。
- `file_deleted`：1=整文件删除，0=存活文件内函数被删。
- **tombstones 是追加式删除日志**：全量重建 `_clear_tables` 不碰它——这正是它的价值（重建后图没了，删除细节还在）。read 端用「当前 diff 的文件窗口 + `qname NOT IN 当前 nodes`」过滤陈旧条目（见读路径）。

### 顶层字段 `delete_change`（summary diff 路径）

删除条目从 `uncovered_changes` **移入** `delete_change`：

```json
{
  "summary": { "files_changed": 5, "lines_added": 10, "lines_removed": 3,
               "changed_functions": 2, "uncovered_changes": 1, "delete_change": 3 },
  "changed_functions": [
    { "qname": "auth::UserService.authenticate", "kind": "method", "file": "auth.py", "start_line": 9, "end_line": 14 }
  ],
  "uncovered_changes": [
    { "file": "app/config.py", "hunks": [ { "start": 90, "count": 2 } ] }
  ],
  "delete_change": [
    { "qname": "auth", "kind": "module", "file": "auth.py", "file_deleted": true,
      "start_line": 1, "end_line": 7, "signature": null, "is_test": 0,
      "upstream": [ { "source": "app::main", "kind": "import", "file": "app/main.py" } ] },
    { "qname": "auth::login", "kind": "function", "file": "auth.py", "file_deleted": true,
      "start_line": 6, "end_line": 7, "signature": "login()", "is_test": 0,
      "upstream": [ { "source": "auth::UserService.authenticate", "kind": "call", "file": "auth.py" } ] },
    { "qname": "utils::gone", "kind": "function", "file": "utils.py", "file_deleted": false,
      "start_line": 3, "end_line": 4, "signature": "gone()", "is_test": 0,
      "upstream": [ { "source": "main::run", "kind": "call", "file": "main.py" } ] }
  ]
}
```

- **被删文件** → 该文件全部节点（module + function + method + class）各一条，`file_deleted=true`；其中 module 记录承载 import 它的模块（import 边是模块级的，塞不进某个函数）——正是「文件被删追踪除 import 外的边」的落点。
- **存活文件内被删函数** → 一条记录，`file_deleted=false`。
- **`upstream` 只取 `call` + `inherits` + `import`，不含 `contains`**（容器关系：删方法不影响类存在）。按 `edges.target = 被删 qname` 匹配，resolution 任意（source 恒为真实节点；target 正是被删 qname 即命中）。
- `record.file` 用 repo-relative（复用 `_relative_to_repo`）；`upstream[].file` 是 source 节点的绝对路径（边自带），同样走 `_relative_to_repo`。
- `symbols=` 路径 → `delete_change: []`、`summary.delete_change: 0`（两条路径 schema 一致）。

## 改动点

### 1. update.py——写 tombstone（核心）

`_apply_nodes_edges_delta` 在删除循环**之前**（事务内）调用新增的 `_collect_tombstones(conn, repo, parsed, changed_set, deleted_set, config) -> list[tuple]` 和 `_insert_tombstones(conn, rows)`：

1. 遍历 `touch`（`changed_set ∪ deleted_set` 的 abs 路径）的**旧节点**：
   - 文件在 `deleted_set`（整文件删除，无新 parse 内容）→ 该文件全部旧节点 tombstone，`file_deleted=1`；
   - 文件在 `changed_set`（存活文件重解析）→ 旧节点 qname − 新 parse 节点 qname 的**差集** = 消失的函数/类/方法 → tombstone，`file_deleted=0`。
2. **上游**：对每个将被 tombstone 的 qname，在删除 loop 前查 `SELECT source, kind, file_path FROM edges WHERE target=? AND kind IN ('call','inherits','import')`——此刻边还 resolved。**排除** source ∈ 本次同批被删 qname 集（同文件一起删的内部调用者不算外部依赖；存活文件里的兄弟函数若仍存活则保留）。
3. `deleted_at_head = current_head(config)`（信息性，v1 不消费）。

`_collect_tombstones` 只读 DB 算行，`_insert_tombstones` 只写；二者与现有节点写同挂一个事务，任何一步失败整体回滚。

### 2. changes.py——读 tombstone → delete_change

- **`_diff_coverage` 简化**：删去「删除文件进 uncovered」分支；新增参 `covered_files: set[str]`（有 delete_change 记录的文件）：
  - 删除文件：`rel ∈ covered_files` → 跳过（已由 delete_change 覆盖）；否则保持 `{file, hunks: [], deleted: true}` 留在 uncovered（诚实呈现无 tombstone 的删除，不编造）。
  - 存活文件、无捕获 hunk（纯删除 / 二进制 / 重命名）：`rel ∈ covered_files` → 跳过；否则保持 `{file, hunks: []}`。
  - **注意**：`_git_diff` 丢弃 `+b,0` 纯删除 hunk → 存活文件里被删函数从不产生可捕获 hunk，**无需逐 hunk 与 tombstone 范围匹配**；文件级抑制即够。部分修改的函数仍命中当前节点 → 走 `changed_functions`（不是删除）。
- **新增 `_delete_change(config, conn, deleted_files, numstat) -> (records, covered_files)`**：
  - 候选文件 = `deleted_files ∪ {rel | numstat[rel][1] > 0}`（删文件 + 存活但有删除的文件）。
  - 对每个候选文件，`SELECT * FROM tombstones WHERE file_path=?`（同一 `os.path.join(config.repo_path, rel)` 构造，与 `nodes.file_path` 格式一致，见限制 7）；过滤 `qname NOT IN (SELECT qualified_name FROM nodes)`（防「已删又加回」）；按 `(file_path, qname)` 去重取最新（max id）。
  - 命中 → 一条记录（`upstream_json` 反序列化，`record.file` 转 repo-relative）；该文件进 `covered_files`。
- **`build_change_summary`** diff 路径：先 `_delete_change` 拿 `(records, covered_files)`，再把 `covered_files` 传进 `_diff_coverage`，最后组装 `delete_change` + `summary.delete_change = len(...)`。`_symbols_summary` 补 `delete_change: []`。
- **`mcp_server.get_change_summary`** docstring 补 `delete_change`。

## 已知限制（本特性明确不覆盖，均诚实呈现）

1. **重建后 / 未被观察的删除 → 上游未知**。tombstone 只在增量 update 观察到删除时写入；全量重建 `_clear_tables` 清空边，但**不清** tombstone——所以「有 tombstone、重建后」仍能给出快照 + 上游。「从没被观察过」的删除（部署首跑、watcher 停机期间）没有任何 tombstone，只能落回 uncovered 的文件级 `deleted:true`，`upstream` 无从谈起。
2. **fallback 不解析旧文件内容**。`git show base:path` + parse-source（上一 spec 非目标的方案 B）本特性不实现 → 无 tombstone 的被删文件只能报文件级，**列不出被删函数 qname**。函数级列出依赖 tombstone。
3. **存活文件内删函数的上游信号依赖图已有的边**。parser 不产边的类型引用——Java `A field;` / `List<A>` / 泛型参数、TS `let x: A`、Python 纯副作用 import 与 Django model 注册——在「删函数」场景下没有上游信号（只有该文件自身的 import 边）。整文件删除场景由 module 记录 + import 边兜住。
4. **dynamic / unresolved 目标天然进不了上游**。`obj.method()` 的 dynamic 边目标是原始表达式，不匹配被删 qname；`from m import *` / 不存在模块不产生 import 边。依赖者只要有 resolved 边仍会出现在上游。
5. **同路径反复删除只留最新**。tombstones 按 `(file_path, qname)` 去重，历史删除不逐条可见。
6. **删除与文件同时存在**。一个文件既有删除（进 delete_change）又有模块级新增 hunk（进 uncovered），两者并存，互不吞噬。
7. **路径格式必须三处一致**。Windows 上 `os.path.join` 产生混合分隔符，`nodes.file_path`（write）、tombstones（写自 node 行）、read 端查 tombstones 必须用同一 `os.path.join(config.repo_path, rel)` 构造，否则查不到 tombstone → 退化为限制 1 的 fallback。

## 测试与验收

- **test_update.py / test_incremental.py**：整文件删除写 module+function tombstone（`file_deleted=1`）；存活文件删函数写 `file_deleted=0`；上游捕获（他文件 caller / importer / subclass），同批被删 source 被排除，存活兄弟调用者保留；tombstone 挺过 `rebuild`（`_clear_tables` 不清）。
- **test_changes.py**：`delete_change` 精确 shape（diff 路径）；被删文件从 uncovered 消失；**无 tombstone** 被删文件留在 uncovered（`deleted:true`）；存活文件纯删函数 → delete_change 记录 + 空 hunk uncovered 条目被抑制；无 tombstone 的纯删常量文件 → 仍在 uncovered；`symbols=` 路径 `delete_change=[]`；`summary.delete_change` 计数。
- **test_db.py**：tombstones 表创建 + `init_schema` 幂等。
- **验收**：`uv run pytest` 全绿；对一个真实「删文件 + 存活文件删函数」的 diff 跑 `uv run code-review-ai summary`，`delete_change` 各记录的上游与 `git diff` / 实际调用关系核对一致，且该文件不再重复出现在 `uncovered_changes`。

## 非目标

- `get_impact` / `get_test_impact` 对被删符号的查询——tombstone 只服务 summary；flow / membership 仍是活图。
- 方案 B（`git show base:path` + parse-source 列出旧函数）——列为限制 2，留作后续特性。
- 给 `changed_functions` 记录加 `in_graph` 标志（沿用 `uncovered-changes` spec 的非目标）。
- `deleted_at_head` 按 git 窗口过滤——v1 只用「diff 文件窗口 + 活节点」过滤，`deleted_at_head` 仅信息性。
