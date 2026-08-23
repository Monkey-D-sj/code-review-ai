# P0 合成语法一致性报告

> 范围：公开 `query_graph` 的 `call`、`contains`、`import`、`extends`、
> `implements` 五类 resolved edge；`all` 和查询参数属于契约检查，不作为第六种边。

## 当前结果

当前报告由 `tests/p0_conformance.py` 的共享评分器从公开 MCP 查询响应实时计算，
并与各语言提交的 metrics JSON 逐字段比较。

| 语言 | Gold resolved 邻居 | TP / FP / FN | 动态负例 | 精确通过 case | Recall | Precision |
|---|---:|---:|---:|---:|---:|---:|
| Python | 26 | 26 / 0 / 0 | 3/3 | 10/10 | 100% | 100% |
| TypeScript | 57 | 57 / 0 / 0 | 3/3 | 29/29 | 100% | 100% |
| Java | 41 | 41 / 0 / 0 | 3/3 | 12/12 | 100% | 100% |
| **合计** | **124** | **124 / 0 / 0** | **9/9** | **51/51** | **100%** | **100%** |

这里的 100% 只表示“当前已登记的合成语法 case 全部满足完整 gold 集合”，不能解释为
“现实世界所有语法已经覆盖”。新增语法必须先登记 case；尚未登记的语法不应被分母隐藏。

## 指标口径

- `resolved_edge_recall = TP / (TP + FN)`；
- `resolved_edge_precision = TP / (TP + FP)`；
- `negative_edge_correctness` 单独统计无法唯一静态绑定的动态/反射机制；
- `exact_case_pass_rate` 要求一个 case 的完整 `in/out` 集合与 gold 完全相等；
- 动态负例没有唯一 target，不进入 resolved Recall 分母；
- 方向不同的同一 qname 是两条独立图事实。

## 当前明确边界

- Python 没有独立 `implements` 语法，标记为 `not_applicable`；
- TypeScript case 不能证明 JavaScript `.js/.mjs/.cjs` grammar 行为；JavaScript 应另建套件；
- Java overload 当前共享无签名 qname，只证明类作用域不串边，不能证明 overload 级绑定；
- 当前分母是 P0 已登记 case，不是 Coverage Matrix 中全部未来 P1/P2 能力；
- 真实仓库只做兼容性 smoke，不参与这套语法 gold 的 Recall/Precision。

## 可重现入口

```powershell
.\.venv\Scripts\python.exe -m pytest tests\p0 -q
```

机器可读结果：

- `tests/p0/python/p0-python-metrics.json`
- `tests/p0/typescript/p0-typescript-metrics.json`
- `tests/p0/java/p0-java-metrics.json`
