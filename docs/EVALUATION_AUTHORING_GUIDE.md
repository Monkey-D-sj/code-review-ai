# 评测用例编写指南

本文说明如何继续扩充 `case-backend` 评测集，以及如何运行、检查和解释评测结果。

当前推荐做法是：把 `full_agent_eval/case-backend` 作为固定的“正确版本”源码树，在
`benchmarks/case-backend-cases.json` 中为每个回归保存一份 **fixed → buggy** 的内联
patch，并用同一个结构化 `gold` 同时驱动 Graph Retrieval 和 Agent Review 两层评分。

## 1. 评测到底在测什么

一条 case 会产生两类互相独立的结果：

| 层级 | 报告字段 | 测量对象 | 是否随 Agent 模式变化 |
| --- | --- | --- | --- |
| Graph Retrieval | `graph_retrieval` | 索引能否从变更符号找全相关符号、文件、入口和测试 | 否；每个 case 计算一次 |
| Agent Review | `agent_review` | Agent 能否判断根因，并给出正确影响范围 | 是；每种模式、每次 repetition 单独评分 |

因此不要用 `graph_retrieval` 比较 Native Agent 和 Full Project Agent。模式对比应查看
`aggregate.<mode>.agent_review`，同时结合成功率、工具采用率、文件读取量和调用次数。

默认在线对比模式是：

- `native_agent`：只有 Read、Glob、Grep 和受限的只读 Bash。
- `full_project_core`：Native 工具加产品核心 MCP 工具集。

需要验证完整 MCP 工具集时，显式使用
`--modes native_agent full_project_agent`。其余 `full_project_*` 模式主要用于工具消融，
不应混入日常回归基线。

## 2. 文件放在哪里

- 正确版本源码：`full_agent_eval/case-backend/`
- case 清单：`benchmarks/case-backend-cases.json`
- Gold 解析和评分：`code_review_ai/eval_gold.py`
- 完整评测执行器：`code_review_ai/full_agent_eval.py`
- schema 和防泄漏测试：`tests/test_case_backend_recall.py`
- Gold 单元测试：`tests/test_eval_gold.py`

评测报告和运行中间产物放在 `.code-review-ai/`，不要提交到仓库。

## 3. 新增一条 case 的标准流程

### 第一步：选择有区分度的回归

优先选择以下类型：

- 变更点很小，但影响跨文件、跨模块或跨入口。
- 调用方存在别名导入、统一 service、任务处理器、定时任务或公共工具函数。
- 只看 diff 可以发现“代码可疑”，但不能完整判断业务影响。
- 有明确、可验证的正确答案，不依赖网络、时间或随机状态。

尽量不要选择：

- 只看变更行就能完整回答、没有任何影响传播的 trivial bug。
- 必须运行外部服务或依赖私有数据才能证明的 bug。
- Gold 只能依赖维护者主观判断、无法从代码或测试验证的 bug。
- 与现有 case 只换了变量名、没有增加新结构覆盖的重复案例。

一条 case 最好只表达一个回归机制。只有当两个问题需要修改不同生产代码位置、修复其中
一个后另一个仍然存在时，才写成两个 `root_causes`。每条回答最多允许 3 个 finding，
因此单个 case 不应包含超过 3 个独立根因。

### 第二步：准备 fixed → buggy patch

`full_agent_eval/case-backend` 始终保存正确代码。patch 则把正确代码改坏：

```text
固定源码树（fixed） --应用 manifest.patch--> 隔离评测仓库（buggy）
```

不要把 buggy 代码直接提交进源码树。可以在临时副本中初始化 Git、制造回归，再复制标准
diff：

```bash
cp -R full_agent_eval/case-backend /tmp/case-backend-new-case
git -C /tmp/case-backend-new-case init
git -C /tmp/case-backend-new-case add -A
git -C /tmp/case-backend-new-case \
  -c user.name=eval -c user.email=eval@example.invalid \
  commit -m fixed

# 在临时副本里制造回归后：
git -C /tmp/case-backend-new-case diff -- app/path/to/file.py
```

