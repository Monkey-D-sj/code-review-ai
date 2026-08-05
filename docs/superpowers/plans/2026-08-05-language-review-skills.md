# 语言审核 skill 注入实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 4 个语言审核 skill（入口 + Python/TypeScript/JavaScript），由 `install --platform claude-code|codex` 部署到用户级 skills 目录，并为 Codex 追加 AGENTS.md 用法文档。

**Architecture:** skill 源文件放在包内 `code_review_ai/skills/<name>/SKILL.md`（hatchling package data，随 wheel 分发）；`installer.py` 增加平台感知的 `deploy_skills(platform)`（镜像复制、原位覆盖即幂等），把 `append_usage_docs` 泛化为 `append_usage_docs(platform)`（marker 守卫），`install(platform)` 按平台分流（claude-code 走现有 `claude mcp add`，codex 无子进程、MCP 手动注册）。

**Tech Stack:** Python 3.14、`uv`、pytest、`importlib.resources`、hatchling。

## Global Constraints

- 4 个 skill：`code-review-langs`（入口）+ `code-review-python` / `code-review-typescript` / `code-review-javascript`；目录名 == frontmatter `name`。
- 语言 skill 正文**必须**含且仅含 5 个小节，标题即规范：`## 安全`、`## 正确性`、`## 性能`、`## 架构`、`## 语言特有`（测试按此断言）。
- 纯静态规则，不联动调用图 MCP 工具；与现有 `~/.claude/skills/code-review/` 并存，不替换。
- `SUPPORTED_PLATFORMS = {"claude-code", "codex"}`；codex 平台**不执行任何** MCP 注册子进程。
- skill 文件归本工具所有，部署即覆盖；用法文档注入用 marker 守卫（`MCP_DOC_START`/`MCP_DOC_END`）。
- 规则输出统一 `error`/`warning`/`info`，与 `code-review` 评分公式（得分 = max(40, 100 − error×10 − warning×3)）兼容。
- 代码规范：函数体 ≤50 行、类 ≤300 行；主控函数只编排；禁单字母变量（数学索引除外）；循环变量语义化；禁止内置名当变量。
- 文件读写一律 `encoding="utf-8"`。
- 测试用 `uv run pytest`（testpaths = `["tests"]`）。

---

### Task 1: 4 个 skill 源文件 + 结构守护测试

**Files:**
- Create: `tests/test_skills.py`
- Create: `code_review_ai/skills/code-review-langs/SKILL.md`
- Create: `code_review_ai/skills/code-review-python/SKILL.md`
- Create: `code_review_ai/skills/code-review-typescript/SKILL.md`
- Create: `code_review_ai/skills/code-review-javascript/SKILL.md`

**Interfaces:**
- Consumes: 无。
- Produces: `code_review_ai/skills/<name>/SKILL.md` —— Task 2 的 `deploy_skills` 用 `importlib.resources.files("code_review_ai").joinpath("skills")` 读取；`SKILL_NAMES` 常量在 Task 2 定义。frontmatter `name`/`description` 与目录名。

- [ ] **Step 1: 写失败测试**（新建 `tests/test_skills.py`）

```python
"""Structure guard for the bundled language-review skills."""
import importlib.resources
import re

SKILL_NAMES = (
    "code-review-langs",
    "code-review-python",
    "code-review-typescript",
    "code-review-javascript",
)
LANGUAGE_SKILLS = SKILL_NAMES[1:]
REQUIRED_SECTIONS = ("安全", "正确性", "性能", "架构", "语言特有")


def _skills_source():
    return importlib.resources.files("code_review_ai").joinpath("skills")


def _read(name: str) -> str:
    return (_skills_source() / name / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict[str, str]:
    body = text.split("---", 2)[1]
    result = {}
    for line in body.strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip().strip('"')
    return result


def test_each_skill_is_a_directory_with_matching_frontmatter():
    for name in SKILL_NAMES:
        fm = _frontmatter(_read(name))
        assert fm.get("name") == name
        assert fm.get("description")


def test_entry_lists_exactly_the_three_language_skills():
    entry = _read("code-review-langs")
    referenced = set(re.findall(r"code-review-(?:python|typescript|javascript)", entry))
    assert referenced == set(LANGUAGE_SKILLS)


def test_language_skills_have_all_required_sections():
    for name in LANGUAGE_SKILLS:
        body = _read(name)
        for section in REQUIRED_SECTIONS:
            assert f"## {section}" in body, f"{name} missing '## {section}'"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_skills.py -v`
