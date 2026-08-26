# `case-backend` Case 扩充执行手册（AI 版）

本文给负责改仓库的 AI 一套可直接执行的步骤，用于扩充当前
`full_agent_eval/case-backend` 评测集。目标不是继续寻找“Graph 最容易赢”的题，而是先把
Native Agent 和 Graph Agent 都应覆盖的难度层补齐，再进行同题、同 Gold、配对的在线比较。

通用的 manifest、Gold 和评分规则仍以
[评测用例编写指南](EVALUATION_AUTHORING_GUIDE.md) 为准；抽样方法和 Native/Graph 对照原则见
[评测集设计](EVAL_SET_DESIGN.md)。本文只规定当前 `case-backend` 的扩充顺序、配额和验收门槛。

## 1. 不可改变的评测原则

1. Native 和 Graph **共用同一批 case、同一份 buggy patch、同一份 Gold**。不要为两种模式
   分别出题，也不要为 Graph 额外提供更详细的正确答案。
2. `full_agent_eval/case-backend` 永远保存正确的基线源码。bug 只存在于 manifest 的
   **bug 注入 patch** 中；该 patch 的应用方向是正确基线（fixed）→ 带回归版本（buggy），
   不是用于修 bug 的修复补丁。
3. 每条 case 必须先有可运行的回归测试，并证明同一个测试在 fixed 上通过、应用 patch 后失败。
4. Gold 必须从 fixed 代码、回归测试和可复核的业务契约独立标注，不能照抄 Graph 输出。
5. case manifest 不使用 `prompt` 字段。可选的 `hint` 只描述用户能观察到的回归，并且仅在
   `--hinted` 消融组中注入；正式 blind 组只看到统一任务说明和 diff。
6. `difficulty` 表示完成审查所需的推理深度，不表示预期由 Native 还是 Graph 获胜。

## 2. 当前基线与偏差

截至本文创建时，manifest 中有 8 条 case：

| 难度 | 数量 | 占比 |
| --- | ---: | ---: |
| trivial | 0 | 0% |
| medium | 2 | 25% |
| hard | 6 | 75% |

机制也比较集中：

- `search_to_dict`：2 条；
- 密码加解密：3 条；
- 存储传输：2 条；
- 存储来源默认值：1 条。

因此，当前集合更适合验证“深跨文件影响能否被 Graph 找回”，不适合直接回答“日常代码审查中
Graph 比 Native 总体好多少”。特别是，当前没有 diff-local、唯一文本可定位、简单状态契约等
Native 也应稳定完成的题。

## 3. 先补什么：两批扩充配额

### 第一批：从 8 条扩到 20 条

新增 12 条，固定配额如下：

- 6 条 trivial；
- 6 条 medium；
- 暂不新增 hard。

完成后应为 6 trivial / 8 medium / 6 hard，即 30% / 40% / 30%。第一批的作用是尽快纠正
当前的 hard 偏科，并验证整套 authoring 流程。

新增的 6 条 medium 中至少包含：

- 3 条 `grep-friendly`：关键符号或常量可以通过直接文本搜索稳定定位；
- 2 条 `graph-blind-spot`：依赖字典映射、注册表、装饰器参数或配置字符串，静态调用图不一定
  能表达关系；
- 其余 1 条可根据模块覆盖补齐，但不得复制现有 8 条的回归机制。

### 第二批：从 20 条扩到 24 条

再新增 4 条 trivial。完成后应为 10 trivial / 8 medium / 6 hard，约为
42% / 33% / 25%，接近设计文档建议的 40% / 35% / 25%。

在达到 24 条之前，除非是在替换无效 case，否则不要继续新增 hard。达到 24 条后，再根据第一轮
在线结果决定是否增加新的仓库、语言和 hard 结构；不要只在同一 Python fixture 中无限扩题。

### 每条新增 case 的标签

在 `complexity_tags` 中至少记录两类信息：

- 可发现性：`diff-local`、`grep-friendly`、`graph-blind-spot` 或 `graph-advantaged`；
- 回归族：以 `family-` 开头，例如 `family-storage-routing`、`family-workflow-registry`。

同一 `family-*` 最多保留 2 条，否则聚合分数会被一个机制重复加权。标签只是分析维度，不会注入
Agent 输入，也不应重复写进 `hint`。

