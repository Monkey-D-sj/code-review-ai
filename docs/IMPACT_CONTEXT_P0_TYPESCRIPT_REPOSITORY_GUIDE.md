# TypeScript P0 测试仓库建设指南

> 面向执行任务的 AI：在本仓库中先建立 TypeScript 的 P0 Query 测试基座。目标不是分别证明 Parser 或 Resolver 的内部实现，而是证明：**给定一个已索引的 TypeScript 小型仓库，公开查询服务 `query_graph` 能返回该项目支持的 resolved 图边邻居。**
>
## 1. 不可违反的边界

1. P0 Query E2E 只能经公开服务入口完成：建库/索引后调用 `query_graph`（若仓库公开名称为 `get_impact`，仅在测试适配层映射名称，不能改变断言语义）。
2. E2E 不得断言 Parser AST、Resolver 私有方法、数据库表结构或私有边类型；只断言公开查询输出。
3. 使用一个公共 TypeScript fixture 仓库；每个 query case 只指定一个主要 changed symbol 和对应 gold 输出。公共仓库可以同时包含 import、重载、字段引用等模块，不能为每个 case 重复建仓库。
4. Query P0 套件对公共 `fixture_repo` 只创建一次临时数据库和索引；全部 case 只读复用该状态，并在整套测试结束后统一清理。Query 不得修改数据库状态。
5. `query_graph` 只返回 `resolved` 边。动态 key、反射和高阶参数也必须建立 case，但其公开断言是“不产生伪造的 resolved 邻居”。
6. 覆盖率以适用的 `(P0 capability, TypeScript case)` 计算，必须 100%。没有 case、没有期望文件或 E2E 未通过的能力一律不是 covered。

## 2. 本轮 P0 范围

本指南只建设 Query P0，并以 `query_graph` 的 E2E 作为唯一门槛。

| 验证方式 | 本轮内容 |
|---|---|
| 完整服务 E2E | `call`、`contains`、`import`、`extends`、`implements`、`all`，以及方向和输入校验 |

case 直接给出待查询 qname 和 `edge_kind`。

## 3. TypeScript Query P0 case 清单

下表是最小必建 case 集。一个 case 可覆盖同一行中紧密相关的语法变体，但不得跨行合并。

| case_id | 图边能力 | 公共 fixture 仓库必须包含 | `query_graph` 必须断言 |
|---|---|---|---|
| `ts_call_edges` | `call` | 同文件函数、跨文件 named import、arrow/async/top-level call、constructor、`this.m()`、`super.m()`、`C.staticM()` | 各待查询函数/方法的 `in`、`out` 和作用域绑定正确 |
| `ts_contains_edges` | `contains` | module 顶层 function/class，class method/constructor，class static block | module/class 的 `out` 成员和成员的 `in` 所属节点正确 |
| `ts_import_edges` | `import` | ESM named/default/namespace/alias/side-effect import、相对路径、`index`、barrel、CJS `require()`、`module.exports`、`import = require()` | 模块节点的 `import` out 邻居指向真实仓库模块 |
| `ts_extends_edges` | `extends` | `class Child extends Base`，以及多层 extends | Child 的 `extends` out 邻居及 Base 的 in 邻居正确 |
| `ts_implements_edges` | `implements` | `class Worker implements Runnable`，一个 class 可 implements 多 interface | 实现类的 `implements` out 邻居及 interface 的 in 邻居正确 |
| `ts_all_edges` | `all` | 同一节点同时拥有 call/contains/import/extends/implements 中至少两类边 | `all` 等于具体 resolved 边查询结果的去重并集 |
| `ts_query_contract` | 公开查询契约 | 已存在节点和不存在节点；多个邻居 | `in/out/both`、`max_per_dir`、not found、非法 edge kind/direction 的结果或错误正确 |
| `ts_nonresolved_call_edges` | `call` 负例 | `obj[name]()`、`Reflect.get(obj, name)()`、将函数作为参数传入但未直接调用 | 不返回伪造的 resolved `call` 邻居；case 标明无法唯一确定的原因 |

## 4. 目录结构

所有 TypeScript P0 测试放在一个公共根目录，且源码 fixture 仓库只维护一份。每个 case 文件只引用该公共仓库中的 changed symbol；测试代码和不同层的期望文件分开。

