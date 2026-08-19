# Python P0 Query 测试仓库建设指南

> 面向执行任务的 AI：在本仓库中建立 Python 的 P0 Query 测试基座。目标是证明：**给定一个已索引的公共 Python fixture 仓库，公开查询服务 `query_graph` 能返回该项目支持的 resolved 图边邻居。**
>
> P0 的公共图边和指标定义见 [Impact P0 Query 上下文覆盖规范](IMPACT_CONTEXT_P0_COVERAGE.md)。本指南只说明 Python fixture、case 和 E2E 的建设方式。

## 1. 不可违反的边界

1. 测试只能经公开服务入口完成：建库/索引后调用 `query_graph`。
2. 不断言 Parser AST、Resolver 私有方法、数据库表结构或私有边类型；只断言公开查询响应。
3. 使用一个公共 Python fixture 仓库；每个 JSON case 只指定一个待查询 qname、edge kind、direction 和 gold 响应。
4. Query P0 套件对公共 `fixture_repo` 只创建一次临时数据库和索引；全部 case 只读复用该状态，并在整套测试结束后统一清理。
5. `query_graph` 只返回 `resolved` 边。动态调用、反射和高阶参数也必须建立负向 case，但其公开断言是“不产生伪造的 resolved 邻居”。
6. 每个适用的 `(P0 capability, python)` 必须有 case、gold 文件和 E2E 证据；全部通过才可标记为 covered。

## 2. Python 的适用 P0 图边

| 图边 | Python 是否适用 | fixture 需要覆盖 |
|---|---|---|
| `call` | 是 | 顶层函数、嵌套函数、实例/类方法、构造调用、控制流内调用、跨模块调用 |
| `contains` | 是 | module → function/class，class → method |
| `import` | 是 | `import`、`from ... import ...`、alias、relative import、package re-export、唯一可解析 wildcard import |
| `extends` | 是 | `class Child(Base)`，同模块和跨模块继承 |
| `implements` | N/A | Python 没有该公开语法边；不创建 Python case，不计入分母 |
| `all` | 是 | 同一 qname 至少拥有两种以上 resolved 边时的去重聚合 |

## 3. Python Query P0 case 清单

下表是最小必建 case 集。每个 case 都使用第 4 节的同一个 `fixture_repo`。

| case_id | 图边能力 | 公共 fixture 仓库必须包含 | `query_graph` 必须断言 |
|---|---|---|---|
| `py_call_top_level_and_control_flow` | `call` | 同文件顶层 `def` 调用；`if/else`、`for`/`while`、`try/except/finally` 中的调用 | 每个唯一可解析 callee 的 `in` 和每个 caller 的 `out` 正确；不要求解释分支或循环执行次数 |
| `py_call_scope_and_recursion` | `call` | nested function、同名函数、直接递归和互递归 | lexical scope 不串边；递归调用生成正确一跳邻居 |
| `py_call_methods_and_constructor` | `call` | `self.m()`、`cls.m()`、同模块 `C.m()`、`C()` 构造调用 | 方法/构造器的 resolved caller/callee 正确 |
| `py_call_cross_module` | `call` | `import pkg.mod` 后 `pkg.mod.fn()`；`from pkg.mod import fn`；alias；相对 import；`__init__.py` re-export | 跨模块 call 解析到真实函数定义 |
| `py_call_super` | `call` | `class Child(Base)` 中的 `super().m()` | gold 记录 Base 方法为唯一 target；当前若未返回，计入 Resolved Edge Recall 缺口而不能删 case |
| `py_contains_edges` | `contains` | package module、顶层 function/class、class method、nested function | module/class 的 `out` 成员和成员的 `in` 所属节点正确 |
| `py_import_edges` | `import` | 普通/alias/relative import、package re-export、带 `__all__` 的唯一 wildcard import | 模块节点的 `import` out 邻居指向真实仓库模块 |
| `py_extends_edges` | `extends` | 同模块和跨模块 `class Child(Base)`；多层继承 | Child 的 `extends` out 邻居和 Base 的 `in` 邻居正确 |
| `py_all_edges` | `all` | 同一节点同时拥有 call/contains/import/extends 中至少两类边 | `all` 等于各具体 resolved edge kind 查询结果的去重并集 |
| `py_query_contract` | 公开查询契约 | 已存在节点、不存在节点、多个邻居 | `in/out/both`、`max_per_dir`、not found、非法 edge kind/direction 的结果或错误正确 |
| `py_nonresolved_call_edges` | `call` 负例 | `getattr(obj, name)()`、`importlib.import_module(path)`、函数作为参数传入但未直接调用 | 仅返回场景内其余正常 resolved 邻居；不能凭动态成员、运行时 import 或参数传递虚构 target |