## 4. 当前源码中的候选池

下表是选题入口，不是已经确认的 Gold。AI 必须逐条执行第 5 节的 fixed/buggy 验证；如果不能
形成单一、可复现的回归，就放弃候选，不得为了凑配额硬写。

| 优先层 | 候选位置 | 可制造的单一回归 | 建议标签 | 为什么值得覆盖 |
| --- | --- | --- | --- | --- |
| trivial | `module_storage/transfer/engine.py::_dt` | `None` 或时间序列化语义错误 | `diff-local`, `family-transfer-payload` | 同文件、契约明确，Native 应稳定命中 |
| trivial | `module_storage/transfer/engine.py::_STEP_RUNNING_PROGRESS` | 执行中进度写成错误状态值 | `grep-friendly`, `family-transfer-state` | 唯一常量和直接引用，适合作为基本盘 |
| trivial | `module_storage/transfer/registry.py::TransferTaskRegistry.clear` | 清理后取消状态仍残留 | `diff-local`, `family-transfer-registry` | 小型状态机，可用确定性单测验证 |
| trivial/medium | `module_storage/transfer/registry.py::TransferTaskRegistry.is_canceled` | 对不存在或已取消任务返回错误结果 | `grep-friendly`, `family-transfer-registry` | 可先测本地契约，再决定是否纳入入口传播 |
| medium | `module_storage/core/constants.py::DEFAULT_PORTS` | 某协议默认端口映射错误 | `grep-friendly`, `family-storage-defaults` | 常量被 schema 和 service 直接消费 |
| medium | `module_storage/core/base.py::StorageAdapterConfig.full_prefix` | 前缀拼接或斜杠归一化错误 | `grep-friendly`, `family-storage-path` | 属性跨 adapter 使用，但仍可文本追踪 |
| medium | `module_storage/file/service.py::_validate_remote_path` | 放过 `..` 等越界路径或错误拒绝合法路径 | `grep-friendly`, `family-storage-path` | 有明确安全契约和多个直接调用方 |
| medium | `module_storage/core/factory.py::_STORAGE_ADAPTERS` | 协议映射到错误 adapter | `graph-blind-spot`, `family-storage-routing` | 数据映射关系不一定形成调用边 |
| medium | `module_task/workflow/flows/handlers/builtin_nodes.py::BUILTIN_NODES` | 注册 code 与查询 code 不一致 | `graph-blind-spot`, `family-workflow-registry` | 装饰器和字典注册是 Graph 的典型盲区 |
| medium | 同文件的 `builtin_node` / `get_builtin_node` / `builtin_node_options` | 注册、执行和下拉选项看到不同节点集合 | `graph-blind-spot`, `family-workflow-registry` | 可比较数据流发现与调用链发现 |

选题时还要遵守：

- 先搜索 manifest，排除与现有 8 条相同的根因、修复点或传播结构；
- 优先覆盖尚未出现的模块和机制，不要再优先增加密码加解密或 `search_to_dict` 变体；
- 一个 patch 只破坏一个生产契约，通常只改一个生产位置；
- 如果候选同时需要修改两个互不依赖的位置才能让测试失败，应拆成两条或放弃；
- 如果静态源码里没有足够证据，Native 和 Graph 都只能猜，则不能作为普通对照 case。

## 5. AI 新增一条 case 的执行算法

以下步骤必须按顺序完成。未通过前一步，不得开始下一步。

### 5.1 建立候选证据卡

在改代码前先记录以下内容，可写在工作记录或 PR 描述中，不要放进 case 的 `hint`：

```text
候选 ID：
业务契约：fixed 版本为什么是对的：
计划制造的唯一回归：
实际修复位置：
预期受影响 symbols / files / entries / tests：
容易混淆但不应命中的 hard negatives：
Native 最短发现路径（只允许 Read/Glob/Grep/只读 Bash）：
Graph 预期提供的额外证据：
预定 difficulty：
预定 complexity_tags：
```

如果无法清楚写出“业务契约”和“唯一回归”，立即换候选。

### 5.2 先写 focused regression test

测试写入 `full_agent_eval/case-backend/tests/`，要求：

