# Harness skill 复盘（loop 内可选功能）

## 背景：这份设计要解的问题

在 field-contract 语料（8 条 case，graph 臂，每条跑一次）上量到的事实：

| | 完成的 4 条 | 未完成的 4 条 |
|---|---|---|
| 轮数 | 13-16 | 25（烧满） |
| `finish_review` 调用 | 1-2 次 | **0 次** |
| 首次触达 gold 的位置 | 全程的 3-21% | 全程的 2-33%（共 41-49 步） |

未完成的那 4 条是 `user-description-column-renamed`、`user-employee-no-missing-export-import`、
`user-employee-no-missing-column`、`dict-import-missing-cache-refresh`。

**它们的召回是 0，但病因不是"没找到"—— 是"没交卷"。** 8 个 recall=0 的 run 与 8 个
`review_complete=False` 的 run 完全重合；跑完的那 8 个里没有一个是 0 分。

看轨迹，四条是同一个形状：

```
前 1/4 ~ 1/3   读完现场（犯罪现场 + 基类 + 相邻模块）        ← 都做对了
中段           出去找旁证，证明自己那个判断成立              ← 方向各异
尾段           旁证把它带偏，再也回不来                     ← 掉进 SQL 种子 / 日志 / 模板 / 别的模块
全程           0 次 finish_review
```

最极端的一条：`user-description-column-renamed` 第 12 步之后就再没看过模块目录，余下 37 步（76%）全在外面。

**两条成因叠在一起：**

1. **loop 从不告诉模型预算。** `loop.py` 在循环顶部硬切 `max_turns`，之前没有任何注入。
   模型不知道有预算这回事，所以没有任何理由停下来。
2. **语言 skill 在无预算环境里被放大。** `_POLICY` 写的是「证据不足就少报，绝不猜测。
   研究完成后调用 finish_review」——「绝不猜测」让它不敢在证据不全时报，「研究完成后」
   没有定义，于是「再找一点证据」永远是对的选择。

反过来验证：手写一条 harness skill（只说"只看本次改动涉及的模块，不要出去找旁证"），
语言那半一个字没动，结果是：

```
召回     0.281 → 0.812
精确率   0.781 → 0.750   （下降全部来自两个已知问题：锚点、一条未验证的发现）
步数     38.3  → 19.4    （8 条全部下降）
```

**结论：harness 这个槽有信号。** 但那条规则是**人找出来的**。这份设计要做的是让 loop
自己能找 —— 把一次 run 的完整过程交给一个复盘 agent，让它指出 skill 里哪些措辞导致了
这次的过程问题。

## 目标 / 非目标

**目标**：loop 内部的一个可选功能。开启后，在一次 run 结束时把完整过程交给一个复盘
agent，产出改过的 harness skill 全文，写到指定路径。

**非目标**：

- **不做验证与采纳。** 复盘 agent 产出的只是**候选**；loop 自己跑一条 case，无法判断
  候选比基线好。"某个候选是否该被采纳"是另一件事，本设计只在代码里**预留接口位置**。
- **不与 SkillOpt 有任何耦合。** 不 import、不读写它的目录、不假设下游是谁。产出就是
  一个文件，谁消费不归这个功能管。
- **不改变默认行为。** 默认全关。

## 设计

### 消息列表：父 agent 的消息原样重放，末尾追加一条指令

```python
initial_messages = list(parent_messages) + [HumanMessage(SKILL_REVIEW_INSTRUCTION)]
```

父 agent 的消息**一条不改**。三个后果：

- **`reasoning_content` 免费。** 它原样留在每条 assistant 消息的 `additional_kwargs`
  里，重放时仍走那个字段。实测该字段**不计入 `prompt_tokens`**（见下节）。
- **工具返回是全文。** `_reply_call` 塞进 `state.messages` 的是完整 content；2000 字的
  截断只发生在 trace 记录里，不在消息列表里。信息不丢。
- **末轮前缀就是复盘的上限。** 不需要另外拼轨迹，也不会漏掉任何一轮。

末尾那条指令要做的事：说明上面是什么、要求指出 harness skill（第二条 system 消息）里
哪些措辞导致了这次的过程问题、给出改好的全文。

### 工具：绑定全套，白名单在运行时校验

```python
tools         = parent_tools + [submit_skill_tool()]   # 绑全套 → 历史里引用的工具全对得上
allowed_tools = {"read_file", "submit_skill"}          # 运行时白名单
```

**为什么绑全套**：历史里引用了 `read_file` / `search_code` / `get_impact` 的 tool_call
和对应的 tool reply。如果绑定的工具集和历史对不上，可能触发 DeepSeek 的
`tool_calls must be followed by tool replies` 校验。绑全套让两者一致。