Expected: FAIL（`FileNotFoundError`，`code_review_ai/skills` 不存在）

- [ ] **Step 3: 创建 4 个 SKILL.md**（内容全文，逐文件）

`code_review_ai/skills/code-review-langs/SKILL.md`:

```markdown
---
name: code-review-langs
description: 审查任何代码前先看本 skill——语言审核 skill 套件的入口。列出可用语言（Python/TypeScript/JavaScript）及对应规范 skill，按代码语言路由到具体 skill；不确定时用它确定该用哪套规范。
---

# 语言审核 skill 入口

本 skill 是语言审核 skill 套件的**入口路由表**：只负责决定"用哪套规范"，不包含具体规则。

## 支持的语言与对应 skill

| 语言 | 扩展名 | 审核 skill |
|---|---|---|
| Python | `.py` | `code-review-python` |
| TypeScript | `.ts` / `.tsx` | `code-review-typescript` |
| JavaScript | `.js` / `.jsx` / `.mjs` / `.cjs` | `code-review-javascript` |

## 路由规则

- 待审代码是**单一语言** → 直接调用上表对应 skill。
- **混合仓库 / 多语言变更** → 按文件扩展名逐个路由，每个文件用对应语言的 skill。
- **其他语言**（上表没有）→ 用通用工程最佳实践审查，并在报告中标注"该语言无专用规范"。

## 与 `code-review` skill 的关系

- 本套件只给"按语言审什么"的**静态规则清单**；`code-review` skill 负责 git diff 审查/评分/报告框架。两者独立，可配合使用。
- 本套件所有规则输出统一 `error` / `warning` / `info` 三级，与 `code-review` 的评分公式（得分 = max(40, 100 − error×10 − warning×3)）兼容。
```

`code_review_ai/skills/code-review-python/SKILL.md`:

```markdown
---
name: code-review-python
description: 按 Python 审核规范审查 Python 代码（.py）。含安全、正确性、性能、架构与 Python 语言特有检查点，发现标注 error/warning/info。
---

# Python 代码审核规范

## 审核方式

通读待审代码，逐类对照以下检查点；每条发现标注严重级（error / warning / info）+ `文件:行号` + 修复建议。本 skill 只输出规则与发现，不生成报告框架（报告交给 `code-review`）。

## 安全

- error：硬编码密码、密钥、Token、数据库连接串。
- error：字符串拼接 SQL 或未参数化的查询（SQL 注入）。
- error：日志输出密码、身份证、银行卡、Token 等敏感信息。
- error：对不可信输入使用 `eval` / `exec`。
- error：不安全的反序列化——对不可信数据 `pickle.loads`、`yaml.load` 不带 `Loader`。

## 正确性

- error：空 `except`，或捕获后既不处理也不重新抛出。
- error：文件、网络、数据库等资源未用 `with` / `finally` 正确关闭。
- error：手动创建裸线程（`threading.Thread`）且无异常处理/守护。
- error：共享可变状态（全局 list/dict、跨线程）缺少同步。
- warning：`except Exception` 过宽 / bare `except`，掩盖具体错误。
- warning：可变默认参数（`def f(items=[])`）。

## 性能

- warning：循环内查询数据库或发起网络请求（N+1）。
- warning：热点路径用 `+` 拼接字符串（应用 f-string / `"".join`）。
- warning：无必要的深拷贝或重复计算（缺少缓存/记忆化）。

## 架构

- warning：函数体超过 50 行、类超过 300 行。
- warning：逻辑 ≥3 步或嵌套 ≥2 层未拆分为语义清晰的子函数。
- warning：主控函数直接写实现细节（应只做参数准备 → 调用子函数 → 返回）。
- info：单字母变量名（数学索引除外）；内置名当变量名（`id`/`list`/`dict`/`str`）；循环变量无语义。
- info：魔法数字、未使用变量/导入、冗余或注释掉的旧代码、缺必要注释。

## 语言特有

- 资源管理优先 `with` 语句；异常尽量捕获具体类型。
- f-string 优先于 `%` 格式化和 `.format()`。
- 遍历序列用 `enumerate`，避免 `for i in range(len(items))`。
- datetime 注意时区：naive 与 aware 不混用，推荐 `timezone.utc`。
- 模块级常量用 `UPPER_SNAKE` 命名。
- 列表推导嵌套不宜过深。
```

