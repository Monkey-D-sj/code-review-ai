# SkillOpt 集成 · 工作流 A 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 review policy 可注入、工具输出正文可保留、每轮 reasoning 能传出进程，打通 SkillOpt 优化的前置条件。

**Architecture:** 三项互相独立、默认行为不变的增量改动。policy 只在 `runner.py` 参数化（loop 不构造消息，传给它等于空操作）；`ToolTrace` 增加一个有上限的 `response_excerpt`；`loop_result_payload()` 增补 `assistant_turns`。

**Tech Stack:** Python 3.14、`uv`、`langchain_core`、`pydantic`、pytest（`uv run pytest`）。

**Spec:** `docs/superpowers/specs/2026-09-11-skillopt-integration-design.md`

## Global Constraints

- **默认行为必须完全不变。** 三个新参数全部可选，缺省走既有行为；现有调用点与测试不得需要修改。
- `--policy-file` 指向不存在的文件时**报错并以 2 退出**，绝不静默回落内置 policy —— 静默回落会让调用方以为注入了新 policy 而实际跑的是内置版。
- `TRACE_RESPONSE_EXCERPT_CHARS = 2000`，模块级常量，可由 `run_loop` / `run_free_loop` 的可选参数覆盖；本阶段不加对应 CLI 参数。
- **`response_chars` 语义不变**（工具返回内容的完整长度，不是截断后的长度）。`benchmarks/eval_cases.py` 与 `review_loop/payload.py` 都在消费它。
- 退出码约定：配置错误（`ValueError`）→ 2；运行失败 → 1；成功 → 0。由 `cli._cmd_review` 的 `except ValueError` → `_BAD_CONFIG` 实现。
- 测试一律 `uv run pytest`。注释与 docstring 的风格跟邻近代码保持一致（现有代码中英混用，跟随所在文件的习惯）。

---

### Task 1: `policy` 参数贯穿 runner 层

**Files:**
- Modify: `code_review_ai/review_loop/runner.py`（`build_initial_messages` :154、`run_review` :202、`run_free_review` :254）
- Test: `tests/test_review_loop_runner.py`

**Interfaces:**
- Consumes: 无（本任务是链条起点）
- Produces:
  - `build_initial_messages(prompt: str, summary: dict, items: list[ReviewItem], policy: str | None = None) -> list[BaseMessage]`
  - `run_review(config, conn, *, ..., policy: str | None = None) -> LoopResult`
  - `run_free_review(config, conn=None, *, ..., policy: str | None = None) -> LoopResult`
  - Task 2 依赖这三个签名。

- [ ] **Step 1: 写失败测试**

先扩展 `tests/test_review_loop_runner.py` 里的 `ScriptedReviewModel`，让它记下 system message 的内容（现在只记了一个布尔值 `saw_system`，断言不了内容）：

```python
# __init__ 里增加一行
        self.system_content = ""

# invoke 里已有的 SystemMessage 分支改成
            if isinstance(message, SystemMessage):
                self.saw_system = True
                self.system_content = message.content
```

再追加四个测试：

```python
def test_build_initial_messages_uses_the_injected_policy():
    items = [ReviewItem(qname="app::login")]

    messages = build_initial_messages("review auth", {"changed_functions": []}, items,
                                      policy="CUSTOM POLICY TEXT")

    assert messages[0].content == "CUSTOM POLICY TEXT"


def test_build_initial_messages_defaults_to_the_builtin_policy():
    items = [ReviewItem(qname="app::login")]

    messages = build_initial_messages("review auth", {"changed_functions": []}, items)

    assert messages[0].content == _POLICY


def test_run_review_threads_the_policy_into_the_request(env):
    config, conn = env
    model = ScriptedReviewModel()
    summary = {"changed_functions": [{"qname": "app::login", "file": "app.py",
                                      "start_line": 1, "end_line": 3}]}

    run_review(config, conn, prompt="p", summary=summary, model=model,
               policy="CUSTOM POLICY TEXT")

    assert model.system_content == "CUSTOM POLICY TEXT"


def test_run_free_review_threads_the_policy_into_the_request(env):
    config, conn = env
    model = ScriptedReviewModel()

    run_free_review(config, prompt="p", diff="DIFF-BODY", model=model,
                    policy="CUSTOM POLICY TEXT")

    assert model.system_content == "CUSTOM POLICY TEXT"
```

