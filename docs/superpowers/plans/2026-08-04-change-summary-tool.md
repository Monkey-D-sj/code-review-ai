# get_change_summary 工具实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 MCP server 与 CLI 中新增 `get_change_summary` / `summary`，返回 `{"summary": diff统计, "changed_functions": [{qname, kind, file, start_line, end_line}]}`。

**Architecture:** 逻辑集中在 `changes.py`（`_git_numstat`、`_changed_functions`、`build_change_summary`，conn 以参数注入，同 `impact.py` 模式）；`mcp_server.py` 与 `cli.py` 各加一个薄前端。`detect_changed_symbols` 复用 `_changed_functions` 但显式限定 `("function","method")`，行为不变。

**Tech Stack:** Python 3.14、`uv`、pytest、tree-sitter parser（`code_review_ai.parser`）、SQLite（`code_review_ai.db`）、git CLI。

## Global Constraints

- qname 一律走 `qname.join` / `qname.short`，禁止手工拼接 `::`/`.`。
- 禁止单字母变量名（数学索引除外）；循环变量用有意义的词。
- 函数体 ≤ 50 行；主控函数只做编排（参数准备 → 调子函数 → 返回）。
- 业务/库模块不持有 DB 连接；DB 以 `conn` 参数注入。
- git diff 子进程一律 `encoding="utf-8", errors="replace"`（中文 Windows 的 GBK 兼容）。
- 测试用 `from conftest import Q, FIXTURES as FIX`；`tests/` 在 `sys.path` 上。

---

### Task 1: `_git_numstat` — 每文件新增/删除行数

**Files:**
- Modify: `code_review_ai/changes.py`
- Test: `tests/test_changes.py`

**Interfaces:**
- Consumes: 无（`git diff --numstat` 是独立子进程）。
- Produces: `changes._git_numstat(base: str, files: list[str] | None) -> dict[str, tuple[int, int]]` — `{file: (added, removed)}`；二进制文件映射为 `(0, 0)` 但保留键；git 失败抛 `RuntimeError`。

- [ ] **Step 1: 写失败测试**（`tests/test_changes.py` 追加）

```python
def test_git_numstat_parses_text_and_binary(monkeypatch):
    import code_review_ai.changes as ch

    class _FakeResult:
        returncode = 0
        stdout = "10\t2\tauth.py\n-\t-\tlogo.png\n"
        stderr = ""
    monkeypatch.setattr(ch.subprocess, "run", lambda *args, **kwargs: _FakeResult())
    assert ch._git_numstat("origin/main") == {"auth.py": (10, 2), "logo.png": (0, 0)}
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_changes.py::test_git_numstat_parses_text_and_binary -v`
Expected: FAIL（`AttributeError: module 'code_review_ai.changes' has no attribute '_git_numstat'`）

- [ ] **Step 3: 最小实现**（`code_review_ai/changes.py` 追加，`subprocess` 已导入）

```python
def _git_numstat(base: str, files: list[str] | None) -> dict[str, tuple[int, int]]:
    """{file: (added, removed)} per changed file. Binary files map to (0, 0)
    but keep their key so files_changed still counts them."""
    args = ["git", "diff", "--numstat", base]
    if files:
        args += ["--"] + files
    out = subprocess.run(args, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(
            f"git diff failed (exit {out.returncode}): {out.stderr.strip()}"
        )
    stats: dict[str, tuple[int, int]] = {}
    for line in out.stdout.splitlines():
        added_s, removed_s, path = line.split("\t", 2)
        if added_s == "-" or removed_s == "-":
            stats[path] = (0, 0)
            continue
        stats[path] = (int(added_s), int(removed_s))
    return stats
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_changes.py::test_git_numstat_parses_text_and_binary -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_changes.py code_review_ai/changes.py
git commit -m "feat(changes): add _git_numstat for per-file added/removed lines"
```

---

### Task 2: `_changed_functions` — 变更函数/类富记录，重构 `detect_changed_symbols`

**Files:**
- Modify: `code_review_ai/changes.py:42-66`
- Test: `tests/test_changes.py`

**Interfaces:**
- Consumes: Task 1 不依赖；复用现有 `_overlaps`、`parse_file`、`_git_diff`。
- Produces: `changes._changed_functions(config, diff_ranges, kinds=("function","method","class")) -> list[dict]` — `[{qname, kind, file, start_line, end_line}]`，`file` 为仓库相对路径。

- [ ] **Step 1: 写失败测试**（断言 class 节点被收集）

