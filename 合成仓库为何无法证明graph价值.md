# 当前合成仓库为何尚未复现 graph 工具价值

> 结论先行：真实仓库的单次 uncapped 基准观察到 native 0.631 → core 0.833，但它证明的是 `get_change_summary`、`search_symbol`、`query_graph` 三件套的组合收益，不是 `query_graph` 的独立因果效果。当前 fast-repo 的有效轮次没有复现该差距；v1 存在路线泄漏，v2 又因多个真实影响遗漏在 gold 外而失效。v3 已修成机械验证的单影响 case，5 模式 × 1 次重跑全部 F1=1.0。

## 1. 实际比较的装备

- **native**：`Read/Glob/Grep/Bash`。
- **core**：同样拥有 `Read/Glob/Grep/Bash`，另外增加 `get_change_summary`、`search_symbol`、`query_graph`；提示词要求优先用 MCP，只在必要时用 Bash/rg。

因此实验比较的是“native 工具”与“native 工具 + core MCP”，不是 graph 与 grep 两套互斥装备。

真实仓库的三方 uncapped 点估计为：

| 模式 | F1 | 限定 |
|---|---:|---|
| native_agent | 0.631 | 12 case × 1 次，2 次 timeout |
| full_project_agent | 0.811 | 12 case × 1 次，1 次 timeout |
| full_project_core | 0.833 | 12 case × 1 次，点估计最高；与 full-project 差异不显著 |

core-native 的观察差为 +20.3pp，但单次重复、timeout 和工具组合混合效应都要求保留。

## 2. 快速合成基准

- `benchmarks/fast-repo`：提交在仓库中的 Python seed，由 `build_repo.py` 物化确定性 git 历史。
- `benchmarks/fast-cases.json`：当前 9 个反向 mutation case。
- 所有模式都直接在任务中获得完整 diff；本评估把 `summary_source` 设为 `none`，所以 `get_change_summary` 提供的是结构化 changed-symbol 元数据，不是额外内联 diff。
- 运行采用 uncapped、`--workers 1`，避免预算上限和 Windows 并发卡死成为混淆因素。

普通 gold 的命中条件是文件匹配且标题/描述包含关键词。large-noise v3 额外要求 `min_matches=2`。

## 3. Case-mix 实验及有效性

| 尝试 | 结果 | 判定 |
|---|---|---|
| 基线 6 case | native 0.94，core 0.94，querygraph 0.83 | 有效但太浅 |
| round A：同名函数 + 两跳契约 | native/core/summary/search 1.00，querygraph 0.94 | 有效；micro-repo 中 grep 足够便宜 |
| noisy-callers | 所有模式约 0.15 | 无效；11 个真实破坏只配置了一个 gold |
| large-noise v1 | native 0.778 = core 0.778 | mutation 有效，但 prompt 和 scheduler docstring 泄漏发现路线 |
| large-noise v2 r5 | native 1.0，其他模式 0.657–0.867 | **比较无效**；多个真实语义变化遗漏在单 gold 外 |
| large-noise v3 r1 | native/querygraph/summary/search/core 全部 1.0 | 单影响修复有效；单次结果仍不区分模式 |

### 3.1 large-noise v2 为什么失效

v2 已删除 prompt 中的 `parse_config`/`timeout` 导航词，把强语义文件名 `scheduler.py` 改成 `dispatch.py`，并将故障放到两跳后：

```text
config.parse_config
  → dispatch.build_plan
    → queue.compute_wait
      → _to_millis(None)
        → TypeError
```

但是“None-safe”只保证消费者不崩溃，不保证行为不变。删除默认值后还出现了：

- `alerts`、`analytics`、`sync`：有效值从 30000 变成 30；
- `backfill`：字符串从 `"30"` 变成 `"None"`；
- `AppConfig.to_json()`：`timeout` 从 30 变成 null；
- 多个 `_describe_*`：日志从 `timeout=30` 变成 `timeout=None`。

querygraph/core 报出的很多“额外 finding”因此是真回归，只是文件不在唯一 gold 的允许列表中。r5 的 1.0 vs 0.657–0.867 奖励了少报，不能解释为 graph 诱发误报。

summary rep1 也不是因为只报“timeout 可能为 None”而 miss。它完整写出了 `dispatch → compute_wait → None * 1000 → TypeError`，但输出的 `file` 为空，所以未匹配 gold。报告中的 `files_read` 只统计显式 Read 事件，也不能排除 Bash 查看过其他文件。

### 3.2 v3 修复

v3 不增加一堆 gold，而是把 case 修成真正的单一可观察回归：

