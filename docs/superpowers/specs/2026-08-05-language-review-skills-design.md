# 语言审核 skill 注入设计

日期：2026-08-05 · 状态：已评审通过

## 1. 背景与目标

`code-review-ai` 目前通过两种方式给 Claude Code 提供上下文：MCP server（用户作用域注册，调用图查询工具）和 `installer.append_usage_docs()` 向用户全局 `~/.claude/CLAUDE.md` 追加的简短工具用法说明。用户希望再补一种：**按语言拆分的代码审核 skill 套件**，随 `install` 部署到用户级 `~/.claude/skills/`（Claude Code）与 `~/.codex/skills/`（Codex），让任何项目里的 Claude Code / Codex 都能按代码语言加载对应的审核规范。

目标：

- 一个**入口 skill** 列出支持的语言并路由到具体语言 skill。
- 三种**语言审核 skill**（Python / TypeScript / JavaScript），各含该语言的静态审核规范。
- 由 `code-review-ai install --platform claude-code|codex` 一键部署到用户级 skills 目录，幂等、可重装。

非目标：

- 不联动调用图 MCP 工具（用户明确选择"纯静态规则"）。
- 不替换现有 `~/.claude/skills/code-review/` skill，两者并存。
- 不做 `uninstall` 命令、不做 `--scope project` 部署（`deploy_skills` 预留参数，后续可扩展）。

## 2. 决策记录（用户确认）

| 决策 | 选择 |
|---|---|
| 注入范围 | 用户级（所有项目），由 `install --platform claude-code|codex` 部署 |
| 平台 | claude-code + codex 双平台（`SUPPORTED_PLATFORMS` 扩展） |
| Codex 支持深度 | skill 部署 `~/.codex/skills/` + marker 注入 `~/.codex/AGENTS.md`；MCP 注册保持手动（`~/.codex/config.toml`，README 说明） |
| 与现有 `code-review` skill 关系 | 新建一套并存，互不干扰 |
| 语言范围 | 恰好 3 种：Python / TypeScript / JavaScript |
| 规范内容来源 | 起草（基于各语言最佳实践 + 现有 code-review skill 审查重点 + 项目 CLAUDE.md 代码规范） |
| 是否联动图工具 | 否，纯静态规则 |
| skill 结构 | 入口 + 3 语言 skill **并行**顶层目录（Claude Code 只发现 `skills/*/SKILL.md`，嵌套目录不会被注册为独立 skill，也无法按名调用） |

## 3. 架构

### 3.1 skill 源文件布局（打进 wheel 的包内资源）

```
code_review_ai/skills/
  code-review-langs/SKILL.md            # 入口 / 路由
  code-review-python/SKILL.md           # Python 审核规范
  code-review-typescript/SKILL.md       # TypeScript 审核规范
  code-review-javascript/SKILL.md       # JavaScript 审核规范
```

源布局与部署目标镜像：`code_review_ai/skills/<name>/SKILL.md` 原样复制到 `<平台 skills 目录>/<name>/SKILL.md`，deploy 无展平逻辑。frontmatter `name` 与目录名一致，`description` 负责触发。Codex 与 Claude Code 的 skill 格式相同（`SKILL.md` + `name`/`description` frontmatter，样例见 FastAPI 官方 `.agents/skills/fastapi/SKILL.md`）。

### 3.2 installer 改动（`code_review_ai/installer.py`）

- `SUPPORTED_PLATFORMS` 扩展为 `{"claude-code", "codex"}`。
- 新增平台感知的 skill 部署（沿用现有 marker 注入模式）：

