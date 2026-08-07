# change summary 未覆盖改动（uncovered_changes）Design

**日期**:2026-08-07
**范围**:`get_change_summary` 的 diff 路径不再静默丢弃解析不到的改动——每个改动 hunk 要么被函数/类覆盖（经 `changed_functions` 可见），要么列在新增的 `uncovered_changes` 里，让 AI 审查者一眼看到覆盖缺口。
**前置**:无。`changed_functions` 现有行为不变。

## 背景

现在 `get_change_summary` diff 路径:numstat 统计了 **git diff 里所有改动文件**（`summary.files_changed`），但 `_changed_functions` 只把**能解析且改动行命中 function/method/class 节点**的记录列出来。解析不了的（非 `_EXT_MAP` 扩展名、文件已删、改动落在模块级/装饰器/匿名函数）被 `continue` 静默跳过。结果 AI 无法区分「这些文件真没改函数」和「改了但解析不到」，`files_changed: 5, changed_functions: 2` 里另外 3 个文件改了什么、为什么没被分析，完全不可见。

另一个更隐蔽的缺口:**删除文件**在 `_git_diff` 里是 `+++ /dev/null`（不匹配 `^\+\+\+ b/(.+)`），且 hunk 为 `+0,0` 不满足 `count>0`，根本进不了 `diff_ranges`；`_changed_functions` 完全看不到它，只剩 numstat 记得。

## 数据形状（向后兼容）

`changed_functions` 记录形状**完全不变**。新增顶层键 `uncovered_changes`，一条一文件，逐 hunk 记录未覆盖的改动：

```json
{
  "summary": {
    "files_changed": 5, "lines_added": 10, "lines_removed": 2,
    "changed_functions": 2, "uncovered_changes": 3
  },
  "changed_functions": [
    { "qname": "auth::login", "kind": "function", "file": "auth.py", "start_line": 6, "end_line": 7 }
  ],
  "uncovered_changes": [
    { "file": "app/config.py", "hunks": [ { "start": 90, "count": 2 } ] },
    { "file": "foo.py",        "hunks": [], "deleted": true },
    { "file": "README.md",     "hunks": [ { "start": 1, "count": 10 } ] },
    { "file": "logo.png",      "hunks": [] }
  ]
}
```

- `hunks` 是 git `+b,m` 的新侧逐 hunk `(start, count)`，位置与大小一体（git diff 风格），不拆分 numstat 聚合计数与位置。
- `deleted: true` 仅删除文件；`hunks: []` 表示无行级 hunk（删除/二进制/纯重命名）。
- `summary.uncovered_changes` 为该列表长度。
- `symbols=` 路径返回 `uncovered_changes: []`，两条路径 schema 一致。

## Parser/`changes.py` 改动

1. **`_git_diff` 改为逐 hunk `(start, count)`**（保留 git `+b,m` 原始形状，不再转成合并的 `(start, end)`），并识别删除：解析到 `+++ /dev/null` 时将该文件标记 deleted。
2. **新增 `_diff_coverage(config, diff_ranges, numstat, deleted_files) -> (records, uncovered_changes)`**，遍历 numstat 全部文件：
   - 删除文件 → `{file, hunks: [], deleted: true}`；
   - 有 hunks 但 `parse_file` 抛 `ValueError`（非支持扩展名）/ `OSError`（读取失败）→ `{file, hunks: 全部 hunks}`；
   - 解析成功 → 逐 hunk 判定：hunk 与任一 function/method/class 节点范围重叠 = covered；否则进 uncovered。**全 covered 的文件不进列表**；
   - 在 numstat 但无 hunks（二进制/重命名）→ `{file, hunks: []}`。
3. **`_changed_functions` 退化为薄封装**只返回 records（现有测试与 `detect_changed_symbols` 行为不变）；`_overlaps` 适配 `(start, count)` 形状。
4. **`build_change_summary`** diff 路径用 `_diff_coverage`；`_symbols_summary` 返回 `uncovered_changes: []`。

**不变量**：每个改动 hunk 要么与某个被捕获的 function/method/class 重叠（经 `changed_functions` 可见），要么在 `uncovered_changes` 里。

## 已知近似

- **装饰器改动**（如新增 `@app.route`）：parser 的 def 节点范围不含装饰器行 → 落进 uncovered。诚实呈现，AI 看位置 + git diff 即知。
- **匿名函数**（Python lambda 等）：parser 无名字跳过 → 落进 uncovered。

## 测试与验收

- 更新精确 shape 断言：`tests/test_changes.py`（diff 路径 summary 多 `uncovered_changes`、顶层多键；symbols 路径多空列表）、`tests/test_cli.py`、`tests/test_mcp_server.py`（`set(data)`）。
- 新增 `tests/test_changes.py`：非支持扩展名、模块级改动（`no_changed_defs` 语义）、部分覆盖（同文件 A covered / B uncovered）、二进制（numstat 有、无 hunks）、删除文件（`deleted: true`）、symbols 路径返回空列表、不变量成立。
- **验收**：`uv run pytest` 全绿；对一个含 `.md`/删除文件/模块级改动的真实 diff 跑 `uv run code-review-ai summary`，输出 `files_changed == changed_functions 去重文件数 + uncovered_changes 数`。

## 非目标

- **方案 B（后续）**：解析 `git show base:path` 旧内容列出被删函数 qname，让 `get_impact` 追查已删除函数的外部调用方（需 parse_file 支持字节解析；索引重建后旧节点消失，依赖 DB 查旧节点不可靠）。
- 给 `changed_functions` 记录加 `in_graph` 标志（文件被 `exclude` 排除、函数不在索引）——改变记录形状，另开。
- 顶层 `summary` 的 `files_changed/lines_added/lines_removed` 语义不变（numstat 契约）。
- `detect_changed_symbols` / `get_impact` / `get_test_impact` 不动。
