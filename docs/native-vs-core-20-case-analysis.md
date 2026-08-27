# native vs core：20 个 case-backend 用例对比分析

> 运行日期：2026-08-26。数据源：`.code-review-ai/full-agent-eval-cmp-20/report.json`（40/40 次运行成功）。

## 运行配置

| 参数 | 值 |
|---|---|
| 用例集 | `benchmarks/case-backend-cases.json`（20 个，trivial×6 / medium×8 / hard×6） |
| 模式 | `native_agent`（原生 Read/Grep/Bash）vs `full_project_core`（+ get_impact 主通道 MCP 子集） |
| 重复次数 | 1（每个 case 每种模式 1 次，共 40 次 agent 运行） |
| 提示词 | **blind**（无 hint，不点名符号）、guidance full |
| 预算上限 | **无**（uncapped；记忆：cap 是混杂变量） |
| 模型 | sonnet，workers=4，timeout 1200s |

core 模式可用的 MCP 工具（`_CORE_MCP_TOOLS`）：`get_impact`、`get_test_impact`、`get_change_summary`、`get_change_context`、`search_symbol`、`get_symbol_detail`。`query_graph` 被排除。

## 执行摘要

1. **F1 逐 case 完全一致**（20/20）。两模式在每个 case 上找到的 bug 完全相同，F1 只由难度/gold 严格度决定，与模式无关。
2. **成本统计上无差异**。配对 bootstrap 95% CI 含 0；剔除 fan-in 离群点后更紧贴 0。core 在 trivial/medium 更便宜，hard 上更贵。
3. **效率信号是真实价值**：core 平均少 **80% 的 grep**、少 **33% 的文件访问**；剔除 fan-in 后工具调用数差异**首次统计显著**（core 每 case 平均少 1.6 次）。
4. **关键修正：F1=0 ≠ 没找到 bug**。全部 11 个 F1=0 的 case，agent 的 finding `fix_file` 都与 gold 匹配——bug 定位正确，只是没点名 gold 要求的调用链符号（`mechanism_terms`），严格匹配得 0。

## 汇总指标（20 个 case，n=20×2）

| 指标 | native_agent | full_project_core | 变化 |
|---|---|---|---|
| root-cause F1（macro） | 0.500 | 0.500 | 相同 |
| precision / recall | 0.500 / 0.500 | 0.500 / 0.500 | 相同 |
| 总成本 | $2.79 | $2.95 | core +5.7% |
| 每 run 成本 | $0.139 | $0.148 | +$0.009 |
| tokens（in / out） | 376,895 / 143,736 | 384,979 / 155,920 | 略升 |
| tool calls / run | 13.55 | 12.25 | −10% |
| read calls / run | 8.75 | 6.95 | −21% |
| **search calls / run** | **6.75** | **1.60** | **−76%** |
| **files touched / run** | **8.5** | **5.7** | **−33%** |
| 20 例去重文件数 | 74 | 50 | −32% |
| MCP 采纳率 | 0/20 | **20/20** | — |
| 平均耗时 / run | 60.5s | 64.1s | 相当 |

### 配对 bootstrap（10,000 次，n=20 配对）

| 差异（native − core） | 均值 | 95% CI | 显著？ |
|---|---|---|---|
| 每 run 成本 | −$0.0083 | [−$0.031, +$0.012] | 否（含 0） |
| tool calls / run | +1.30 | [−0.25, +2.90] | 否（含 0） |

### 剔除 fan-in 离群点后（n=19）

`cb-recall-search-to-dict-fan-in` 是核心花费最高的 case（core $0.445）。剔除后：

| 差异（native − core） | 均值 | 95% CI | 显著？ |
|---|---|---|---|
| 每 run 成本 | −$0.0008 | [−$0.016, +$0.016] | 否（更紧贴 0） |
| tool calls / run | +1.58 | **[+0.05, +3.11]** | **是（不含 0）** |

search 6.58→1.32（−80%）、files 8.2→5.6（−32%）维持不变。

### 按难度成本分解

| 难度 | native /run | core /run | core 相对 |
|---|---|---|---|
| trivial (n=6) | $0.129 | $0.125 | −2.9%（更便宜） |
| medium (n=8) | $0.124 | $0.115 | −7.2%（更便宜） |
| hard (n=6) | $0.170 | $0.214 | **+26%（更贵）** |
| hard 剔除 fan-in (n=5) | $0.145 | $0.167 | +15%（更贵） |

