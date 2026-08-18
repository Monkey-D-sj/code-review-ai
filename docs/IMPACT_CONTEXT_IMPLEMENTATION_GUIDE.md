# Impact 上下文召回实施指南

> 面向对象：负责在本仓库中实现 Impact 覆盖能力的编码模型/工程师。
>
> 需求目录：[`IMPACT_CONTEXT_COVERAGE_MATRIX.md`](IMPACT_CONTEXT_COVERAGE_MATRIX.md)。
>
> 目标：逐步实现 Python、TypeScript/JavaScript、Java 的节点上下文召回，同时保证不确定关系不会污染确定调用图。

## 1. 实施原则

### 1.1 不做一次性重写

每次只选择一组相关 coverage ID，例如 `JS-M01`～`JS-M10`。一个任务应能在一次独立提交中完成，并同时包含：

1. 最小复现 fixture；
2. Parser/IR 测试；
3. Resolver 正例和负例；
4. 索引级 Impact/Test Impact 测试；
5. 增量结果与全量 rebuild 等价测试；
6. 覆盖状态和边界文档更新。

禁止先大规模重构、最后再补测试。禁止仅凭 Parser 测试就将 Coverage Matrix 条目标成完成。

### 1.2 正确性优先于召回数量

- 唯一确定的目标才允许标记为 `resolved`。
- 多个合理目标必须标记为 `candidate`，不能任选一个。
- 运行时目标不可知时使用 `dynamic`。
- 符号文本明确但仓库内不存在时使用 `unresolved` 或 `external`。
- Flow、Test Impact 和默认 Impact 只能遍历允许确定传播的边。
- 候选和动态信息应作为 `uncertainty` 返回给 Agent，而不是静默丢弃。

### 1.3 保持现有核心不变量

实现过程中必须保持：

- 全量 rebuild 单事务原子性；
- 增量更新结果与同一快照全量 rebuild 等价；
- 删除节点的 tombstone 能保留旧上游；
- flow 只消费符合遍历策略的确定边；
- 索引失败不能破坏上一个可用索引；
- MCP 默认响应有明确预算，不因候选集爆炸；
- 对外现有字段保持兼容，只能增量增加字段或做版本化变更；
- Windows/Linux 路径最终统一成仓库相对 POSIX 形式参与匹配。

## 2. 当前架构落点

| 模块 | 当前职责 | 实施时的约束 |
|---|---|---|
| `parser.py` | 文件发现、Tree-sitter 解析、统一 IR | Parse 阶段保持配置无关；不要在 AST walker 中访问数据库 |
| `resolver.py` | call/import/inherit/DI 等边解析 | 所有目标绑定最终必须从这里或注册的 resolver rule 汇总 |
| `java_routing.py` | Spring Mapping/MockMvc 语义桥 | 后续框架规则应迁入统一 adapter/rule 接口 |
| `db.py` | SQLite schema、迁移、事务 | schema 语义变化必须提升 `INDEX_VERSION` |
| `indexer.py` | 全量构建与写库 | 新 IR/edge 字段必须在这里完整写入 |
| `update.py` | 增量更新、修复 resolution、tombstone | 新关系必须验证增量重算范围和删除清理 |
| `flow_builder.py` | 从入口沿确定调用边 BFS | 不得遍历 candidate/dynamic/unresolved |
| `impact.py` | upstream/downstream/entry 输出 | 增加 evidence/uncertainty 时保持旧字段兼容 |
| `testimpact.py` | 从 changed symbol 反查测试 | 不确定时宁可返回安全降级原因，不得假装测试集合完整 |
| `changes.py` | Git diff → changed symbols | 每种语言都要覆盖新增、修改、删除、模块级变更 |
| `mcp_server.py` | 对 Agent 暴露工具 | 新字段需紧凑、可解释、有数量上限 |

当前 IR 的核心类型是 `ParsedNode`、`RawCall`、`RawInherit`、`ImportEntry`、`DiDecl`、`ParsedFile`；当前边是 `Edge(source, target, kind, file_path, resolution)`。实现新能力时优先扩展这些类型，避免为每个框架写一条完全独立的数据通道。