```python
def test_changed_functions_includes_class():
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    records = ch._changed_functions(cfg, {"auth.py": [(1, 1)]})
    user_service = [r for r in records if r["qname"] == Q("auth", "UserService")]
    assert len(user_service) == 1
    assert user_service[0]["kind"] == "class"
    assert user_service[0]["file"] == "auth.py"
    assert user_service[0]["start_line"] == 1
    assert user_service[0]["end_line"] == 3
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_changes.py::test_changed_functions_includes_class -v`
Expected: FAIL（`AttributeError: ... no attribute '_changed_functions'`）

- [ ] **Step 3: 实现 `_changed_functions` 并重构 `detect_changed_symbols`**

在 `code_review_ai/changes.py` 把 `detect_changed_symbols` 现有循环抽为 `_changed_functions`：

```python
def _changed_functions(config: Config, diff_ranges: dict[str, list[tuple[int, int]]],
                       kinds: tuple[str, ...] = ("function", "method", "class")) -> list[dict]:
    """Rich records for nodes overlapping changed line ranges.

    Returns [{qname, kind, file, start_line, end_line}] with repo-relative file.
    """
    repo = config.repo_path
    out: list[dict] = []
    for rel, ranges in diff_ranges.items():
        path = f"{repo}/{rel}"
        try:
            pf = parse_file(path, repo)
        except OSError:
            continue
        for node in pf.nodes:
            if node.kind not in kinds:
                continue
            if _overlaps(node.start_line, node.end_line, ranges):
                out.append({"qname": node.qualified_name, "kind": node.kind,
                            "file": rel, "start_line": node.start_line,
                            "end_line": node.end_line})
    return out
```

并把 `detect_changed_symbols` 的函数体替换为复用（保持原有行为只含 function/method）：

```python
def detect_changed_symbols(config: Config,
                           symbols: list[str] | None = None,
                           files: list[str] | None = None) -> list[str]:
    """Changed symbol qnames: explicit `symbols`, or the git diff of `files`
    (or the whole tree when neither is given) against config.diff_base.

    Raises RuntimeError if the git diff fails (e.g. diff_base doesn't exist) so
    callers surface the misconfiguration instead of returning an empty list."""
    if symbols is not None:
        return list(symbols)
    diff = _git_diff(config.diff_base, files)
    return [record["qname"] for record in _changed_functions(
        config, diff, kinds=("function", "method"))]
```

- [ ] **Step 4: 运行确认通过，并加回归护栏**

Run: `uv run pytest tests/test_changes.py::test_changed_functions_includes_class -v`
Expected: PASS

追加回归测试（防重构回退，当前代码本应已通过）：

```python
def test_detect_changed_symbols_still_excludes_classes(monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff", lambda base, files: {"auth.py": [(1, 3)]})
    out = detect_changed_symbols(cfg, files=["auth.py"])
    assert Q("auth", "UserService") not in out               # class excluded
    assert Q("auth", "authenticate", Q("auth", "UserService")) in out  # method kept
```

Run: `uv run pytest tests/test_changes.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/changes.py tests/test_changes.py
git commit -m "feat(changes): extract _changed_functions (incl. classes); reuse in detect_changed_symbols"
```

---

### Task 3: `build_change_summary` — 唯一入口（diff 路径 + symbols 路径）

**Files:**
- Modify: `code_review_ai/changes.py`
- Test: `tests/test_changes.py`

**Interfaces:**
- Consumes: Task 1 的 `_git_numstat`、Task 2 的 `_changed_functions`、现有 `_git_diff`。
- Produces:
  - `changes.build_change_summary(config, conn, symbols=None, files=None) -> dict` — `{"summary": {...}, "changed_functions": [...]}`；git diff 失败抛 `RuntimeError`。
  - `changes._relative_to_repo(config, file_path: str) -> str`。
  - `changes._symbols_summary(config, conn, symbols) -> dict`。

- [ ] **Step 1: 写失败测试**（diff 路径 + symbols 路径 + 二进制文件参与计数）

