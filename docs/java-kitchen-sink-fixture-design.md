# Java Kitchen-Sink Fixture 设计

日期：2026-08-19
状态：fixture 已构建（`fixtures/java_kitchen_sink/`，27 文件，0 解析错误）并通过关键边验证；验证暴露并修复 3 个解析缺陷（见 §验证）；本文档是场景对照清单与后续迁移/验证蓝图

## 背景

项目评测经历了两个口径问题：

1. **覆盖矩阵 327 ID 偏多**：矩阵是护栏，不是"一个函数找上下文"的核心。核心问题是——一个函数能否找到它的上下文（上游调用方 / 下游被调方）。
2. **提交级 gold 有标签噪声**：真实 commit 的 co-change gold（`SPRING_PETCLINIC_RECALL_ANALYSIS.md` 中 3/15）与改动符号无任何调用链，造成 80% 的指标天花板。这是评测口径问题，不是 resolver 质量问题。

本设计的回答：用**一个手写合成 fixture** 当 Java 场景的单一事实来源。合成 fixture 的 ground truth 是**构造出来的**——每个符号的上下游由我们写代码时定义，不存在 commit 共变噪声，gold 天然等于"A 的真实上下文"。

## 目标

一个 canonical Java fixture（`fixtures/java_kitchen_sink/`），同时承担三个角色：

1. **覆盖矩阵 Java 相关 ID 的单一事实来源**（`docs/IMPACT_CONTEXT_COVERAGE_MATRIX.md`）；
2. **Impact contract / resolver 断言的主要数据源**（替代目前散落的内联 GRAPH）；
3. **Phase 4/6 推进的固定容器**：同一份代码、两代断言（现在测降级契约，Phase 4/6 升语义边）。

## 设计原则

- **P1 — Ground truth by construction**：fixture 无 commit 历史，上游/下游是构造已知的；gold 与工具目标对齐，无 co-change 噪声。
- **P2 — 按测试目的分流**：canonical fixture 承载覆盖矩阵/契约断言；**单独小图**承载需要改文件或最小复现的尖边（见 §6）。不要把所有测试钉在一个大 fixture 上。
- **P3 — 两代断言**：C 档（框架语义）代码现在进 fixture，断言记录当前诚实行为（dynamic/unresolved + evidence + uncertainty + 回退建议）；Phase 4/6 落地后**同一份数据**升级为语义边断言。Phase 推进在同一 fixture 上可见。
- **P4 — 查询式断言，不全边快照**：断言写"查图 → 对 qname 集合"（`get_impact` / `get_symbol_detail` / `query_graph`），不写整库边快照——否则加第 N+1 个文件时全部断言崩掉，制造交叉污染。
- **P5 — 每簇场景独立包/文件，少交叉引用**：只让"跨文件调用"类的场景互相引用；其余场景靠包隔离。
- **P6 — 跨语言各建各的 fixture**：`java_kitchen_sink` 先行，`py/ts/js_kitchen_sink` 同构后补（语法不同，无法共享文件集）。

## 目录布局（27 文件，已构建于根目录 `fixtures/`）

```
fixtures/java_kitchen_sink/
  src/main/java/
    com/example/App.java                    入口 + 静态 + FQ
    com/example/core/BaseEntity.java        跨包基类（COM-M05）
    com/example/controller/OwnerController.java   @RestController + 类级 @RequestMapping
    com/example/controller/PetController.java     @Valid 参数 + @InitBinder
    com/example/controller/OwnerAdvice.java       @ControllerAdvice/@ExceptionHandler（F13）
    com/example/service/ClinicService.java        接口（default 方法、抽象方法）
    com/example/service/ClinicServiceImpl.java    @Service 构造器注入
    com/example/repo/OwnerRepository.java         Spring Data 接口 + derived query
    com/example/repo/OwnerRepositoryImpl.java     @Repository 实现
    com/example/domain/Owner.java                 @Entity extends BaseEntity
    com/example/domain/Pet.java                   @Entity extends BaseEntity
    com/example/domain/PetType.java               枚举 + constant class body（S13）
    com/example/domain/Address.java               record（S14）
    com/example/validator/PetValidator.java       implements Validator
    com/example/util/CommonUtil.java              静态工具（S08/S09/COM-C12）
    com/example/config/AppConfig.java             @Configuration/@Bean（F07/F08/F06/F24）
    com/example/async/CallbackSamples.java        Stream/Optional/Future/Runnable（I01-I08 预留）
    com/example/modela/Widget.java                wildcard 唯一命中目标
    com/example/modelb/Widget.java                wildcard 冲突目标（与 modela 同名）
    com/example/WildcardConsumerA.java            import modela.* 唯一 → resolved
    com/example/WildcardConsumerB.java            import modela.* + modelb.* 冲突 → candidate
    com/example/legacy/LegacyService.java         extends com.base.BaseService
    com/base/BaseService.java                     跨包基类（COM-M05/S07）
  src/test/java/com/example/
    OwnerControllerTests.java                     @Test + MockMvc
    PetControllerTests.java                       MockMvc + @ParameterizedTest
    ClinicServiceTests.java                       直连 Service + @Mock/@InjectMocks
    PetValidatorTests.java                        Validator 单测
```