## 3. 目标数据模型

### 3.1 Source span

所有会参与 evidence 的 IR 记录都应携带源码位置：

```python
@dataclass(frozen=True)
class SourceSpan:
    file_path: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int
```

最低要求：`RawCall`、import、inherit、DI、route、callback registration 都能定位到文件和行。没有位置会让误边无法诊断，也无法生成可信评测证据。

### 3.2 通用原始关系

不要继续为每种框架无限增加互不兼容的字段。保留现有专用 IR 以兼容，同时引入可逐步迁移的通用结构：

```python
@dataclass
class RawRelation:
    source_qname: str
    relation: str
    target_expr: str
    language: str
    span: SourceSpan
    attributes: dict[str, object] = field(default_factory=dict)
```

建议 relation：

- `call`
- `construct`
- `import`
- `extends`
- `implements`
- `callback_register`
- `route_bind`
- `dependency_inject`
- `event_publish`
- `event_subscribe`
- `test_fixture`
- `test_lifecycle`

`attributes` 只能存可 JSON 序列化、可复现的语法事实，例如 HTTP method/path、declared type、token、annotation。不要把数据库连接、AST node 对象或环境对象放入 IR。

### 3.3 Edge provenance

扩展 `Edge` 和 `edges` 表，至少增加：

```python
origin: str          # syntax|module|type|framework|heuristic
rule_id: str         # 如 JS-M10、JAVA-F04
confidence: float    # 0.0..1.0
evidence_json: str   # 结构化证据
```

推荐 resolution 枚举：

| resolution | 语义 | 默认参与 flow |
|---|---|---:|
| `resolved` | 唯一确定的仓库内目标 | 是 |
| `semantic` | 框架规则确定的仓库内目标 | 仅允许名单中的规则 |
| `candidate` | 多个合理仓库内目标之一 | 否 |
| `dynamic` | 目标依赖运行时值 | 否 |
| `unresolved` | 有静态文本但仓库内未找到 | 否 |
| `external` | 明确属于仓库外部依赖/运行时 | 否 |

不要仅用 `confidence >= threshold` 决定是否进入 flow。遍历应由显式策略决定，例如：

```python
TRAVERSABLE = {
    ("call", "resolved"),
    ("construct", "resolved"),
    ("route_bind", "semantic"),
    ("dependency_inject", "semantic"),
    ("callback_register", "semantic"),
}
```

每条 semantic rule 必须有误匹配负例，才能加入 `TRAVERSABLE`。

### 3.4 多候选边

一个调用点存在多个候选时，为每个已知仓库内候选写一条 `candidate` 边，并使用同一个 `site_id` 归组。另保留原始表达式和候选生成原因。

候选数必须有硬上限，建议默认 20。超过上限时保存总数和截断标记，不向 MCP 返回无限列表。

### 3.5 符号身份与重载

当前 `nodes.qualified_name` 唯一，无法正确表达 Java overload。不要用“遇到同名保留第一个”继续扩展。

分两步迁移：

1. 增加内部唯一 `symbol_key`，普通符号可暂时等于 qname；Java 方法使用包含规范化参数类型的 key；
2. `qualified_name` 保留为人类可读发现名，查询遇到多个 overload 时返回 disambiguation 列表。

建议 Java key：

```text
com.example::Service.save(java.lang.String,int)
```

迁移完成前，不实现依赖 overload 唯一性的高级分派。Schema 迁移必须覆盖旧索引重建、FTS、tombstone、edges、changes 和 MCP 查询。

## 4. 解析—解析绑定流水线

按以下固定顺序实现，避免规则互相覆盖：

```text
文件发现/工程配置
  → Tree-sitter 语法抽取
  → module/package 归一
  → 符号表与类型环境
  → 确定显式调用解析
  → import/re-export/继承解析
  → 候选分派
  → 框架语义 rules
  → resolution 校验和去重
  → 写库
  → flow/impact/testimpact
```

### 4.1 Parse 阶段

Parse 阶段只记录源码事实：

