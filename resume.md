# code-review-ai — 基于静态调用图的 AI 代码评审基础设施

- **多语言静态解析**：基于 Tree-sitter 解析 Python / TypeScript / JavaScript / Vue(SFC `<script>` 抽取) AST，构建函数级调用图 + BFS 调用流，落库 SQLite 图数据库；单事务原子重建，watchfiles 文件级增量更新。中型项目（约 400 文件 / 4.5k 节点）全量索引约 2~4s，索引体积约 10MB。
- **AI 代码评审提效**：以 MCP Server 接入 Claude Code（一条命令自注册），提供影响链 / 变更摘要 / 图邻域 / 社区查询 / skill注入；AI 审查只拉相关调用链，无需读整文件，大幅节省 LLM token。另提供 CLI 与交互式 HTML 可视化（调用图 / 调用流 / 社区）。
- **影响链分析**：对变更符号按调用流切分上游调用方 / 下游被调方 / 受影响业务入口；单点图邻域查询毫秒级（约 1ms），40 例历史基准的完整影响链分析平均约 175ms，符号命中率 97.5%。
- **基准验证（可复现）**：自建基准套件 = SWE-bench Verified 30 例（Flask/Requests/pytest/Xarray）+ FastAPI 官方 Git 历史 10 例，全部检出至修复前 base_commit 重建索引；测试文件 Recall@All 50% / Top-10 27.5%；相关生产文件 leave-one-out（25 折）Recall@All 70.7% / Top-10 33.3%，同时报告 Precision@K 与全候选规模。
- **社区检测（可选）**：基于结构边（contains / import / inherits）的 Leiden 社区划分，给出符号的横向爆炸半径，与纵向调用链互补。
