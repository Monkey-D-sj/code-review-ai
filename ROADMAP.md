# code-review-ai Roadmap

## 2026-08-10：热索引在线 Agentic Eval 进展

- 已新增 `full-agent-eval`：从真实修复提交创建隔离 worktree，只反向恢复生产文件，并让 Agent 在完整仓库中使用真实 MCP。
- 已覆盖 itsdangerous、p-limit、Gson 三个公开仓库和 Python / JavaScript / Java 三种语言；6 个案例预检识别 8/8 个变更符号。
- 新版正式轮共 36 次 bare Claude 调用，36/36 成功；索引在 Agent 计时前预建，`rebuild_index` 不对 Agent 开放，完整项目组 18/18 自主使用 MCP。
- 相比仅有 Read/Glob/Grep 的 Native Agent，完整项目组 Precision 由 85.2% 到 88.0%，F1 由 90.4% 到 91.3%，Recall 由 100.0% 到 97.2%。
- 配对 F1 差值为 +0.9 个百分点，按 case 聚类的 95% bootstrap 区间为 -9.8 到 +8.5；质量结论仍不显著。
- 完整项目组读取文件数下降 25.3%，单次成本仅增加约 3.0%，平均延迟增加约 2.3%；`get_impact` 仅在 9/18 次运行中被按需选择。
- 下一步优先扩展到 30+ 个预注册、盲审标注的自然 PR，并优化 MCP 返回预算，争取把文件读取缩减进一步转化为 Token / 延迟下降。
- `agent-eval` 中的 `graph_agent` 已明确降级为预计算 `get_impact` 上下文消融，不再代表完整项目效果。

本路线图面向项目的下一阶段：不再以继续堆叠静态分析功能为主，而是重点证明
`code-review-ai` 能否让 Coding Agent 用更少的上下文完成更准确、更高效的代码评审。

## 一、当前能力基线

项目已经具备一条完整的 AI 代码评审基础设施链路：

- Tree-sitter 多语言解析：Python / TypeScript / JavaScript / Vue / Java。
- SQLite 函数级调用图、BFS 调用流、Leiden 社区与增量索引。
- 变更摘要、影响链、图邻域、测试影响分析、死代码检测与删除符号 tombstone。
- MCP Server、CLI、Claude Code / Codex review skills。
- Git hooks、GitHub Actions、GitLab CI 自动评审与测试选择模板。
- SWE-bench Verified、FastAPI、Spring PetClinic 共 50 个历史变更案例的可复现基准。
- 250 个通过的自动化测试用例，另有 5 个按环境跳过。

因此，TIA、Java 支持、死代码检测、性能基准和自动评审闭环不再列为候选功能。
下一阶段优先补齐“价值验证、检索质量、真实使用和产品展示”。

## 二、P0：Agentic Eval——证明对 Coding Agent 的实际价值

### MVP 状态（已完成）

- 已提供 `agent-eval` CLI 与独立 manifest，支持 `diff_only`、
  `search_baseline`、`graph_agent`、`hybrid_agent` 四种受控上下文模式；
  Hybrid 将变更函数源码、少量直接邻居源码与图证据组合，并设置最终字符预算。
- Agent 命令可插拔，通过 stdin 接收统一 prompt，并以 JSON 契约返回 findings、
  files read、tool calls 和 token usage。
- 已实现基于文件、行范围和关键词约束的确定性 finding 匹配，输出
  Precision / Recall / F1、成功率、延迟、Token、文件读取数和工具调用数。
- 每次运行保存完整 prompt、stdout、stderr、解析结果与评分，支持多次重复实验。
- 已提供示例 manifest 与测试覆盖。

MVP 解决了统一 runner 和指标采集问题；本阶段剩余工作是构建真实 gold 数据集、
接入具体模型 adapter，并执行足够次数的对照实验，形成可引用结论。

当前进度：已建立 10 个来自真实修复提交的反向变异案例，changed symbol 预检命中率
100%；已完成 Claude 四模式 × 三次重复共 120 次调用，并生成按 case 聚类的 bootstrap
置信区间。Search 当前 F1 点估计最高，但相对 Diff 的提升区间仍跨 0；下一阶段需要
多仓库 30+ 案例和按缺陷类型分层评估。

