# Harness skill 复盘：实现计划

实现 `docs/harness-skill-review.md`（下称 spec）。spec 定的是意图与形状，这份定**落点、顺序与签名**——包括 spec 的改动清单没写到、但不解决就跑不起来的四处。

## 已确认的决定

| # | 决定 | 取值 |
|---|---|---|
| 1 | 复盘时机 | 父 run 结束后、`run_review` 返回前；同进程、同步，第二次 `run_loop` |
| 2 | 哪些 run 复盘 | **全部**（`trigger` 不设）。交卷的也复盘——路径本身可以优化，不只救火 |
| 3 | 复盘模型 | 复用父 run 手上那个 `model` 对象；`--skill-review-model` 给了才 `create_model` |
| 4 | 复盘预算 | `max_turns=6`，不传 `max_total_tokens` |
| 5 | 复盘失败 | 不写文件、`run_skill_review` 返回 `""`、父 run 的 payload/failure_reason/退出码不变 |
| 6 | 候选落点 | `--skill-review <目录>`，文件名走时间戳：`2026-09-14T20-31-05__skill.md` |
| 7 | 事实块 | **不附**。严格按 spec：父消息原样 + 纯指令 |
| 8 | 进度区分 | hook context 加 `phase`（`"review"` / `"skill_review"`） |
| 9 | harness skill 注入 | 新增 `--harness-skill <path>` → 第 9 节 |
| 10 | 父消息出口 | `LoopResult` 新增 `messages` 字段 → 第 1 节 |

「默认全关」不变：不传 `--skill-review`，这条链路一行都不执行。

## spec 没写到的四处

### 1. 父消息没有出口

spec 的地基是「父 agent 的消息一条不改，原样重放」，但 `run_loop` 只返回 `LoopResult`，而它现在**没有消息列表**：

- `assistant_turns` 只有 `content`/`reasoning`/工具**名**——没有 tool_call id、没有 args；
- `tool_trace.response_excerpt` 是**截断到 2000 字**的（`TRACE_RESPONSE_EXCERPT_CHARS`），全文只在 `state.messages` 里。

全文没丢，但也没有出口：`state.messages` 是 `run_loop` 的局部变量，随 `_LoopState` 一起被丢掉。

**改**：`LoopResult` 加 `messages: list[BaseMessage] = field(default_factory=list)`，由 `_settle_result` 赋值（`state.result.messages = state.messages`）。run 已结束，交出去的是同一个 list 引用；复盘那边 `list(parent_messages) + [指令]` 本来就复制，不必 deepcopy。

### 2. 第二条 system 消息没有注入通道

`build_initial_messages` 现在只产出 `[SystemMessage(policy or _POLICY), HumanMessage(...)]`——**只有一条 system 消息**。spec 的复盘指令指向「harness skill（第二条 system 消息）」，但那条消息在父 run 里根本不存在，cli.py 的改动清单里也没有注入它的开关。

**改**：见第 4、6 节。注意这是**唯一**会碰到父 run 请求形状的改动。

### 3. 终止工具的 payload 落在哪（`apply` 可以不要）

spec 说「ToolSpec 加 terminates / apply」。实现时发现 **`apply` 不需要**。

`finish_review` 唯一真正特殊的地方是**它要结束 run**，执行侧的其余部分（校验 payload、回一条 ToolMessage）loop 本来就会做——「它不能被执行」只是说 `run` 是个 `raise AssertionError` 的占位，而它作为工具、带 schema、早就提供给模型了。所以唯一需要的新概念是「这个工具会结束 run」；payload 落点用一个**通用槽位**就够，loop 不必认识任何一种 payload 的形状：

- `ToolSpec` 只加 `terminates: bool`；
- `LoopResult` 加 `submission: BaseModel | None`——谁来都落这里；
- `LoopResult.findings` 从字段变成 property（`submission` 是 `ReviewSubmission` 时返回它的 findings）。