## 4. 目录结构

所有 Python P0 测试放在一个公共根目录，且源码 fixture 仓库只维护一份。

```text
tests/p0/python/
  fixture_repo/
    pyproject.toml
    src/
      p0_fixture/
        calls/
        classes/
        modules/
        inheritance/
        negative/
  cases/
    query/
      py_call_top_level_and_control_flow.json
      py_contains_edges.json
      ...
  e2e/
    test_query_graph_python.py
  p0-python-coverage.json
```

每个 case 都指向 `fixture_repo`，通过 qname 定位其中不同模块或 symbol。不要为每个 case 建单独 Python 项目。

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
  "case_id": "py_call_cross_module",
  "language": "python",
  "p0_capabilities": ["P0-G01"],
  "fixture": "fixture_repo",
  "qualified_name": "p0_fixture.modules.target::abc",
  "edge_kind": "call",
  "direction": "both",
  "expected_in": ["p0_fixture.calls.consumer::run"],
  "expected_out": []
}
```

gold 必须比较完整集合，额外邻居即失败。`py_nonresolved_call_edges` 需要写 `negative_kind`（`dynamic_member`、`runtime_import` 或 `higher_order_argument`），并计入 Negative Edge Correctness；没有唯一 target 的调用不进入 Resolved Edge Recall 分母。

## 6. 覆盖清单和 CI 门禁

创建 `tests/p0/python/p0-python-coverage.json`，逐项登记 P0-G01、P0-G02、P0-G03、P0-G04、P0-G06、P0-G07；P0-G05 标记为 `not_applicable` 并注明 Python 无 `implements` 语法边。

```json
{
  "language": "python",
  "items": [
    {
      "capability": "P0-G01",
      "case_id": "py_call_top_level_and_control_flow",
      "test_kind": "query_e2e",
      "status": "covered"
    },
    {
      "capability": "P0-G05",
      "status": "not_applicable",
      "reason": "Python has no implements edge"
    }
  ]
}
```

CI 必须失败于任一情形：

1. 第 3 节任一适用 case 缺失；
2. case 未被 E2E 测试加载；
3. 适用能力存在 `missing` 或 `partial`；
4. case 指向的 qname 不在公共 fixture 仓库中；
5. `all` case 不等于具体 edge kind 的去重并集；
6. 负向 call case 返回了未声明的 resolved 邻居。

## 7. AI 执行顺序

1. 阅读现有 `query_graph` 公开 API 和 Python 索引配置，确认实际 qname、响应 schema 与 fixture 的 `src` layout；不要修改产品 API。
2. 建立 `tests/p0/python/` 目录、公共 `fixture_repo` 和覆盖清单；先登记第 3 节全部 case，初始状态设为 `missing`。
3. 在公共 fixture 中添加模块和类，先完成 `call`、`contains`、`import`、`extends`，再补 `all`、查询契约和负向 case。
4. 实现 suite-scoped E2E harness：只建库/索引一次，循环加载 JSON 并调用公开 `query_graph`。
5. case 通过 E2E 后才将清单项标为 `covered`；计算并报告 Resolved Edge Recall、Resolved Edge Precision、Negative Edge Correctness 与 Case Coverage。
6. 只有所有适用 Python P0 项均通过，才能交付 Python P0 Query 测试基座。

完成时交付：公共 Python fixture 仓库、Query case/gold 文件、`query_graph` E2E 测试、Python 覆盖清单、CI 门禁和 P0 指标报告。