1. `AppConfig.timeout` 明确定义为 `int | None`，`None` 表示调用方没有提供值。
2. `timeout_seconds` 和 `to_json()` 都把 `None` 规范化成 `DEFAULT_TIMEOUT_SECONDS=30`。
3. 14 个噪音模块仍使用不同的语法形态，但每个 resolver 在 `timeout=30` 与 `timeout=None` 下返回相同的值和类型。
4. `_describe_*` 使用规范化后的 resolver 输出，不再泄漏 `None`。
5. alerts 仍保留 `compute_wait` 调用诱饵，但调用点先兜底，两侧行为一致。
6. 只有 `dispatch.build_plan` 把原始可选值直接传给 `queue.compute_wait`，因此只有该链在 mutation 后抛出 TypeError。
7. 测试逐个比较 14 个模块的 resolver、description、序列化面和 alerts 诱饵，并断言 fixed dispatch 成功、buggy dispatch 崩溃。

当前 gold 仍只有一个，但弱关键词 `none`、`dispatch` 已移除：

```json
{
  "id": "dropped-timeout-default-breaks-wait-computation",
  "file": "src/bigapp/config.py",
  "keywords": ["typeerror", "compute_wait", "multiply", "multiplication", "millis"],
  "min_matches": 2,
  "alternate_files": ["src/bigapp/dispatch.py", "src/bigapp/queue.py"]
}
```

它要求 finding 至少命中两个具体计算机制词；仅说“timeout 可能为 None”不再得分。

### 3.3 v3 5×1 结果

uncapped、`workers=1`、五个模式各运行一次：

| 模式 | F1 | finding | 读文件 | 耗时 | 成本 |
|---|---:|---:|---:|---:|---:|
| native_agent | 1.0 | 1 | 4 | 56.0s | $0.251 |
| full_project_search | 1.0 | 1 | 4 | 56.8s | $0.303 |
| full_project_summary | 1.0 | 1 | 4 | 64.1s | $0.324 |
| full_project_core | 1.0 | 1 | 2 | 74.2s | $0.354 |
| full_project_querygraph | 1.0 | 1 | 4 | 83.5s | $0.407 |

总运行时间 334.6 秒，总成本 $1.638。人工复核五份 transcript：每个模式都准确报告了 `parse_config` 删除默认值后，`dispatch.build_plan → queue.compute_wait → _to_millis(None)` 抛出 TypeError；没有模式再报告 v2 中的 config 序列化或噪音消费者变化，也没有额外 finding。

这说明 v3 的单影响修复生效，同时再次得到“在当前合成 case 上 native 与 MCP 的正确性打平”。单次运行不能估计方差；耗时、成本和文件读取只作为本轮观测值，不推广为稳定排序。

## 4. 目前能下什么结论

可以下的结论：

- 文件总行数本身不会自动产生 graph 优势；native 会用 grep 缩小范围，而不是整读整个仓库。
- 小型、词法锚点清楚的合成仓库中，grep 经常足以近似局部调用图。
- fast-repo 适合发现 prompt、配置和工具协议的回归。
- case 必须通过差分测试证明 gold 完整，不能把“不崩溃”等同于“没有语义变化”。

目前不能下的结论：

- “任何合成仓库都无法证明 graph 价值”；现有实验没有覆盖所有合成方法。
- “query_graph 单独带来真实仓库 +20pp”；真实结果对应三工具组合。
- “动态/registry 调用是当前 query_graph 的既证优势”；`query_graph` 只遍历 resolved edges，dynamic/unresolved 需要单独验证。
- “v2 证明 graph 穷举导致误报”；v2 的所谓误报大多是真 finding。

## 5. 用途边界

v3 的 5×1 结果可以引用为“修复后的单次平局”，不能引用为稳定模式排名。快速 eval 继续承担回归信号；工具价值判断应引用真实仓库结果，并明确写成“core MCP 组合的观察收益”，同时保留单次重复和 timeout 限定。

证据文件：

- `.code-review-ai/fast-eval/large-noise-v2-r5/report.json`：已失效的 v2 比较，保留作审计记录；
- `.code-review-ai/fast-eval/large-noise-v3-r1/report.json`：v3 五模式各一次，全部 F1=1.0；
- `.code-review-ai/fast-eval/large-noise-r3/report.json`：v1；
- `benchmarks/FULL_AGENT_EVAL_REAL_REPOS.md`：真实仓库与 case-mix 总记录；
- `tests/test_fast_repo.py`：v3 单影响差分约束。

## 6. 下一档实验已落地：Gson medium