`code_review_ai/skills/code-review-typescript/SKILL.md`:

```markdown
---
name: code-review-typescript
description: 按 TypeScript 审核规范审查 TypeScript 代码（.ts/.tsx）。含安全、正确性、性能、架构与 TypeScript 语言特有检查点，发现标注 error/warning/info。
---

# TypeScript 代码审核规范

## 审核方式

通读待审代码，逐类对照以下检查点；每条发现标注严重级（error / warning / info）+ `文件:行号` + 修复建议。本 skill 只输出规则与发现，不生成报告框架（报告交给 `code-review`）。

## 安全

- error：硬编码密码、密钥、Token。
- error：字符串拼接 SQL（如 `SELECT ... + userInput`）。
- error：日志输出密码、身份证、银行卡、Token 等敏感信息。
- error：把不可信输入传入 `eval` / `new Function`。
- error：XSS 注入点——把不可信输入直接放入 `innerHTML` / `dangerouslySetInnerHTML` / `document.write`（应使用 `textContent` 或转义）。

## 正确性

- error：空 `catch`，或捕获后既不处理也不重新抛出。
- error：文件句柄、数据库连接、`EventSource`/`WebSocket` 等资源未关闭。
- error：共享可变状态缺少同步；未处理的异步 rejection 被吞掉。
- warning：`catch (error)` 类型过宽，丢失类型信息。
- warning：`null` 与 `undefined` 混用/未区分。

## 性能

- warning：循环内查询数据库或发起网络请求（N+1）。
- warning：渲染热点中无必要的重计算（缺 memoization）或大数组频繁全量拷贝。

## 架构

- warning：函数体超过 50 行、类超过 300 行。
- warning：逻辑 ≥3 步或嵌套 ≥2 层未拆子函数；组件/模块单一职责被破坏。
- info：单字母变量名；魔法数字；未使用变量/导入；冗余代码。

## 语言特有

- 开启 `strict` 模式；`strictNullChecks` 开启，不依赖隐式 `any`。
- 避免 `any` / `as any` 逃逸类型安全；公共 API 显式标注返回类型。
- 区分 `null` 与 `undefined`，用可选链 `?.` 与空值合并 `??`。
- 不可变量用 `readonly` / `const` 建模。
- async 错误用 try/catch 传播，`Promise.all` 失败要处理，不吞 rejection。
- 用 discriminated union 收窄类型，避免过度断言。
```

`code_review_ai/skills/code-review-javascript/SKILL.md`:

