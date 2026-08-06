# Performance Benchmark Design

Date: 2026-08-06

## 目标

roadmap item 7：为 code-review-ai 提供**可复现的性能基准**，产出简历级硬数字——
「对 X 规模仓库 Y 秒建索引、Z 毫秒查询」。

测量三项指标，且随仓库规模展示缩放关系（而不是只看均值）：

1. **build 时间 vs 仓库规模**（各阶段耗时 + 总数）
2. **查询 p99 延迟**（采样 `get_impact`，出 p50/p95/p99）
3. **内存占用**（build 进程峰值 RSS）

## 范围

**新增**

- `code_review_ai/perf.py` — 可测试核心：合成仓库生成器、单仓库测量、百分位、Markdown 渲染
- `scripts/run_perf_benchmark.py` — 薄编排：解析参数 → 组装仓库列表 → 逐仓库测量 → 写 report.json + 渲染 PERF.md
- `benchmarks/PERF.md` — 渲染出的报告，入库（范式同 `benchmarks/BASELINE.md`）
- `tests/test_perf.py` — TDD 单测

**修改**

- `pyproject.toml` — 新增可选依赖 `perf = ["psutil"]`（范式同现有 `community = ["leidenalg", "igraph"]`）

**不动**

- CLI、MCP、`benchmark.py` 质量基准、`indexer.py` 及以下管线

## 仓库策略（已确认）

- **真实仓库**：`.benchmark-cache/repos/*` 下已有的 5 个（flask/requests/xarray/pytest/fastapi，7M→74M 自然梯度）+ 当前 code-review-ai 仓库
- **合成仓库**：500/1000/2000/4000 文件四档，得到平滑、可外推的缩放曲线
- 报告注明：合成样本仅供参考，不代表真实解析特征

## 关键方法学决策

**所有仓库用同一份固定配置**，曲线不被仓库间配置差异污染：

- `--community` 默认开（对齐生产 pyproject `community_detection=true`，`community_weight=hub_pruned`）
- 固定 exclude 默认值
- 每仓库独立临时 DB（放 `--cache-dir` 下）
- 报告顶部写明实际使用的配置

## `perf.py` 设计

四个可测试单元，各 ≤50 行：

### 1. `build_synthetic_repo(target_dir, file_count) -> SyntheticStats`

确定性合成器，**无 RNG**（可复现）：

- 生成文件 `m0.py`..`m{n-1}.py`
- 循环 import：`m{i}` import `m{(i+1) % n}` 与 `m{(i+2) % n}`
- 每文件 10 个函数 `f0`..`f9`，各 ~8 行；`f{j}` 调用本文件 `f{(j+1) % 10}` + 两个被 import 模块里各一个函数 → 全为 `resolved` 边，喂饱 resolver 与 flow_builder
- 每 10 个文件放一个 `main` 函数，命中 `entry_names=["main"]` → 流可建
- 返回 `SyntheticStats(files, nodes, edges, flows)` 结构计数

### 2. `measure_repo(config) -> dict`

对单仓库：临时 DB → `rebuild`（取 `stage_timings`、nodes/edges/flows、源文件数）→ 采样符号跑 `get_impact` 记每次耗时 → 组装测量结果 dict。

### 3. `percentile(sorted_values, q) -> float`

线性插值百分位。

### 4. `render_markdown(report) -> str`

报告模板渲染：环境 + 固定配置说明 + 仓库主表 + 缩放观察。

## 查询 p99 采样

- 重建后：`SELECT id, qualified_name FROM nodes WHERE kind IN ('function','method') ORDER BY id`
- 每第 k 个取 1 个样本（`k = ceil(node_count / 200)`），最多 200 个
- 每个样本跑 `get_impact(conn, [symbol])` 记单次耗时 → `percentile` 出 p50/p95/p99 + samples 数
- 按 id 等距确定性采样，无随机性

## 内存峰值 RSS（psutil 可选）

- psutil 可导入时：`measure_repo` 起后台轮询线程，build 期间每 ~20ms 读
  `psutil.Process().memory_info().rss`，记最大值 → `peak_rss_mb`
- 缺 psutil 时：`peak_rss_mb` 置 `null`，降级为 `database_bytes`（索引体积）当代理，模板注明
- 同一进程内轮询，避免子进程序列化复杂度

## report.json schema

```json
{
  "schema_version": 1,
  "date": "2026-08-06",
  "environment": {"platform": "win32", "python": "3.14", "psutil_available": true},
  "config": {"community_detection": true, "community_weight": "hub_pruned",
             "query_samples": 200, "exclude": ["*/test*"]},
  "repos": [{
    "name": "synthetic-1000", "kind": "synthetic", "source_files": 1000,
    "nodes": 10000, "edges": 41000, "flows": 100,
    "build_ms": {"list_files": 12.3, "parse": 900.1, "resolve": 450.2,
                 "write_db": 600.5, "communities": 300.0, "total": 2263.1},
    "query_ms": {"p50": 1.2, "p95": 4.5, "p99": 9.8, "samples": 200},
    "peak_rss_mb": 234.5, "database_bytes": 3145728
  }]
}
```

## PERF.md 模板

- 环境 + 固定配置说明
- 主表：仓库 | 类型 | 源文件 | 节点 | Build 总耗时(s) | Query p99(ms) | 峰值RSS(MB) | DB大小
- 缩放观察：每千节点 build 秒数、p99 随规模增长形态；合成样本仅供参考

## 脚本参数

`scripts/run_perf_benchmark.py`：

- `--repos`（追加真实仓库，可重复）
- `--synthetic`（默认 `500,1000,2000,4000`）
- `--community` / `--no-community`（默认开）
- `--cache-dir`（默认 `.benchmark-cache`）
- `--out`（默认 `benchmark-results/perf-<日期>.json`）
- `--report`（默认 `benchmarks/PERF.md`）

仓库列表 = `.benchmark-cache/repos/*` + 当前仓库 + 合成档。

## 测试计划（TDD，`tests/test_perf.py`）

1. 合成器：file_count / 节点数(10×files) / resolved 跨文件边 > 0 / 入口存在；两次生成结构一致（确定性）
2. `percentile`：已知序列的 p50/p95/p99
3. `measure_repo`（跑 FIXTURES）：`build_ms.total` > 0、query p50/p95/p99 齐全、`database_bytes` > 0、psutil 在时 `peak_rss_mb` > 0
4. `render_markdown`：输出含仓库名与 p99 表头

## 项目规范约束

- 函数 ≤50 行，类 ≤300 行
- 主控函数只做参数准备 / 编排 / 返回
- 禁单字母变量名、禁内置名当变量名
- 逻辑 ≥3 步拆分语义子函数