**代价**：终止工具回给模型的话从 `{"accepted": true, "findings": 3}` 变薄成 `{"accepted": true}`；`review_complete` 这个名字开始偏——它现在的意思是「有终止工具被接受」，复盘那次 run 里也会是 `True`，要更准可以改名 `submitted`（`payload.py` 输出的字段名不用跟着变）。已核对：仓库里没有任何 `LoopResult(findings=...)` 的构造，读 `.findings` 的地方（`payload.py` + 5 处测试断言）一处都不用改。

### 4. 白名单与终止派发的顺序

循环体现在第一句是 `if call["name"] == FINISH_REVIEW_TOOL`。泛化之后，顺序必须是：

```
白名单校验 → spec.terminates ? 终止派发 : _execute_call
```

反过来（先判 `terminates`）的话，复盘 agent 调 `finish_review` 会**直接把 run 结束掉**——`finish_review` 仍然是绑定且 `terminates=True` 的——白名单形同虚设。

## 改动顺序

### 1. `schemas.py`

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_schema: type[BaseModel]
    run: Callable[..., str]
    terminates: bool = False        # 唯一的新字段：这个工具会结束 run
```

`LoopResult` 加两个字段，并把 `findings` 从字段改成 property：

```python
    submission: BaseModel | None = None      # 通用槽位：终止工具交上来的 payload
    messages: list[BaseMessage] = field(default_factory=list)   # 完整过程（见 §1）
    skill_review: "LoopResult | None" = None # 复盘那一次 run 自己的结果（runner 填）

    @property
    def findings(self) -> list[Finding]:
        payload = self.submission
        return list(payload.findings) if isinstance(payload, ReviewSubmission) else []
```

没有 `TerminationRejected`——`apply` 都不要了，也就没有「拒绝」这个动作；校验失败就是一条 error ToolMessage。

`skill_review` 存的是**内层 `LoopResult`** 而不是散字段：内层的 `usage`/`cost`/`failure_reason`/`submission` 都已经有形状，payload 与 bench 直接读，schemas 不必新增类型。

### 2. `loop.py`

`_validate_args` 改为回「已校验的模型」（`_execute_call` 自己 dump）：

```python
def _validate_args(spec, call) -> tuple[BaseModel | None, str | None]:
    try:
        validated = spec.args_schema.model_validate(call["args"])
    except ValidationError as exc:
        return None, _error_content(
            "error",
            f"tool arguments do not match the allowed schema "
            f"({exc.error_count()} validation error(s))")
    return validated, None
```

新增三个小函数：

```python
def _allowed(state: _LoopState, name: str) -> bool:
    return state.allowed_tools is None or name in state.allowed_tools

def _refuse_call(state: _LoopState, call: ToolCall, name: str) -> None:
    allowed = ", ".join(sorted(state.allowed_tools or ()))
    _reply_call(state, call, name, _error_content(
        "error", f"tool {name!r} is not permitted in this run; allowed: {allowed}"),
        "error")

def _apply_terminating(state: _LoopState, call: ToolCall, spec: ToolSpec) -> bool:
    """结束 run 的工具：校验 payload、回一条、把它落进通用槽位。

    没有 apply 回调，也没有「拒绝」——校验失败就是一条 error ToolMessage、run 继续，
    对称原来 finish_review 的重试语义。落点是 result 上那个通用槽位，所以这里不认
    识任何具体 payload 的形状。
    """
    validated, rejection = _validate_args(spec, call)
    if rejection is not None:
        _reply_call(state, call, spec.name, rejection, "error")
        return False
    _reply_call(state, call, spec.name, json.dumps({"accepted": True}), "success")
    state.result.submission = validated
    state.result.review_complete = True
    return True
```

`_apply_finish` 整个删除。循环体改成：

```python
        for call in calls:
            name = call["name"]
            if not _allowed(state, name):            # ① 白名单在前（§4）
                _refuse_call(state, call, name)
                continue
            spec = state.tool_map.get(name)
            if spec is not None and spec.terminates:  # ② 终止派发
                if _apply_terminating(state, call, spec):
                    submitted = True
                    break
                continue
            _execute_call(state, call)                # ③ 普通执行