## 场景 → 文件对照表（核心）

状态列 = `benchmarks/gen_impact_coverage.py` OVERLAY 当前值（✅ covered / ◐ partial / ✗ missing / ⛔ unsupported）。
"两代断言" = 现在测降级契约，Phase 4/6 升语义边断言。

### `App.java` — 入口 + 静态 + FQ

| ID | 场景 | 现状 | 断言 |
|---|---|---|---|
| JAVA-S01 | class 类型节点 + contains | ✅ | App 类含 main() 成员 |
| JAVA-S03 | method/constructor 独立成员节点 | ✅ | main() 节点 |
| JAVA-M01 | package 声明 → FQCN/qname | ✅ | qname 前缀 `com.example` |
| JAVA-M05 | fully-qualified call/type | ✅ | `com.example.util.CommonUtil.trim()` 精确绑定 |
| JAVA-S08 | 静态调用 `C.m()` | ◐ | `CommonUtil.trim()` 静态绑定 |
| COM-C12 | 外部库/builtin | ✅ | `String.format()` → unresolved 不进 flow |

### `controller/` — Spring MVC 簇

| ID | 场景 | 现状 | 断言 |
|---|---|---|---|
| JAVA-F01 | @Controller/@RestController mapping → entry | ✅ | GET/POST 路由入口 |
| JAVA-F02 | 类级+方法级 RequestMapping 合并 | ✅ | `/owners/{ownerId}/pets/new` |
| JAVA-F03 | path var/wildcard/query | ◐ | @PathVariable 模板证据 |
| JAVA-F11 | @Valid → validator | ✗ | **两代断言** |
| JAVA-F12 | @ModelAttribute/@InitBinder | ✗ | **两代断言** |
| JAVA-F13 | @ControllerAdvice/@ExceptionHandler | ✗ | OwnerAdvice.java 落点 |
| JAVA-S15 | 声明类型 receiver 绑定 | ✅ | `clinicService.…` 字段绑定 |
| COM-C03/C05/C07/C08 | 传递调用/多入口/菱形/环 | ✅ | 经 controller→service→repo 链 |

### `service/` — 接口 + 实现

| ID | 场景 | 现状 | 断言 |
|---|---|---|---|
| JAVA-M10 | interface extends/implements 闭合 | ✅ | impl→接口边 |
| JAVA-M09 | class extends | ✅ | impl→Base 类边 |
| JAVA-F04 | 构造器注入 | ✅ | rule_id=JAVA-F04 + evidence |
| JAVA-M11 | default interface method | ✗ | Phase 4 落点 |
| JAVA-M12 | abstract method dispatch | ✗ | 多实现 → candidate |
| JAVA-M13 | runtime polymorphism | ✗ | declared/points-to 缩小 |
| COM-M06 | override 索引 | ✗ | @Override 对 |

### `repo/` — Spring Data

| ID | 场景 | 现状 | 断言 |
|---|---|---|---|
| JAVA-F09 | Spring Data Repository proxy | ✗ | Phase 6：接口方法作稳定目标 |
| JAVA-F10 | derived query method | ✗ | `findByLastName` Phase 6 |
| JAVA-M15 | generic/bridge method | ✗ | `JpaRepository<Owner,Integer>` |
| JAVA-S16 | `var` 推断 | ◐ | `var owners = …` receiver 绑定 |
| COM-C14 | 多候选 | ◐ | 接口/impl 双目标 → candidate |

### `domain/` — 实体与类型

| ID | 场景 | 现状 | 断言 |
|---|---|---|---|
| JAVA-M09 | 跨包 class extends | ✅ | Owner → com.example.core.BaseEntity |
| JAVA-S02 | annotation 使用 | ◐ | @Entity/@Id 使用边 |
| JAVA-S13 | enum constant class body | ✗ | PetType 枚举 |
| JAVA-S14 | record ctor/accessor | ✗ | Address record |
| JAVA-S11 | anonymous class | ✗ | `new Thread(){}` |
| JAVA-S12 | local/nested class | ◐ | 方法内局部类 |
| JAVA-S21 | initializer/static block | ◐ | `static { }` |
| JAVA-S22 | try-with-resources | ✗ | AutoCloseable 候选 |
| JAVA-F20 | JPA entity callbacks | ✗ | @PrePersist |
| JAVA-D01 | reflection | ✗ | 常量类/方法名 → 候选 |