- 定义、作用域、签名、装饰器/注解；
- call target、receiver、参数、类型参数和位置；
- import/export/package/module；
- declared type、return type、继承；
- route/DI/callback/test 等框架可见声明；
- 匿名函数使用稳定的文件+位置身份。

Parse 阶段禁止：

- 猜测仓库内目标；
- 读取其他文件进行绑定；
- 根据用户配置决定 DI/entry 是否生效；
- 将无法识别的表达式直接丢掉。

### 4.2 Workspace symbol table

构建统一索引：

```text
module → local name → symbol keys
type → members/signatures
source scope → declared variables/types
import alias → module/export
base/interface → subtypes/implementations
export → local/re-export target
```

符号表必须允许多值；唯一性由 resolver 在规则和上下文充分时确认。不要用 `dict[name] = last_value` 静默覆盖 overload 或同名声明。

### 4.3 Resolver rule 返回契约

每个 rule 返回提议，不直接写数据库：

```python
@dataclass
class EdgeProposal:
    source_key: str
    relation: str
    targets: list[str]
    resolution: str
    origin: str
    rule_id: str
    confidence: float
    evidence: dict[str, object]
```

中央汇总器负责：

- 验证 source/target 是否存在；
- 统一 candidate 截断；
- 去重但合并多条 evidence；
- 防止低确定性规则覆盖高确定性结果；
- 记录 rule 命中/拒绝/截断统计；
- 根据 traversal policy 决定 flow 输入。

### 4.4 Rule 优先级

建议优先级从高到低：

1. 词法作用域唯一符号；
2. 显式 import/FQCN；
3. declared receiver type；
4. 构造/返回类型推断；
5. inheritance/implementation candidate；
6. framework semantic rule；
7. 名称启发式候选；
8. dynamic/unresolved fallback。

如果高优先级规则已经得到唯一目标，低优先级规则只能增加 evidence，不能改变目标。

## 5. Impact 查询模型

### 5.1 默认确定图

默认 `get_impact` 只遍历 traversal policy 允许的边，并返回：

```json
{
  "found": true,
  "upstream": [],
  "downstream": [],
  "affected_entries": [],
  "affected_tests": [],
  "uncertainty": [],
  "coverage": {
    "resolved_edges": 0,
    "semantic_edges": 0,
    "candidate_edges": 0,
    "dynamic_edges": 0,
    "truncated": false
  }
}
```

保持现有字段和含义；新增字段必须是向后兼容的。

### 5.2 Uncertainty 输出

对 changed symbol 周围一跳或结果路径上的不确定调用返回紧凑说明：

```json
{
  "source": "plugins::dispatch",
  "expression": "getattr(plugin, name)",
  "resolution": "dynamic",
  "candidates": [],
  "rule_id": "PY-D01",
  "reason": "attribute name comes from runtime input"
}
```

默认最多返回 20 条，优先：

1. 直接触及 changed symbol 的不确定边；
2. 入口到 changed symbol 路径附近的断点；
3. 测试路径附近的断点；
4. 其他候选。

### 5.3 排序

候选上下文排序应使用可解释因素，不直接引入不可审计的综合分数：

1. 直接 caller/callee；
2. 图距离；
3. entry/test 关系；
4. resolution/origin；
5. 同模块/同社区作为次级信号；
6. 稳定 qname 作为最终 tie-breaker。

每个结果提供 `reason`，例如 `direct caller`、`2-hop caller`、`MockMvc route binding`。

### 5.4 Test Impact 安全性

`get_test_impact` 增加：

```json
{
  "complete": false,
  "fallback_recommended": true,
  "fallback_reasons": ["dynamic edge on test path"]
}
```

以下情况必须建议全量测试：

- changed symbol not_found；
- 索引 stale/版本不兼容；
- 查询异常；
- changed symbol 周围存在关键 dynamic 断点且无确定测试；
- 不支持扩展名或模块级改动无法映射；
- 配置声明要求保守回退。

## 6. 分阶段实施计划

### Phase 0：冻结基线和覆盖状态

目标：确保后续变化可比较。

任务：