复制出来的 patch 路径必须是仓库相对路径，例如 `a/app/...` 和 `b/app/...`。dry-run 会在
隔离副本中实际执行 `git apply`，所以它也是 patch 方向和格式的最终校验。

### 第三步：填写 manifest

可复制的最小模板如下：

```json
{
  "id": "case-backend-short-stable-name",
  "repo_name": "case-backend",
  "repo_url": "",
  "source_commit": "",
  "mutation_paths": [],
  "source_dir": "full_agent_eval/case-backend",
  "patch": "diff --git a/app/... b/app/...\n...",
  "hint": "只描述回归现象和业务契约，不直接说出调用方答案。",
  "difficulty": "hard",
  "complexity_tags": ["cross-module", "shared-util"],
  "gold": {
    "root_causes": [
      {
        "id": "stable-root-cause-id",
        "fix_file": "app/path/to/fix.py",
        "alternate_files": ["app/path/to/accepted/report/location.py"],
        "mechanism_terms": ["StableCaller", "affected_operation"],
        "min_matches": 2
      }
    ],
    "context": {
      "symbols": ["app.module.service::StableCaller.method"],
      "files": [
        "app/path/to/fix.py",
        "app/path/to/affected_caller.py"
      ],
      "entries": ["app.module.controller::public_entry"],
      "tests": ["tests/test_feature.py::test_regression"],
      "hard_negatives": {
        "symbols": ["app.unrelated.service::SimilarName.method"],
        "files": ["app/unrelated/service.py"]
      }
    }
  }
}
```

内联 patch 模式下，`repo_url`、`source_commit` 和 `mutation_paths` 保持上面的空值；
`source_dir` 必须指向正确版本源码树。已有个别记录中的 `changed_symbols` 只是辅助信息，
patch 模式会从 diff 自动检测变更符号，不能把该字段当作 Gold。

### 第四步：先跑单 case preflight

```bash
code-review-ai full-agent-eval \
  --cases benchmarks/case-backend-cases.json \
  --case-ids case-backend-short-stable-name \
  --dry-run \
  -o .code-review-ai/full-agent-preflight.json
```

preflight 不调用模型，但会完成以下检查：

1. manifest 和统一 Gold 能否解析。
2. patch 能否应用到正确版本源码树。
3. 变更符号能否从 patch 检出并在索引中找到。
4. Graph Retrieval 对四个 context 维度的命中、遗漏和误报。

只有单 case 通过后，再跑完整清单。

## 4. Gold 怎么标

### 4.1 `root_causes`

一个 root cause 代表一个独立生产代码修复单元。

- `id`：稳定、唯一的机器标识；修正文案时不要随意改名。
- `fix_file`：真正应该修改的生产文件，不是任意受影响调用方。
- `alternate_files`：Agent 在这些位置报告同一根因也应算正确。它不是影响文件清单，不能把
  所有 context files 都复制进来。
- `line_start` / `line_end`：可选。只有行号在固定源码树中长期稳定、且报告位置确实重要时
  才填写；填写后预测行号必须落在区间内。
- `mechanism_terms`：判断描述是否真的沿调用链找到了机制的稳定标识符。
- `min_matches`：title 与 description 合并后至少要命中的 term 数量。

建议每个根因选择 2–4 个稳定 term，并将 `min_matches` 设为 2。term 匹配不区分大小写，
采用子串匹配。

最重要的防泄漏规则：`mechanism_terms` 不得出现在 diff 或 `hint` 中。否则 Agent 只需复述
输入就能得分，评测无法证明它完成了图遍历。现有测试会自动检查这一点。

不要使用“crash”“error”“service”这类过于通用的词；优先使用必须读取受影响调用链才能
发现的类名、函数名或业务操作标识。

### 4.2 `context`

四个正向维度都使用精确字符串集合：

- `symbols`：受影响的生产符号，使用索引 qname，例如
  `app.module.service::Class.method`。