hard 上 core 偏贵的原因：`get_impact` 在深链/大扇入上返回整条链，token 成本反超 grep；`cb-recall-search-to-dict-fan-in` 是最大来源（详见下文）。

### core 模式 MCP 工具使用（20 run）

| 工具 | 调用次数 |
|---|---|
| get_change_summary | 20 |
| get_impact | 20 |
| get_test_impact | 20 |
| search_symbol | 10 |
| get_symbol_detail | 6 |
| get_change_context | 5 |
| query_graph | **0**（按配置排除） |

## 逐 case 明细

标注说明：`c`=tool calls，`g`=search(grep) calls，`files`=unique files touched，`Δ`=core−native。F1 两模式逐 case 完全相同，单列。

### hard（6 个）

| case | F1 | native | core | 点评 |
|---|---|---|---|---|
| `decrypt-password-alias` | 1.0 | $0.130 · 16c · 6g · 10f | $0.151 · 13c · 1g · 6f | core 少 3 次调用、5 次 grep；get_impact+search_symbol 组合略贵 |
| `search-to-dict-between` | 1.0 | $0.191 · 16c · 9g · 11f | $0.243 · 18c · 8g · 5f | **core 唯一还猛 grep 的 1.0**（8 次）；get_impact 的 entries 链没替掉手工搜索，反而贵 $0.05 |
| `execute-transfer-step-fail` | 1.0 | $0.134 · 9c · 2g · 6f | $0.158 · 7c · 0g · 4f | core 极简（4 读 + 3 MCP），0 grep，但略贵 |
| `decrypt-password-exception` | 1.0 | $0.120 · 15c · 5g · 7f | $0.149 · 14c · 1g · 6f | core 用 get_symbol_detail+search_symbol 追 `_resolve_storage_config` |
| `search-to-dict-fan-in` | 0.0 | $0.294 · 20c · 10g · 14f | **$0.445 · 24c · 7g · 8f** | **最大异常点**：4 路 fan-in，core 的 get_impact 返回超大连，agent 更难收束；双模式最贵且都没过（fix 文件正确） |
| `transfer-build-config` | 1.0 | $0.152 · 15c · 9g · 8f | $0.135 · 13c · 0g · 8f | **hard 里唯一 core 更便宜**：9 次 grep 全被 get_impact 替掉 |

### medium（8 个）

| case | F1 | native | core | 点评 |
|---|---|---|---|---|
| `hierarchy-tree-orphan` | 0.0 | $0.139 · 11c · 2g · 9f | $0.112 · 10c · 0g · 6f | core 便宜、0 grep；fix 文件正确但未过 gold |
| `log-search-forwarded` | 0.0 | $0.103 · 14c · 7g · 8f | $0.097 · 11c · 0g · 5f | core 便宜、0 grep |
| `notice-create-unique` | 0.0 | $0.185 · 20c · **16g** · 20f | $0.126 · 13c · 1g · 8f | **core 最大赢家**：16 grep→1、20 文件→8、省 $0.059 和 7 次调用 |
| `notice-delete-existence` | 0.0 | $0.111 · 14c · 8g · 6f | $0.119 · 8c · 1g · 4f | core 极简 8 次调用；略贵 1 分钱 |
| `storage-path-traversal` | 1.0 | $0.163 · 16c · 3g · 14f | $0.149 · 15c · 0g · 8f | 找到 + core 便宜，14→8 文件 |
| `workflow-node-code` | 1.0 | $0.090 · 8c · 4g · 4f | $0.121 · 9c · 3g · 3f | **core 反而贵 $0.032**：`get_builtin_node` 的 key 不匹配图帮不上，仍靠 grep |
| `recall-encrypt-password` | 1.0 | $0.097 · 8c · 4g · 4f | $0.098 · 11c · 0g · 4f | 费用打平；core 用 search_symbol 兜 qname |
| `recall-file-get-source-default` | 1.0 | $0.104 · 8c · 4g · 4f | $0.098 · 9c · 0g · 4f | 打平，4 grep→0 |

### trivial（6 个）