```text
tests/p0/typescript/
  fixture_repo/
    package.json
    tsconfig.json
    src/
      calls/
      contains/
      classes/
      modules/
      inheritance/
  cases/
    query/
      ts_direct_function_call.json
      ...
  e2e/
    test_query_graph_typescript.py
  p0-typescript-coverage.json
```

`fixture_repo/` 是唯一共享源码输入。所有 case 通过 `case_id` 定位公共仓库内的模块和 changed symbol。

## 5. Query E2E 的固定执行流程

整套 Query P0 测试必须严格执行：

```text
创建临时数据库
→ 使用 fixture_repo 建立索引
→ 依次读取每个 cases/query/*.json
→ 对每个 case 调用公开 query_graph(qualified_name, edge_kind, direction, max_per_dir)
→ 规范化公开响应（忽略无意义排序、临时 ID、时间戳）
→ 与该 case 的 gold in/out 邻居和公开响应字段比较
→ 关闭并删除临时数据库
```

测试不得预先向 DB 插入 node 或 edge，也不得 mock Parser、Resolver、Indexer 或查询服务。

一个 Query case 文件采用以下最小结构：

```json
{
  "case_id": "ts_direct_function_call",
  "language": "typescript",
  "p0_capabilities": ["P0-G01"],
  "fixture": "fixture_repo",
  "qualified_name": "src/service::save",
  "edge_kind": "call",
  "direction": "both",
  "expected_in": ["src/controller::create"],
  "expected_out": []
}
```

gold 必须比较完整集合，而不仅是“包含一个正确结果”。额外的邻居视为失败；只比较公开响应中的 resolved 邻居和契约字段。

`ts_nonresolved_call_edges` 必须声明场景中其余正常 resolved 邻居的精确集合；但不能凭动态 key、反射或参数传递额外生成 target。case 元数据写明 `negative_kind`（`dynamic_key`、`reflection` 或 `higher_order_argument`）。它们计入 Case Coverage 和 Negative Edge Correctness；没有唯一 target 的那一条调用不计入 Resolved Edge Recall 的分母。

## 6. 覆盖清单和 CI 门禁

创建 `tests/p0/typescript/p0-typescript-coverage.json`，本阶段只记录第 3 节 Query P0 的证据：

```json
{
  "language": "typescript",
  "items": [
    {
      "capability": "P0-G01",
      "case_id": "ts_call_edges",
      "test_kind": "query_e2e",
      "status": "covered"
    }
  ]
}
```

本阶段 CI 必须失败于任一情形：

1. 第 3 节任一 case 缺失；
2. case 未被对应 E2E 测试加载；
3. coverage 清单包含 `missing` 或 `partial`；
4. 清单存在没有公共仓库中 changed symbol 或 E2E 测试函数的 Query P0 条目；
5. 测试检查内部组件而没有通过 `query_graph`；
6. `all` case 的邻居不等于具体 edge kind 查询结果的去重并集。
7. 任一负向 call case 返回了未声明的 resolved 邻居。

## 7. AI 执行顺序

1. 先阅读现有公开查询 API 和现有 TypeScript fixture，确认 `query_graph` 的真实入参与响应 schema；不要修改产品 API。
2. 建立目录和 coverage 清单，只登记本指南第 3 节的 Query P0 case，初始状态均为 `missing`。
3. 在公共 `fixture_repo/src` 中逐个添加最小模块，并编写对应 `cases/query` gold 文件；先完成 call、contains、import、extends、implements、all、query contract 七类。
4. 实现通用 E2E harness：临时 DB、索引、公开查询、响应规范化、gold 比较。
5. 让每个 Query case 通过 E2E；每完成一项才将清单改为 `covered`。
6. 当第 3 节所有适用 Query P0 条目均为 `covered` 时，交付本阶段；不要在本任务中开始删除/重命名/增量测试、JavaScript 或其他语言。

完成时交付：公共 fixture 仓库、Query case/gold 文件、`query_graph` E2E 测试、Query P0 覆盖清单、CI 门禁，以及一次完整的 TypeScript Query P0 测试报告。
