# Code Review AI — 变更风险路由与按需 get_impact 设计

- **日期**: 2026-08-09
- **状态**: 设计已确认，待实现
- **语言**: Python 3.14（uv 管理）

## 1. 背景与问题

当前 post-commit 评审的 `_REVIEW_PROMPT`（`code_review_ai/hooks.py`）无条件要求 LLM 对每个变更都调用 `get_impact`：

> 先用 get_change_summary 确认变更明细，再用 get_impact 查上游调用方 / 下游被调方 / 受影响业务入口……

而 `benchmarks/AGENT_EVAL_BASELINE.md` 的评测（10 个 reverse mutation × 3 次 × 4 模式 = 120 runs）显示：

| 模式 | F1 (95% CI) | 结论 |
|---|---|---|
| Diff Only | 48.6% | 基线 |
| Search | 62.1% | 最优、便宜 |
| Graph（= impact 链上下文） | 46.9% | 不如 diff-only |
| Hybrid | 51.3% | 召回同 Search，但贵 76%、慢 71% |

结论：对以局部改动为主的数据集，**完整 impact 链上下文是负资产**——拉进更多符号与行反而干扰精度。调用图只对跨文件行为变更、删除影响、路由/DI 场景才可能值回成本。

因此不再「每次必调 get_impact」，改为：**工具给每个变更节点一个 risk 评分，LLM 先判重要性再按风险阈值决定是否升级到 get_impact；query_graph（直接邻居）作为默认动作。**

## 2. 目标与非目标

**目标**
- 在 `build_change_summary` 输出中给每个变更函数 / 每个删除函数附加一个 0–100 的 `risk` 整数评分。
- 修订 `_REVIEW_PROMPT`：重要性过滤 → 默认 `query_graph` → 高风险才 `get_impact`。
- 新增离线验证子命令，用现有 baseline 数据验证「risk 高分确实预测 impact 上下文收益为正」。

**非目标**
- 不新增 agent-eval 的上下文模式（不做 query_graph 邻域模式的离线评测）。
- 不做活工具评测（让 agent 运行时真调工具并观察）。
- 不改动 get_impact / query_graph 工具本身的语义。
- risk 评分不做任何配置项（YAGNI）。

## 3. 设计

### 3.1 风险评分：`assess_symbol_risk(conn, symbol, deleted=False) -> int`

新增于 `code_review_ai/changes.py`（change summary 归属），单符号函数，纯 SQL 基于现有 `nodes` / `edges` 表。`deleted=True` 表示符号在本次删除集合（tombstone），由 delete_change 路径传入：

```
规则（按优先级，取第一个命中的结果）：
1. deleted=True（符号属本次删除）                      → 90
2. 符号解析到，且有跨模块（不同 file_path）resolved 入边
     → min(100, 60 + 10 × cross_module_callers)
3. 符号解析到，只有同模块 resolved 入边
     → min(59, 30 + 5 × same_module_callers)
4. 符号解析到，零入边（叶子）                         → 10
5. 符号未解析（新文件 / 未索引 / 不在 graph）         → 50
```

delete_change 与 changed_functions 不相交（删除符号来自 tombstone，不来自解析当前树），因此同一函数不会命中两条规则；删除路径统一以 `deleted=True` 走规则 1。

语义：≥60 高风险（跨模块 / 删除）；30–59 中风险（同模块调用方）；<30 低风险（叶子）；50 表示「无法从图评估」——此时 `get_impact` 会返回 `found=false`，LLM 应用 search 而非强行升级。

跨模块判定：`source` 与 `target` 的 `file_path` 不同即跨模块（本工具 module 即文件，与 `qname` 的 `module::` 段一致）。

### 3.2 数据形态

`build_change_summary`（含 `_symbols_summary` 路径）的每条 `changed_functions[i]` 增加 `"risk": <int>`；每条 `delete_change[i]` 直接带 `"risk": 90`。示例：

```json
"changed_functions": [
  {"qname": "auth::UserService.authenticate", "kind": "method",
   "file": "auth.py", "start_line": 10, "end_line": 40, "risk": 90},
  {"qname": "auth::_normalize_email", "kind": "function",
   "file": "auth.py", "start_line": 80, "end_line": 92, "risk": 10}
]
```

`uncovered_changes`（模块级 hunk、未支持扩展名、二进制）无符号，不加 risk——它们本来就不是图能归因的。`summary` 顶层不加聚合字段，来源只有各节点自己的 risk。

### 3.3 `_REVIEW_PROMPT` 修订（`code_review_ai/hooks.py`）

