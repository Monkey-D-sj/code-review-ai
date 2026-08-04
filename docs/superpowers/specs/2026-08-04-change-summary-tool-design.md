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
    {"qname": "auth::login", "kind": "function", "file": "auth.py", "start_line": 6, "end_line": 9}
  ]
}
```

- `summary.files_changed`：diff 涉及的文件数。
- `summary.lines_added` / `summary.lines_removed`：新增 / 删除行数（二进制文件计 0）。
- `summary.changed_functions`：变更函数/方法个数（= `changed_functions` 长度）。
- `changed_functions[]`：与变更行区间重叠的 function/method 节点，含
  `qname`（`module::scope.scope.name` 格式）、`kind`（function/method）、`file`
  （仓库相对路径）、`start_line` / `end_line`。

## 组件设计

### `changes.py` — 新增 3 个函数（保持模块 DB-free）

1. `_git_numstat(base: str, files: list[str] | None) -> dict[str, tuple[int, int]]`
   `git diff --numstat <base>` → `{file: (added, removed)}`。numstat 对二进制文件
   显示 `-`，映射为 `(0, 0)` 但**仍保留为键**，这样 `files_changed` 不会漏掉
   二进制变更。这是标准的取增删行数方式，复用 `changes.py` 现有的 git diff
   编码处理（UTF-8 + `errors="replace"`）。

2. `_changed_functions(config: Config, diff_ranges: dict[str, list[tuple[int, int]]]) -> list[dict]`
   从 `detect_changed_symbols` 现有循环抽取：遍历变更文件 → 找与区间重叠的
   function/method 节点，返回富记录 `{qname, kind, file, start_line, end_line}`。
   `detect_changed_symbols` 改为复用它（映射为 qname 列表），消除重复。

3. `build_change_summary(config: Config, files: list[str] | None = None) -> dict`
   只做编排：
   1. `_git_diff(config.diff_base, files)` 取变更行区间；
   2. `_git_numstat(config.diff_base, files)` 取每文件增删行数（`files_changed =
      len(numstat)`，即权威的变更文件集，含二进制文件）；
   3. `_changed_functions(config, diff)` 取变更函数明细；
   4. 组装 `{"summary": {...}, "changed_functions": [...]}` 返回。

   `files` 为空则 diff 整棵树（现有行为）。

### `mcp_server.py` — 新增 tool `get_change_summary`

```python
@mcp.tool()
def get_change_summary(symbols: list[str] | None = None,
                       files: list[str] | None = None) -> str:
    """变更摘要：从 git diff（diff_base）计算 summary（diff 统计）+ changed_functions（变更函数明细）。"""
```

- `symbols` 省略 → 走 `build_change_summary(config, files=files)`（git diff 路径）。
- 显式传 `symbols` → 与 `get_impact` 保持一致：逐个 qname 查代码图
  （`nodes` 表）拿 `kind`/`file`/`start_line`/`end_line`；查不到的回退为仅
  `qname`（`kind`/`file`/行号置空/0）。`summary` 的行数统计置 0，
  `files_changed` 为该批符号的去重文件数，`changed_functions` 为符号数。
- 查图的逻辑放前端（直接查 `conn`，与 `search_symbol` / `get_symbol_detail`
  同层），`changes.py` 保持不依赖 DB。

## 错误处理

- git diff 失败（如 `diff_base` 不存在）→ 抛 `RuntimeError`，与
  `detect_changed_symbols` 现有行为一致（不吞错，让调用方看到配置错误）。
- 变更文件中某个文件已被删除 → 解析抛 `OSError`，跳过（现有行为）。

## 测试

### `tests/test_changes.py` 新增

- monkeypatch `_git_diff` + `_git_numstat`，调用 `build_change_summary(cfg)`：
  断言 `summary` 各字段（files/lines/changed_functions）与 `changed_functions`
  明细含 `start_line`/`end_line`。
- 沿用 fixtures（`tests/fixtures/repo`），如 `auth.py` 的 `authenticate()`
  （2-3 行）、`login()`（6-7 行）。
- 新增 `_git_numstat` 解析测试：普通行 + 二进制文件（`-`）跳过。

### `tests/test_mcp_server.py` 新增

- 复用 `_server(tmp_path)`，调 `get_change_summary` tool 的
  `fn(symbols=[Q("auth","login")])`：断言返回 JSON 含 `summary` 与
  `changed_functions`，且 `start_line`/`end_line` 与 fixture 一致。

## 明确不做（YAGNI）

- 不做自然语言总结（无 LLM）。
- 不新增 CLI 子命令（MCP 是主界面；CLI 如需可后续补）。
- 不改 `flows` / `impact` 模型。
