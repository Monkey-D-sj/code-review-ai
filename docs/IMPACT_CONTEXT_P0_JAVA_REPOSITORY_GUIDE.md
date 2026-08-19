# Java P0 Query 测试仓库建设指南

> 面向执行任务的 AI：在本仓库中建立 Java 的 P0 Query 测试基座。目标是证明：**给定一个已索引的公共 Java fixture 仓库，公开查询服务 `query_graph` 能返回该项目支持的 resolved 图边邻居。**
>
> P0 的公共图边和指标定义见 [Impact P0 Query 上下文覆盖规范](IMPACT_CONTEXT_P0_COVERAGE.md)。本指南只说明 Java fixture、case 和 E2E 的建设方式。

## 1. 不可违反的边界

1. 测试只能经公开服务入口完成：建库/索引后调用 `query_graph`。
2. 不断言 Parser AST、Resolver 私有方法、数据库表结构或私有边类型；只断言公开查询响应。
3. 使用一个公共 Java fixture 仓库；每个 JSON case 只指定一个待查询 qname、edge kind、direction 和 gold 响应。
4. Query P0 套件对公共 `fixture_repo` 只创建一次临时数据库和索引；全部 case 只读复用该状态，并在整套测试结束后统一清理。
5. `query_graph` 只返回 `resolved` 边。反射、动态代理和无法唯一确定的 receiver 也必须建立负向 case，但其公开断言是“不产生伪造的 resolved 邻居”。
6. 每个 `(P0 capability, java)` 必须有 case、gold 文件和 E2E 证据；全部通过才可标记为 covered。

## 2. Java 的适用 P0 图边

| 图边 | Java 是否适用 | fixture 需要覆盖 |
|---|---|---|
| `call` | 是 | 同类 bare/`this` 调用、静态调用、构造调用、控制流内调用、跨 package 调用、static import、重载 |
| `contains` | 是 | package/module → class，class → method/constructor/inner class |
| `import` | 是 | 普通 import、alias 不存在的 Java import、唯一可解析 wildcard import；static import 只作为 call 解析场景 |
| `extends` | 是 | class extends class、interface extends interface、跨 package 继承 |
| `implements` | 是 | class implements 一个或多个 interface、跨 package 实现 |
| `all` | 是 | 同一 qname 至少拥有两种以上 resolved 边时的去重聚合 |

## 3. Java Query P0 case 清单

下表是最小必建 case 集。每个 case 都使用第 4 节的同一个 `fixture_repo`。

| case_id | 图边能力 | 公共 fixture 仓库必须包含 | `query_graph` 必须断言 |
|---|---|---|---|
| `java_call_same_class_and_control_flow` | `call` | 同类 bare method、`this.m()`、`if/else`、`for`/`while`、`try/catch/finally` 内调用 | 每个唯一可解析 callee 的 `in` 和每个 caller 的 `out` 正确；不要求解释分支、循环或异常执行次数 |
| `java_call_static_and_constructor` | `call` | `Class.staticMethod()`、`new Service()`、显式 constructor | 静态方法与 constructor 的 resolved caller/callee 正确 |
| `java_call_cross_package_and_static_import` | `call` | 普通 package import 后的方法调用、`import static` 后的裸调用、fully-qualified static 调用 | call 解析到真实仓库内 method；static import 本身不作为 Java `import` 图边断言 |
| `java_call_overload_and_scope` | `call` | 同名不同参数的 overload、inner/local class、不同 class 的同名 method | 根据调用参数和作用域返回唯一目标；若当前实现无法区分，作为 Resolved Edge Recall 缺口保留，不删除 case |
| `java_call_recursion` | `call` | 直接递归与互递归 | 一跳 `in/out` 邻居正确且不重复 |
| `java_contains_edges` | `contains` | package 下多个 class、class method/constructor、inner class | package/class 的 `out` 成员和成员的 `in` 所属节点正确 |
| `java_import_edges` | `import` | 普通 import、跨 package import、唯一命中 wildcard import | 源模块/package 的 `import` out 邻居指向真实仓库内目标 |
| `java_extends_edges` | `extends` | class extends class、interface extends interface、跨 package base type | Child 的 `extends` out 邻居和 Base 的 `in` 邻居正确 |
| `java_implements_edges` | `implements` | class implements 一个/多个 interface、跨 package interface | 实现类的 `implements` out 邻居及 interface 的 `in` 邻居正确 |
| `java_all_edges` | `all` | 同一节点同时拥有 call/contains/import/extends/implements 中至少两类边 | `all` 等于具体 resolved edge kind 查询结果的去重并集 |
| `java_query_contract` | 公开查询契约 | 已存在节点、不存在节点、多个邻居 | `in/out/both`、`max_per_dir`、not found、非法 edge kind/direction 的结果或错误正确 |
| `java_nonresolved_call_edges` | `call` 负例 | `Class.forName(name)`、`getMethod(name).invoke(obj)`、`Proxy`/动态代理、函数式接口参数但未直接调用 | 仅返回场景内其余正常 resolved 邻居；不能凭反射、代理或参数传递虚构 target |