```markdown
---
name: code-review-javascript
description: 按 JavaScript 审核规范审查 JavaScript 代码（.js/.jsx/.mjs/.cjs）。含安全、正确性、性能、架构与 JavaScript 语言特有检查点，发现标注 error/warning/info。
---

# JavaScript 代码审核规范

## 审核方式

通读待审代码，逐类对照以下检查点；每条发现标注严重级（error / warning / info）+ `文件:行号` + 修复建议。本 skill 只输出规则与发现，不生成报告框架（报告交给 `code-review`）。

## 安全

- error：硬编码密码、密钥、Token。
- error：字符串拼接 SQL。
- error：日志输出密码、身份证、银行卡、Token 等敏感信息。
- error：把不可信输入传入 `eval` / `new Function`。
- error：XSS——不可信输入直接写入 `innerHTML` / `document.write`（用 `textContent` 或转义）。
- error：合并对象时原型污染——对不可信输入用 `{...obj}` / `Object.assign` 覆盖 `__proto__`。

## 正确性

- error：空 `catch`，捕获后不处理不抛出。
- error：资源（文件流、WebSocket、EventSource）未关闭。
- error：未处理/被吞掉的异步 rejection（`unhandledRejection`）。
- warning：共享可变状态、模块级可变全局缺约束。
- warning：`NaN` 判断用 `==`（应用 `Number.isNaN`）；浮点相等直接比较（`0.1 + 0.2 === 0.3`）。

## 性能

- warning：循环内 DB/网络（N+1）；DOM 查询在循环内重复执行。
- warning：渲染/计算热点无谓重算（缺缓存）。

## 架构

- warning：函数体超过 50 行、类超过 300 行；回调嵌套 ≥3 层（回调地狱）。
- warning：≥3 步或嵌套 ≥2 层逻辑未拆子函数。
- info：隐式全局变量；未使用变量/导入；魔法数字；冗余代码。

## 语言特有

- 用 `const` / `let`，弃用 `var`。
- 用严格相等 `===` / `!==`，避免隐式类型转换。
- 异步优先 async/await 或 Promise，注册 `unhandledRejection` 兜底。
- 模块化：用 import/export，避免隐式全局与命名空间污染。
- DOM 写入优先 `textContent` / 建节点，避免 `innerHTML` 拼接不可信数据。
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_skills.py -v`
Expected: PASS（3 tests）

- [ ] **Step 5: 验证打包包含 skills 目录**

Run: `uv build --wheel && uv run python -c "import zipfile, glob, pathlib; w = glob.glob('dist/*.whl')[0]; names = zipfile.ZipFile(w).namelist(); assert any('code_review_ai/skills/code-review-python/SKILL.md' in n for n in names), 'skills not packaged'; print('skills packaged OK')"`
Expected: `skills packaged OK`

若失败（hatchling 未默认包含包内 `.md`），在 `pyproject.toml` 补：

```toml
[tool.hatch.build.targets.wheel.force-include]
"code_review_ai/skills" = "code_review_ai/skills"
```

然后重跑上述命令。

- [ ] **Step 6: 提交**

```bash
git add tests/test_skills.py code_review_ai/skills
git commit -m "feat(skills): bundle language-review skill files (entry + python/typescript/javascript)"
```

---

### Task 2: 平台感知 installer（deploy_skills + append_usage_docs 泛化 + install 分流）

**Files:**
- Modify: `code_review_ai/installer.py`
- Modify: `tests/test_installer.py`

**Interfaces:**
- Consumes: Task 1 的 `code_review_ai/skills/<name>/SKILL.md`（通过 `importlib.resources`）。
- Produces:
  - `installer.SKILL_NAMES: tuple[str, ...]`（4 个 skill 名）
  - `installer.deploy_skills(platform: str = "claude-code", skills_root: Path | None = None) -> Path | None`
  - `installer._global_skills_dir(platform: str) -> Path`
  - `installer._global_context_file(platform: str) -> Path`（替换原 `_global_claude_md`）
  - `installer.append_usage_docs(platform: str = "claude-code") -> Path | None`（签名扩展，行为不变）
  - `installer.install(platform=..., ...)` 新增 `codex` 支持（返回 `InstallResult`，`success=True` 时无子进程）
  - `installer.SUPPORTED_PLATFORMS = {"claude-code", "codex"}`