- 测试名描述用户可观察行为，而不是实现细节；
- 不访问网络、真实 Redis、真实数据库或外部服务；
- 不依赖当前时间、随机数或用例顺序；
- 失败断言能说明回归机制，而不是仅检查“抛了任意异常”；
- 能以一个精确 node id 单独运行。

在 fixed 树上执行并保存结果：

```powershell
Push-Location full_agent_eval/case-backend
uv run pytest tests/test_<feature>_regressions.py::test_<behavior> -q
Pop-Location
```

预期：通过。fixed 版本不通过时，先修复测试设计或放弃候选，不能把 fixture 改成迎合测试。

### 5.3 在临时副本导出 bug 注入 patch

不要直接改正确基线 fixture。复制到临时目录、初始化 Git、提交正确基线，再制造回归。此后
`git diff` 导出的就是“正确代码 → 错误代码”的 bug 注入 patch：

```powershell
$caseTmp = Join-Path $env:TEMP "case-backend-authoring-<case-id>"
Copy-Item full_agent_eval/case-backend $caseTmp -Recurse
git -C $caseTmp init
git -C $caseTmp add -A
git -C $caseTmp -c user.name=eval -c user.email=eval@example.invalid commit -m fixed

# 仅在 $caseTmp 中制造回归，然后导出：
git -C $caseTmp diff -- app/path/to/file.py
```

把 diff 原样放入 `benchmarks/case-backend-cases.json` 的 `patch`。路径必须是 `a/app/...`
和 `b/app/...` 形式。

### 5.4 证明同一个测试在 buggy 上失败

在临时副本中运行 5.2 的同一个 node id：

```powershell
Push-Location $caseTmp
uv run pytest tests/test_<feature>_regressions.py::test_<behavior> -q
Pop-Location
```

预期：失败，并且失败原因正是证据卡中声明的回归。若测试仍通过、导入失败、fixture 启动失败或
失败原因无关，则 patch 无效。

### 5.5 独立填写 manifest 和 Gold

按 [评测用例编写指南](EVALUATION_AUTHORING_GUIDE.md) 填写 manifest。当前仓库对新增 case 的
最低要求是：

- `changed_symbols` 精确指向 patch 实际改变的生产符号；
- `gold.root_causes[*].fix_file` 是实际应修位置，而不只是报错位置；
- `gold.context.symbols/files/entries/tests` 四个维度均有可验证内容；
- 至少有一个 `hard_negatives.symbols` 或 `hard_negatives.files`；
- `alternate_files` 默认留空，只有两个文件确实是等价修复位置时才能填写；
- `mechanism_terms` 不得出现在 patch 或 `hint` 中；现有防泄漏测试会自动检查；
- `test_names` 只写真实测试 node，不要把名字以 `test_` 开头的生产函数误标为测试。

Gold 只收录“定位和解释该回归所必需”的影响，不把同社区、同文件或 Graph 返回的所有结果都
塞进去。

### 5.6 做 Native 可解性审计

在运行 Graph preflight 前，用 Native 允许的工具重新走一遍：

1. 从执行器的统一 blind 任务说明和 diff 出发，不读取 `hint`；
2. 记录第一条合理的 `rg` 查询；
3. 记录为判断根因必须阅读的最少文件；
4. 确认不依赖 Gold 中泄露的 qname、测试名或入口名；
5. 判断熟练 reviewer 是否能在有限轮次内形成正确 finding。

trivial/medium case 如果连这条路径都不存在，应改为 robustness 专项或放弃，不能继续当作公平的
Native/Graph 对照。`hint` 仅用于额外业务背景的消融组，不能拿它弥补 blind case 本身不可解。

### 5.7 运行 Graph preflight

Graph preflight 用来检查索引、变更符号和独立 Gold 是否一致，不用来生成 Gold：

```powershell
.\.venv\Scripts\code-review-ai.exe full-agent-eval `
  --cases benchmarks/case-backend-cases.json `
  --case-ids <case-id> `
  --dry-run `
  -o .code-review-ai/<case-id>-preflight.json
```

检查：patch 能应用、changed symbol 能解析、四个 context 维度有合理召回、hard negative 未被错误
命中。如果 Graph 与 Gold 不一致，回到 fixed 代码和测试取证；只有独立证据支持时才改 Gold，不能
因为工具输出不同就照抄工具输出。

### 5.8 运行 schema、防泄漏和评分测试