把 `_POLICY` 加进文件顶部从 `code_review_ai.review_loop.runner` 的导入列表。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_review_loop_runner.py -v -k "policy"`
Expected: 4 个测试 FAIL —— `TypeError: build_initial_messages() got an unexpected keyword argument 'policy'`（后两个同理，`run_review` / `run_free_review` 不认识 `policy`）。

- [ ] **Step 3: 实现**

`build_initial_messages`（`runner.py:154`）——加参数，改 :184 的返回，并在 docstring 里说明回落规则：

```python
def build_initial_messages(prompt: str, summary: dict,
                           items: list[ReviewItem],
                           policy: str | None = None) -> list[BaseMessage]:
    """...（保留原有 docstring 段落）

    ``policy`` replaces the built-in ``_POLICY`` as the system message when
    given; ``None`` keeps the built-in. SkillOpt injects the policy under
    optimization here.
    """
```

```python
    return [SystemMessage(content=policy or _POLICY), HumanMessage(content=user)]
```

`run_review`（`runner.py:202`）——在关键字参数区末尾加 `policy: str | None = None`，并把 `:241` 的调用改为：

```python
    messages = build_initial_messages(prompt, summary, items, policy=policy)
```

`run_free_review`（`runner.py:254`）——同样加参数，并把 `:281-284` 的消息构造改为：

```python
    messages = [
        SystemMessage(content=policy or _FREE_POLICY),
        HumanMessage(content=f"{prompt}\n\nDIFF\n{diff or '(no working-tree diff)'}"),
    ]
```

两个函数的 docstring 各补一句说明 `policy=None` 走内置。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_review_loop_runner.py -v`
Expected: 全部 PASS，包括既有的 8 个测试（默认行为未变）。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/review_loop/runner.py tests/test_review_loop_runner.py
git commit -m "feat(review): let callers inject the review policy"
```

---

### Task 2: `--policy-file` CLI 开关

**Files:**
- Modify: `code_review_ai/cli.py`（parser :83-110、新增 `_resolve_policy`、`_graph_review` :251、`_noindex_review` :265、`_run_review_command` :280）
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1 的 `run_review(..., policy=...)` 与 `run_free_review(..., policy=...)`
- Produces: `review --policy-file PATH`；`_resolve_policy(args) -> str | None`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_cli.py`：

```python
def test_review_accepts_a_policy_file_flag():
    args = cli._build_parser().parse_args(["review", "--policy-file", "p.md"])

    assert args.policy_file == "p.md"


def test_review_policy_file_defaults_to_none():
    args = cli._build_parser().parse_args(["review"])

    assert args.policy_file is None


def test_cli_review_passes_the_policy_file_content_to_the_arm(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "build_diff_text", lambda cfg, files=None: "")
    policy = tmp_path / "policy.md"
    policy.write_text("CUSTOM POLICY", encoding="utf-8")
    calls = {}

    def fake_free_review(config, conn=None, **kwargs):
        calls.update(kwargs)
        return FakeResult()

    monkeypatch.setattr("code_review_ai.review_loop.runner.run_free_review",
                        fake_free_review)
    code = main(["review", "--arm", "nograph", "--repo", str(tmp_path),
                 "--db", str(tmp_path / "i.db"), "--model", "m",
                 "--policy-file", str(policy)])

    assert code == 0
    assert calls["policy"] == "CUSTOM POLICY"


def test_missing_policy_file_exits_2(tmp_path, monkeypatch, capsys):
    """A missing policy file is bad configuration, never a silent fallback.

    Falling back to the built-in policy would let a caller believe it injected
    one while the run used the original.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)

    code = main(["review", "--repo", str(tmp_path), "--db", str(tmp_path / "r.db"),
                 "--model", "m", "--policy-file", "does-not-exist.md"])

    assert code == 2
    assert "does-not-exist.md" in capsys.readouterr().err


def test_empty_policy_file_exits_2(tmp_path, monkeypatch, capsys):
    """An empty file is as dangerous as a missing one.

    The runner falls back with `policy or _POLICY`, so `""` is indistinguishable
    from `None`: an empty file would silently run the baseline while the caller
    believed it was evaluating an injected policy.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")

    code = main(["review", "--repo", str(tmp_path), "--db", str(tmp_path / "r.db"),
                 "--model", "m", "--policy-file", str(empty)])

    assert code == 2
    assert "empty.md" in capsys.readouterr().err


def test_empty_policy_argument_exits_2(tmp_path, monkeypatch, capsys):
    """`--policy-file ""` -- an unset shell variable -- must not silently
    select the built-in policy either."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)

    code = main(["review", "--repo", str(tmp_path), "--db", str(tmp_path / "r.db"),
                 "--model", "m", "--policy-file", ""])

    assert code == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run python -m pytest tests/test_cli.py -v -k "policy"`