- [ ] **Step 1: 更新 + 新增测试**（`tests/test_installer.py`）

把顶部 import 改为：

```python
from code_review_ai.installer import (
    DEFAULT_MCP_ENTRY, DEFAULT_NAME, DEFAULT_SOURCE,
    MCP_DOC_END, MCP_DOC_START, SKILL_NAMES,
    _claude_add_command, _claude_executable, _launch_command,
    append_usage_docs, deploy_skills, install,
)
```

更新 `test_install_success_runs_claude_mcp_add`（追加 `deploy_skills` monkeypatch + 消息断言）：

```python
def test_install_success_runs_claude_mcp_add(monkeypatch, tmp_path):
    monkeypatch.setattr("code_review_ai.installer.shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr("code_review_ai.installer.append_usage_docs",
                        lambda platform="claude-code": tmp_path / "CLAUDE.md")
    monkeypatch.setattr("code_review_ai.installer.deploy_skills",
                        lambda platform="claude-code", skills_root=None: tmp_path / "skills")
    captured = {}

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr("code_review_ai.installer.subprocess.run", fake_run)
    res = install()
    assert res.success is True
    assert captured["cmd"] == [
        "/usr/bin/claude", "mcp", "add", DEFAULT_NAME, "-s", "user", "--",
        "uvx", "--from", DEFAULT_SOURCE, DEFAULT_MCP_ENTRY,
    ]
    assert "Appended tool usage docs" in res.message
    assert "Deployed 4 review skills" in res.message
```

更新 `test_install_failure_does_not_append_docs`（追加 `deploy_skills` 不被调用的断言）：

```python
def test_install_failure_does_not_append_docs(monkeypatch):
    monkeypatch.setattr("code_review_ai.installer.shutil.which", lambda _: "/usr/bin/claude")
    calls = []

    class _P:
        returncode = 1
        stdout = ""
        stderr = "server already exists"

    monkeypatch.setattr("code_review_ai.installer.subprocess.run", lambda cmd, **kw: _P())
    monkeypatch.setattr("code_review_ai.installer.append_usage_docs",
                        lambda **kw: calls.append("docs") or None)
    monkeypatch.setattr("code_review_ai.installer.deploy_skills",
                        lambda **kw: calls.append("skills") or None)
    res = install()
    assert res.success is False
    assert "server already exists" in res.message
    assert calls == []  # 失败时不写文档、不部署 skill
```

更新 `test_append_usage_docs_is_idempotent` 与 `test_append_usage_docs_preserves_existing_content`：monkeypatch 目标从 `_global_claude_md` 改为 `_global_context_file`：

```python
def test_append_usage_docs_is_idempotent(monkeypatch, tmp_path):
    md = tmp_path / "CLAUDE.md"
    monkeypatch.setattr("code_review_ai.installer._global_context_file",
                        lambda platform="claude-code": md)
    append_usage_docs()
    append_usage_docs()
    content = md.read_text(encoding="utf-8")
    assert content.count(MCP_DOC_START) == 1
    assert content.count(MCP_DOC_END) == 1
    assert "code-review-ai MCP tools" in content


def test_append_usage_docs_preserves_existing_content(monkeypatch, tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("<!-- CODEGRAPH_END -->\n", encoding="utf-8")
    monkeypatch.setattr("code_review_ai.installer._global_context_file",
                        lambda platform="claude-code": md)
    append_usage_docs()
    content = md.read_text(encoding="utf-8")
    assert content.startswith("<!-- CODEGRAPH_END -->")
    assert MCP_DOC_START in content
```

新增测试（追加到文件末尾）：

