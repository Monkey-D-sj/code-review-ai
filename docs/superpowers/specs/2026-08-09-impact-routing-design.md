# Code Review AI — 变更影响路由与按需 get_impact 设计

- **日期**: 2026-08-09(risk 评分方案);2026-08-12(修订:移除数值 risk,深度判定并入决策表)
- **状态**: 已实现
- **语言**: Python 3.14(uv 管理)

## 1. 背景与问题

当前 post-commit 评审的 `_REVIEW_PROMPT`(`code_review_ai/hooks.py`)无条件要求 LLM 对每个变更都调用 `get_impact`:

> 先用 get_change_summary 确认变更明细,再用 get_impact 查上游调用方 / 下游被调方 / 受影响业务入口……

而 `benchmarks/AGENT_EVAL_BASELINE.md` 的评测(10 个 reverse mutation × 3 次 × 4 模式 = 120 runs)显示:

| 模式 | F1 (95% CI) | 结论 |
|---|---|---|
| Diff Only | 48.6% | 基线 |
| Search | 62.1% | 最优、便宜 |
| Graph(= impact 链上下文) | 46.9% | 不如 diff-only |
| Hybrid | 51.3% | 召回同 Search,但贵 76%、慢 71% |

结论:对以局部改动为主的数据集,**完整 impact 链上下文是负资产**——拉进更多符号与行反而干扰精度。调用图只对跨文件行为变更、删除影响、路由/DI 场景才可能值回成本。

因此不再「每次必调 get_impact」,改为:**LLM 先按决策表判断改动是否自包含;需要上下文时默认用 query_graph 看上游;只有跨服务/删除/被跨模块调用的接口变更才升级到 get_impact**。最初设计了数值 risk 评分来驱动该路由,但离线验证证伪了「risk 高分预测上下文收益为正」,最终移除 risk,把「要不要看 + 看多深」全部并入决策表类别。

## 2. 目标与非目标

**目标**
- 决策表(skill/prompt)成为「是否查上下文 + 查多深」的唯一事实来源:`hooks._REVIEW_PROMPT` 与 `full_agent_eval._REVIEW_PREFIX` 语义一致(parity 测试强制)。
- 修订 `_REVIEW_PROMPT`:自包含判断 → 默认 `query_graph` 上游 → 类别深度路由(`get_impact` 仅用于跨服务/删除/被跨模块调用的接口变更)。
- 移除数值 `risk` 评分及其全部消费(变更摘要不再输出 risk 字段)。

**非目标**
- 不新增 agent-eval 的上下文模式。
- 不做活工具评测(让 agent 运行时真调工具并观察)。
- 不改动 get_impact / query_graph 工具本身的语义。
- 风险不设任何配置项(YAGNI)。

## 3. 设计

### 3.1 深度路由:决策表类别 → 查证深度

`build_change_summary` 不再输出数值 risk。审查 prompt 用「自包含」决策表决定是否查上下文,用类别决定查多深:

```
自包含(纯注释/文档/改名/格式化、仅函数内部局部计算、
不改对外签名/返回类型/异常语义、不改变调用方依赖的行为)
  → 直接按 diff 评审,不查上下文。

需要上下文(改了对外签名/返回类型/新增异常、改变了调用方依赖的语义、
新增/移除跨模块调用、路由/DI 装配、被其他模块调用且改动可能破坏它们)
  → 默认 query_graph 看上游(direction=in);涉及下游再 direction=out。

深度(需要上下文的改动里):
- 跨服务/RPC/API 变更、删除的函数、被跨模块调用的接口变更
    → 追加 get_impact 查完整影响链(上游调用方、受影响业务入口)。
- 私有或同模块内小范围的改动
    → 只看直接调用点即可,不需要 get_impact。

拿不准 → 按「需要上下文」处理。
```

### 3.2 数据形态

`changed_functions[i]` 与 `delete_change[i]` 记录**不带** `risk` 字段。示例:

```json
"changed_functions": [
  {"qname": "auth::UserService.authenticate", "kind": "method",
   "file": "auth.py", "start_line": 10, "end_line": 40},
  {"qname": "auth::_normalize_email", "kind": "function",
   "file": "auth.py", "start_line": 80, "end_line": 92}
]
```

`uncovered_changes`(模块级 hunk、未支持扩展名、二进制)无符号,本就不加字段。`summary` 顶层不加聚合字段——深度判定不在数据层,而在审查 prompt 的决策表。

### 3.3 `_REVIEW_PROMPT` 修订(`code_review_ai/hooks.py`)