```

`run_loop` 新增两个参数：

```python
    allowed_tools: Collection[str] | None = None,   # None = 不限制（父 run 行为不变）
    phase: str = "review",                          # 进 hook context，见 §8
```

建 state 前的前置校验也不用加了——`apply` 没了，就没有「声明了终止却没给实现」这种非法状态。

`_LoopState` 加 `allowed_tools` / `phase` 两个字段；`emit` 带上 phase：

```python
    def emit(self, point: str, **context: object) -> None:
        self.hooks.emit(point, phase=self.phase, **context)
```

`_settle_result` 末尾加 `state.result.messages = state.messages`。

### 3. `tools.py`

`finish_review_tool` 只多一个字段；`_accept_finish` 不用写，落点是 loop 那个通用槽位：

```python
def finish_review_tool() -> ToolSpec:
    def _handled(*_args, **_kwargs) -> str:
        raise AssertionError("finish_review is applied by the loop, never run")
    return ToolSpec(name=FINISH_REVIEW_TOOL, description=..., args_schema=ReviewSubmission,
                    run=_handled, terminates=True)
```

**行为变化（测试会红）**：schema 校验失败的文案从 `invalid finish_review payload: ...` 变成通用的 `tool arguments do not match the allowed schema (N validation error(s))`——`tests/test_review_loop_core.py:537` 的断言要跟着改；`:118` 那个自建的 `ToolSpec(FINISH_REVIEW_TOOL, ...)` 必须补 `terminates=True`，否则整个提交路径的测试全废。

### 4. `runner.py`

```python
def build_initial_messages(prompt, diff, policy=None, summary=None,
                           harness_skill=None) -> list[BaseMessage]:
    head = f"{prompt}\n\n"
    if summary:
        head += f"CHANGE SUMMARY\n{summary}\n\n"
    messages = [SystemMessage(content=policy or _POLICY)]
    if harness_skill:                                   # 第二条 system 消息
        messages.append(SystemMessage(content=harness_skill))
    messages.append(HumanMessage(content=f"{head}DIFF\n{diff or '(no working-tree diff)'}"))
    return messages
```

`run_review` 新增两个参数并接线：

```python
def run_review(..., harness_skill: str | None = None,
               skill_review: SkillReview | None = None) -> LoopResult:
    ...
    messages = build_initial_messages(prompt, diff, policy=policy, summary=summary,
                                      harness_skill=harness_skill)
    tools = [*_repo_tools(config, conn, tool_names), finish_review_tool()]
    result = run_loop(model, tools, initial_messages=messages, hooks=hooks,
                      max_turns=..., max_total_tokens=max_total_tokens)
    result.cost = compute_cost(result.usage)
    if skill_review is not None:
        _review_harness_skill(config, skill_review, result=result, tools=tools,
                              parent_model=model, base_url=base_url,
                              api_key_env=api_key_env)
    return result
```

复盘放在 runner 而不是 CLI，理由只有一个：**只有这一层同时握着 `messages` 和绑好的 `tools`**（`_repo_tools` 是 runner 私有的）。

```python
def _review_harness_skill(config, skill_review, *, result, tools, parent_model,
                          base_url, api_key_env) -> None:
    if not result.messages:
        return
    model = parent_model
    if skill_review.model:
        model = create_model(config, model_name=skill_review.model,
                             base_url=base_url, api_key_env=api_key_env)
    run_skill_review(model, result, tools, out_dir=skill_review.out_dir,
                     max_turns=skill_review.max_turns)
```

`run_review` 顶部加一次配置校验，挡住库调用方走错路（CLI 侧另有一道，见 §6）：

```python
    if skill_review is not None and not harness_skill:
        raise ValueError("skill_review needs harness_skill: 没有第二条 system 消息，"
                         "复盘 agent 无从指出「哪些措辞」")