```python
SKILL_NAMES = (
    "code-review-langs",
    "code-review-python",
    "code-review-typescript",
    "code-review-javascript",
)

def _global_skills_dir(platform: str) -> Path:
    home = Path.home()
    return home / ".claude" / "skills" if platform == "claude-code" else home / ".codex" / "skills"

def deploy_skills(platform: str = "claude-code",
                  skills_root: Path | None = None) -> Path | None:
    """把包内语言审核 skill 复制到目标平台 skills 目录。幂等：原位覆盖 SKILL.md。
    返回目标目录；失败返回 None、不阻断 install。"""
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

- 现有 `append_usage_docs()` 泛化为 `append_usage_docs(platform="claude-code")`：目标文件由 `_global_claude_md()` 改为 `_global_context_file(platform)` → `~/.claude/CLAUDE.md`（claude-code）或 `~/.codex/AGENTS.md`（codex）。marker 守卫、幂等逻辑不变（`~/.codex/AGENTS.md` 已在用 `<!-- CODEGRAPH_START/END -->` 同类模式）。
- `install(platform)` 按平台分流：
  - **claude-code**：`claude mcp add` → `append_usage_docs("claude-code")` → `deploy_skills("claude-code")`（现有流程，新增 skill 部署）。
  - **codex**：**跳过 MCP 注册**（本机无 `codex mcp add` CLI，MCP 由用户手动写 `~/.codex/config.toml`，README 说明）→ `append_usage_docs("codex")` → `deploy_skills("codex")`。
  - 两个非致命步骤（文档注入、skill 部署）失败都不阻断 install，成功消息带上 `Deployed N review skills to <dir>`。

### 3.3 数据流

```
code-review-ai install --platform claude-code
  → claude mcp add code-review-ai            （注册 MCP，已有）
  → append_usage_docs("claude-code")          （写 ~/.claude/CLAUDE.md，已有）
  → deploy_skills("claude-code")              （新增：写 ~/.claude/skills/ 下 4 个 skill）

code-review-ai install --platform codex
  → 跳过 MCP 注册（无 codex CLI；手动写 ~/.codex/config.toml，README 说明）
  → append_usage_docs("codex")                （marker 注入 ~/.codex/AGENTS.md）
  → deploy_skills("codex")                    （写 ~/.codex/skills/ 下 4 个 skill）