- `files`：受影响生产文件，使用 `/` 分隔的仓库相对路径。
- `entries`：受影响的外部入口，如 HTTP controller、CLI、定时任务或消费入口；字符串应与
  `get_impact` 的 `affected_entries` 输出一致。
- `tests`：应该运行的测试。Graph Retrieval 的测试证据可能包含测试 qname、测试文件路径
  或测试入口，Gold 应选择系统实际能够稳定返回、且人工确认相关的标识。

空数组表示该维度 **尚未标注或确实不适用**，评分会返回 `applicable: false`，不会记成
0 分。为了让评测逐渐完整，新 case 原则上至少标注 `files`，并优先补齐 `symbols`、
`entries` 和 `tests`。

可以先运行 preflight，用 `cases[].graph_retrieval.evidence` 帮助发现候选项；在线报告中的
对应路径是 `graph_retrieval.cases[].evidence`。但不能把工具返回的所有内容直接复制成
Gold。每一项仍需通过源码、调用链或测试语义人工确认，否则会形成“用被测系统定义自己的
答案”的循环评测。

### 4.3 `hard_negatives`

Hard negative 是名称或结构上很像、但实际上不受影响的符号或文件，用于识别过度扩散。

适合标注的例子：

- 同名 service 或方法，但属于另一业务模块。
- 同一接口的不同实现，其中只有一个实现位于实际调用路径。
- 被文本搜索命中，但没有可达调用关系的文件。

不要为了增加难度随意选无关文件。每个 hard negative 都应有“为什么容易被误判、为什么
实际上不受影响”的代码证据。报告中的 `hard_negative_correctness` 为 1 表示没有误命中，
越低说明扩散越严重。

## 5. `hint`、难度和标签

正式结果默认是 blind：Agent 只看到统一任务说明和 diff，看不到 `hint`。`--hinted` 是
消融实验，用来判断给出额外业务背景能带来多大帮助，不应作为正式基线。

`hint` 只描述业务契约或可观察症状，不要直接写出：

- 受影响调用方、入口或测试的名字。
- `mechanism_terms` 中的任何词。
- 应调用哪个图工具、沿哪个符号向哪个方向查询。

难度只能是 `trivial`、`medium`、`hard` 或 `unclassified`：

- `trivial`：变更文件内即可完成根因和影响判断。
- `medium`：需要一到两跳调用关系或一个明确跨文件调用方。
- `hard`：深链路、别名/重导出、多入口、共享基础设施、状态机或容易混淆的同名目标。

`complexity_tags` 用于分组分析，不参与得分。优先复用已有标签，只有确实出现新结构时才
增加新标签。

## 6. 在线评测

先只跑新增 case，建议至少 3 次 repetition：

```bash
code-review-ai full-agent-eval \
  --cases benchmarks/case-backend-cases.json \
  --case-ids case-backend-short-stable-name \
  --modes native_agent full_project_core \
  --agent-command "python -m code_review_ai.agent_adapter claude --model sonnet --max-budget-usd 1.00" \
  --repetitions 3 --workers 2 \
  -o .code-review-ai/full-agent-report.json
```

确认新增 case 行为合理后，再去掉 `--case-ids` 跑完整回归集。固定对比模型、预算、模式、
repetition 数和 prompt 配置；一次只改变一个实验变量。

Agent 的结构化回答包含：

```json
{
  "findings": [
    {
      "file": "app/path.py",
      "line": 10,
      "title": "...",
      "description": "..."
    }
  ],
  "affected_symbols": [],
  "affected_files": [],
  "affected_entries": [],
  "tests": [],
  "files_read": [],
  "tool_calls": []
}
```

`findings` 用于 root cause 评分，其余 `affected_*` 和 `tests` 用于 Agent Review 的 context
评分。Native 文件访问和 MCP 调用以执行器观测到的 telemetry 为准，不要只相信模型自行
填写的 `files_read` / `tool_calls`。

## 7. 如何读报告

### 7.1 先判断评测是否有效

先检查：