```

### 5. `skill_review.py`（新增）

```python
"""复盘 agent：把一次 run 的完整过程交给它，产出改过的 harness skill 全文。

可选功能，默认关闭（不传 --skill-review 就一行都不执行）。它只产**候选**：写到
out_dir 就结束，不覆盖任何生效中的 skill。「候选是否该被采纳」是另一件事，
接口位置留在 SkillReview.accept（不实现、不调用）。

两个结构性约束：
- 工具绑全套（父 agent 的工具 + submit_skill），历史里引用过的 tool_call 全对得上；
- 运行时白名单只有 read_file / submit_skill —— 复盘 agent 结构性不能搜。它诊断的是
  「父 agent 在别处瞎搜」，那个失败模式不能在它身上重演。
"""

SKILL_REVIEW_MAX_TURNS = 6
SUBMIT_SKILL_TOOL = "submit_skill"
ALLOWED_TOOLS = frozenset({"read_file", SUBMIT_SKILL_TOOL})

SKILL_REVIEW_INSTRUCTION = """......（全文见实现；见下方注）......"""

class SkillSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill: str = Field(min_length=1)                    # 改好后的全文
    changes: list[str] = Field(default_factory=list)    # 每条：改了哪句 + 轨迹里的依据

def submit_skill_tool() -> ToolSpec:
    """终止工具，与 finish_review_tool() 同一个机制，只是 payload 不同。"""
    def _handled(*_args, **_kwargs) -> str:
        raise AssertionError("submit_skill is applied by the loop, never run")
    return ToolSpec(name=SUBMIT_SKILL_TOOL, description="提交改好的 harness skill 全文。",
                    args_schema=SkillSubmission, run=_handled, terminates=True)

@dataclass(frozen=True)
class SkillReview:
    out_dir: Path
    model: str | None = None          # None = 父 run 那个 model 对象
    max_turns: int = SKILL_REVIEW_MAX_TURNS
    trigger: Callable[[LoopResult], bool] | None = None   # 现值 None：全部都复盘
    accept: Callable[[str, str], bool] | None = None      # 预留闸门，不实现、不调用

def run_skill_review(model, parent_result, parent_tools, *, out_dir,
                     max_turns=SKILL_REVIEW_MAX_TURNS) -> str:
    if parent_result is None or not parent_result.messages:
        return ""
    messages = list(parent_result.messages) + [HumanMessage(SKILL_REVIEW_INSTRUCTION)]
    tools = [*parent_tools, submit_skill_tool()]
    result = run_loop(model, tools, initial_messages=messages,
                      allowed_tools=ALLOWED_TOOLS, max_turns=max_turns,
                      phase="skill_review")
    result.cost = compute_cost(result.usage)
    parent_result.skill_review = result          # payload/bench 靠它分开算成本
    submission = result.submission
    if not isinstance(submission, SkillSubmission):
        return ""                                # 没交卷：不写文件
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / _timestamped_name()).write_text(submission.skill, encoding="utf-8")
    return submission.skill
```

### 指令全文的落点

`SKILL_REVIEW_INSTRUCTION` 的全文（含每句为什么这么写）在
`code_review_ai/review_loop/skill_review.py`，**以那里为准**。这份计划早期把全文抄在这里，
实现时改了强调记号与折行就漂了一次——常量抄进文档就会这样，所以只留指针。

它在设计上要说清的几件事（细节见实现处的注释与提交信息）：

- **目的**：决定 #2 让交卷的 run 也复盘，而「这条路径值不值得」需要一个判据；不给判据，它会
  按「找得更多 = 更好」去改，方向正好相反。
- **「过程有问题」的可观察形状**：决定 #7 去掉了事实块，它不知道这次跑得好不好，只能从轨迹推；
  不定义「问题」，产出必然是「建议增加严谨性」这类废话。
- **允许原样提交**：一个「每次都必须改点什么」的优化器会单调退化这条 skill。
- **落到具体句子 + 因果**：闸门接上之前看候选的是人，这是候选可被判断的唯一依据。
- **禁止加长 / 禁止通用最佳实践**：实测就是一行规则赢了原本那一整套措辞。
- **禁止要求 loop 改**：成因 #1（loop 从不告诉模型预算）不是 skill 能修的。

### 候选文件名

时间戳格式 `%Y-%m-%dT%H-%M-%S`（Windows 文件名不能带冒号，用 `-`）。时间戳保证互不覆盖；
**看不出是哪条 case**，8 条跑完对比候选时靠时间顺序对齐（要带 case id 得由 eval 侧把标识传进来，
CLI 不知道 case 是什么，暂不做——见「批跑」一节）。

`reasoning_content` 白拿这条依赖自带成立：父消息的 `AIMessage.additional_kwargs["reasoning_content"]` 是同一批对象，`ReasoningChatModelMixin` 只对「带 tool_calls 的 assistant」回显，而父 agent 的空轮不落历史、落历史的每条都带 tool_calls。

`hooks` **透传父 run 的**（实现时改的，见「实现记录」）：不传的话 `phase` 就没有任何观察者，
第 8 条决定会退化成一个没人读的字段。透传之后 CLI 的进度行、eval 的 timeline 都能把两段分开。

### 6. `cli.py`

新增三个参数：

```python
review.add_argument("--harness-skill",
                    help="注入为第二条 system 消息的 harness skill 文件")