```python
def test_append_usage_docs_codex_writes_agents_md(monkeypatch, tmp_path):
    agents = tmp_path / "AGENTS.md"
    monkeypatch.setattr("code_review_ai.installer._global_context_file",
                        lambda platform="codex": agents)
    append_usage_docs("codex")
    append_usage_docs("codex")
    content = agents.read_text(encoding="utf-8")
    assert content.count(MCP_DOC_START) == 1
    assert "code-review-ai MCP tools" in content


def test_deploy_skills_copies_all_skills_claude_code(monkeypatch, tmp_path):
    target = tmp_path / "claude-skills"
    monkeypatch.setattr("code_review_ai.installer._global_skills_dir",
                        lambda platform="claude-code": target)
    result = deploy_skills()
    assert result == target
    for name in SKILL_NAMES:
        text = (target / name / "SKILL.md").read_text(encoding="utf-8")
        assert f"name: {name}" in text


def test_deploy_skills_copies_all_skills_codex(monkeypatch, tmp_path):
    target = tmp_path / "codex-skills"
    monkeypatch.setattr("code_review_ai.installer._global_skills_dir",
                        lambda platform="codex": target)
    result = deploy_skills("codex")
    assert result == target
    for name in SKILL_NAMES:
        assert (target / name / "SKILL.md").exists()


def test_deploy_skills_is_idempotent(monkeypatch, tmp_path):
    target = tmp_path / "skills"
    monkeypatch.setattr("code_review_ai.installer._global_skills_dir",
                        lambda platform="claude-code": target)
    deploy_skills()
    before = {name: (target / name / "SKILL.md").read_text(encoding="utf-8")
              for name in SKILL_NAMES}
    deploy_skills()
    after = {name: (target / name / "SKILL.md").read_text(encoding="utf-8")
             for name in SKILL_NAMES}
    assert before == after
    assert len(list(target.iterdir())) == len(SKILL_NAMES)


def test_deploy_skills_missing_resource_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr("code_review_ai.installer.importlib.resources.files",
                        lambda *args, **kwargs: tmp_path / "does-not-exist")
    assert deploy_skills() is None


def test_install_codex_skips_subprocess_and_deploys(monkeypatch, tmp_path):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return None

    monkeypatch.setattr("code_review_ai.installer.subprocess.run", fake_run)
    monkeypatch.setattr("code_review_ai.installer.append_usage_docs",
                        lambda platform="codex": tmp_path / "AGENTS.md")
    monkeypatch.setattr("code_review_ai.installer.deploy_skills",
                        lambda platform="codex", skills_root=None: tmp_path / "codex-skills")
    res = install(platform="codex")
    assert res.success is True
    assert calls == []  # codex 不执行任何 MCP 注册子进程
    assert "manual" in res.message.lower()
    assert "Deployed 4 review skills" in res.message
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_installer.py -v`
Expected: FAIL（`ImportError: cannot import name 'SKILL_NAMES'` / `AttributeError: module ... has no attribute 'deploy_skills'` / 等）

- [ ] **Step 3: 实现 installer 改动**（`code_review_ai/installer.py`）

顶部新增 import：

```python
import importlib.resources
```

`SUPPORTED_PLATFORMS` 与 `SKILL_NAMES`：

```python
SUPPORTED_PLATFORMS = {"claude-code", "codex"}

SKILL_NAMES = (
    "code-review-langs",
    "code-review-python",
    "code-review-typescript",
    "code-review-javascript",
)
```

把 `_global_claude_md` 替换为平台感知版本，并新增 skills 目录解析：

```python
def _global_context_file(platform: str) -> Path:
    """The platform's always-injected context file for tool-usage docs."""
    home = Path.home()
    if platform == "codex":
        return home / ".codex" / "AGENTS.md"
    return home / ".claude" / "CLAUDE.md"


def _global_skills_dir(platform: str) -> Path:
    """The platform's user-scope skills directory."""
    home = Path.home()
    if platform == "codex":
        return home / ".codex" / "skills"
    return home / ".claude" / "skills"
```

`append_usage_docs` 改为平台参数（其余逻辑不变）：