```python
def test_build_change_summary_diff_path(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff", lambda base, files: {"auth.py": [(6, 7)]})
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files: {"auth.py": (10, 2), "logo.png": (0, 0)})
    out = build_change_summary(cfg, _conn(tmp_path))
    assert out["summary"] == {"files_changed": 2, "lines_added": 10,
                              "lines_removed": 2, "changed_functions": 1}
    assert out["changed_functions"] == [
        {"qname": Q("auth", "login"), "kind": "function",
         "file": "auth.py", "start_line": 6, "end_line": 7}]


def test_build_change_summary_symbols_path(tmp_path):
    cfg = load_config(FIX)
    cfg.repo_path = FIX
    from code_review_ai.db import connect, init_schema
    from code_review_ai.indexer import rebuild
    conn = connect(str(tmp_path / "m.db"))
    init_schema(conn)
    rebuild(cfg, conn)
    out = build_change_summary(cfg, conn, symbols=[Q("auth", "login")])
    assert out["summary"]["changed_functions"] == 1
    record = out["changed_functions"][0]
    assert record["qname"] == Q("auth", "login")
    assert record["file"] == "auth.py"
    assert record["start_line"] == 6
    assert record["end_line"] == 7
```

`tests/test_changes.py` 顶部需加 `from code_review_ai.changes import build_change_summary, detect_changed_symbols`，并加一个 conn 辅助：

```python
def _conn(tmp_path):
    from code_review_ai.db import connect, init_schema
    conn = connect(str(tmp_path / "m.db"))
    init_schema(conn)
    return conn
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_changes.py::test_build_change_summary_diff_path tests/test_changes.py::test_build_change_summary_symbols_path -v`
Expected: FAIL（`ImportError: cannot import name 'build_change_summary'`）

- [ ] **Step 3: 最小实现**（`code_review_ai/changes.py` 追加；文件顶部加 `from pathlib import Path`）

```python
def _relative_to_repo(config: Config, file_path: str) -> str:
    try:
        return str(Path(file_path).resolve().relative_to(Path(config.repo_path).resolve()))
    except ValueError:
        return file_path


def _symbols_summary(config: Config, conn, symbols: list[str]) -> dict:
    files: set[str] = set()
    records: list[dict] = []
    for symbol in symbols:
        row = conn.execute(
            "SELECT kind, file_path, start_line, end_line FROM nodes WHERE qualified_name=?",
            (symbol,),
        ).fetchone()
        if row is None:
            records.append({"qname": symbol, "kind": None, "file": None,
                            "start_line": 0, "end_line": 0})
            continue
        rel = _relative_to_repo(config, row["file_path"])
        files.add(rel)
        records.append({"qname": symbol, "kind": row["kind"], "file": rel,
                        "start_line": row["start_line"], "end_line": row["end_line"]})
    return {"summary": {"files_changed": len(files), "lines_added": 0,
                        "lines_removed": 0, "changed_functions": len(symbols)},
            "changed_functions": records}


def build_change_summary(config: Config, conn, symbols: list[str] | None = None,
                         files: list[str] | None = None) -> dict:
    """Change summary + changed functions. With `symbols`, resolve each qname
    from the graph; otherwise compute from the git diff of `files` (or the whole
    tree) against config.diff_base. Returns {"summary", "changed_functions"}.
    Raises RuntimeError if the git diff fails (bad diff_base)."""
    if symbols is not None:
        return _symbols_summary(config, conn, symbols)
    diff = _git_diff(config.diff_base, files)
    numstat = _git_numstat(config.diff_base, files)
    functions = _changed_functions(config, diff)
    return {"summary": {"files_changed": len(numstat),
                        "lines_added": sum(added for added, _ in numstat.values()),
                        "lines_removed": sum(removed for _, removed in numstat.values()),
                        "changed_functions": len(functions)},
            "changed_functions": functions}
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_changes.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/changes.py tests/test_changes.py
git commit -m "feat(changes): add build_change_summary (diff + explicit-symbol paths)"
```

---

### Task 4: MCP tool `get_change_summary`

**Files:**
- Modify: `code_review_ai/mcp_server.py:9-15`（import）与 `create_server` 内新增 tool
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: Task 3 的 `build_change_summary`、`create_server` 闭包内的 `config`/`conn`。
- Produces: MCP tool `get_change_summary(symbols=None, files=None) -> str`（JSON 字符串）。

- [ ] **Step 1: 写失败测试**

```python
def test_get_change_summary_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    tools = server._tool_manager._tools
    assert "get_change_summary" in tools
    data = json.loads(tools["get_change_summary"].fn(symbols=[Q("auth", "login")]))
    assert set(data) == {"summary", "changed_functions"}
    assert data["summary"]["changed_functions"] == 1
    record = data["changed_functions"][0]
    assert record["qname"] == Q("auth", "login")
    assert record["file"] == "auth.py"
    assert record["start_line"] == 6
    assert record["end_line"] == 7
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_mcp_server.py::test_get_change_summary_tool -v`
Expected: FAIL（`KeyError: 'get_change_summary'`）