review.add_argument("--skill-review", dest="skill_review",
                    help="复盘候选写到这个目录（每个 run 一个时间戳文件）")
review.add_argument("--skill-review-model", help="复盘 agent 的模型（默认同父 agent）")
```

- `--harness-skill` 的路径校验照抄 `_resolve_policy` 那一套（空路径 / 不存在 / 不可读 / 空文件 → 全部 `ValueError` → exit 2）。理由与 `--policy-file` 一模一样：静默回落会让「skill 没生效」看起来像「skill 没用」。
- **注入前剥掉 frontmatter**：bundled 的走 `skills.load_skill_body`（它本来就干这个），外部文件用同一个 `_FRONTMATTER_RE`。否则第二条 system 消息开头是一段 YAML——既是噪音，也让复盘指令里「改好后的全文」这个词变得含糊（它看到的「全文」到底含不含那段 YAML）。剥掉之后，复盘产出的候选也是无 frontmatter 的正文，可以直接回喂给 `--harness-skill`。
- `--skill-review` 不带 `--harness-skill` → exit 2（配置错误，不是空操作），理由同 `--summary` 在 nograph 臂上被拒。
- `_run_review_command` 里构造 `SkillReview(out_dir=Path(args.skill_review), model=args.skill_review_model)`，透传给两个 arm runner。
- 复盘开始/结束各打一行 stderr（`[review] 开始复盘…` / `[review] 复盘完成：写过 N 字 / 没交卷`），并给 `_format_progress` 加 phase 前缀：`phase == "skill_review"` 时输出 `复盘：...`。

### 7. `payload.py`

`loop_result_payload` 增加一块，让 bench 能把两段成本分开：

```python
        "skill_review": _skill_review(result.skill_review),
```

```python
def _skill_review(inner) -> dict | None:
    """复盘那一次 run 的产出与用量；没跑就是 None。"""
    if inner is None:
        return None
    submission = inner.submission
    return {"chars": len(getattr(submission, "skill", "") or ""),
            "review_complete": inner.review_complete,
            "failure_reason": inner.failure_reason,
            "turn_count": len(inner.assistant_turns),
            "cost": inner.cost,
            "usage": {"input_tokens": _token_count(inner.usage, "input_tokens"),
                      "output_tokens": _token_count(inner.usage, "output_tokens"),
                      "cache_read_input_tokens": _token_count(inner.usage, "cache_read")},
            "tool_calls": [record["tool"] for record in inner.tool_trace]}
