# TypeScript P0 Query 测试报告

日期：2026-08-19

## 范围

本阶段验证纯 `.ts` 前端 fixture 仓库通过公开 MCP `query_graph` 查询 resolved 图邻居，覆盖：

- `call`：当前文件、跨文件、分支、循环、异常、arrow、async、`new`、`this`、`super`、动态 key、反射和高阶参数；
- `contains`：module、class、constructor、method、static method、static block；
- `import`：ESM named/default/namespace/alias/side-effect、barrel、CJS、`require`；
- `extends`：单层、反向查询和多层继承；
- `implements`：单接口、多接口和反向查询；
- 查询契约：`in/out/both`、`max_neighbors`、not found、非法 edge kind/direction。

每个 case 只查询一个 qname；所有 case 共享一次索引和临时数据库。动态 key、反射和高阶参数作为 negative case 单独统计。

## 结果

| 指标 | 分子 / 分母 | 结果 |
|---|---:|---:|
| Strict Resolved Edge Recall | 49 / 49 | 100% |
| Negative Edge Correctness | 3 / 3 | 100% |
| Effective Graph Recall | 49 / 52 | 94.23% |
| Query Case Coverage | 28 / 28 | 100% |

`Effective Graph Recall` 将 49 条可唯一确定的 resolved gold target 与 3 个图无法唯一确定目标的场景统一纳入真实仓库口径；后者不要求伪造 target，因此不计入 Strict Resolved Edge Recall，但会降低有效覆盖率。

## 未覆盖项

`all` edge 聚合本阶段按范围暂缓，coverage manifest 中保持 `missing`，未计入上述 52 个检查单位。

## 测试执行

```text
tests/p0/typescript：32 passed
全量 pytest：507 passed
```

全量测试只有一个 pytest cache 权限 warning，不影响测试结果。