### `modela/` + `modelb/` + `WildcardConsumer*` — wildcard 簇

| ID | 场景 | 现状 | 断言 |
|---|---|---|---|
| JAVA-M04 | `import a.b.*` 唯一命中 | ✅ | WildcardConsumerA → resolved |
| JAVA-M04 | 双包冲突 | ✅ | WildcardConsumerB → candidate（共享 site_id + candidates 证据） |
| COM-M04 | 跨语言 wildcard | ✅ | 与 Python/TS star 同型 |

### `config/` — Spring 装配

| ID | 场景 | 现状 | 断言 |
|---|---|---|---|
| JAVA-F07 | @Bean method | ✗ | Phase 6 |
| JAVA-F08 | component scan | ✗ | Phase 6 |
| JAVA-F06 | qualifier/primary/named | ✗ | Phase 6 |
| JAVA-F24 | config properties | ✗ | Phase 6 |
| COM-I08 | DI provider→consumer | ◐ | 已有 F04/F05 边 |

### `validator/` — 校验语义

| ID | 场景 | 现状 | 断言 |
|---|---|---|---|
| JAVA-F11 | Validator 本体 | ✗ | **两代断言** |
| COM-T10 | framework integration test | ◐ | route 语义边可达 |

### `com/base/BaseService.java` + `legacy/LegacyService.java` — 跨包继承

| ID | 场景 | 现状 | 断言 |
|---|---|---|---|
| COM-M05 | 跨包继承经 import | ✅ | extends com.base.BaseService |
| JAVA-S07 | `super.m()`/`super()` | ✗ | 派生类 super 调用 |

### `test/` — 四个测试文件

| ID | 场景 | 现状 | 断言 |
|---|---|---|---|
| JAVA-T01 | @Test 识别 | ✅ | |
| JAVA-T10 | MockMvc route | ✅ | method/path → handler |
| JAVA-T13 | 测试源集/文件命名 | ✅ | |
| JAVA-T02 | @ParameterizedTest | ◐ | 归一到测试方法 |
| JAVA-T07 | @Mock/@Spy/@InjectMocks | ✗ | |
| JAVA-T08 | when/verify 弱上下文 | ✗ | |
| JAVA-T09 | @MockBean | ✗ | |
| JAVA-T03/T04/T05/T06 | @RepeatedTest/@Nested/Before-After/extension | ✗ | |
| JAVA-T11/T12 | WebTestClient / integration test DI | ✗ | |
| COM-T01..T06 | test impact contract | ✅/◐ | 直接/传递/合并/not_found/回退 |

### `async/CallbackSamples.java` — Phase 5 预留

| ID | 场景 | 现状 | 断言 |
|---|---|---|---|
| JAVA-I01..I04 | Stream/Optional/CompletableFuture/Executor | ✗ | 现在只断言降级契约 |
| JAVA-I05..I08 | Timer/listener/ServiceLoader/serialization | ✗ | 同上 |

## 状态总览（90 个 Java ID）

| 段 | ID 数 | ✅ | ◐ | ✗ | ⛔ |
|---|---:|---:|---:|---:|---:|
| S 类型/成员/调用 | 22 | 6 | 7 | 9 | 0 |
| M 包/模块/继承 | 16 | 7 | 1 | 8 | 0 |
| I 回调/并发/stdlib | 8 | 0 | 0 | 8 | 0 |
| F Spring/Jakarta | 24 | 4 | 1 | 19 | 0 |
| T 测试生态 | 13 | 3 | 1 | 9 | 0 |
| D 动态边界 | 7 | 0 | 0 | 3 | 4 |
| **合计** | **90** | **20** | **10** | **56** | **4** |

- **现在实断言**：✅20 + ◐10 = 30 个 ID
- **预留（两代断言）**：✗ 中 C 档（Spring F06-F24、测试 T03-T12、回调 I01-I08）代码进 fixture，现断言降级，Phase 4/6 升级
- **永不覆盖**：JAVA-M06/M07/M08（需构建配置摄取，推迟）+ JAVA-D02/D03/D04/D07（unsupported）

## 单独小图（不进 fixture）

| 测试 | 原因 |
|---|---|
| JAVA-M02 无 package 回退 | 尖边，保留现有 inline repo |
| COM-C08/C09 调用环/互递归终止 | 最小复现（test_flow_builder 微型图） |
| COM-T05/T06 无覆盖回退 / not_found | 最小复现（test_testimpact 微型图） |
| 增量等价（incremental 改文件） | 要改文件，自拷贝图 |
| baseUrl 等互斥配置态 | 状态不能共存于一个文件集 |