Expected: 6 个测试全部失败，失败原因分三类 —— `-k "policy"` 会同时选中两个 parse 测试和四个 `main()` 测试：
- `test_review_policy_file_defaults_to_none`：`args.policy_file` 不存在 → `AttributeError`
- `test_review_accepts_a_policy_file_flag`：argparse 不认识 `--policy-file` → `SystemExit`
- 其余四个：要么 argparse 提前退出，要么断言 `calls["policy"]` / 期望 exit 2 而落空

（本机 `uv run pytest` 有 uv trampoline 报错，用等价的 `uv run python -m pytest`。）

- [ ] **Step 3: 实现**

`cli.py:83` 附近的 `review` 子命令加参数（放在 `--api-key-env` 之后、`--no-progress` 之前）：

```python
    review.add_argument("--policy-file",
                        help="markdown file to use as the review policy (the "
                             "system prompt); defaults to the built-in policy")
```

新增 `_resolve_policy`（放在 `_review_settings` 附近，`:224` 一带）：

```python
def _resolve_policy(args) -> str | None:
    """The policy markdown for this run, or ``None`` to use the built-in one.

    A missing, empty, or empty-argument path raises ``ValueError`` so
    ``_cmd_review`` maps it to ``_BAD_CONFIG`` (exit 2). Falling back silently
    would let a caller believe it injected a policy while the run used the
    built-in one -- the failure would then look like "the policy made no
    difference", which is the hardest kind to diagnose.

    Empty matters as much as missing. The runner's fallback is
    ``policy or _POLICY``, so ``""`` is indistinguishable from ``None``: an
    empty file would run the baseline while reporting an optimized run. The
    same holds for an empty *argument* -- ``--policy-file "$POLICY_PATH"`` with
    the variable unset -- which is why the guard tests ``is None`` rather than
    falsiness, so ``""`` falls through to the path checks below.
    """
    if args.policy_file is None:
        return None
    path = Path(args.policy_file)
    if not path.is_file():
        raise ValueError(f"--policy-file {args.policy_file} does not exist")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"--policy-file {args.policy_file} is empty")
    return text
```

`Path` 已在 `cli.py:20` 导入，无需新增 import。

两个 arm 各加一个尾参 `policy`：

```python
def _graph_review(args, ctx, settings: _ModelSettings, hooks, policy) -> object:
```

并在其 `run_review(...)` 调用（`:258-262`）末尾加 `policy=policy`；

```python
def _noindex_review(args, ctx, settings: _ModelSettings, hooks, policy) -> object:
```

并在其 `run_free_review(...)` 调用（`:268-274`）末尾加 `policy=policy`。

`_run_review_command`（`:280`）在派发前解析一次（两个 arm 共用一条错误路径）：

```python
    policy = _resolve_policy(args)
    result = _ARM_RUNNERS[args.arm](args, ctx, settings, hooks, policy)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 全部 PASS，包括既有的 9 个 CLI 测试。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/cli.py tests/test_cli.py
git commit -m "feat(cli): add --policy-file to the review command"
```

---

### Task 3: `ToolTrace` 保留工具输出正文

