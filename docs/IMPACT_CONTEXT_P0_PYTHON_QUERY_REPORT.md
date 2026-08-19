# Python P0 Query 测试报告

## 结论

Python P0 Query 套件整体正确性为 **100%（29/29）**。

其中：

| 指标 | 结果 | 说明 |
|---|---:|---|
| Resolved Edge Recall | 26/26 = 100% | 所有 gold resolved 邻居均被查询服务返回 |
| Resolved Edge Precision | 26/26 = 100% | 没有额外 resolved 邻居 |
| Negative Edge Correctness | 3/3 = 100% | 动态成员、运行时 import、高阶参数均未伪造 resolved 邻居 |
| Case Coverage | 10/10 = 100% | 所有适用 Python P0 case 均被 E2E 加载并通过 |
| Overall Correctness | (26 + 3)/(26 + 3) = 29/29 = 100% | 正向和负向断言合计 |

## 覆盖范围

- P0-G01：顶层/控制流调用、作用域与递归、方法与构造器、跨模块调用、非 resolved 调用
- P0-G02：module/class/member contains
- P0-G03：普通、alias、relative、re-export、wildcard import
- P0-G04：同模块、跨模块、多级 extends
- P0-G06：`all` 与具体 resolved edge kind 去重并集一致
- P0-G07：方向、邻居上限、not found、非法参数
- P0-G05：`not_applicable`，Python 没有 `implements` 语法

## 验证证据

```text
uv run pytest tests/p0/python/e2e/test_query_graph_python.py -q
18 passed

uv run pytest -q
508 passed
```

机器可读明细见 [p0-python-metrics.json](../tests/p0/python/p0-python-metrics.json)。