### 目标

建立端到端 Agent 评测集，回答以下问题：

1. MCP 图上下文能否提高代码评审缺陷召回率？
2. 能否减少模型读取的文件数和输入 Token？
3. 工具调用本身带来了多少延迟和成本？
4. Agent 是否会在正确时机调用正确工具，并正确引用图证据？

### 对照组

对同一批真实变更至少运行三种模式：

| 模式 | 可用上下文 |
|---|---|
| Diff Only | 仅 Git Diff |
| Search Baseline | Diff + grep/文件搜索 |
| Graph Agent | Diff + code-review-ai MCP |

固定模型、提示词、温度、最大上下文和重复次数，避免把模型差异误认为工具收益。

### 核心指标

- Review 缺陷 Recall / Precision / F1。
- 输入与输出 Token、估算调用成本。
- 读取文件数、读取代码行数。
- MCP 工具调用次数、工具选择准确率、无效调用率。
- 首次有效结论延迟与端到端耗时。
- 带调用链证据的结论比例，以及证据是否真实支持结论。
- 工具失败、空结果和超时情况下的任务完成率。

### 数据集与评分

- 优先复用现有 50 例历史变更基准，并补充“修复前代码 + 缺陷描述 + 标准修复”信息。
- 增加跨文件行为缺陷、删除变更、测试遗漏、框架隐式连接等专项案例。
- 使用确定性规则评分文件/符号定位，使用人工复核或双评审器评分缺陷结论。
- 保存每次 Agent transcript、工具调用、Token、耗时和最终答案，确保结果可审计。
- 报告均值、中位数、P95 和置信区间，不只展示最佳运行结果。

### 完成标准

- 至少 30 个可重复运行的端到端 Review 案例。
- 三组模式使用统一 runner 一键执行并生成对比报告。
- 给出“Token/文件读取下降 X%，Review Recall 变化 Y%”的可复现结论。
- README 展示完整实验方法、失败案例和原始结果链接。

## 三、P0：统一并版本化基准口径

当前历史结果包含 baseline、优化后结果以及 direct gold 等不同口径。需要避免文档间数字冲突，
也避免招聘方或用户将不同指标误认为同一实验结果。

### 工作项

- 为每次基准记录代码 commit、数据集版本、配置、运行时间和运行环境。
- 明确区分 `all changed tests`、`directly related tests` 和生产文件 leave-one-out。
- 在同一张表中展示 baseline 与优化后结果，不覆盖旧结果。
- 自动从机器可读 JSON 生成 `benchmarks/BASELINE.md` 和 README 摘要，消除手工同步。
- 将 Recall@10、Recall@All、Precision@K、候选规模和 resolved-call rate 同时展示。
- 为退化设置 CI 门槛；指标下降超过阈值时阻止合并或要求显式确认。

### 完成标准

- README、`resume.md` 和基准报告中的数字均可追溯到同一份结果文件。
- 任一指标都能说明数据集、gold 定义、分母、代码版本和运行命令。
- 新解析器或排序策略可以与历史版本自动生成对比报告。

## 四、P1：混合代码检索与排序质量

现有静态图对显式调用有效，但在 decorator、依赖注入、fixture、hook、动态分派和框架路由上
仍存在连接缺口。下一阶段采用“静态图为主、其他信号补充”的混合检索，而不是无限增加
语言特例。

### 候选信号

- 调用图距离、边类型、入口可达性和社区关系。
- 符号名、文件路径、docstring 和代码文本的 BM25/词法相似度。
- 可选 embedding 相似度；默认保持本地、可关闭、可替换 provider。
- Git co-change 历史，但必须防止未来信息泄漏进入历史评测。
- 测试名与生产符号名匹配、同模块邻近度。
- 框架语义边：FastAPI dependency、pytest fixture/hook、Spring route/DI、TS path alias。

### 排序与可解释性

