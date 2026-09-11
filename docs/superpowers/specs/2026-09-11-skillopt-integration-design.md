# SkillOpt 集成设计 —— 以 review policy 为优化对象

> 日期：2026-09-11
> 状态：待评审
> 分支：`harden/review-agent-guardrails`

## 1. 背景与目标

`code-review-ai` 的 review loop 把一份 policy（`_POLICY`）作为 system prompt 注入，
policy 的内容直接决定 agent 怎么查、查什么、何时判 confirmed/dismissed。这份 policy
目前是 `runner.py` 里的硬编码常量，无法被系统性地改进。

SkillOpt 是一个 skill 优化框架：它跑一批任务、按得分反思、改写一份 markdown 文档，
再验证改动是否让得分上升。把 review policy 交给它，就能用真实用例的命中率来驱动
policy 的迭代，而不是靠人拍脑袋改措辞。

**本设计的目标**：打通 `code-review-ai` → SkillOpt 的集成通路，让 policy 可注入、
过程可观测、得分可计算。

**非目标**：改动 review loop 的算法、改变 worksheet 模型、优化 graph 查询本身。

## 2. 已核实的现状

以下事实经读码核实，带位置。

| 事实 | 位置 |
|---|---|
| policy 作为 system prompt 注入，硬编码 | `review_loop/runner.py:184`（`_POLICY`）、`:282`（`_FREE_POLICY`） |
| 两个循环入口 | `review_loop/loop.py:514`（`run_loop`，worksheet）、`:449`（`run_free_loop`） |
| `ToolTrace` 只存字符数，不存正文 | `review_loop/schemas.py:28-40`（`tool_call_id, tool, input, status, response_chars`） |
| `AssistantTurn` 有完整 reasoning | `review_loop/schemas.py:102-117`（`turn, content, reasoning, tool_calls`） |
| `LoopResult` 已收集 `assistant_turns` | `review_loop/schemas.py:183` |
| 但 CLI payload **丢弃** `assistant_turns` | `review_loop/payload.py:10-31`（输出字段清单里没有它） |
| 用例 manifest：21 个 bug-injection 用例 | `benchmarks/case-backend-cases.json` |
| 打分是二值：finding 落在 gold fix site 即命中 | `benchmarks/eval_cases.py:146-156` |
| 每个用例恰好 1 个 root cause | 实测 `{1: 21}` |
| gold 带 `mechanism_terms`（21/21）与 `min_matches`，但加载器忽略 | `eval_cases.py:112-137` 只读 `id`/`fix_file`/`alternate_files` |
| CLI `review` 的 JSON 走 stdout，进度走 stderr | `cli.py:194-198` |
| 用例 materialize：scratch 拷贝 + apply patch + 建索引 | `benchmarks/review_loop_case_compare.py:101` |
| scratch repo 需 `CRAI_DIFF_BASE=HEAD` | `review_loop_case_compare.py:81` |

**结论**：数据集、打分器、trace 采集三样都已存在。缺口是「policy 不可注入」和
「过程信息到不了优化器」。

## 3. 工作流 A：`code-review-ai` 侧改动

三项改动，均为**默认行为不变**的增量修改。

### A1 — system prompt 参数化

**接口**

```python
# review_loop/loop.py
def run_loop(..., policy: str | None = None) -> LoopResult
def run_free_loop(..., policy: str | None = None) -> LoopResult
```

`policy` 为 `None` 时回落各 arm 的内置常量（`_POLICY` / `_FREE_POLICY`），
非 `None` 时以其内容作为 system message。

**链路**：`cli.py` → `runner.py` → `loop.py`。具体：

- `build_initial_messages()`（`runner.py:154`）增加 `policy` 参数，`:184` 处
  `SystemMessage(content=_POLICY)` 改为 `SystemMessage(content=policy or _POLICY)`
- free arm 的 `runner.py:282` 同理回落 `_FREE_POLICY`
- `cli.py:81` 的 `review` 子命令新增：

```
--policy-file PATH   # 读该 markdown 文件作为 policy；缺省用内置
```

**行为**：`--policy-file` 覆盖当前 arm 的内置 policy（graph arm 覆盖 `_POLICY`，
nograph arm 覆盖 `_FREE_POLICY`）。文件不存在时以非零码退出并给出明确错误，
不静默回落——静默回落会让 SkillOpt 以为注入了新 policy 而实际跑的是旧的内置版。

**测试**：`tests/` 下新增——给定 `policy` 时 system message 内容为给定值；
不给定为内置常量；`--policy-file` 指向不存在路径时退出码非零。

### A2 — `ToolTrace` 保留工具输出正文

**接口**（`review_loop/schemas.py`）

```python
class ToolTrace(TypedDict):
    tool_call_id: str
    tool: str
    input: object
    status: ToolCallStatus
    response_chars: int
    response_excerpt: str   # 新增：工具返回内容的截断正文
```

**上限**：模块级常量 `TRACE_RESPONSE_EXCERPT_CHARS = 2000`，并由
`run_loop` / `run_free_loop` 的可选参数覆盖：

```python
def run_loop(..., trace_response_excerpt_chars: int = TRACE_RESPONSE_EXCERPT_CHARS) -> LoopResult
def run_free_loop(..., trace_response_excerpt_chars: int = TRACE_RESPONSE_EXCERPT_CHARS) -> LoopResult
```

本阶段不加对应的 CLI 参数——SkillOpt 侧走默认值即可，需要时再加。

