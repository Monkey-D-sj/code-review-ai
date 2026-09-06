# review_loop vs native：怎么跑对比测试

> 目标：可复现地量化「code-review-ai 索引产品（worksheet 图）」相对「不用索引的
> 原生评审」的 token / 成本 / 收敛 / 命中差异。三档形态，两条命令。

## 一、三档形态（先定好"比什么"）

| 形态 | 谁在跑 | 输入 | 定位影响面靠 | 收尾 |
|---|---|---|---|---|
| `native_agent` | 真 Claude Code（`claude` CLI，经 `agent_adapter`） | 内嵌 diff + 受控 prompt | 原生 `Read`/`Grep`/`Bash(rg)`，无 MCP | 自由文本 → harness 解析 |
| `product` | review_loop `run_review`（worksheet） | diff + **索引 summary**（候选行） | `get_impact`（调用图/别名/affected_entries） | `update_review_item` 全决 |
| `plain` | review_loop `run_free_loop`（free-form） | 只 diff（无 summary） | 只 `read_file`/`search_code`，**无 get_impact** | `finish_review` |

- **产品 vs 原生** = `product` vs `native_agent`（或 `product` vs `plain`）。
- **图工具本身的边际** = review_loop 内 `product` vs `plain`（同框架同记账，唯一差 get_impact+summary 的组合；free-form 下单独拿掉 get_impact 收益≈0，见 §五）。

## 二、跑法

### native（真 Claude Code，无图）
```bash
uv run --no-sync python -m code_review_ai.cli full-agent-eval \
  --cases benchmarks/case-backend-cases.json \
  --case-ids case-backend-decrypt-password-alias \
  --agent-command "C:\Users\44550\Desktop\code-review-ai\.venv\Scripts\python.exe -m code_review_ai.agent_adapter claude" \
  --model deepseek-v4-flash \
  --modes native_agent \
  --repetitions 3 \
  --work-dir eval-results/<run-name> \
  -o eval-results/<run-name>/report.json
```
- `--agent-command` 必须用 **venv python 绝对路径**（Windows 陷阱见
  `docs/full-agent-eval-windows-run-guide.md`）。
- 结果在 `report.json`（聚合在 `aggregate.native_agent`）+ `transcripts/.../run-N.json`（逐条 `parsed_output`/`tool_trace`）。

### review_loop（product 与 plain，同一脚本、同批跑）
```bash
uv run --frozen python benchmarks/review_loop_case_compare.py \
  --runs 6 --arms product plain \
  -o eval-results/<run-name>.json
```
- 每 run 独立 materialize（git init + apply patch）+ rebuild 索引 + 跑一臂。
- 输出控制台逐行 + JSON（`rows` 每条含 complete/gold_hit/search/impact/tools/total/input/cache_read/cost）。

## 三、指标与口径（最容易出错的地方）

| 指标 | native | review_loop | 注意 |
|---|---|---|---|
| 命中 | `aggregate.macro_f1` / run `matched_findings` | `gold_hit`（finding 的 file 是否 = gold fix_file） | 命中是"收敛后都中"，别把不收敛算 0 |
| total tokens | `aggregate.mean_total_tokens`（**input_tokens 是 fresh，不含 cache_read**） | `usage.total_tokens`（input 含全部 cache 命中） | **两个 harness 记账口径不同，跨 harness 直接相减不可靠**；要同口径只在 review_loop 内比 |
| cache | `cache_read_input_tokens` 单列、值巨大（多轮全量重发命中累加） | `usage.cache_read` | 真实吞吐 ≈ fresh/cache 分开看 |
| cost | `total_cost_usd`（USD 计价表） | `compute_cost`（DeepSeek 人民币单价：miss1.5 / hit0.05 / out4.5 元每百万） | **货币/单价表不同，别直接减**；要省多少，先把两边放同一张单价表 |
| 收敛 | run `success` | `complete` / `failure_reason` | product 空轮会被 nudge；plain 空轮=显式失败 |

**样本量**：单 run 方差大，`--repetitions 3` 起步、6 更稳（native 更慢更贵，常 3）。

## 四、数据落点

- `eval-results/` 已被 gitignore，报告/transcript 留本地即可。
- review_loop 对比脚本已入库：`benchmarks/review_loop_case_compare.py`（可改 `--case`/`--runs`/`--arms`）。

## 五、alias case 现状快照（2026-09，deepseek-v4-flash）

| 形态 | n | avg total tokens | 收敛/命中 | search 次数 |
|---|---|---|---|---|
| `native_agent` | 3 | ~30,970 | 3/3 · F1 1.0 | 全靠 grep |
| `product` | 6 | ~7,700（修复 400 前成功样本） | 5/6 · 5/5 | 0（1 次除外） |
| `plain` | 6 | ~23,375 | 5/6 · 5/5 | 6/6 |

- 结论口径：**token 省 ~75%（native 的 ¼）；同 DeepSeek 单价表下成本省约一个数量级**。
- 注意：product 那 5/6 的一次失败是已修复的 loop bug（DeepSeek 400，见 `7a898d7`），
  **修复后应重跑 product 确认 6/6** 再当正式数。
- free-form 消融发现：单独拿掉 get_impact（不拿掉 summary）≈0 token 收益——get_impact
  依赖前一步索引给的 qname 才有用，价值在与 summary 配套（产品不能拆开卖）。

## 六、跑完一个 case 的核对清单

1. native：`--dry-run` 先预检（建 worktree+索引，无 LLM 成本）；确认 3/3 success 再读 token。
2. review_loop：先 `--runs 1` 冒烟（确认 400 类 bug 不出现、两臂都能收敛），再扩 runs。
3. 读数时先标口径再比较；gold 命中只看"收敛的那几次"。
4. 结论别单 case 下：alias 是单符号局部 bug，图最该赢；换深链路/多符号 case 再验。