- `success` 和 `failure_reasons`：解析失败、超时或 provider 失败不能当成模型能力差。
- 变更符号应全部成功进入索引：preflight 查看
  `aggregate.graph_retrieval.symbol_found_rate`，在线报告查看
  `graph_retrieval.aggregate.symbol_found_rate`。
- 每个适用维度的 `misses`：确认是真漏召回，还是 Gold 拼写/路径格式错误。
- `unknown_file_access`：为 true 时，Native 的文件访问量不能视为完整观测。
- `hint_mode`、`guidance_mode`、模型和预算是否与对照组一致。

### 7.2 再看能力指标

Graph Retrieval：

- `macro_recall`：Gold 影响面找回了多少，通常是第一优先级。
- `macro_precision`：返回内容中有多少属于 Gold；只有 Gold 足够完整时才有意义。
- `macro_f1`：召回与精确率的折中。
- `hard_negative_correctness`：是否避开已知误导项。

Agent Review：

- `aggregate.<mode>.agent_review.macro_root_cause_f1`：根因发现质量。
- `aggregate.<mode>.agent_review.macro_affected_context_recall`：Agent 最终明确报告的影响面召回。
- 每个 run 的 `agent_review.root_causes` 和 `agent_review.affected_context`：定位具体漏项。

如果 context 只标了部分真实影响项，低 precision 可能只是 Gold 不完整。此时先完善 Gold，
不要立刻通过裁剪检索结果来“优化”分数。

模式比较应按同一个 case、同一个 repetition 配对观察，并同时报告质量、耗时、token/响应
字符量、文件读取量和工具调用量。不要只用一次运行的总体平均数宣布收益。

## 8. 修正 Gold 后重评分

如果只是根据证据修正 Gold，不必重新调用模型。保留原报告和 transcript，然后运行：

```bash
code-review-ai full-agent-eval-rescore \
  --report .code-review-ai/full-agent-report.json \
  --cases benchmarks/case-backend-cases.json \
  --transcripts .code-review-ai/full-agent-eval/transcripts \
  -o .code-review-ai/full-agent-report-rescored.json
```

只允许在看到模型输出前已经定义、或能由独立代码证据证明的 Gold 修正。不能因为某个模式
没有命中就删答案，也不能因为它报了某项就把该项追加为 Gold。

## 9. 提交前检查

运行 schema、防泄漏和评分测试：

```bash
python -m pytest \
  tests/test_eval_gold.py \
  tests/test_case_backend_recall.py \
  tests/test_full_agent_eval.py \
  tests/test_agent_adapter.py -q
```

提交一条新 case 前确认：

- [ ] fixed 源码树仍然是正确实现，bug 只存在于 patch。
- [ ] patch 是 fixed → buggy，且单 case dry-run 能成功应用。
- [ ] 变更符号全部找到，`symbol_found_rate` 为 1。
- [ ] `fix_file` 是实际修复位置，`alternate_files` 没有被当成 context 清单。
- [ ] `mechanism_terms` 不出现在 diff 或 hint 中。
- [ ] 至少标注完整的受影响文件；其他空维度有明确原因。
- [ ] 每个 Gold context 和 hard negative 都经人工验证。
- [ ] blind 模式下至少运行 3 次，并保留失败原因和 telemetry。
- [ ] 新 case 增加了新的业务结构覆盖，而不是重复已有题型。

## 10. 何时需要改评分代码

通常新增 case 只修改 manifest 和必要的 fixed fixture/test，不需要改 Python 评分器。

只有新增新的 Gold 维度、改变匹配语义或改变 Agent 输出契约时，才同时修改：

1. `code_review_ai/eval_gold.py` 的数据模型、解析、序列化和评分。
2. `code_review_ai/agent_adapter.py` 的输出 schema。
3. `code_review_ai/agent_eval.py` 与 `code_review_ai/full_agent_eval.py` 的 prompt/报告聚合。
4. `tests/test_eval_gold.py`、完整评测测试和本文档。

不要为单个 case 在评分器中加入特判；特判应表达在结构化 Gold 中，或说明当前 schema 缺少
一种可复用的评测概念。