**Files:**
- Modify: `code_review_ai/review_loop/schemas.py:28-40`（`ToolTrace`）
- Modify: `code_review_ai/review_loop/loop.py`（新增常量、`_LoopState` :59、`_trace_record` :128、`_reply_call` :319、`run_loop` :514、`run_free_loop` :449）
- Test: `tests/test_review_loop_core.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `loop.TRACE_RESPONSE_EXCERPT_CHARS: int = 2000`
  - `ToolTrace` 新键 `response_excerpt: str`
  - `run_loop(..., trace_response_excerpt_chars: int = TRACE_RESPONSE_EXCERPT_CHARS)`
  - `run_free_loop(..., trace_response_excerpt_chars: int = TRACE_RESPONSE_EXCERPT_CHARS)`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_review_loop_core.py`（该文件已有 `FakeModel` / `_run` / `_candidates` / `_call` 这些辅助，直接复用）：

```python
def test_tool_trace_excerpt_covers_a_short_tool_body_entirely():
    model = FakeModel([("", [_call("echo", {"text": "hi"}, "e-1")]),
                       ("", [_confirm_update("app::run")])])

    result = _run(model, _candidates("app::run"))

    record = result.tool_trace[0]
    assert len(record["response_excerpt"]) == record["response_chars"]


def test_tool_trace_excerpt_is_capped_and_chars_stays_the_full_length():
    """The optimizer needs the body, but a 50-turn run over whole source files
    would balloon the payload unbounded."""
    model = FakeModel([("", [_call("echo", {"text": "x" * 5000}, "e-1")]),
                       ("", [_confirm_update("app::run")])])

    result = _run(model, _candidates("app::run"))

    record = result.tool_trace[0]
    assert len(record["response_excerpt"]) == TRACE_RESPONSE_EXCERPT_CHARS
    assert record["response_chars"] > TRACE_RESPONSE_EXCERPT_CHARS
```

第二轮用 `_confirm_update` 而不是空轮：candidate 决完之后 loop 才会干净收尾，
否则它会走 nudge 分支继续要轮次，把 `FakeModel` 的脚本耗穿。

文件顶部加导入：

```python
from code_review_ai.review_loop.loop import TRACE_RESPONSE_EXCERPT_CHARS
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_review_loop_core.py -v -k "excerpt"`
Expected: 2 个 FAIL —— `KeyError: 'response_excerpt'`（第一个测试）；第二个还会因 `ImportError: cannot import name 'TRACE_RESPONSE_EXCERPT_CHARS'` 在收集阶段失败。

- [ ] **Step 3: 实现**

`schemas.py` 的 `ToolTrace`（`:28-40`）加键，并在 docstring 里说明两者的分工：

```python
class ToolTrace(TypedDict):
    """One auditable disposition of a requested tool call.

    Records are appended in execution order, so list position is the ordinal --
    no explicit sequence number is stored. ``tool_call_id`` is the tool call's
    ``id``, which ``ToolCall`` guarantees is present.

    ``response_chars`` is the returned content's full length; ``response_excerpt``
    the content itself, truncated to the loop's configured cap. Both are kept so
    a consumer can tell "the tool returned 40k chars and here are the first 2000"
    from "the tool returned 1200 chars total".
    """

    tool_call_id: str
    tool: str
    input: object
    status: ToolCallStatus
    response_chars: int
    response_excerpt: str
```

`loop.py` 模块级常量（放在 `MAX_EMPTY_TURNS` 之后，`:56` 一带）：

```python
# How much of a tool's returned content the trace keeps. The optimizer needs to
# see what the agent actually read; unbounded, a 50-turn run over whole source
# files would balloon every payload that carries the trace.
TRACE_RESPONSE_EXCERPT_CHARS = 2000
```

`_LoopState`（`:59`）加字段：

```python
    trace_response_excerpt_chars: int = TRACE_RESPONSE_EXCERPT_CHARS
```

`_trace_record`（`:128`）加参数与键：

```python
def _trace_record(call: ToolCall, tool_call_id: str, status: ToolCallStatus, *,
                  response_chars: int = 0,
                  response_excerpt: str = "") -> ToolTrace:
    return {
        "tool_call_id": tool_call_id,
        "tool": call["name"],
        "input": call["args"],
        "status": status,
        "response_chars": response_chars,
        "response_excerpt": response_excerpt,
    }
```