- 全量运行测试并记录基线；
- 将 Coverage Matrix ID 转成机器可读清单，例如 `benchmarks/impact-coverage.json`；
- 状态仅允许 `missing/partial/covered/unsupported`；
- 为当前已有能力补映射，不改变功能；
- 固定当前 50-case 结果、配置、commit 和命令。

门禁：没有 coverage ID 和回归测试的功能提交不得合入。

### Phase 1：跨语言 Impact contract

目标：先证明现有能力真正贯穿最终查询。

新增建议目录：

```text
tests/impact_contract/
  conftest.py
  test_python_contract.py
  test_typescript_contract.py
  test_javascript_contract.py
  test_java_contract.py
  fixtures/
```

四种语言形态统一覆盖：

- direct call；
- cross-file call；
- transitive call；
- multiple callers；
- cycle/diamond；
- direct/transitive test impact；
- not_found/no coverage；
- diff modify/add/delete；
- incremental equals rebuild。

JavaScript必须使用真实 `.js/.mjs/.cjs`，不能仅复用 `.ts`。

门禁：每种语言均通过 `parse → resolve → rebuild → get_impact/get_test_impact`。

### Phase 2：Evidence、candidate 和安全降级

目标：建立后续高级能力所需的可信数据模型。

任务：

- SourceSpan；
- Edge provenance；
- candidate/semantic/external resolution；
- traversal policy；
- uncertainty MCP 输出；
- schema/index version 与迁移；
- 修正 `repair_resolutions`，禁止把 candidate/dynamic 误翻成 resolved；
- flow hash 纳入所有可遍历边的 relation/resolution/rule。

门禁：所有旧测试通过；candidate 边永不进入 flow；semantic rule 未登记时永不进入 flow。

### Phase 3：模块解析闭合

按语言拆成独立提交。

Python：

- star import + `__all__`；
- namespace/src layout；
- `.pyi` 仅作为类型信息；
- 跨模块继承通过 import 解析。

TS/JS：

- default export/import；
- `export *` 和 barrel；
- extension/index resolution；
- CommonJS；
- tsconfig `baseUrl/extends/paths`；
- package exports、workspace/project references；
- bundler/test alias adapter。

Java：

- wildcard import 候选；
- cross-package inherit；
- Maven/Gradle source set 和 multi-module；
- JPMS 的 uses/provides。

门禁：模块解析正例、冲突负例、缺失模块 external/unresolved、Impact contract、增量等价。

### Phase 4：符号身份、类型环境和多态

顺序：

1. `symbol_key` 与 Java overload；
2. receiver declared type；
3. constructor/assignment inference；
4. return-type chain；
5. inheritance/implementation index；
6. `self/this/super`；
7. interface/abstract/protocol candidate dispatch；
8. union/generic/structural 类型候选。

语言要求：

- Python：注解只用于缩小候选，不假定运行时严格遵守；
- TypeScript：结构类型可能产生多个候选，默认 candidate；
- Java：overload 可按可见静态类型解析，runtime override 默认候选集合。

门禁：同名/重载不串边；多态 case 不随机选择实现；候选数受限。

### Phase 5：函数值、回调和异步

先实现通用 function-value IR，再做生态 adapter：

- Python lambda/partial/async task；
- JS arrow/anonymous callback/Promise/EventEmitter；
- Java lambda/method reference/Executor/CompletableFuture；
- 常量 event/topic/queue；
- 动态 event 名作为 dynamic。

门禁：callback 正例进入 semantic edge；变量多次赋值时返回 candidate；动态字符串不 resolved。

### Phase 6：框架 adapters

定义接口：

```python
class SemanticRule(Protocol):
    rule_id: str
    def propose(self, workspace: WorkspaceIR) -> list[EdgeProposal]: ...
```

建议目录：

```text
code_review_ai/semantic/
  registry.py
  python_fastapi.py
  python_flask.py
  python_django.py
  python_celery.py
  js_express.py
  ts_nest.py
  frontend_react.py
  frontend_vue.py
  java_spring.py
  java_junit.py
```

优先级：

