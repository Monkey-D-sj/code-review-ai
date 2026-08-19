# Impact P0 Query 上下文覆盖规范

> 发布目标：对已完成索引的 Python、TypeScript/JavaScript、Java 仓库，可靠查询图中已解析的一跳邻居。
>
> P0 是当前发布契约，必须 100% 达标。
>
> 本文是自包含的能力规范：定义 P0 必须提供的公开行为，不依赖任何其他覆盖目录或内部组件划分。

## 1. P0 边界

P0 严格等同于 `query_graph` 当前公开的、`resolution='resolved'` 的边查询：

- `call`；
- `contains`；
- `import`；
- `extends`；
- `implements`；
- `all`（以上边类型的聚合）。

每种边都必须验证 `in`、`out`、`both` 三种方向，以及 node-not-found、非法 `edge_kind` 和非法 `direction` 的公开契约。

评测目录还必须收录无法唯一解析的调用形态，例如动态 key、反射和高阶函数参数。`query_graph` 不展示这些边的 resolution state，因此它们的公开期望是“不产生伪造的 resolved 邻居”；它们与已解析边使用不同指标统计。

## 2. 跨语言公共 P0 契约

| ID | 能力 | 期望输出 |
|---|---|---|
| P0-G01 | `call` | 返回已解析调用的 caller（`in`）和 callee（`out`） |
| P0-G02 | `contains` | 返回模块或类包含的成员，及其所属模块或类 |
| P0-G03 | `import` | 返回模块到已解析仓库内模块的 import 邻居 |
| P0-G04 | `extends` | 返回子类型与父类/父接口的继承邻居 |
| P0-G05 | `implements` | 返回实现类型与接口的实现邻居 |
| P0-G06 | `all` | 返回所有上述已解析边的去重聚合 |
| P0-G07 | 查询契约 | 正确处理 `in/out/both`、not found、非法参数和 `max_per_dir` |

## 3. 各语言语法映射

公共契约相同；每种语言只用自己的语法 fixture 证明它。没有相应语法的语言标为 `N/A`，不能标为 `missing`。

| 公共能力 | Python | TypeScript / JavaScript | Java |
|---|---|---|---|
| `call` | `def`、method、`self`/`cls`、`C()` | declaration、arrow/function expression、method、`this`、`new C()`、static block | method、constructor、`this`/static call、`new C()`、initializer |
| `contains` | module/class → function/method | module/class → function/method | package/class → method/constructor/inner class |
| `import` | `import`、`from`、alias、relative import、`__init__` re-export | ESM、barrel、CommonJS、`import = require()`、`export =` | package、import/static import、fully-qualified type |
| `extends` / `implements` | class base | class `extends` / `implements` | class/interface `extends`，class `implements` |

各语言的 P0 fixture 至少覆盖其适用的每个公共图边一次；`P0-G01`–`P0-G03` 和 `P0-G07` 必须在三种语言中均有端到端 case，`P0-G04` 与 `P0-G05` 在该语言支持相应语法时必须有 case。

## 4. P0 Query 完成定义：100%

P0 的覆盖率按 **适用的 `(P0 ID, language)` 组合** 计算：

```text
P0 coverage = covered applicable combinations / all applicable combinations
```

发布门槛：

1. P0 coverage 必须为 **100%**；
2. 不存在 `missing` 或 `partial` 的适用 P0 组合；
3. 每个组合都有通过公开 `query_graph` 的端到端测试证据；
4. `all` 的结果等于各具体边类型结果的去重并集，且只包含 resolved 邻居；
5. 每个 P0 ID 至少有一个真实仓库 benchmark case。

## 5. 评测指标

| 指标 | 分母 | 成功条件 |
|---|---|---|
| Resolved Edge Recall | 所有具有唯一 gold target 的边 | `query_graph` 返回该 target |
| Resolved Edge Precision | 所有 `query_graph` 返回的邻居 | 邻居属于该 case 的 resolved gold |
| Negative Edge Correctness | 动态 key、反射、高阶参数等无唯一 target 的 case | 不产生未声明的 resolved 邻居 |
| Case Coverage | 全部已登记 edge case | 每个 case 都被 E2E 加载并执行 |

动态调用没有确定 target，不能进入 Resolved Edge Recall 的分母；否则“正确地不知道目标”的行为会被错误地计为漏召回。