`_reply_call`（`:319`）填充正文（`content` 就在手边）：

```python
    state.result.tool_trace.append(
        _trace_record(call, tool_call_id, status, response_chars=len(content),
                      response_excerpt=content[:state.trace_response_excerpt_chars]))
```

两个入口函数加参数并传给 `_LoopState`：

```python
def run_free_loop(..., max_total_tokens: int | None = None,
                  trace_response_excerpt_chars: int = TRACE_RESPONSE_EXCERPT_CHARS,
                  ) -> LoopResult:
```

```python
def run_loop(..., max_empty_turns: int = MAX_EMPTY_TURNS,
             trace_response_excerpt_chars: int = TRACE_RESPONSE_EXCERPT_CHARS,
             ) -> LoopResult:
```

各自的 `_LoopState(...)` 构造里加一行
`trace_response_excerpt_chars=trace_response_excerpt_chars,`。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_review_loop_core.py -v`
Expected: 全部 PASS。重点确认既有的 `tool_trace[0]["status"]` 一类断言（`:302`、`:537`、`:682` 等）无回归 —— `response_chars` 语义未变。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/review_loop/schemas.py code_review_ai/review_loop/loop.py tests/test_review_loop_core.py
git commit -m "feat(review): keep a bounded excerpt of each tool output in the trace"
```

---

### Task 4: payload 暴露 `assistant_turns`

**Files:**
- Modify: `code_review_ai/review_loop/payload.py:10-31`（`loop_result_payload`）
- Test: `tests/test_review_loop_payload.py`（新建 —— `payload.py` 目前没有测试文件）

**Interfaces:**
- Consumes: `LoopResult.assistant_turns`（`schemas.py:183`，已存在）
- Produces: payload 新键 `assistant_turns: list[dict]`，每项键为 `turn` / `content` / `reasoning` / `tool_calls`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_review_loop_payload.py`：

```python
"""Tests for the review command's JSON payload contract (review_loop.payload)."""

from __future__ import annotations

from code_review_ai.review_loop.payload import loop_result_payload
from code_review_ai.review_loop.schemas import AssistantTurn, LoopResult


def test_payload_carries_every_assistant_turn():
    """The per-turn reasoning is the only way an out-of-process consumer (the
    SkillOpt optimizer) can see *why* the agent missed a defect, rather than
    only that it missed one."""
    result = LoopResult(assistant_turns=[
        AssistantTurn(turn=1, content="", reasoning="check callers first",
                      tool_calls=["read_file"]),
        AssistantTurn(turn=2, content="no regression", reasoning=None, tool_calls=[]),
    ])

    payload = loop_result_payload(result, "fake-model")

    assert payload["assistant_turns"] == [
        {"turn": 1, "content": "", "reasoning": "check callers first",
         "tool_calls": ["read_file"]},
        {"turn": 2, "content": "no regression", "reasoning": None,
         "tool_calls": []},
    ]


def test_payload_assistant_turns_is_empty_without_turns():
    payload = loop_result_payload(LoopResult(), None)

    assert payload["assistant_turns"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_review_loop_payload.py -v`
Expected: 2 个 FAIL —— `KeyError: 'assistant_turns'`。

- [ ] **Step 3: 实现**

在 `loop_result_payload` 的返回 dict 里（`payload.py:24` 的 `"tool_trace"` 之后）加一行：

```python
        "assistant_turns": [turn.model_dump() for turn in result.assistant_turns],
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_review_loop_payload.py -v`
Expected: 2 个 PASS。

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/review_loop/payload.py tests/test_review_loop_payload.py
git commit -m "feat(review): expose assistant turns in the payload"
```

---

## 收尾验证

- [ ] 全量测试：`uv run pytest` —— 必须全绿。
- [ ] 手动跑一次真实 review，确认 `--policy-file` 端到端生效：

```bash
printf '# 测试 policy\n你是一个只读代码评审 Agent。\n' > /tmp/policy.md
uv run code-review-ai review --repo . --db .code-review-ai/index.db \
    --policy-file /tmp/policy.md --no-progress
```

确认输出的 JSON 里 `assistant_turns` 非空，且 `tool_trace` 的每一项都带 `response_excerpt`。