```

### 8. `__init__.py`

导出 `SkillReview`、`run_skill_review`、`submit_skill_tool`、`SUBMIT_SKILL_TOOL`（与现有导出同一风格）。

### 9. 种子 harness skill（已落地）

`code_review_ai/skills/code-review-harness/SKILL.md` 已存在，是这条链路第一次跑起来时要注入的那份全文（`--harness-skill` 指向它）。它**故意没有**进 `installer.SKILL_NAMES`：它写着「评审 loop 有轮数上限」，部署给交互式会话是错的。要不要部署另议。

## 测试

`tests/test_review_loop_core.py`（改 + 增）

- 改 `:118`（自建 finish_review 补 `terminates=True`）与 `:537`（校验失败文案）。
- 新增：自定义终止工具（`terminates=True` → payload 落进 `result.submission`、run 停；payload 校验失败 → 回 error、run 继续）。
- 新增：`allowed_tools` 拦住一个**已绑定**的工具 → 那条 ToolMessage 是 error，run 继续。
- 新增：**白名单里的终止工具能结束 run；白名单外的终止工具不能**（§4 的回归防线）。
- 新增：`LoopResult.findings` 在 `submission` 是 `SkillSubmission`（或 None）时是空列表。
- 新增：`phase` 出现在每个事件的 context 里。

`tests/test_review_loop_skill_review.py`（新增）

- 复盘模型看到的 system/human/tool 消息与父 run **逐条同一**（含 `reasoning_content` 与 tool_call id）；末尾是那条指令。
- 绑定的是父工具集 + `submit_skill`；`read_file` 之外的调用回 error 且**不执行**。
- `submit_skill` 提交 → 文件落在 `out_dir/<时间戳>__skill.md`，内容与 payload 一致。
- 没交卷 → 不写文件、返回 `""`、`parent_result.skill_review` 仍挂着内层结果。
- `accept` 不被调用。

`tests/test_review_loop_runner.py`

- 传 `harness_skill` → 系统消息变成两条，顺序是「政策, harness, human」。
- `skill_review` 且无 `harness_skill` → `ValueError`。
- 复盘跑完后 `result.skill_review` 非 None，父 run 的 `findings`/`failure_reason`/`cost` 一个没变。

`tests/test_review_loop_payload.py` / `tests/test_cli.py`

- payload 里的 `skill_review` 块；没跑复盘的 run 是 `None`。
- 三个新 flag：`--harness-skill` 的四类坏输入 → exit 2；`--skill-review` 不带 `--harness-skill` → exit 2。

## eval 接线（验证用，非本功能的一部分）

`benchmarks/run_field_contract_case.py` 的 `_review_command` 要能带上 `--harness-skill` 与 `--skill-review`，否则这条链路在 8 条 case 上一次也跑不到。注意 8 条串行跑 = 8 个候选文件（时间戳命名），且**每条 case 多一次 4-5 万 token 的调用**（无缓存，system 与工具集都与父 agent 不同，前缀无一段重合）。这块附加成本必须用 §7 的 `skill_review.usage` 单独报，别混进「graph 臂 vs native」的对比里。

### 批跑：8 条 case 用同一份基线，还是串成链

**v1 固定基线。** 8 条 case 的父 run 都在同一份 `--harness-skill` 下跑，产出 8 份**对同一基线**的独立诊断。这不是疏漏，是可比性的前提：一旦第 2 条用上第 1 条的候选，第 2 条的父 run 就与第 1 条不同条件，8 条分数不再能回答「换了 skill，召回从哪变到哪」。

**链式（自举）不需要改本功能的代码**——「用哪份 skill」是调用方的参数：

```python
skill = BASELINE
for case in cases:
    run(case, harness_skill=skill, skill_review=out_dir)
    skill = newest_file_in(out_dir)   # 这一行就是「链」；accept 闸门守在这里