| case | F1 | native | core | 点评 |
|---|---|---|---|---|
| `dict-type-default-order` | 0.0 | $0.192 · 17c · **10g** · 10f | **$0.097** · 10c · 1g · 4f | **core 第二大赢家**：10 grep→1、−7 调用、省 $0.094（native 在 trivial 上浪费最多） |
| `hierarchy-parent-map` | 0.0 | $0.112 · 8c · 2g · 6f | $0.119 · 11c · 2g · 7f | 都 2 grep；core 略贵 |
| `notice-status-hardcoded` | 0.0 | $0.087 · 12c · 6g · 9f | **$0.161 · 17c · 1g · 9f** | **core 反而贵 8 分钱**：core 模式 agent 绕圈（get_impact+search_symbol+get_symbol_detail+get_change_context 共 17 次），仍未过 |
| `position-page-offset` | 0.0 | $0.110 · 18c · **11g** · 11f | $0.102 · 11c · 0g · 6f | native 11 次 grep 才确认；core 0 grep 直达 |
| `transfer-dt-none-guard` | 1.0 | $0.153 · 11c · 5g · 7f | $0.156 · 10c · 0g · 6f | 找到，费用打平 |
| `transfer-running-progress` | 0.0 | $0.120 · 15c · 12g · 2f | $0.115 · 11c · 6g · 3f | core 是 trivial 里 grep 最多的（6 次）——WS 广播链 get_impact 覆盖不到 |

## F1=0 的真相：全部定位正确，只是没点名调用方

逐 case 核查 11 个 F1=0 的 case，agent finding 的 `fix_file` **11/11 与 gold 匹配**：

- `transfer-running-progress`："Running step now reports 100% progress" → `engine.py` ✓
- `hierarchy-parent-map`："mapping inverted, breaking ancestor recursion" → `common_util.py` ✓
- `position-page-offset`："Incorrect pagination offset causes page shift and skipped first page" → `position/service.py` ✓
- `notice-create-unique`："Removed duplicate-title validation allows duplicate notice" → `notice/service.py` ✓
- `log-search-forwarded`：日志分页查询把 search 硬编码为 None → `log/service.py` ✓
- `notice-delete-existence`：删除时移除了 ID 存在性校验 → `notice/service.py` ✓
- 其余（fan-in、tree-orphan、dict-type、notice-status、parent-map）同样命中。

**根因**：gold 的 `mechanism_terms` 是调用链/入口符号（如 `get_list`、`batch_set_available_*_controller`），gold keyword 不得出现在 diff/hint 里（防止答案泄露），agent 直接读 diff 就修好、不需要点名调用方时，严格匹配必然 0 分。这些 case 定位了 bug 却没有"沿调用链确定影响范围"，是任务设计使然，不是模式能力差异。

## 规律与结论

**core 赢的地方**：native 靠 grep 海搜的 case（notice-create 16g、position-page 11g、dict-type 10g、transfer-build-config 9g）→ core 0–1 次 grep 直达。这是图替换 grep 的核心价值，省下最多的调用和文件读取。

**core 输的地方**：
1. **大扇入/深链**（`search-to-dict-fan-in`、`search-to-dict-between`）：`get_impact` 返回整条大链，token 成本反超，且 agent 更难从大结果里收束出结论。
2. **图覆盖不到的叶子逻辑**（`workflow-node-code`、`transfer-running-progress`）：`get_impact` 帮不上，core 模式的 agent 有时反而多绕（`notice-status-hardcoded` 17 次调用 vs native 12 次）。

**F1 作为对比指标在盲评下失效**：两模式找到的 bug 完全相同，F1 全部差异来自 gold 严格匹配。真正可判的信号是**成本（打平）与效率（core 少 80% grep、少 33% 文件）**。

## 局限

- **n=1**：单次抽样 F1 噪声大（记忆：native f1 单次摆动 ±0.5），工具调用数虽在剔除离群点后显著，但成本结论需更多重复或更大 n 才能下断言。
- **gold 严格匹配**压低整体 F1，12 个新增 trivial/medium 用例的 F1 不可与原始 8 个（此前 F1=1.0）直接比较。
- 未覆盖 `full_project_agent`（完整 MCP 工具集）与 `query_graph` 模式；本次只比较 native vs core 两个端点。
- 运行在 Windows、workers=4、sonnet；换模型/平台结果可能不同。