```python
def append_usage_docs(platform: str = "claude-code") -> Path | None:
    """Append (or refresh) the MCP tool-usage section to the platform's
    user-global context file (CLAUDE.md / AGENTS.md), so the AI in any project
    knows how to call the tools. Idempotent: the block between the markers is
    replaced in place. Returns the path written, or None if it couldn't be
    written (install still succeeds)."""
    md = _global_context_file(platform)
    try:
        md.parent.mkdir(parents=True, exist_ok=True)
        content = md.read_text(encoding="utf-8") if md.exists() else ""
        start = content.find(MCP_DOC_START)
        end = content.find(MCP_DOC_END)
        if start != -1 and end != -1 and end > start:
            content = content[:start] + MCP_USAGE_DOC + content[end + len(MCP_DOC_END):]
        else:
            if content and not content.endswith("\n\n"):
                content = content.rstrip("\n") + "\n\n"
            content += MCP_USAGE_DOC
        md.write_text(content, encoding="utf-8")
        return md
    except OSError:
        return None
```

新增 skill 部署：

```python
def deploy_skills(platform: str = "claude-code",
                  skills_root: Path | None = None) -> Path | None:
    """Copy the bundled language-review skills into the target platform's
    user-scope skills dir. Idempotent: overwrites SKILL.md in place. Returns
    the target dir, or None on failure (install still succeeds)."""
    target = skills_root or _global_skills_dir(platform)
    try:
        source = importlib.resources.files("code_review_ai").joinpath("skills")
        for name in SKILL_NAMES:
            skill_dir = target / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            payload = (source / name / "SKILL.md").read_text(encoding="utf-8")
            (skill_dir / "SKILL.md").write_text(payload, encoding="utf-8")
        return target
    except (OSError, FileNotFoundError):
        return None
```

`install` 改为平台分流（新增两个私有辅助，保持主函数只编排）：

```python
def _deploy_docs_and_skills(platform: str, msg: str) -> str:
    """Append usage docs + deploy skills for a platform; fold the outcomes
    into the success message. Both steps are non-fatal."""
    doc_path = append_usage_docs(platform)
    skills_dir = deploy_skills(platform)
    if doc_path is not None:
        msg += f" Appended tool usage docs to {doc_path}."
    if skills_dir is not None:
        msg += f" Deployed {len(SKILL_NAMES)} review skills to {skills_dir}."
    return msg


def _install_codex() -> InstallResult:
    """Codex has no ``codex mcp add`` CLI: deploy skills + usage docs only,
    MCP registration stays manual (edit ~/.codex/config.toml)."""
    msg = _deploy_docs_and_skills(
        "codex",
        "Registered review skills with Codex. MCP registration is manual: "
        "add a [mcp_servers.code-review-ai] block to ~/.codex/config.toml "
        "(see README).",
    )
    return InstallResult(True, msg, [])


def _install_claude(source: str, scope: str, name: str,
                    mcp_entry: str) -> InstallResult:
    """Register the MCP server with Claude Code, then deploy docs + skills."""
    add_cmd = _claude_add_command(name, scope, _launch_command(source, mcp_entry))
    claude = _claude_executable()
    if claude is None:
        return InstallResult(
            False,
            "claude CLI not found on PATH. Install Claude Code, then run:\n  "
            + " ".join(add_cmd),
            add_cmd,
        )
    add_cmd = [claude, *add_cmd[1:]]
    proc = subprocess.run(add_cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return InstallResult(
            False,
            f"claude mcp add failed (exit {proc.returncode}):\n{detail}\n"
            f"If '{name}' already exists, remove it first: claude mcp remove {name}",
            add_cmd,
        )
    msg = _deploy_docs_and_skills(
        "claude-code",
        f"Registered '{name}' with Claude Code (scope={scope}).",
    )
    msg += " Restart Claude Code (or run /mcp) to see the tools."
    return InstallResult(True, msg, add_cmd)


def install(platform: str = "claude-code", source: str = DEFAULT_SOURCE,
            scope: str = "user", name: str = DEFAULT_NAME,
            mcp_entry: str = DEFAULT_MCP_ENTRY) -> InstallResult:
    """Register MCP + deploy skills/docs for the target platform. Returns a
    result; never raises - callers just print ``message`` and map ``success``
    to exit code."""
    if platform not in SUPPORTED_PLATFORMS:
        return InstallResult(False, f"unsupported platform: {platform}", [])
    if platform == "codex":
        return _install_codex()
    return _install_claude(source, scope, name, mcp_entry)
```