**为什么必须有上限**：一次 50 轮、每轮读整个源文件的运行，全文会让 JSON
与内存显著膨胀。2000 字符足以让优化器看清「工具返回了什么」，又不至于失控。

**改动点**：`loop.py:128-136` 的 `_trace_record()` 是唯一构造点，增加填充。
`response_chars` 保留原语义（完整长度，非截断后长度），确保
`benchmarks/eval_cases.py` 与 `payload.py` 的既有消费者不受影响。

**测试**：短于上限时正文完整；超长时截断到上限且 `response_chars` 仍为完整长度。

### A3 — payload 暴露 `assistant_turns`

**接口**（`review_loop/payload.py:10`）

`loop_result_payload()` 的输出增加：

```python
"assistant_turns": [turn.model_dump() for turn in result.assistant_turns],
```

**为什么必须做**：`assistant_turns` 承载每一轮的 `reasoning` 与 `tool_calls`，
是优化器判断「agent 为什么漏掉这个 bug」的唯一依据。目前它被收集后丢弃，
等于让优化器只能看到「答对/答错」而看不到过程。

**测试**：payload 含 `assistant_turns`，且每项的键与 `AssistantTurn` 一致。

## 4. 工作流 B：SkillOpt 侧新增 env

**在 A 落地并通过验证之后再开始。** 本节只定契约，实施细节在设计评审后细化。

### 目录

```
skillopt/envs/codereview/
├── __init__.py
├── dataloader.py        # 读 21 用例 manifest → train/val/test
├── rollout.py           # materialize → subprocess 调 CLI → 解析 → 打分 → 写 conversation.json
├── adapter.py           # EnvAdapter
└── skills/initial.md    # 初始 policy（由 _POLICY 内容导出）
configs/codereview/default.yaml
```

### 集成方式：subprocess

`code-review-ai` 是独立的 uv 工程（Python 3.14 + langchain），与 SkillOpt 的
venv 不是同一套，in-process import 会引入依赖冲突。CLI 已有干净的 JSON 契约，
直接作为集成点：

```
uv run --project <code-review-ai> code-review-ai review \
    --repo <scratch> --out <json> --policy-file <skill.md> \
    --arm graph --max-turns N
```

环境需注入 `CRAI_DIFF_BASE=HEAD`。materialize 复用
`benchmarks/review_loop_case_compare.py:101` 的 `prepare_case()` 逻辑。

### `conversation.json` 构造（优化器实际读到的东西）

```python
for turn in payload["assistant_turns"]:
    conversation.append({
        "step": turn["turn"],
        "reasoning": turn.get("reasoning") or "",
        "action": "+".join(turn["tool_calls"]) or "(no tool call)",
        "env_feedback": <该轮工具调用的 response_excerpt>,
    })
conversation.append({"role": "system",
    "content": f"命中={hard} 漏报站点={fix_file} 机制词={mechanism_terms}"})
```

经 SkillOpt 的 `fmt_trajectory()`（`skillopt/gradient/reflect.py:65-106`）渲染为
`[step N think]` / `[step N action]` / `[step N obs]`，末条渲染为 `[verification]`。
`reference_text` 放 `gold.root_causes` 的 fix site 与机制词，渲染为
`#### Hidden Reference`。

### 打分

- `hard` = 现有 `score()`（`eval_cases.py:146`）的 0/1
- `soft` 暂与 `hard` 同值
- `mechanism_terms` 分级打分**本阶段不做**；它是小而独立的数据集只有 21 个用例时
  提升信号粒度的主要手段，留作后续独立设计

### 注册与配置

- 注册到 `scripts/train.py` 与 `scripts/eval_only.py` 的 `_ENV_REGISTRY`
- `configs/codereview/default.yaml`：`train_size` 必须**精确等于** train split 大小
  （`skillopt/engine/trainer.py:437` 会校验），`batch_size` / `minibatch_size`
  按 21 个用例的规模压小

## 5. 测试策略

**A 部分**：纯单元测试，不调用真实模型。覆盖 A1/A2/A3 各自的接口行为与边界
（缺省回落、截断、payload 字段完整性）。运行 `uv run pytest`。

**B 部分**：
1. 先单独验证 subprocess 通路能起（`uv run --project` 的 venv 隔离是首要技术风险）
2. 用 `--limit 1` 跑通单个用例的完整链路
3. 再逐步放大

## 6. 风险与未决

| 风险 | 说明 | 缓解 |
|---|---|---|
| subprocess venv 隔离 | `uv run --project` 从 SkillOpt 进程调用能否正常起，未验证 | B 的第一步就单独验它 |
| `reasoning` 填充率 | `_record_assistant_turn` 从 `additional_kwargs["reasoning_content"]` 取，DeepSeek 下可能为空 | 单用例验证时检查 payload 实际内容 |
| 21 个用例规模 | 训练信号粗（每 epoch 仅约 13 条轨迹） | 用户已知悉并选择先跑通；扩用例是后续性价比最高的动作 |
| 单用例成本 | 一次完整 ReAct 循环（≤50 轮、真实仓库），token 可能远超之前 searchqa 的整轮 smoke | 首次务必 `--limit 1` |
| 打分粒度 | 二值打分对优化器信号较粗 | `mechanism_terms` 分级留作后续设计 |

## 7. 实施顺序

1. **A1 + A2 + A3**（本设计的第一阶段，用户已确认先做）
2. 验证 subprocess 通路（B 的前置风险）
3. B 的骨架：dataloader → rollout → adapter → 注册 → config
4. `--limit 1` 端到端验证
5. 放大到全量 21 用例