```

平台重启后：skill 列表出现 4 项 → 审代码时 AI 按入口描述触发 → 入口按语言路由 → 调用对应语言 skill。

### 3.4 打包验证

hatchling 默认把包内非 `.py` 文件作为 package data。实施期用 `uv build` 验证 wheel 内含 `code_review_ai/skills/*/SKILL.md`；若未包含，补显式 hatch include 配置。

## 4. skill 内容规范

### 4.1 入口 skill：`code-review-langs`

frontmatter：

```yaml
---
name: code-review-langs
description: 审查任何代码前先看本 skill——语言审核 skill 套件的入口。列出可用语言（Python/TypeScript/JavaScript）及对应规范 skill，按代码语言路由到具体 skill；不确定时用它确定该用哪套规范。
---
```

正文四段：

1. **用途**：语言审核 skill 的入口路由表，只负责"该用哪套规范"，不含具体规则。
2. **语言 → skill 对照表**：
   - Python（`.py`）→ `code-review-python`
   - TypeScript（`.ts`/`.tsx`）→ `code-review-typescript`
   - JavaScript（`.js`/`.jsx`/`.mjs`/`.cjs`）→ `code-review-javascript`
3. **路由规则**：单一语言直接调用对应 skill；混合仓库按文件扩展名逐个路由；无专用 skill 的语言用通用最佳实践审查并标注"该语言无专用规范"。
4. **与 `code-review` 的关系**：本套件只给"按语言审什么"的静态规则清单；`code-review` 负责 diff 审查/评分/报告框架，两者独立可配合。所有规则输出统一 `error`/`warning`/`info` 三级，与 `code-review` 评分公式（得分 = max(40, 100 − error×10 − warning×3)）兼容。

### 4.2 语言 skill 共享骨架（第 1–5 类，三份通用）

每份 `SKILL.md` 开头一段"审核方式"：通读代码 → 逐类对照检查点 → 每条发现标注严重级 + 文件:行号 + 修复建议；不生成报告框架。

每份语言 skill 正文**必须**含且仅含以下 5 个小节（标题即规范，测试按此断言）：`安全`、`正确性`、`性能`、`架构`、`语言特有`。

| 小节 | 严重级 | 检查点（三语言共用） |
|---|---|---|
| **安全** | error | 硬编码密钥/Token/连接串；字符串拼接 SQL；日志输出敏感信息；不可信输入进 eval/exec 类执行点 |
| **正确性** | error/warning | 空 except/catch 后不处理不抛出；资源未用 with/finally 关闭；手动裸线程；共享可变状态缺同步；异常类型过宽；可变默认参数/浅拷贝误用 |
| **性能** | warning | 循环内 DB/网络（N+1）；热点字符串拼接；无谓深拷贝/重复计算 |
| **架构** | warning/info | 函数 >50 行、类 >300 行；≥3 步或嵌套 ≥2 层未拆子函数；主控函数写实现细节；单字母变量名、内置名当变量；魔法数字；未用变量/导入；冗余/注释旧代码 |
| **语言特有** | 混合 | 见 4.3 |

### 4.3 语言特有检查点

**Python**：资源管理用 `with`、异常用具体类型；f-string 优先；`for i in range(len(x))` → `enumerate`；datetime 时区（naive vs aware、`timezone.utc`）；模块级常量 `UPPER_SNAKE`；避免可变默认参数；不安全的 `pickle`/`yaml.load`。

**TypeScript**：`strict` 模式（`strictNullChecks`）、避免 `any`/`as any` 逃逸；区分 `null`/`undefined`，用 `?.`/`??`；`readonly` 建模不变量；async 错误传播与 rejection 不吞；discriminated union 收窄、不过度断言；公共 API 显式返回类型。

**JavaScript**：`const`/`let` 弃 `var`；严格相等 `===`、避免隐式转换；异步 rejection 未处理 / `unhandledRejection`；浮点陷阱（`0.1+0.2`）、`Number.isNaN`；合并对象防原型污染；`innerHTML`/`document.write` 不可信输入 → 用 `textContent`；隐式全局。

三份语言 skill 规则骨架完全一致，只换语言特有段与 frontmatter 描述——实现为"一份模板 × 3 处实例化"。

## 5. 幂等、错误处理

- **幂等（skill）**：SKILL.md 原位覆盖、内容相同，天然幂等，不需要 marker；对两个平台同样成立。
- **幂等（用法文档）**：`append_usage_docs(platform)` 用 marker 守卫、原位替换（`~/.claude/CLAUDE.md` 与 `~/.codex/AGENTS.md` 均是"往用户已有内容里追加"的场景，必须 marker）。
- **覆盖策略**：四个 skill 文件归本工具所有，重装即覆盖，保持与当前安装版本一致；不做"已存在就跳过"（会导致 skill 静默漂移过时）。
- **错误处理**：包内资源缺失（`FileNotFoundError`）或目标目录不可写（`OSError`）→ `deploy_skills` 返回 `None`，`install` 仍成功，消息注明 skill 部署被跳过——与 `append_usage_docs` 失败不阻断 install 的行为一致。codex 平台无 `codex mcp add`，`install --platform codex` 不执行任何 MCP 注册子进程。

## 6. 测试

### `tests/test_installer.py` 追加 / 更新

- `test_deploy_skills_copies_all_skills`：monkeypatch `_global_skills_dir` 到 tmp_path，断言 4 个目录各有一份 `SKILL.md`、frontmatter `name` 与目录名一致。**两平台各测一次**（claude-code → `.claude/skills`，codex → `.codex/skills`）。
- `test_deploy_skills_is_idempotent`：跑两次，仍是 4 个目录、内容一致、无重复。
- `test_deploy_skills_missing_resource_returns_none`：资源指向不存在的路径 → 返回 `None` 不抛异常。
- `test_append_usage_docs_platform_codex`：monkeypatch `_global_context_file` 到 codex 路径，断言 marker + 工具文档写入 `AGENTS.md`、幂等（替换而非重复追加）。
- `test_install_success_codex_skips_cli_and_deploys`：`platform="codex"`，断言**不调用**任何 mcp 注册子进程，`append_usage_docs`/`deploy_skills` 均被调用，消息含"手动注册 MCP"提示与 skill 部署信息。
- 更新现有 `test_append_usage_docs_*` 与 `test_install_success_*`：monkeypatch 目标从 `_global_claude_md` 改为 `_global_context_file`；`test_install_success_runs_claude_mcp_add` 追加 monkeypatch `deploy_skills`，断言成功消息含 skill 部署信息。

### 新增 `tests/test_skills.py`（结构守护）

- 包内 4 份 `SKILL.md`：frontmatter 可解析、`name` == 目录名、`description` 非空。
- 入口正文列出且**只**列出那 3 个语言 skill。
- 每个语言 skill 含 5 个必需小节（安全/正确性/性能/架构/语言特有）且语言特有段非空。

## 7. CLI 表面

不加新命令。`install --platform claude-code|codex` 始终部署对应平台 skill。README 补一段：Codex 平台的 MCP 注册需手动在 `~/.codex/config.toml` 的 `[mcp_servers.*]` 下添加（给出示例，与现有 `code-review-graph` 条目同构）。
