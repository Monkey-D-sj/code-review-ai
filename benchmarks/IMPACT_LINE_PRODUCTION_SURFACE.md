# Impact 线指标:生产影响面(评审轴)

日期:2026-08-18

便宜线(`run_swebench_suite.py` → `benchmark.py`)的**主指标从"gold 测试文件召回"重定向为"fix 触及的生产符号集能否被 impact 可达性找出来"**。测试召回保留为诊断指标(服务 CI 回归选择),不再作为评审质量的代理。本文件记录重定向的动机、12-case v1 结果的两层解读、注解 DI 盲区修复,以及新指标语义。

## 为什么重定向

`agentic-eval-real-repos.json` 是两条线的统一 manifest:

- **impact 线**(便宜,`benchmark.py`):零 LLM,从变更符号跑 `get_impact`,测能否找回 gold 目标。此前目标是 gold 测试文件。
- **full-agent 线**(昂贵,`full_agent_eval.py`):真实 Agent 在线上评审路径里跑,LLM F1 on gold findings。

评审任务的目标物是 **diff 里的 bug**,证据是 diff + 真实调用/被调链;**测试文件不是评审目标**。把 impact 线的主指标钉在"找回测试文件"上,等于用一个 CI 回归选择工具去预测评审质量——两个目标不同轴。

## v1 结果的两层解读(重定向前)

12 case,`top_k=10`:

| 指标 | v1 |
|---|---:|
| `macro_test_file_recall_at_k` | 0.2361 |
| `macro_test_file_precision_at_k` | 0.0917 |
| `macro_direct_test_file_recall_all` | 0.5 |
| `symbol_found_rate` | 1.0 |
| `mean_resolved_call_rate` | 0.2326 |
| `production_file_eligible_cases` / `folds` | 2 / 8 |
| `macro_related_production_file_recall_at_k` | 0.0417 |

**测试召回贴近静态天花板,不是排序问题。** 拆 direct_recall 缺口:30 个 direct gold 里只有 4 个命中(测试直接 resolved-call 变更符号),26 个未命中中 3 个是"测试调用兄弟符号"、23 个是**纯 annotation/DI、零调用边**——FastAPI 的 `test_*` 通过 `TestClient` + 框架构造器把路由搭起来,测试函数与被测符号之间没有静态 call 边,`_edges_fallback` 和 flow 都够不到。`recall_all == recall_at_k == 0` 说明候选集里根本不出现 golds,不是排序能救的。

**因此:测试召回在 annotation-heavy repo 上测的是"静态可达性天花板",测不出 impact 检索的质量,更不能预测 full-agent 的 F1。** 它在 Java/Spring 这类"测试直接 new 对象调方法"的 repo 上才有点判别力(petclinic 两 case `direct_recall_all=1.0`)。

**生产影响面才是评审轴的度量。** 评审要回答的是"这个 fix 触及了谁、改一个函数哪些消费者受影响"。现有 `production_file_folds`(文件级 co-change)最接近,但缺符号级视角,且被测试召回盖过。

## 新指标:符号级 co-change fold

`benchmark.py::_symbol_folds` — 对 fix 的每个变更符号 `si` 做 seed,`get_impact` 跑可达性,**gold = 同一 fix 里的其他变更符号**:

- `reached` = {si} ∪ upstream qnames ∪ downstream qnames,有序(**direct callers 在前**,见 `_edges_fallback` 排序);
- `recall_at_k` = `|gold ∩ candidates[:top_k]| / |gold|`,`candidates = reached[1:]`(丢 seed);
- `recall_all` = `|gold ∩ candidates| / |gold|`;
- 每次查询打一个 fold;`macro_changed_symbol_*` 在 aggregate 里对所有 fold 取均值(`_fold_mean`,镜像 `macro_related_production_file_*`)。

语义:给定 fix 里一个变更符号,**impact 可达性能否把其余变更符号(触达的调用方/被调方)找出来**——即评审要的"消费者可被发现"。一个纯 rename/API 改动:seed=API,gold=5 个调用点,`impact(api)` 的 upstream = 5 个调用点 → recall 1.0。这是评审轴信号。

注意:v1 的 `production_file_folds` 在 12 case 上几乎全空(`eligible_cases=2`),因为大部分 case 单文件改动。符号级 fold 的门槛是 `len(changed_symbols) >= 2`,12 case 里 fastapi-validation(12 个符号)、fastapi-frontend(6 个)、petclinic-owner-scoped(4 个)等都有 ≥2 符号,能产出更多样本。

## 注解 DI 盲区 + dependency_markers

`Depends(get_db)` 里 `get_db` 是**被调对象之外的一个参数**:route 在注解里声明"我依赖 get_db",但静态图上没有 `route → get_db` 边 → 改一个被多处依赖的 get_db,消费者映射不出来。这是 annotation-DI 的通用盲区,也是 fastapi 那 23 个零调用边 golds 的根源。

修复(config `dependency_markers`,默认 `["Depends"]`):

```toml
# pyproject.toml [tool.code-review-ai]
dependency_markers = ["Depends", "my_framework.Inject"]
# 或环境变量(逗号分隔)
CRAI_DEPENDENCY_MARKERS=Depends,Inject
```

对每个 marker 调用(`Depends(...)`),resolver 把其**可解析的标识符参数**经正常 local/imports 机制解析,发一条 `source → 参数` 边,`kind='call'` + `resolution='resolved'`——flow/impact/`_edges_fallback`/degrees 全部自动纳入,flow_builder 零改动。

- `Depends(get_db)` → `route → app::get_db`(resolved call 边);
- `Query(...)`/`Body(...)` 是参数配置不是依赖,**不在默认 marker 集**,`Query(min_length=1)` 不会把 `min_length` 当 callee;
- `Depends(make_db())` 的嵌套 call 本就被 parser 递归捕获为 `route → make_db`,DI 边只补裸标识符缺口;
- 该键进 `_CONFIG_HASH_KEYS`,改它触发全量重建。

## 指标语义速查(aggregate 键)

| 键 | 含义 |
|---|---|
| `macro_changed_symbol_recall_at_k` / `_all` | 变更符号 co-change fold 的召回(评审轴主指标) |
| `macro_changed_symbol_precision_at_k` / `_all` | 同上,precision(暴露面里多少是 fix 本身) |
| `changed_symbol_eligible_cases` / `folds` | 有 ≥2 变更符号的 case 数 / 总 fold 数 |
| `macro_test_file_recall_*` / `macro_direct_test_file_recall_all` | **诊断指标**:测试召回,服务 CI 回归选择,不是评审代理 |
| `macro_related_production_file_*` | 文件级 co-change fold(保留) |
| `mean_resolved_call_rate` | 图里 call 边的静态可达率——annotation-heavy repo 上偏低,正是 DI 边要补的 |

## 复现

```bash
uv run --no-sync python scripts/run_swebench_suite.py \
  --cases benchmarks/agentic-eval-real-repos.json \
  --cache-dir .code-review-ai/impact-eval-cache \
  --out .code-review-ai/impact-eval/report2.json \
  --dataset-name "agentic-eval-12 impact line v2"
```

v1 报告在 `.code-review-ai/impact-eval/report.json`(`metric_target: "gold historical test files"`);v2 的 `metric_target` 改为 `"fix 触及的生产符号集 (impact reachability); 测试召回为诊断指标"`。