```

但链式留到闸门接上之后，理由有两条，且第一条是反直觉的：

1. **最可能产出坏候选的，恰恰是最该复盘的那些 run。** 跑飞的那 4 条轨迹最乱、复盘 agent 拿到的证据最少，候选质量最可疑。没有闸门的链式 = 把最可疑的候选无条件喂给下一轮，错误沿链放大。这正是 spec 把「采纳」划成非目标的原因。
2. **归因会失效。** 第 5 条的复盘看到的是「被前 4 条候选改过的世界」，它诊断的病因可能早被第 3 条候选修掉了，而它不知道。

两条前提条件，做链式之前必须补上：

- `accept(candidate, baseline) -> bool` 至少有一个「候选不比基线差」的判据（否则链就是无条件接受）；
- `--skill-review` 要能接一个名字前缀（`--skill-review-tag <case-id>`，文件名 `<tag>__<时间戳>.md`）。时间戳能防互相覆盖，但**说不出第 N 份候选是在第 N 条 case 上产的**——链式下这是必需信息，而 CLI 不知道 case 是什么，标签必须由 eval 侧传进来。v1 不实现。

## Prompt cache：能复用多少，值不值得管

spec 的成本一节把「没有缓存」列进已知风险，并自标「估算，未实测」。实测前先把**结构上**能复用多少说清楚——结论是不能完全复用，而且**不该为它改动父 run 的形状**。

### 能命中的只有「父 run 最后一次请求」那一段

父 run 的最后一次请求，是在最后一轮的消息**被生成之前**发出的（`_model_turn` 里那次 `invoke(state.messages)`）。那一轮的 assistant 与工具回复是**这次请求的输出**，进列表后 run 就结束了——不会再有下一次请求把它们带出去。所以：

- 父最后一次请求携带的整段 → 复盘的完美前缀，**有缓存资格**（`list(parent_messages) + [指令]` 逐字节相同）；
- 最后一轮的 assistant（含 `reasoning_content`）+ 它的工具回复 → 从来没作为输入发出去过，**必然 miss**；
- 指令 → 必然 miss。

miss 的大小取决于父 run **怎么结束**：

| 父 run 怎么结束 | 复盘比父最后一次请求多带的 | miss 量级 |
|---|---|---|
| 交卷（`finish_review`） | 最后一轮 assistant + `{"accepted": true, "findings": N}` + 指令 | 几百 token |
| 撞 `max_turns` | 最后一轮 assistant（含 reasoning）+ **那一轮全部工具返回全文** + 指令 | 上千 ~ 上万（单次 `read_file` 最多 60k 字符） |
| 超 token 预算 | 只有指令 | 最小——预算检查在 `state.messages.append(response)` **之前**，历史与那次请求逐字节相同 |
| provider 挂 | 只有指令 | 最小（同上，`_model_turn` 返回 `None` 即 break） |

第二行是个不顺的巧合：**最需要复盘的那 4 条（跑飞、烧满 25 轮），恰恰是 miss 最多的 4 条**——「它最后在干什么」正好发生在缓存覆盖不到的那一段里。

对 spec 成本估算的修正：「末轮完整前缀约 4-5 万 token」是**下界**。真实输入 = 4-5 万（唯一有缓存资格的部分）+ 最后一轮新增（0 ~ 上万，看它怎么结束）+ 指令（几百）。

### 另一个未知量：tools 进不进前缀

父 run 与复盘的工具集差一个 `submit_skill`（已压到最小：父工具集原样 + 追加在末尾）。若提供方把 tools 纳入前缀且排在 messages 之前，这一个工具就让**后面所有 message token 全部失配**（零命中）；若 tools 单独结算，父历史那 4-5 万基本全命中。**这一条只能实测。**

### 量级：这条在钱上最多值 5 角

按 `pricing.py` 的档位，一个 4.5 万输入 token 的复盘请求：

| | 每条 case | 8 条 case |
|---|---|---|
| 全不命中（1.5 元/M） | 0.068 元 | 0.54 元 |
| 全命中（0.05 元/M） | 0.002 元 | 0.018 元 |

复盘请求的输入量 = 父 run 末轮的量级，所以**复盘就是父 run 再多跑一轮**，成本也是一轮的成本；父 run 自己 25 轮累计 598,689 输入 token（全不命中 0.9 元），复盘最多是它的 7.5%。

### 实测：零新代码

读 `result.skill_review.usage.cache_read_input_tokens` 即可——`_accumulate_usage` 早就在累积 `input_token_details.cache_read`，`pricing.py` 也早按 0.05 / 1.5 分开计价，只是此前没有第二个调用方读过它。三个对照足以定性：

1. 绑完整父工具集 + `submit_skill`（当前设计）
2. 只绑父工具集（不追加）
3. 不绑任何工具

三者 `cache_read` 一比，tools 进不进前缀立刻见分晓。**实现完之后第一件该做的事**——它同时回答「这个功能值不值 8 次调用」这个 spec 唯一没量过的成本问题。

### 结论：不为缓存动父 run

理论上有一条路能让前缀对齐：让父 run 也声明同一个 `submit_skill`（同名、同 description、同 schema，但注册成只回「本轮不可用」的 stub），且**只在开启复盘时**这么绑——这样基线 run 一个字节都不变。但按上面的账，为 5 角钱去改父 run 在这个开关下的请求形状（并让它与手上 8 条 case 的历史数据不可比），不划算。**不做。**

## 实现记录

已按本计划落地（`harden/review-agent-guardrails`）。与计划的差异如下，都是实现时才看清的：

| 差异 | 为什么 |
|---|---|
| 复盘**透传**父 run 的 `hooks`（计划写的是「空注册」） | 不传的话 `phase` 没有任何观察者，第 8 条决定等于没做。透传后 CLI 的进度行与 eval 的 timeline 才分得开两段 |
| `SkillSubmission` 采纳了 `changes`（计划里标「待定」） | spec 自己写的是「让它**指出**哪些措辞」，只收全文的话「指出」就丢了；而候选文件里只写 `skill`（保证能直接当 `--harness-skill` 用），理由只能走 payload / stderr |
| `failure_reason` 的文案改成通用的 `agent stopped without submitting` | 原来写的是 `... without submitting finish_review`。loop 已经不认任何工具名了，文案里留一个名字就把刚拆掉的耦合又写了回来。`tests/test_review_loop_core.py` 两处断言跟着改 |
| `skills.strip_frontmatter` 提成公开函数，正则容忍 CRLF | CLI 要剥外部文件的 frontmatter，而 `\r?\n` 是必须的：Windows 检出会把 bundled skill 写成 CRLF，原正则匹配不上——而匹配不上不报错，它会把 YAML 原样送进 prompt |
| 三处 docstring（`loop.py` / `schemas.py` 模块头、`AssistantTurn`）跟着改 | 它们把「结束于 finish_review」写成了事实，而现在的契约是「结束于任何带 `terminates` 的工具」 |

新增测试 23 条：`test_review_loop_core.py`（终止契约泛化、白名单先于派发、`phase`）、
`test_review_loop_skill_review.py`（新文件：重放逐条同一、白名单、落盘、没交卷、trigger 拒绝、
phase 标签）、`test_review_loop_runner.py`（第二个 system 消息、`skill_review` 需要
`harness_skill`、两段成本分开、`accept` 从不被调用）、`test_review_loop_payload.py` 与
`test_cli.py`（payload 的 `skill_review` 块、三个 flag 的四类坏输入）。

**没做的一步**：两条连续 system 消息的真实请求冒烟测试（本机没有 `.env`，也没有
`OPENAI_API_KEY`）。这是整个设计的承重点，跑第一条 case 之前必须验一次。

## 不做的事

- **不做候选的采纳/否决**。`SkillReview.accept` 只留位置。
- **不做中途触发**。复盘产出是给后续 rollout 用的，父 run 看不到它；中途插入还要破 `hooks.py` 的 observer-only 约定。
- **不改默认行为**。不传 flag，父 run 的请求形状、工具集、行为与今天完全一致。
- **不与 SkillOpt 耦合**。不 import、不读它的目录；产出就是一个文件。

## 代价与已知风险

- **复盘输入 ~4-5 万 token 起，且只有一部分有缓存资格**。见「Prompt cache」一节：4-5 万是**下界**（还要加上父 run 最后一轮的新增），「全部都复盘」= 每 run 一笔附加成本，全批次最多 5 角。实现后第一件事是量它，不是优化它。
- **盘点预算靠轮数**（`max_turns=6`）。没交卷就是不写文件，没有重试。
- **候选无人验证**。默认只写文件；在闸门接上之前必须由人看。
- **n=1**。spec 里所有召回/步数数字都是每条 case 单次运行，没有重复样本；这份计划不改变这一点。
- **两条连续 system 消息没有实测过**。DeepSeek 走 OpenAI 兼容协议，多条 system 通常接受，但仓库里没有先例——实现时第一步就该发一次真实请求验证，别等 8 条 case 跑完才发现 400。
