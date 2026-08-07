# code-review-ai Roadmap

功能完善方向整理，每项附**简历价值 / 吹牛话术**与**落地要点**（规模 S/M/L）。

## 一、现状：已有的可吹点

- **核心能力**：tree-sitter 解析 + SQLite 调用图 + 影响链查询，经 MCP 输出给 AI 审查器（Claude Code / Codex）。
- **三段式流水线**：解析→解析器 → 建图（Phase A）→ BFS 流（Phase B）→ Leiden 社区检测（Phase C，可选）。
- **标准基准**：SWE-bench Verified 30 例 + FastAPI 历史 10 例 + 生产文件折叠评测（Recall@K / Precision@K / Recall@All）。
- **增量与自动化**：增量索引、watchfiles 热更新、git hooks。
- **AI 接入层**：语言审核 skill 套件（Python / TS / JS），`install --platform claude-code|codex` 双平台注入。
- **可视化**：交互式 HTML 图导出（graph / communities / flow）。

> 简历口径参考：*用 tree-sitter 与图算法（BFS 流 + Leiden 社区发现）为 LLM 构建结构化代码知识图谱，并以 SWE-bench 标准基准验证检索质量；配套 MCP 工具与语言审核 skill 注入 Claude Code / Codex。*

## 二、候选功能（按简历性价比排序）

### 1. 测试影响分析（Test Impact Analysis）— 最推荐
- **做什么**：给定变更符号，反向定位哪些测试文件会受影响 → 输出「这个 PR 只需跑这 10 个测试」。
- **简历价值**：TIA 是行业热点；可量化「CI 时间下降 X%」「测试缓存命中率提升」。
- **落地要点**：调用图已在，本质是**反向流查询 + 测试文件归属**；新增 MCP 工具 + CLI + 演示用例。
- **规模**：M

### 2. 自动化 PR 审查机器人（GitHub Action）
- **做什么**：PR 触发 → 建索引 → 变更符号 → 影响链 → 调语言 skill → 带 impact 上下文发审查评论（可复用 TIA 做「只跑受影响测试」）。
- **简历价值**：端到端产品故事——「我做的 AI 审查机器人每天审 N 个 PR」，简历上限最高。
- **落地要点**：复用现有 MCP + skill + 基准，新增 GitHub Actions workflow + 报告格式。
- **规模**：L

### 3. 变更风险评分（Risk-scored change report）
- **做什么**：把 `get_impact` + 社区归属 + 变更摘要合成一个 0–100 风险分并给理由。
- **简历价值**：「基于调用链拓扑的风险量化」——能讲故事又能出数字。
- **落地要点**：新评分模型 + CLI/MCP 输出。
- **规模**：S–M

### 4. Agentic 评测（LLM 工具使用能力评测）
- **做什么**：让 LLM agent 用这套 MCP 工具解影响分析题，测工具调用准确率、少读文件数。
- **简历价值**：agent + evals 双热点；给出「agent 用本工具后少读 N% 文件」的硬指标。
- **落地要点**：评测题目集 + 打分器 + 报告。
- **规模**：M

### 5. 更多语言（Go / Java）
- **做什么**：parser 已是数据驱动，加 grammar + LANG 条目即可接入新语言。
- **简历价值**：「跨 Python / TS / JS / Go / Java 的调用图分析」——覆盖面即说服力。
- **落地要点**：每语言一个新 LANG 条目 + 语法测试 + 基准样本。
- **规模**：M / 语言

### 6. 死代码 / 孤儿符号检测
- **做什么**：无 caller、无 flow、无 community 的符号 → 可安全删除清单。
- **简历价值**：具体、可演示、见效快。
- **落地要点**：现有图查询即可支撑，新增一个 MCP/CLI 工具。
- **规模**：S

### 7. 性能基准（Performance Benchmark）
- **做什么**：可复现脚本，测 build 时间 vs 仓库规模、查询 p99、内存占用。
- **简历价值**：硬数字——「对 X 规模仓库 Y 秒建索引、Z 毫秒查询」。
- **落地要点**：基准脚本 + 报告模板。
- **规模**：S

## 三、建议节奏

- **主推组合**：先做 **1（TIA）**，再叠 **2（PR 机器人）**，把 TIA 变成「只跑受影响测试」的亮点——两个都是行业热点，且能互相成就。
- **快而见效路径**：先做 **6（死代码）+ 7（性能基准）**，凑一版可演示增量，再回头做 TIA。
- 每项落地均走：brainstorming → 设计文档 → 实现计划 → TDD 实施。