```
1. get_change_summary 确认变更明细。
2. 对每个变更函数,先判断它是否"自包含":只凭 diff 与该函数自身的代码,
   能否完整判断这次改动的正确性与影响范围?
   - 能 → 直接按 diff 评审,不查上下文。
     典型:纯注释/文档/改名/格式化、仅函数内部局部计算、
     不改对外签名/返回类型/异常语义、不改变调用方依赖的行为。
   - 不能 → 需要上下文。
     典型:改了对外签名/返回类型/新增异常、改变了调用方依赖的语义、
     新增/移除跨模块调用、路由/DI 装配、被其他模块调用且改动可能破坏它们。
   - 拿不准 → 按"需要上下文"处理(多看一眼 query_graph 比漏看强)。
3. 需要上下文的真实改动 → 默认用 query_graph 看该函数的【上游】(direction=in,即调用方):
   改动一个函数,最可能的破坏在调用它的人——谁调了它、会不会被这次改动影响。
   仅当改动涉及【下游】时才同时看下游(direction=out):改了传给被调方的入参/实参、
   新增或移除对某函数的调用、返回值被下游进一步消费等——这类改动要确认下游怎么接。
   函数自身签名入参变化(如新增必填参数)砸的是调用方,归入上游(默认方向已覆盖)。
4. 需要上下文的改动里,只有跨服务/RPC/API 变更、删除的函数、被跨模块调用的接口变更
   才追加 get_impact 查完整影响链(上游调用方、受影响业务入口);私有或同模块内小范围的改动
   只看直接调用点即可,不需要 get_impact。
5. search_symbol / Read 按需补充;不要用 git diff / grep 自己重算
再按语言用 code-review 系列 skill 评审,按 error / warning / info 输出,
每条给出文件、行号、问题描述与具体失败场景,用中文回答。
```

### 3.4 决策表在 agent-eval 中的一致性

`full_agent_eval._REVIEW_PREFIX`(两模式共用)注入同一套决策表的英文版:自包含 MUST/NO 分类、兜底规则,以及深度路由句(cross-service / deleted / cross-module interface → full caller chain;其他需上下文 → 只看 callers;private / narrow → 只读直接 call sites)。`tests/test_full_agent_eval.py` 的 parity 测试双向断言 hook 与 eval 的关键 trigger 词对,防止任一侧编辑时丢失 trigger。full_project 模式的 `project_note` 按决策表 gate 图查询:只有决策表标记「需要上下文」的改动才做图查询,深度按类别;自包含改动不做图查询。

(曾存在 `agent-eval-route-check` 离线验证子命令 + `agent_eval_analysis.route_check_analysis`,用 baseline transcripts 验证「risk 高分才值得升级 get_impact」;该假设被数据证伪,命令与函数已随 risk 一并移除。)

### 3.5 测试

- `tests/test_changes.py`:变更摘要记录不含 `risk` 字段(预期 dict 断言)。
- `tests/test_hooks.py`:post-commit 审查 prompt 含自包含判据、`query_graph`、`direction=in`、深度路由(跨服务)。
- `tests/test_full_agent_eval.py`:`_REVIEW_PREFIX` 与 `hooks._REVIEW_PROMPT` 的 parity(9 trigger + 3 depth 词对)。

## 4. 错误处理与边界

- 未解析符号:`get_impact` 返回 `found=false`,prompt 引导用 search 而非升级。
- 空变更(无可归因函数、纯删除):`changed_functions` 为空,review 只看 `delete_change` / `uncovered_changes`。
- 索引过期:hook 在 review 前已跑 `sync` 重建;新文件未索引属预期,决策表按「自包含」从 diff 判断。
- `_symbols_summary`(显式 symbols 路径)与 diff 路径行为一致,均不输出 risk。

## 5. 影响面

- `code_review_ai/changes.py`:移除 `assess_symbol_risk`;`build_change_summary` / `_symbols_summary` / `_delete_change` 不再输出 `risk` 字段。
- `code_review_ai/hooks.py`:`_REVIEW_PROMPT` 步骤 1/4 文案修订(去掉 risk,改为类别深度路由)。
- `code_review_ai/full_agent_eval.py`:`_REVIEW_PREFIX` 追加深度路由句;`project_note` 按决策表 gate 图查询。
- `code_review_ai/agent_eval_analysis.py` / `cli.py`:移除 `route_check_analysis` 与 `agent-eval-route-check` 子命令。
- `tests/test_changes.py`、`tests/test_hooks.py`、`tests/test_agent_eval_analysis.py`、`tests/test_full_agent_eval.py`:同步更新。
- `benchmarks/AGENT_EVAL_BASELINE.md`:移除已证伪的 risk 路由离线验证小节,保留结论说明。