**白名单在哪拦**：`loop.py` 的 `_execute_call`（`state.tool_map.get(name)` 那一处）。
不在白名单的调用**不执行**，回一条 `error` ToolMessage —— 模型能收到"不允许"并自己纠正，
而不是崩掉整个 run。

**效果**：复盘 agent 结构性地不能搜。它诊断的是"你在别处瞎搜"，而它自己连搜都不能搜 ——
父 agent 那个失败模式在它身上不存在。

### 终止工具

`submit_skill` 形状对称 `finish_review_tool()`：schema 里装改好的 skill 全文，靠
`ToolSpec.terminates` 结束整个 run。

### 预留的闸门（不实现）

```python
# 候选 skill 的采纳/否决在这里。
# 现在不实现：loop 只产候选，写到 out_path 就结束。
# 将来接闸门时，accept(candidate, baseline) -> bool 决定是否覆盖 out_path。
accept: Callable | None = None
```

## 本设计依赖的测量

### `reasoning_content` 不计入 `prompt_tokens`

构造一个带 `tool_calls` 的 assistant 消息，只改 `reasoning_content` 的长度，
看 DeepSeek 报的 `prompt_tokens`（`deepseek-v4-flash`）：

| 3000 字放哪 | `prompt_tokens` |
|---|---|
| 不放 | 95 |
| `reasoning_content` 字段 | **95** |
| `content` | 1595 |

0 / 200 / 1000 / 3000 字的 `reasoning_content` 全部返回 95 —— **该字段完全不计费**。
所以原样重放父 agent 的消息，reasoning 是白拿的。

（附带观测：带 `tool_calls` 但不回传 `reasoning_content` 返回 HTTP 200，未报错。
`providers.py` 的注释称该字段在绑定工具时是必需的，且失败模式出现在**后续的多工具
请求**上 —— 本次测量是单个请求，**没有证伪它**，只是没在此形状下触发。）

### 成本量级（估算，未实测）

父 run 的 `usage.input_tokens = 598,689`（25 轮累计）。按前缀线性增长粗估，末轮完整
前缀约 **4-5 万 tokens**。复盘调用的输入大致就是这个量级，且**没有缓存**（system 与
工具集都和父 agent 不同，前缀无一段重合 —— 前缀缓存按前缀算，第一段就不一样）。

这是"不丢信息"的代价。若日后要压，可裁剪工具返回部分 —— 它对诊断"去了哪里"用处最小。

### 语料 gold 的修正（本轮已落地）

复盘要读的评价数据本身有错标，已修：

| case | 修正 |
|---|---|
| `user-role-names-partial-sync` | + `service.py:197-200`（`current_info` 未同步 `role_names`） |
| `user-role-names-fully-wired` | + `service.py:199-202`（同一落点；该 case 原标记为"全跟齐、gold 为空"，实际并不成立） |
| `user-employee-no-missing-export-import` | + `schema.py:132-143`、`schema.py:207-218`（`UserCreateSchema` / `UserUpdateSchema` 未同步 `employee_no`） |

修正前后，原本被计为"假阳性"的发现里有四处**核实为真**：漏标的是语料，不是模型。
语料现在**没有空 gold 对照**了 —— 原来唯一的那个正是 `user-role-names-fully-wired`。

## 改动清单

```
新增  code_review_ai/review_loop/skill_review.py
      · SKILL_REVIEW_INSTRUCTION
      · submit_skill_tool()                对称 finish_review_tool()
      · run_skill_review(...) -> str       内部就是一次 run_loop(...)
      · SkillReview dataclass              out_path / model / trigger / accept(预留)

改    loop.py     · ToolSpec 加 terminates / apply；把硬编码的 finish_review 分派改掉
                  · _execute_call 加白名单校验
改    runner.py   run_review 透传 harness_skill / skill_review
改    cli.py      --skill-review <路径> / --skill-review-model
```

## 已知风险与未决

- **触发条件没有好信号。** 测过三个候选（重复调用率、离开模块的时间、首次触达 gold
  的位置），**全都不区分完成与未完成**；唯一干净分开的是 `finish_review` 本身，那是
  终点不是征兆。所以默认是「跑完复盘」，阈值形式（`trigger=`）留着备用。
  好在：这个功能改的是给**后续** rollout 用的，中途触发买不到额外的东西。
- **无缓存。** 见上。若日后工具集与父 agent 保持一致（代价是复盘 agent 能搜），前缀
  有可能命中缓存 —— 但这与"只给 read + 提交"的目标互斥。
- **候选无人验证。** 默认行为是写文件，不覆盖任何东西。在闸门接上之前，候选必须由人看。
- **`n=1`。** 上面所有召回/步数数字都是每条 case 单次运行，没有重复样本。