为了先验证“真实拓扑是否能改善单位成本”，而不是直接跳到 FastAPI 的
高成本压力档，新增了单仓库、单语言的 Gson medium benchmark：

- 3 个真实修复提交的反向变异，覆盖局部容器契约、mutable builder 生命周期、
  跨模块 adapter 委托链与递归类型；
- prompt 全部为中性 review 请求，不包含 duplicate、reuse、delegate、recursive
  等 gold 导航词；
- 5 个 gold 全部要求至少命中两个根因词；
- dry-run 的 3 个 mutation 均非空，7/7 changed symbols 被索引找到；
- 第三个 case 的独立 Java harness 在修复版通过，在 mutation 版同时复现
  TreeTypeAdapter 与 FutureTypeAdapter 丢失子类字段。

定义、运行命令和预检结果见 `benchmarks/GSON_MEDIUM_BENCHMARK.md`，case
清单见 `benchmarks/gson-medium-cases.json`。

2026-08-21 的首次配对运行已经完成：3 case × 2 mode，6/6 成功。报告原始
macro F1 为 core 0.889、native 0.722，但 transcript 审计发现这是 finding
粒度与一对一 matcher 的差异：map 的同一 gold 被两边按两个分支拆报；委托链
的两个 gold 被 native 合并在同一个 finding 中。两种模式在语义上都覆盖了
5/5 个预注册故障，因此不能把 +16.7pp 写成 graph 的质量收益。

成本信号则很明确：core 总成本 `$1.736`，比 native 的 `$1.386` 高 25.3%；
core 总 agent 时间 365.2 秒，比 native 的 556.4 秒低 34.4%。也就是说，仓库
变大并出现真实委托拓扑后，graph 首次表现出导航提速信号，但仍未实现项目的
核心目标——省钱。完整逐 case 结果和评分审计已写入
`benchmarks/GSON_MEDIUM_BENCHMARK.md`。

该轮使用的是 Core v1：强制从 `get_change_summary` 启动，并同时暴露
`search_symbol` 与 `query_graph`。现在已实现 Core v2：LLM 先看 diff 和局部
代码，自包含改动不调用 MCP；只有非局部改动才调用一次
`get_change_context(files|symbols)`。服务端内部解析 changed qname，默认只返回
上游、4 个变更符号、每个 5 个邻居，不带签名，并设置 8KB 硬上限。旧工具继续
保留兼容，但不再暴露给 Core v2。对现有 Gson 委托链 worktree 的实际只读调用
返回 2 个 changed method，JSON 仅 952 字符。下一轮必须使用独立报告目录，不能
与 Core v1 的结果混算。

Core v2 的 3×2 首轮随后完成，6/6 成功。它没有实现预期路由：LLM 在 3/3
case 都调用了 `get_change_context`，包括应当保持本地审查的 map case；调用后
仍继续使用 Bash 和 Read。Core v2 总成本 `$1.881`，Native `$1.503`，仍高
25.1%；Core v1 的对应差值是 25.3%，几乎没有变化。Core v2 总耗时还从 v1
的较快变成比同轮 Native 慢 7.7%。原始 F1 0.933 vs 0.778 继续受 finding
拆分/合并粒度影响，语义审计不能支持质量收益。

因此问题已进一步定位：压缩单次图响应不够，prompt-only 路由也不可靠。只要
Graph 工具在同一 review session 中可见，这个模型倾向于“保险起见”调用它，
随后再做原生验证。下一步需要把“是否开放 Graph”变成显式、可测量的前置决策
gate，而不是继续加强一句“自包含时不要调用”的提示词。Core v2 原始报告为
`.code-review-ai/gson-medium-context-v2-r1/report.json`。

2026-08-22 已把这个 gate 改成完全确定性的本地 Context Planner，不再让 LLM
决定是否调用 graph。它只读取 diff、AST change summary 和本地 SQLite graph，
输出 local/graph 路由及不超过 8,000 字符的冻结证据包；批量 evaluator 不联网、
不调用 provider，报告明确为 `llm_calls=0`、`model_cost_usd=0`。

Gson 三 case 的 evaluator-only 预期是 map=local、builder/runtime=graph；规划器
3/3 命中。三个包的 mutation 文件和 gold test 文件 macro recall 都是 100%，
重叠源码片段为 0，平均 7,629 字符。该结果只证明“路由和取上下文可以用代码
离线完成”，不等于 graph 已提高语义评审 F1，也不能用三个 case 宣称路由器已
泛化。完整口径与报告见 `benchmarks/GSON_MEDIUM_BENCHMARK.md` 和
`.code-review-ai/gson-context-plan-local-v1/report.json`。