- [ ] **Step 3: 最小实现**

`code_review_ai/mcp_server.py` 的 import 行改为：

```python
from code_review_ai.changes import build_change_summary, detect_changed_symbols
```

在 `get_impact` tool 定义之后新增：

```python
    @mcp.tool()
    def get_change_summary(symbols: list[str] | None = None,
                           files: list[str] | None = None) -> str:
        """Change summary: from the git diff (diff_base) compute `summary`
        (diff stats) + `changed_functions` (changed function/method/class
        detail). Pass explicit `symbols` to resolve those qnames from the
        graph instead of the diff. Returns a JSON object."""
        return json.dumps(build_change_summary(config, conn,
                                               symbols=symbols, files=files))
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(mcp): add get_change_summary tool"
```

---

### Task 5: CLI `summary` 子命令

**Files:**
- Modify: `code_review_ai/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 3 的 `build_change_summary`、`cli` 的 `cfg`/`conn`。
- Produces: `uv run code-review-ai summary [--symbols ...] [--files ...]` 打印 JSON；git 失败时打印 stderr 并返回 1。

- [ ] **Step 1: 写失败测试**（`tests/test_cli.py` 顶部加 `import json`）

```python
def test_cli_summary(tmp_path, capsys):
    code = main(["rebuild", "--repo", FIX, "--db", str(tmp_path / "c.db")])
    assert code == 0
    _ = capsys.readouterr()  # discard rebuild output
    code = main(["summary", "--symbols", Q("auth", "login"),
                 "--repo", FIX, "--db", str(tmp_path / "c.db")])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"summary", "changed_functions"}
    assert data["summary"]["changed_functions"] == 1
    assert data["changed_functions"][0]["qname"] == Q("auth", "login")
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_cli.py::test_cli_summary -v`
Expected: FAIL（`SystemExit` / argparse `invalid choice: 'summary'`）

- [ ] **Step 3: 最小实现**

`code_review_ai/cli.py`：
- import 行改为 `from code_review_ai.changes import build_change_summary, detect_changed_symbols`
- 在 `query` 子解析器之后加：

```python
    s = sub.add_parser("summary")
    _add_common(s)
    s.add_argument("--symbols", nargs="*")
    s.add_argument("--files", nargs="*")
```

- 在 `main` 的 `query` 分支之后加：

```python
    elif args.cmd == "summary":
        try:
            payload = build_change_summary(cfg, conn,
                                           symbols=args.symbols, files=args.files)
        except RuntimeError as exc:
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
git commit -m "feat(cli): add summary subcommand mirroring get_change_summary"
```

---

### Task 6: 文档同步（CLAUDE.md / AGENTS.md）

**Files:**
- Modify: `CLAUDE.md`、`AGENTS.md`

**Interfaces:**
- Consumes: Task 4/5 产出的工具名与命令名。
- Produces: 文档中列出新工具与新命令。

- [ ] **Step 1: 更新 Commands 段**

`CLAUDE.md` 与 `AGENTS.md` 的 Commands 代码块中 `query` 两行之后各加：

```bash
uv run code-review-ai summary --symbols auth::login   # change summary JSON (summary + changed_functions)
uv run code-review-ai summary                        # same, computed from the git diff of the whole tree
```

- [ ] **Step 2: 更新 Frontends 段工具列表**

两文件的 `Frontends` 段中 `tools rebuild_index, get_impact, ...` 列表加入 `get_change_summary`（放在 `get_impact` 后）。

- [ ] **Step 3: 更新模块职责行**

两文件的模块职责行 `changes.py git diff / files / symbols → changed qnames` 改为 `changes.py git diff / files / symbols → changed qnames + change summary`。

- [ ] **Step 4: 提交**

```bash
git add CLAUDE.md AGENTS.md
git commit -m "docs: document get_change_summary tool and summary CLI subcommand"
```

---

### Task 7: 全量验证

- [ ] **Step 1: 全量测试**

Run: `uv run pytest`
Expected: 全部 PASS（含既有用例，无回归）

- [ ] **Step 2: 手动冒烟（CLI，可选，需真实 git 仓库且有 diff）**

Run: `uv run code-review-ai summary`
Expected: 打印含 `summary` 与 `changed_functions` 的 JSON