- 将候选生成与候选排序分离，分别测 Recall@All 和 Top-K 排序质量。
- 每个结果返回命中原因，例如 `direct caller`、`route binding`、`co-change`。
- 通过 ablation 分析每类信号带来的增益，避免无法解释的综合分数。
- 为动态语言和框架边保留置信度，不将启发式连接伪装成 resolved call。

### 完成标准

- 50 例固定基准上 Top-10 Recall 有稳定提升，且不存在明显的分仓库退化。
- pytest、FastAPI 和 Xarray 等当前弱项分别给出专项错误分析。
- 混合检索关闭后仍能退化为纯静态图模式。

## 五、P1：真实 PR 与 CI 使用验证

功能完成不等于实际有效，需要在真实开发流程中收集使用数据。

### 试点计划

- 选择 3～5 个不同语言和规模的真实仓库连续运行至少 2 周。
- 记录索引成功率、增量更新时间、MCP 查询延迟和工具失败率。
- 对每个 PR 记录 Review 建议数量、开发者采纳率、误报率和漏报案例。
- 对 TIA 记录选中测试比例、测试耗时下降、全量回退率和漏测率。
- 收集至少 3 个由跨文件影响链发现的真实缺陷案例。

### 安全要求

- TIA 无结果、索引陈旧或查询失败时必须回退全量测试。
- AI Review 默认只提供建议，不自动修改或合并代码。
- 明确源码是否离开本机、发送给哪个模型以及日志中是否包含敏感代码。

### 完成标准

- 形成一份匿名化真实使用报告，而不是只展示合成 Demo。
- 获得可写入简历的真实指标，例如“平均减少 X% 测试执行时间、回退率 Y%”。
- README 至少展示一个从 PR Diff 到 Review 评论的完整案例。

## 六、P1：生产可观测性与可靠性

### 工作项

- 为 rebuild、incremental update、MCP query、LLM review 建立统一 trace ID。
- 记录索引版本、数据新鲜度、查询耗时、候选数量、失败原因和降级路径。
- 增加 MCP 超时、取消、并发查询、损坏数据库恢复和模型命令失败测试。
- 将运行日志分为结构化指标和可读 debug 日志，默认避免记录完整敏感源码。
- 暴露 `doctor`/health check，检测索引过期、grammar 缺失、hook 配置和 MCP 注册问题。

### 完成标准

- 任一错误都能定位到解析、索引、检索、MCP 或 LLM 执行阶段。
- 索引损坏或过期不会静默返回可信外观的错误结果。
- CI 与本地 hook 的失败和回退行为有端到端测试覆盖。

## 七、P2：产品展示与采用门槛

### README 与演示

- 增加一张系统架构图和一段 30～60 秒演示 GIF。
- 展示真实 PR：变更 → 影响链 → Agent 工具调用 → Review 评论。
- 展示 Diff Only 与 Graph Agent 的文件读取数、Token 和结果对比。
- 将所有 MCP Tools、CLI 命令和已完成能力同步到 README。
- 把已完成项目从本路线图候选区移除，避免代码与文档状态不一致。

### 安装与兼容性

- 提供最小示例仓库和一条命令的 smoke test。
- 验证 Windows、Linux、macOS 的安装、hooks 和路径行为。
- 评估 Python 3.14 最低版本要求是否会增加采用成本；若无必要，降低最低版本并加入兼容矩阵。
- 发布版本化 package、changelog 和迁移说明，而不只依赖 Git URL 安装。

## 八、建议实施顺序

1. **统一基准口径**：先保证现有数字可信且可追溯。
2. **Agentic Eval MVP**：建立三组对照和最小 30 例 runner。
3. **混合检索**：以固定 Eval 驱动优化，避免凭 Demo 调参。
4. **真实仓库试点**：收集 PR Review、TIA 和稳定性数据。
5. **可观测性与产品展示**：把实验系统收敛成易安装、可诊断、可展示的工具。

下一阶段最重要的简历成果不应是“又新增了一个 MCP Tool”，而应是：

> 在固定模型和真实代码评审任务上，相比 Diff/Search baseline，Graph Agent 将输入 Token
> 降低 X%、文件读取降低 Y%，同时将缺陷召回率提升 Z%；在真实 CI 中将测试耗时降低 N%，
> 且无漏测事故。