原则：**大 fixture 当"覆盖面"的地基，微型图当"聚焦复现"的手术刀**，按测试目的分流，不是按场景普通/特殊。

## 两代断言说明

C 档（框架语义）代码现在进 fixture，但 resolver 尚未产出对应语义边。两代断言：

- **现在**：断言当前诚实行为——调用落 dynamic/unresolved/candidate，带 `evidence_json`，经 `get_impact.uncertainty` 暴露，`get_test_impact` 建议回退。
- **Phase 4/6 后**：同一份 fixture，断言升级为 semantic/resolved 边（`rule_id` + provenance），`uncertainty` 项消失。

好处：Phase 推进在**同一份数据**上可见，fixture 无需重写，只升级断言文件。

## 与评测口径的关系

- fixture 无 commit 历史 → 无 co-change gold → 主指标天然等于"A 的上下文还原"，无 80% 天花板问题。
- 真实仓库历史 benchmark（PetClinic history-10）保留为**次指标**（提交级覆盖率 / 开发价值信号），两指标分离报告。
- direct/co-change 拆分（`benchmarks/SPRING_PETCLINIC_RECALL_ANALYSIS.md` §评测口径拆分）继续用于真实仓库评测。

## 构建与迁移计划（提交拆分）

1. **骨架 + App/domain**（S01/S03/M01/M05/S08、M09、S02）：建目录 + 基础文件，加 build_index 入口。
2. **controller/service/repo**（F01/F02/F04、M10、S15、M09）：Spring MVC + DI 簇。
3. **wildcard 簇**（M04/COM-M04）：modela/modelb + 两个 consumer。
4. **test/**（T01/T10/T13、COM-T 契约）：四个测试文件，MockMvc 断言。
5. **预留文件**（F06-F24 代表、I01-I08 代表、S 档 ✗ 代表）：只建代码 + 降级断言。
6. **迁移**：`test_java_contract` / `test_resolver_java` / `test_java_routing` / `test_span`（DI 锚点）从内联 GRAPH 改读 fixture（增量，不动基建）。
7. **记录**：OVERLAY/覆盖矩阵同步。

## 验证

- 每个提交后 `uv run --no-sync pytest tests/impact_contract/ tests/test_resolver_java.py tests/test_java_routing.py`（定向）+ 全量。
- fixture 建成后固定重跑：断言全绿 = 覆盖矩阵当前档位全部成立；Phase 4/6 后重跑同一断言文件 = 升级生效。
- 交叉污染护栏：断言只用查询式（`get_impact`/`get_symbol_detail`/`query_graph`），禁止整库边快照；加文件后全量回归必须无涟漪。

### 关键边验证记录（2026-08-19，一次性脚本，已删）

27 文件 0 解析错误；92 节点 201 边。验证暴露并修复 3 个解析缺陷（均同根因：**Java 包 = module，同包多文件共享 module 级字典被互相污染**）：

| # | 缺陷 | 修复 |
|---|---|---|
| 1 | `star_map` 按 module 聚合 → 同包两个 consumer 的 `import a.b.*` 合并，`WildcardConsumerA` 唯一 wildcard 也判成 candidate | resolver：`resolve_calls` 按文件算 per-file `star_modules`，线程传入 `_resolve_one`/`_resolve_java`（Python/TS 的 module==file，语义不变） |
| 2 | `_build_di_edges`/`_build_inherits` 用 `all_import_maps.get(module)`（dict 推导后文件覆盖前文件）→ `OwnerController` ctor 注入 `ClinicService` 的 F04 边丢失、同包多文件继承 import 解析不稳 | resolver：两函数改按文件 `_import_map(pf)` |
| 3 | 无参 `@GetMapping` 无字符串参数 → `_annotation_strings` 返回 [] → 无路由映射，`get("/owners")` 命中不了 findOwners | parser：`_java_mappings` 无 path 时产出 `""` 默认路径，`_join_mapping_path("", prefix)` 返回类级前缀 |

修复后关键边全 PASS（继承 kind=`extends`；`Owner→BaseEntity`/`Pet→BaseEntity`/`LegacyService→BaseService` resolved；3 条 ctor-F04；4 条 MockMvc 路由；`WildcardConsumerA` resolved、`WildcardConsumerB` 双 candidate；`super.start` 保持 dynamic 即 JAVA-S07 诚实降级）。全量 `458 passed`（修复前 390 基线之上含先前阶段新增）。

## 注意

- `uv run --no-sync`：code-review-ai-mcp.exe 持锁时避免 uv sync 卡死。
- qname 一律走 `qname.join`/`qname.short`（CLAUDE.md 强制）。
- fixture 文件是测试**数据**（零测试逻辑）；测试断言**引用** fixture 符号（契约+校验关系）——改 fixture 文件会连带改对应断言，这是单一事实来源的预期代价。