```powershell
.\.venv\Scripts\pytest.exe `
  tests/test_eval_gold.py `
  tests/test_case_backend_recall.py `
  tests/test_full_agent_eval.py `
  tests/test_agent_adapter.py -q

git diff --check
```

### 5.9 最后才做在线盲测

offline oracle 全部通过后，先对单条 case 做 Native/Graph 同题试跑。两种模式使用相同的统一审查
任务、diff、buggy repo、hint 开关和 Gold；执行器只按模式加入相应的工具能力说明。每种至少 3 次
repetition，不要看完一种模式的答案后再修改 case 或另一种模式的输入。

```powershell
.\.venv\Scripts\python.exe -m code_review_ai.agent_adapter claude `
  --cases benchmarks/case-backend-cases.json `
  --case-ids <case-id> `
  --modes native_agent full_project_core `
  --repetitions 3 `
  -o .code-review-ai/<case-id>-paired.json
```

在线失败首先区分：case 无法启动、Agent 调用失败、输出解析失败，还是审查结论真的错误。基础设施
失败不能按模型能力失败计分。

## 6. 单条 case 的验收清单

- [ ] 与现有 8 条不是同一回归机制的改名版本。
- [ ] evidence card 已记录 fixed 契约、修复点和 Native 最短发现路径。
- [ ] focused test 在 fixed 上通过。
- [ ] 同一个 focused test 在 buggy 上因预期断言失败。
- [ ] patch 只引入一个可解释的生产回归，并能被 dry-run 应用。
- [ ] patch 和可选 `hint` 不泄露 Gold 的 `mechanism_terms`；manifest 不含 `prompt` 字段。
- [ ] `fix_file`、symbols、files、entries、tests 均有独立源码或测试证据。
- [ ] 至少标注一个可解释的 hard negative。
- [ ] `complexity_tags` 包含可发现性和 `family-*` 两类标签。
- [ ] 难度及配额符合第 3 节，未擅自继续增加 hard。
- [ ] Graph preflight 与 schema、防泄漏、评分测试全部通过。
- [ ] Gold 经第二次独立复核，没有从 Graph 输出反向污染。

## 7. 必须拒绝或返工的情况

出现以下任一项，不得合入：

- patch 应用后 focused test 仍通过，或 fixed 版本本来就失败；
- 需要真实外部服务、私有数据或不稳定时间条件才能复现；
- `hint` 直接给出修复符号、文件、入口、测试名或 `mechanism_terms`；
- Gold 的唯一来源是 Graph preflight 输出；
- Native 和 Graph 都缺少静态证据，只能猜运行时行为；
- 一个 case 混合多个独立根因，任意一个都可单独修复；
- 同一 `family-*` 已有 2 条，新增 case 没有提供新的传播结构；
- 仅因 exact qname 格式或评分器格式偏好，伪造出某种模式的优势。

## 8. 批次跟踪模板

扩充时维护下表，每完成一个门槛再更新状态：

| Case ID | 候选/模块 | 难度 | 可发现性 | Family | fixed pass | buggy fail | Native 审计 | Graph preflight | Gold 复核 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `<id>` | `<symbol>` | trivial | grep-friendly | family-... | ☐ | ☐ | ☐ | ☐ | ☐ | draft |

## 9. 可以开始“全量 Native vs Graph”测试的定义

至少满足以下条件后，再投入完整在线评测成本：

1. 总数达到 24 条，分布约为 10 trivial / 8 medium / 6 hard；
2. 每个 `family-*` 不超过 2 条，且不再只集中于加解密、搜索转换和存储传输；
3. 24 条全部有 fixed-pass / buggy-fail 的同测试证据；
4. schema、防泄漏、patch dry-run 和 changed-symbol preflight 全部通过；
5. Gold 完成独立二审；
6. 先用 2 条 case 做在线 pilot，确认两种模式的启动、遥测、输出解析和配对记录都可靠；
7. 完整运行时保留每个 case × repetition 的原始结果，最终按难度、family 和可发现性分层聚合，
   不只报告一个总平均分。

达到这些门槛后，评测结果才能较可信地说明：Native 的基本盘是否稳定、Graph 在深调用链上增加了
多少收益，以及 Graph 在数据驱动/动态注册结构上是否存在退化。
