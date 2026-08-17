# code-review-ai - 基于静态调用图的 AI 代码评审基础设施

- **多语言静态解析**：基于 Tree-sitter 解析 Python / TypeScript / JavaScript / Vue(SFC `<script>` 抽取) / Java AST，构建函数级调用图 + BFS 调用流，落库 SQLite 图数据库；单事务原子重建，watchfiles 文件级增量更新。中型项目（约 400 文件 / 4.5k 节点）全量索引约 2~4s，索引体积约 10MB。
- **AI 代码评审提效**：以 MCP Server 接入 Claude Code / Codex（一条命令自注册），提供影响链 / 变更摘要 / 图邻域 / 社区查询 / 死代码 / skill 注入；支持 AI 审查按需拉取相关调用链，减少无关文件上下文输入。另提供 CLI、交互式 HTML 可视化（调用图 / 调用流 / 社区）与 CI 自动评审模板（GitHub Actions / GitLab CI，PR/MR 触发建索引 -> 影响链 -> LLM 评审 -> 评论/工件）。
- **端到端 Agentic Eval**：构建真实仓库隔离 worktree、热索引在线评审、MCP 工具轨迹与 case-clustered bootstrap 评测链路；在 itsdangerous / p-limit / Gson 的 6 个真实修复、36 次 bare Claude 调用中，完整项目组 18/18 自主使用 MCP、仅 9/18 按需调用 `get_impact`，将平均读取文件数降低 25.3%，同时把相对 Native Agent 的成本 / 延迟开销控制在 3.0% / 2.3%；F1 点估计 90.4%→91.3%，并披露 95% 区间跨 0，避免将小样本包装成显著收益。
- **影响链分析**：对变更符号按调用流切分上游调用方 / 下游被调方 / 受影响业务入口；单点图邻域查询毫秒级（约 1ms），50 例历史基准的完整影响链分析平均约 100ms，符号命中率 98%。
- **测试影响分析（TIA）**：给定变更符号，反向走调用流定位覆盖它的测试函数 -> 输出「这个 PR 只需跑这些测试文件」；复用既有反向流查询 + 测试节点 `is_test` 标记，零额外建图。提供 MCP 工具 + CLI（`--format paths` 直出 shell-ready 测试文件清单）+ GitHub/GitLab CI 模板（建索引 -> test-impact -> 只跑受影响测试，无源码改动 / 无覆盖 / 查询失败时回退全量）。
- **死代码 / 孤立符号检测**：基于索引侧只读查询，标出无 resolved caller 且非入口的函数/方法/类，以及无模块引用的整文件，给出可安全删除候选清单（附静态分析免责说明：动态调用/反射/多态不可见，删除前人工核对）。
- **基准验证（可复现）**：自建基准套件 = SWE-bench Verified 30 例（Flask/Requests/pytest/Xarray）+ FastAPI 官方 Git 历史 10 例 + Spring PetClinic Java 历史 10 例，全部检出至修复前 base_commit 重建索引；50 例测试文件 Recall@All 55.7% / Top-10 37.7%（直接相关 gold——引用改动类的测试——Recall@All 66.3%），相关生产文件 leave-one-out（41 折）Recall@All 54.9% / Top-10 32.1%，同时报告 Precision@K 与全候选规模；其中 Spring PetClinic Java 10 例经 MockMvc 路由边 + Java 类型绑定 + 类级 @RequestMapping 前缀后 Recall@All 78.3%（直接相关 gold 94.4%）。
- **社区检测（可选）**：基于结构边（contains / import / inherits）的 Leiden 社区划分，给出符号的横向爆炸半径，与纵向调用链互补。