## 4. 目录结构

所有 Java P0 测试放在一个公共根目录，且源码 fixture 仓库只维护一份。

```text
tests/p0/java/
  fixture_repo/
    pom.xml
    src/main/java/
      com/acme/p0/
        calls/
        classes/
        imports/
        inheritance/
        negative/
  cases/
    query/
      java_call_same_class_and_control_flow.json
      java_contains_edges.json
      ...
  e2e/
    test_query_graph_java.py
  p0-java-coverage.json
```

每个 case 都指向 `fixture_repo`，通过 qname 定位其中不同 package、class 或 method。不要为每个 case 建单独 Maven 项目；`pom.xml` 仅用于使 fixture 的 Java 项目结构清晰，不要求在 E2E 中执行 Maven。

## 5. Query E2E 固定流程

```text
创建临时数据库
→ 使用 fixture_repo 建立索引
→ 依次读取 cases/query/*.json
→ 对每个 case 调用 query_graph(qualified_name, edge_kind, direction, max_per_dir)
→ 规范化公开响应（忽略无意义排序、临时 ID、时间戳）
→ 与 gold 的 in/out 邻居和公开响应字段比较
→ 关闭并删除临时数据库
```

测试不得预先向数据库插入 node 或 edge，也不得 mock Parser、Resolver、Indexer 或查询服务。

case 的最小格式：

```json
{
  "case_id": "java_call_cross_package_and_static_import",
  "language": "java",
  "p0_capabilities": ["P0-G01"],
  "fixture": "fixture_repo",
  "qualified_name": "com.acme.p0.imports::TargetService::save",
  "edge_kind": "call",
  "direction": "in",
  "expected_in": ["com.acme.p0.calls::Consumer::run"],
  "expected_out": []
}
```

实际 qname 必须先由当前 Java 索引器验证后写入 gold，不能手猜 package/class/method 分隔方式。gold 必须比较完整集合，额外邻居即失败。

`java_nonresolved_call_edges` 需要写 `negative_kind`（`reflection`、`dynamic_proxy` 或 `functional_argument`），并计入 Negative Edge Correctness；没有唯一 target 的调用不进入 Resolved Edge Recall 分母。

## 6. 覆盖清单和 CI 门禁

创建 `tests/p0/java/p0-java-coverage.json`，逐项登记 P0-G01 至 P0-G07 的唯一证据。

```json
{
  "language": "java",
  "items": [
    {
      "capability": "P0-G01",
      "case_id": "java_call_same_class_and_control_flow",
      "test_kind": "query_e2e",
      "status": "covered"
    }
  ]
}
```

CI 必须失败于任一情形：

1. 第 3 节任一 case 缺失；
2. case 未被 E2E 测试加载；
3. 任一适用能力存在 `missing` 或 `partial`；
4. case 指向的 qname 不在公共 fixture 仓库中；
5. `all` case 不等于具体 edge kind 的去重并集；
6. 负向 call case 返回了未声明的 resolved 邻居。

## 7. AI 执行顺序

1. 阅读现有 `query_graph` 公开 API 和 Java 索引配置，先建立一个最小 Java 文件并确认实际 qname 和响应 schema；不要修改产品 API。
2. 建立 `tests/p0/java/` 目录、公共 `fixture_repo` 和覆盖清单；先登记第 3 节全部 case，初始状态设为 `missing`。
3. 在公共 fixture 中添加 package、class 和 interface，先完成 `call`、`contains`、`import`、`extends`、`implements`，再补 `all`、查询契约和负向 case。
4. 实现 suite-scoped E2E harness：只建库/索引一次，循环加载 JSON 并调用公开 `query_graph`。
5. case 通过 E2E 后才将清单项标为 `covered`；计算并报告 Resolved Edge Recall、Resolved Edge Precision、Negative Edge Correctness 与 Case Coverage。
6. 只有所有适用 Java P0 项均通过，才能交付 Java P0 Query 测试基座。

完成时交付：公共 Java fixture 仓库、Query case/gold 文件、`query_graph` E2E 测试、Java 覆盖清单、CI 门禁和 P0 指标报告。