1. FastAPI route/include_router/Depends；
2. pytest fixture；
3. Express/Nest route 与 DI；
4. Jest callback；
5. Spring route/DI/Repository/Validation；
6. JUnit/Mockito/MockMvc/WebTestClient；
7. React/Vue template/event；
8. event/listener/scheduler/plugin。

每个 adapter 必须：

- 只消费统一 IR，不直接查 SQLite；
- 输出 rule_id、evidence 和 confidence；
- 有至少一个真实框架语法正例；
- 有路径/类型/名称相似但不应匹配的负例；
- 明确哪些边允许 flow 遍历；
- 配置关闭后不产生边。

### Phase 7：动态边界和运行时证据接口

静态分析无法闭合的能力不要无限堆启发式。实现统一降级：

- 动态表达式分类；
- candidate/dynamic 计数；
- Impact uncertainty；
- Test Impact fallback；
- 可选 runtime trace import 接口，但与静态边分开存储；
- runtime evidence 标记时间、环境、测试/生产来源，不能永久冒充普遍静态事实。

覆盖：Python reflection/monkey patch，JS eval/Proxy/dynamic import，Java reflection/proxy/bytecode/JNI。

### Phase 8：历史 Benchmark

创建统一 strong-gold manifest，至少：

- Python 20+ cases / 3+ repos；
- TS/JS 20+ cases / 3+ repos，TS 与 JS 都出现；
- Java 20+ cases / 3+ repos；
- A/B/C/D-E 分层满足 Coverage Matrix 要求。

runner 输出：

- Found Rate；
- Context Recall@10/All；
- Precision@10；
- MRR；
- caller/callee/entry/test 分项；
- resolved/semantic/candidate/dynamic 校准；
- query/index latency；
- 分语言、框架、规则失败归因。

不要把 co-change weak gold 与人工 strong gold 混成同一个 Recall。

## 7. 单个 Coverage ID 的标准工作流

编码模型每次接到一个能力任务，必须按以下顺序执行。

### Step 1：确认范围

- 阅读 Coverage Matrix 对应行；
- 阅读相关 Parser/Resolver/Impact/Incremental 测试；
- 写出本次唯一要闭合的关系；
- 明确成功行为和降级行为；
- 不顺手实现相邻的大功能。

### Step 2：先写失败测试

至少包含：

```text
parser positive
resolver positive
resolver near-miss negative
impact end-to-end
incremental == rebuild
```

D/E 级任务则以“不得产生 resolved/semantic traversable edge”为主要断言。

### Step 3：最小扩展 IR

- 优先增加通用字段/RawRelation；
- 不在 Parser 中做跨文件猜测；
- 保存 source span 和原始文本；
- 保证旧语言 fixture 不受影响。

### Step 4：实现 resolver/rule

- 返回 EdgeProposal；
- evidence 明确写出匹配事实；
- 冲突时 candidate；
- 外部目标 external；
- 没有足够事实时 dynamic/unresolved。

### Step 5：闭合最终产品路径

- 全量写库；
- 增量写库/删除；
- flow hash；
- impact 输出；
- testimpact 和回退；
- MCP 输出预算。

### Step 6：验证

先运行相关测试，再运行全量测试。若修改 schema、qname、resolver 或 incremental，必须额外运行：

```text
tests/test_db.py
tests/test_indexer.py
tests/test_incremental.py
tests/test_changes.py
tests/test_flow_builder.py
tests/test_impact.py
tests/test_testimpact.py
tests/test_mcp_server.py
```

### Step 7：记录

- Coverage ID 状态；
- 支持的确切语法；
- 不支持/降级边界；
- 新增 rule_id；
- 回归测试命令和结果；
- Benchmark 变化和失败 case。

## 8. 测试模板

### 8.1 Resolver 正例

```python
def test_rule_resolves_unique_target(tmp_path):
    parsed = parse_fixture(tmp_path)
    edges = resolve_edges(parsed, all_qnames(parsed), ...)
    edge = find_edge(edges, source="...", target="...")
    assert edge.resolution in {"resolved", "semantic"}
    assert edge.rule_id == "COVERAGE-ID"
    assert edge.evidence
```

### 8.2 Near-miss 负例

