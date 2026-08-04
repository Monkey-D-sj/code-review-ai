# 设计：MCP 新增 `get_change_summary` 工具

日期：2026-08-04

## 目标

在 `code-review-ai` 的 MCP server 中新增一个**只读**工具 `get_change_summary`：
调用者调用它，返回一个从 git diff 计算的 JSON，内含 `summary`（diff 统计）与
`changed_functions`（变更函数明细）。它让审查方一次调用即可拿到「改了什么 + 影响
面」的概览，而不必分别调 `get_impact` / `search_symbol`。

约束：MCP server 本身没有 LLM，`summary` 是结构化统计信息，不是自然语言总结。

## JSON 结构

```json
{
  "summary": {
    "files_changed": 3,
    "lines_added": 42,
    "lines_removed": 7,
    "changed_functions": 5
  },
  "changed_functions": [
    {"qname": "auth::login", "kind": "function", "file": "auth.py", "start_line": 6, "end_line": 7}
  ]
}
```

- `summary.files_changed`：diff 涉及的文件数。
- `summary.lines_added` / `summary.lines_removed`：新增 / 删除行数（二进制文件计 0）。
- `summary.changed_functions`：变更函数/方法/类个数（= `changed_functions` 长度）。
- `changed_functions[]`：与变更行区间重叠的 function/method/**class** 节点，
  含 `qname`（`module::scope.scope.name` 格式）、`kind`
  （function/method/class）、`file`（仓库相对路径）、`start_line` / `end_line`。

## 组件设计

### `changes.py` — 新增 3 个函数（conn 以参数注入，与 `impact.py` 同款模式）

1. `_git_numstat(base: str, files: list[str] | None) -> dict[str, tuple[int, int]]`
   `git diff --numstat <base>` → `{file: (added, removed)}`。numstat 对二进制文件
   显示 `-`，映射为 `(0, 0)` 但**仍保留为键**，这样 `files_changed` 不会漏掉
   二进制变更。这是标准的取增删行数方式，复用 `changes.py` 现有的 git diff
   编码处理（UTF-8 + `errors="replace"`）。

2. `_changed_functions(config: Config, diff_ranges,
   kinds=("function", "method", "class")) -> list[dict]`
   从 `detect_changed_symbols` 现有循环抽取：遍历变更文件 → 找与区间重叠的
   function/method/class 节点，返回富记录 `{qname, kind, file, start_line,
   end_line}`。`detect_changed_symbols` 复用它但显式传
   `kinds=("function", "method")`，**保持其现有行为不变**（避免静默改变
   `get_impact` 的符号集合）。

3. `build_change_summary(config: Config, conn,
   symbols: list[str] | None = None, files: list[str] | None = None) -> dict`
   唯一入口，供 MCP tool 与 CLI 子命令共用（避免两个前端重复逻辑）：
   - `symbols` 省略 → 只做编排：`_git_diff(config.diff_base, files)` 取区间 →
     `_git_numstat(config.diff_base, files)` 取行数（`files_changed =
     len(numstat)`，即权威的变更文件集，含二进制文件）→ `_changed_functions`
     取明细 → 组装 `{"summary", "changed_functions"}`。`files` 为空则 diff
     整棵树（现有行为）。
   - 显式传 `symbols` → 逐个 qname 查 `conn` 的 `nodes` 表拿
     `kind`/`file`/`start_line`/`end_line`（查不到的回退为仅 `qname`）；
     `summary` 行数置 0，`files_changed` 为这批符号的去重文件数。

   `conn` 以参数注入（与 `impact.get_impact(conn, ...)` 同款模式），模块不持有
   自己的连接。

### `mcp_server.py` — 新增 tool `get_change_summary`

```python
@mcp.tool()
def get_change_summary(symbols: list[str] | None = None,
                       files: list[str] | None = None) -> str:
    """变更摘要：从 git diff（diff_base）计算 summary（diff 统计）+ changed_functions（变更函数明细）。"""
```

- 两个分支都委托给 `build_change_summary(config, conn, symbols=symbols,
  files=files)`（符号解析已内置，见上文 changes.py）。
- 与 `get_impact` 入参一致：`symbols` / `files` 均可省略。

### `cli.py` — 新增 `summary` 子命令

`uv run code-review-ai summary [--symbols ...] [--files ...]`，镜像 MCP tool：
调用 `build_change_summary(cfg, conn, symbols=..., files=...)`，print JSON。
`RuntimeError`（git diff 失败）捕获后输出到 stderr 并返回 1（复用 `query`
子命令的处理方式）。

## 错误处理

- git diff 失败（如 `diff_base` 不存在）→ 抛 `RuntimeError`，与
  `detect_changed_symbols` 现有行为一致（不吞错，让调用方看到配置错误）。
- 变更文件中某个文件已被删除 → 解析抛 `OSError`，跳过（现有行为）。

## 测试

### `tests/test_changes.py` 新增

- monkeypatch `_git_diff` + `_git_numstat`，调用
  `build_change_summary(cfg, conn)`：断言 `summary` 各字段
  （files/lines/changed_functions）与 `changed_functions` 明细含
  `start_line`/`end_line`。
- 变更区间命中 class 定义行 → `changed_functions` 含该 class 节点（fixture
  `UserService`，1-3 行）。
- 断言 `detect_changed_symbols` 仍只返回 function/method（`kinds` 参数生效，
  行为不回退）。
- 新增 `_git_numstat` 解析测试：普通行 + 二进制文件（`-`）跳过。
- `symbols` 路径：传显式 symbols，断言从 `conn` 解析出 `kind`/`file`/行号。

### `tests/test_mcp_server.py` 新增

- 复用 `_server(tmp_path)`，调 `get_change_summary` tool 的
  `fn(symbols=[Q("auth","login")])`：断言返回 JSON 含 `summary` 与
  `changed_functions`，且 `start_line`/`end_line` 与 fixture 一致。

### `tests/test_cli.py` 新增

- `summary` 子命令输出合法 JSON 且含 `summary` / `changed_functions` 键
  （monkeypatch `build_change_summary` 或走真实 fixture）。

## 明确不做（YAGNI）

- 不做自然语言总结（无 LLM）。
- 不改 `flows` / `impact` 模型。
- `changed_functions` 字段名保持不变（虽含 class，但该字段名是工具契约的一部分）。