- [ ] **Step 4: 运行确认通过**

Run: `uv run pytest tests/test_installer.py -v`
Expected: PASS（原 10 个 + 新增 6 个，共 16 个；`test_install_unsupported_platform` 仍用 `"cursor"`，继续通过）

- [ ] **Step 5: 全量测试**

Run: `uv run pytest`
Expected: PASS（`tests/test_skills.py` + 全部既有测试；`test_cli.py` 只 monkeypatch `install`，无需改动）

- [ ] **Step 6: 提交**

```bash
git add code_review_ai/installer.py tests/test_installer.py
git commit -m "feat(installer): platform-aware skill deployment (claude-code + codex)"
```

---

### Task 3: 文档更新（README + AGENTS.md）

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`（第 26 行 install 注释 + 第 77 行 Codex 描述）

**Interfaces:**
- Consumes: Task 2 的 `install --platform codex` 行为。
- Produces: 用户可读的双平台安装说明。

- [ ] **Step 1: 更新 README**（在 "Register with Claude Code" 小节后追加两段）

在 `## Register with Claude Code (one command)` 小节末尾追加：

```markdown
### Register with Codex

```bash
code-review-ai install --platform codex
```

This deploys the four review skills to `~/.codex/skills/` and appends the MCP
tool-usage docs to `~/.codex/AGENTS.md` (marker-guarded, idempotent). Codex
has no `codex mcp add` CLI, so MCP registration is manual — add a block to
`~/.codex/config.toml`:

```toml
[mcp_servers.code-review-ai]
command = "uvx"
args = ["--from", "git+https://github.com/Monkey-D-sj/code-review-ai", "code-review-ai-mcp"]
type = "stdio"
```

Both installs also deploy four user-scope code-review skills:
`code-review-langs` (entry/router) plus `code-review-python`,
`code-review-typescript`, and `code-review-javascript`, each carrying the
static review rules for its language. They coexist with any existing
`code-review` skill and never call the MCP graph tools.
```

- [ ] **Step 2: 修正 AGENTS.md 的两处 Codex 表述**（现在是"文档先行、代码未实现"的错误声明）

第 26 行：

```text
uv run code-review-ai install --platform Codex        # deploy review skills + AGENTS.md docs (MCP registered manually)
```

第 77 行将 `install --platform Codex` 相关句改为：

```text
CLI (`code-review-ai`) mirrors `rebuild`/`query`/`search`/`communities` for manual use, plus `install --platform claude-code|codex` which registers MCP with Claude Code and deploys the bundled language-review skills + usage docs (Codex MCP registration is manual via `~/.codex/config.toml`; see `installer.py`). `graph` (`export_graph.py`) renders the index as interactive HTML: `-m communities` draws the persisted community graph (bubbles sized by node count, cross-community edges read straight from `community_edges` — never re-derived), `-m graph` the raw function-level call graph, `-m flow` the BFS flow chains.
```

- [ ] **Step 3: 验证**

Run: `git diff --stat README.md AGENTS.md`
Expected: 两文件各若干行变更；无语法问题（纯文档）。

- [ ] **Step 4: 提交**

```bash
git add README.md AGENTS.md
git commit -m "docs: document Codex install + bundled language-review skills"
```