```
1. get_change_summary 确认变更明细与各函数的 risk
2. 对每个变更函数，先判断它是否"自包含"：只凭 diff 与该函数自身的代码，
   能否完整判断这次改动的正确性与影响范围？
   - 能 → 直接按 diff 评审，不查上下文。
     典型：纯注释/文档/改名/格式化、仅函数内部局部计算、
     不改对外签名/返回类型/异常语义、不改变调用方依赖的行为。
   - 不能 → 需要上下文。
     典型：改了对外签名/返回类型/新增异常、改变了调用方依赖的语义、
     新增/移除跨模块调用、路由/DI 装配、被其他模块调用且改动可能破坏它们。
   - 拿不准 → 按"需要上下文"处理（多看一眼 query_graph 比漏看强）。
3. 需要上下文的真实改动 → 默认用 query_graph 看该函数的【上游】（direction=in，即调用方）：
   改动一个函数，最可能的破坏在调用它的人——谁调了它、会不会被这次改动影响。
   仅当改动涉及【下游】时才同时看下游（direction=out）：改了传给被调方的入参/实参、
   新增或移除对某函数的调用、返回值被下游进一步消费等——这类改动要确认下游怎么接。
   函数自身签名入参变化（如新增必填参数）砸的是调用方，归入上游（默认方向已覆盖）。
4. 仅当该函数 risk ≥ 60（跨模块 / 删除）且改动重要
   → 追加 get_impact 查完整影响链（上游调用方、受影响业务入口）
5. search_symbol / Read 按需补充；不要用 git diff / grep 自己重算
再按语言用 code-review 系列 skill 评审，按 error / warning / info 输出，
每条给出文件、行号、问题描述与具体失败场景，用中文回答。
```

### 3.4 离线验证：`agent-eval-route-check`

新增 CLI 子命令 + `code_review_ai/agent_eval_analysis.py` 聚合函数，**不跑新 agent**：

输入
- `--cases benchmarks/agent-eval-real-10.json`：case manifest（含每 case 的 `changed_symbols`）
- `--runs-dir .code-review-ai/agent-eval-real-10-r3`：baseline transcripts（`<case>/<mode>/run-N.json`，含每 run 的 `f1`）
- `--repo / --db`（走 rebuild 或现有索引）

步骤
1. rebuild 索引。
2. 对每 case 的 `changed_symbols` 逐个算 `assess_symbol_risk`，`max_risk` = 各符号风险最大值。
3. 从 transcripts 汇总每 case/mode 的 F1（多 rep 取均值）。
4. 输出：`{case_id, max_risk, graph_delta_f1, hybrid_delta_f1}` 全表 + `max_risk` 与两个 delta 的 Pearson 相关系数 + ≥60 / <60 两组的分组表。

验证判据：信号有效时，`max_risk ≥ 60` 组的 graph/hybrid delta 均值应显著 > 0，`< 60` 组应 ≈ 0 或为负——证明「risk 高分才值得升级 get_impact」成立。

### 3.5 测试

- `tests/test_changes.py`：`assess_symbol_risk` 单测——跨模块入边 → ≥60、同模块 → 30–59、叶子 → 10、未解析 → 50；delete 路径 → 90。用现有 fixtures。
- `tests/test_agent_eval_analysis.py`：route-check 聚合函数测试——给定合成报告/风险映射，分组统计与相关性正确。

## 4. 错误处理与边界

- 未解析符号：risk=50，`get_impact` 返回 `found=false`，prompt 引导用 search 而非升级。
- 空变更（无可归因函数、纯删除）：`changed_functions` 为空 → risk 列表为空，验证侧 `max_risk` 取不到时跳过该 case。
- 索引过期：hook 在 review 前已跑 `sync` 重建，风险基于最新图；新文件未索引属预期（→ 50）。
- `_symbols_summary`（显式 symbols 路径）同样附加 risk，行为与 diff 路径一致。

## 5. 影响面

- `code_review_ai/changes.py`：新增 `assess_symbol_risk`；`build_change_summary` / `_symbols_summary` / `_delete_change` 附加 `risk` 字段。
- `code_review_ai/hooks.py`：`_REVIEW_PROMPT` 文案修订。
- `code_review_ai/agent_eval_analysis.py`：新增 route-check 聚合函数。
- `code_review_ai/cli.py`：新增 `agent-eval-route-check` 子命令。
- `tests/test_changes.py`、`tests/test_agent_eval_analysis.py`：新增测试。
- `benchmarks/AGENT_EVAL_BASELINE.md`：跑完 route-check 后追加一节验证结果（不阻塞实现）。