```python
def test_rule_does_not_match_similar_but_unrelated_code(tmp_path):
    parsed = parse_near_miss_fixture(tmp_path)
    edges = resolve_edges(parsed, all_qnames(parsed), ...)
    assert not any(
        e.rule_id == "COVERAGE-ID"
        and e.resolution in {"resolved", "semantic"}
        for e in edges
    )
```

### 8.3 Impact contract

```python
def test_rule_reaches_final_impact_output(tmp_path):
    cfg, conn = build_git_repo_and_index(tmp_path)
    result = get_impact(conn, ["changed::symbol"])[0]
    assert "expected::caller" in qnames(result["upstream"])
    assert result["affected_entries"]
```

### 8.4 不确定性

```python
def test_dynamic_relation_is_reported_but_not_traversed(tmp_path):
    cfg, conn = build_git_repo_and_index(tmp_path)
    result = get_impact(conn, ["changed::symbol"])[0]
    assert "false::caller" not in qnames(result["upstream"])
    assert any(x["rule_id"] == "COVERAGE-ID"
               for x in result["uncertainty"])
```

### 8.5 增量等价

```python
def test_rule_incremental_equals_clean_rebuild(tmp_path):
    incremental = apply_change_and_sync(tmp_path)
    rebuilt = rebuild_fresh_copy(tmp_path)
    assert normalized_nodes(incremental) == normalized_nodes(rebuilt)
    assert normalized_edges(incremental) == normalized_edges(rebuilt)
    assert normalized_impact(incremental) == normalized_impact(rebuilt)
```

## 9. 性能与安全预算

- Parser/Resolver 不得对每个调用点扫描全部仓库节点；先建索引再查询。
- 常用查找目标复杂度应接近 O(1) 或 O(log n)。
- candidate 默认最多 20 个/调用点，Impact uncertainty 最多 20 条/changed symbol。
- framework rule 应按 annotation/API/topic/path 预分组，禁止全量笛卡尔积。
- 全量 rebuild 的新增阶段必须被计时。
- Incremental 只重算受变更文件及其依赖闭包；若无法证明局部安全，明确回退全量。
- 任何由外部配置、路径或字符串生成的目标都要规范化并限制在 repo root。
- 日志默认不输出完整源码，只输出 qname、相对路径、行号和 rule_id。

## 10. 禁止的实现方式

- 看到同名函数就直接 resolved。
- 把所有继承实现都当作确定运行时目标。
- 用文件名相似度生成可遍历调用边。
- 把 Git co-change 写进静态调用图。
- 用一个综合置信分数掩盖 resolution 的语义区别。
- candidate/dynamic 边参与默认 flow/Test Impact。
- Parser 直接依赖 Flask/FastAPI/Spring 的运行环境。
- 为通过单个 benchmark case 写仓库名、commit 或文件名特判。
- 修改 qname/schema 后只跑语言单测，不跑 changes/incremental/tombstone/MCP。
- 文档宣称支持，但没有索引级 Impact 测试。
- 对无测试结果直接输出“无需测试”；必须区分“确定无影响”和“无法证明”。

## 11. 每阶段交付清单

编码模型完成一个阶段时，最终答复必须包含：

```text
实现的 Coverage IDs：
修改的 IR/Schema/API：
新增的确定关系：
新增的候选/动态降级：
新增正例：
新增负例：
增量等价验证：
相关测试结果：
全量测试结果：
Benchmark 变化：
仍未覆盖的边界：
```

若任一项不适用，应写明原因，不能省略。

## 12. 推荐执行顺序

```text
Phase 0 基线/状态
  → Phase 1 三语言 Impact contract
  → Phase 2 evidence/candidate/降级模型
  → Phase 3 模块解析
  → Phase 4 类型/重载/多态
  → Phase 5 回调/异步
  → Phase 6 框架与测试 adapters
  → Phase 7 动态边界
  → Phase 8 历史 Benchmark
  → Full Agent Eval
```

不要从最复杂的反射、AOP 或前端模板开始。先建立跨语言最终查询契约和不确定性模型，否则新增语法即使被 Parser 抽取，也无法证明 Agent 最终能够安全地得到上下文。
