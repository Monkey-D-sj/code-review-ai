# Spring PetClinic Recall@All 分析

日期：2026-08-07

数据集：`benchmarks/spring-petclinic-history-10.json`

最新结果：`benchmark-results/spring-petclinic-history-10-rerun.json`

## 结论

加入 MockMvc 路由语义边后，Spring PetClinic 的 Test Recall@10 和
Recall@All 从 **23.33% 提升到 58.33%**。10 个 case 中有 **7 个**至少命中
一个 gold test，原始基线为 3 个。

这证明 MockMvc 请求到 Spring Controller 方法之间的隐式路由是此前最重要的
断点之一。当前 Recall@All 仍未达到较高水平，主要瓶颈已经转为：

1. Java 实例接收者的类型绑定；
2. Spring Bean、Repository 和 Service 依赖关系；
3. 实体/Validator 到间接测试的关系；
4. gold 文件中不存在静态调用关系的提交级噪声。

Top-K 暂时不是瓶颈：Recall@10 与 Recall@All 仍然相同。

## 本次结果

| 指标 | 原始基线 | 本次重跑 | 变化 |
|---|---:|---:|---:|
| Test Recall@10 | 23.33% | **58.33%** | +35.00pp |
| Test Recall@All | 23.33% | **58.33%** | +35.00pp |
| Test Precision@10 | 4.61% | **10.85%** | +6.24pp |
| Test Precision@All | 4.28% | **10.51%** | +6.23pp |
| 至少命中一个 gold 的 case | 3/10 | **7/10** | +4 |
| Symbol Found Rate | 100% | **100%** | 不变 |
| 平均 resolved-call rate | 5.65% | **7.85%** | +2.20pp |
| Production Recall@10 | 10.94% | **17.19%** | +6.25pp |
| Production Recall@All | 10.94% | **17.19%** | +6.25pp |
| 平均候选文件数 | 4.7 | **6.4** | +1.7 |
| 平均索引时间 | 332.9 ms | **345.1 ms** | +12.2 ms |
| 平均查询时间 | 1.53 ms | **1.54 ms** | 基本不变 |

15 个 gold test 文件中实际命中 7 个，micro recall 为 **46.67%**。报告中的
58.33% 是先分别计算每个 case 的 recall，再做 macro average，因此两者不同。

## 调用边覆盖

本次 10 个历史快照的调用解析统计合计如下：

| 调用边状态 | 数量 | 占比 | 是否参与 flow |
|---|---:|---:|---|
| resolved | 918 | 7.76% | 是 |
| dynamic | 7,554 | 63.87% | 否 |
| unresolved | 3,356 | 28.37% | 否 |

报告里的平均 resolved-call rate 是 **7.85%**；上表的 **7.76%** 是把 10 个
快照的边数合计后计算的加权占比，统计方式不同。

当前 flow 只遍历 `resolution="resolved"` 的边。Recall@All 会把查询数量上限
扩展到全部索引节点，但不会遍历 dynamic/unresolved 边。因此它衡量的是
“当前已解析图的完整覆盖率”，并非无条件返回仓库内全部文件。

## Case 明细

| 提交 | Gold 数 | 基线 Recall@All | 本次 Recall@All | 本次表现 |
|---|---:|---:|---:|---|
| `bb37aad8c3` owner search | 1 | 0% | **100%** | 命中 `OwnerControllerTests` |
| `e0db9b184e` unique pet names | 3 | 0% | 0% | Controller、Service、并发测试均未命中 |
| `753d35c2f8` future visit dates | 1 | 0% | **100%** | 命中 `VisitControllerTests` |
| `142321aa3e` findPetTypes | 3 | 33.33% | 33.33% | 只命中 `PetTypeFormatterTests` |
| `40a41375e6` PetValidator test | 1 | 0% | 0% | seed 与 gold 缺少直接关系 |
| `1cad4124b7` owner logic refactor | 2 | 0% | **50%** | 命中 `OwnerControllerTests`，漏 Service test |
| `50866def72` pet validation | 1 | 0% | 0% | 实体/验证逻辑未连到 Controller test |
| `14af47d4e5` owner id mismatch | 1 | 0% | **100%** | 命中 `OwnerControllerTests` |
| `405cdc635b` owner error message | 1 | 100% | **100%** | 保持命中 `OwnerControllerTests` |
| `4926e29270` validation annotations | 1 | 100% | **100%** | 保持命中 `ValidatorTests` |

## 已经解决的主要问题：MockMvc 路由断点

Spring Controller 测试通常使用：

`mockMvc.perform(get("/owners"))`

业务方法则由 `@GetMapping`、`@PostMapping` 等注解在运行时选择，测试源码不会
直接调用 `OwnerController.processFindForm()`。普通静态调用解析无法发现这条
关系。

当前实现已经解析 MockMvc 请求和 Controller mapping，并建立 synthetic resolved
edge：

`JUnit test method -> HTTP method/path -> Controller method`

这使以下 case 得到直接改善：

- `bb37aad8c3`：0% -> 100%；
- `753d35c2f8`：0% -> 100%；
- `1cad4124b7`：0% -> 50%；
- `14af47d4e5`：0% -> 100%。

说明框架语义边是有效的，也说明仅分析源码里的显式方法调用不足以覆盖 Spring
应用测试。

## 当前剩余瓶颈

### 1. Java 实例接收者没有完整类型绑定

解析器可以提取 `owners.findByLastName(...)`、`repository.findAll(...)` 之类调用，
但尚未完整建立字段、构造器参数、方法参数和局部变量到声明类型的符号表。

当接收者是 `owners`、`repository` 或其他实例变量时，resolver 无法稳定绑定到
`OwnerRepository.findByLastName()` 等具体符号，调用会落入 dynamic。这仍是
63.87% dynamic 边的主要来源。

它直接影响：

- `e0db9b184e` 中 Controller 到 Repository/Service 的关系；
- `1cad4124b7` 中尚未命中的 Service test；
- 多业务文件提交的 Production Recall@All。

### 2. Spring Bean、Repository 和 Service 关系不完整

当前调用图仍缺少或不完整覆盖：

- 构造器参数类型 -> 注入字段；
- Controller -> Repository/Service Bean；
- Repository 接口 -> Spring Data 运行时实现；
- 接口方法、继承方法和泛型方法的动态分派；
- `@ModelAttribute`、validation callback 等隐式框架调用。

对 Spring Data 而言，即使运行时实现不存在于源码中，也应至少把实例调用解析到
Repository 接口方法，否则业务链会在 Controller/Service 层断开。

### 3. 实体和 Validator 到测试的间接关系

`50866def72` 的改动集中在 `NamedEntity` 和 Pet validation，但 gold 是通过
MockMvc 验证行为的 `PetControllerTests`。只建立 route edge 不够，还需要把：

`Controller -> validation -> model/validator`

这条语义链建出来。可考虑解析 `@Valid`、`Validator.validate()`、绑定对象类型和
Controller 方法参数。

### 4. 外部框架调用仍为 unresolved

28.37% 的调用为 unresolved，主要来自 Spring、JUnit、Mockito、Jakarta
Validation 和 Java 标准库。外部库本身不一定需要完整索引，但承担路由、回调、
依赖注入或 validation 语义的调用不能全部丢弃，应转成有限且可信的 synthetic
edge。

### 5. Gold 是提交级关联，不一定存在调用链

本 benchmark 使用真实 commit 中同时修改的测试文件作为 gold。这种口径可复现，
但不保证生产 seed 与每个 gold test 之间存在静态调用关系。

例如 `40a41375e6` 的生产改动是 `OwnerRepository.java`，gold 却是
`PetValidatorTests.java`，两者没有明显调用链。即使 resolver 完全正确，这个
case 也可能无法通过调用图召回。

`e0db9b184e` 同时包含 Controller test、Service test 和并发测试，也要求从一个
Controller seed 召回全部三者。这同时考察了调用关系和更宽泛的提交级共变关系。

## 为什么 Recall@10 仍等于 Recall@All

本次平均完整候选集合只有 6.4 个文件。相关测试一旦进入 resolved 图，通常已经
排在前 10；没有命中的测试则根本不在可达图中。

所以继续增大 K 不会改善当前结果。只有当 Recall@All 明显高于 Recall@10 时，
排序和上下文预算才成为主要问题。

## 后续优先级

### P0：Java 类型绑定

解析字段、构造器参数、方法参数和局部变量的声明类型，将
`receiver.method()` 绑定到 `DeclaredType.method()`。优先覆盖构造器注入字段、
`this.field` 和字段简称。

### P0：Spring Repository/Service 语义

建立 Controller、Service、Repository 接口之间的依赖边；即使没有运行时实现，
也将调用绑定到仓库内的接口方法。

### P1：Validation 语义边

解析 `@Valid`、Validator、Controller 参数类型和模型绑定，覆盖实体/Validator
改动到 Controller test 的路径。

### P1：JUnit、Mockito 和注入字段

使用 `@Test`、`@Mock`、`@InjectMocks`、`@Autowired` 及测试字段声明辅助测试入口
和接收者类型解析。

### P2：拆分评测口径

同时报告两类 gold：

- **Direct/framework-call gold**：测试与 seed 存在直接或框架语义调用关系；
- **Commit co-change gold**：保留当前真实提交文件口径。

这样可以区分 resolver 漏边与提交级非调用关联。

## 验收指标

下一次改进后继续固定重跑这 10 个 case，并观察：

- Test Recall@All 和 Test Recall@10；
- resolved/dynamic/unresolved 比例；
- `e0db9b184e`、`40a41375e6`、`50866def72` 三个零命中 case；
- `142321aa3e`、`1cad4124b7` 两个部分命中 case；
- Production Recall@All；
- 新增 synthetic edge 的误匹配测试。

当前最有价值的目标不是调排序，而是继续把可信的 Java/Spring 隐式关系转成
resolved edge。

## 类型绑定后结果（2026-08-07）

实现 Java 接收者类型绑定（`docs/superpowers/specs/2026-08-07-java-type-binding-design.md`，字段/参数/局部变量→声明类型）后重跑同一 10 条 case：

| 指标 | MockMvc 后 | 类型绑定后 | 变化 |
|---|---:|---:|---:|
| Test Recall@10 / @All | 58.33% | **65.00%** | +6.67pp |
| Test Precision@10 | 10.85% | **11.03%** | +0.18pp |
| Production Recall@All | 17.19% | **34.90%** | +17.71pp |
| 平均 resolved-call rate | 7.85% | **10.53%** | +2.68pp |

per-case 关键变化：

- `142321aa3e` findPetTypes：33% → **100%**（`PetControllerTests` + `PetTypeFormatterTests` + `ClinicServiceTests` 全命中——Service test 经 `clinicService.…` 字段绑定连通）；
- `1cad4124b7` 仍 50%（Service test 未达：gold Service test 与 Controller seed 之间仍缺一条业务链）；
- `e0db9b184e` / `50866def72` 仍 0%。

PetClinic 索引里新增 **51 条**进入 Repository 方法的 resolved 边（`OwnerController.findOwner -> OwnerRepository.findById` 等）。

**下一步**（剩余零/部分命中归因）：

- `1cad4124b7` / `e0db9b184e`：Controller → Service → Repository 的业务链已有 51 条 Repository 边，但 Service 层的字段/构造器注入绑定、以及「测试直连 Service」到「Controller 经 Service」的连通仍需 Spring Bean/DI 语义（P0）；
- `50866def72`：改动在实体/Validator，Controller test 经 `@Valid`/Validator 语义链（P1）才可达；
- `40a41375e6`：提交级噪声，静态图无法召回。

## 类级 @RequestMapping 前缀后结果（2026-08-07）

实现类级 `@RequestMapping` 前缀合并（`docs/superpowers/specs/2026-08-07-class-level-requestmapping-prefix-design.md`）后重跑：

| 指标 | 类型绑定后 | 前缀合并后 | 变化 |
|---|---:|---:|---:|
| Test Recall@10 / @All | 65.00% | **78.33%** | +13.33pp |
| Test Precision@10 | 11.03% | **15.32%** | +4.29pp |
| Production Recall@All | 34.90% | **30.21%** | -4.69pp |

per-case 关键变化：

- `50866def72` pet validation：0% → **100%**（PetController 的 `/owners/{ownerId}/pets/new` 路由拼上前缀后命中 `PetControllerTests`）；
- `e0db9b184e` unique pet names：0% → **33.33%**（`PetControllerTests` 命中；`ClinicServiceTests`/`PetClinicConcurrencyTests` 仍是提交级 co-change，非静态可达）；
- 其余 case 不变。10 个 case 中 **8 个**至少命中一个 gold test。

**Production Recall 小幅回退（34.90% → 30.21%）**：来自单个生产文件 fold（`1cad4124b7` 第 3 个 fold，1.0 → 0.5）。原因是新增的 PetController route 边改变了该 fold 的候选集合（cand 5 → 4），一个 gold 生产文件被挤出。这是「加边改变候选构成」的副作用，不是前缀逻辑错误（拼接后的路由是准确的）。Test 主指标净提升 +13pp。

**剩余零/部分命中归因**：

- `e0db9b184e` 的 `ClinicServiceTests`/`PetClinicConcurrencyTests`：改动在 PetController 校验逻辑，这两个 test 直连 Repository/Service 层，与改动符号无静态调用链（co-change）；
- `1cad4124b7` 的 `ClinicServiceTests`：只调 `findById`/`findPetTypes`，改动的是 `findByLastName`/`findAll`（co-change）；
- `40a41375e6`：提交级噪声，无调用关系。

## 评测口径拆分（2026-08-07）

实现 P2「拆分评测口径」(`code_review_ai/benchmark.py` 的 `_classify_golds`):给每个 gold 文件打 **direct / co-change** 标签,聚合同时报告两个 recall。

- **direct gold** = 测试源码以词边界**提及**(引用)改动生产文件里定义的类(图无关判据;Java 同包引用不需要 import,故用文本匹配而非 import 检查)。
- **co-change gold** = 同一 commit 改动、但测试源码未引用任何改动类(提交级噪声)。

同一 10 条 case 的两种口径:

| 指标 | 数值 |
|---|---:|
| macro_test_file_recall_all(原始,co-change 口径) | 78.33% |
| **macro_direct_test_file_recall_all**(仅 direct gold) | **94.44%** |
| cochange_gold_count | 3 / 15 |

per-case 归因(只列 direct 部分):

| 提交 | direct_recall | direct gold 命中情况 |
|---|---:|---|
| bb37aad8c3 | 100% | OwnerControllerTests ✓ |
| e0db9b184e | 100% | PetControllerTests ✓;Concurrency/ClinicServiceTests 为 co-change |
| 753d35c2f8 | 100% | VisitControllerTests ✓ |
| 142321aa3e | 100% | 3 个 gold 全命中 |
| 40a41375e6 | — | 唯一 gold 是 co-change(无 direct) |
| 1cad4124b7 | 50% | OwnerControllerTests ✓;ClinicServiceTests 引用 OwnerRepository 但只调 `findById`/`findPetTypes`,改动的是 `findByLastName`/`findAll`——符号级不相关 |
| 50866def72 | 100% | PetControllerTests ✓ |

**结论**:resolver 在「真正引用改动代码」的 gold 上 Recall@All 达 **94.44%**——几乎到顶。原始 78.33% 与 94.44% 的差距主要来自 3 个 co-change 噪声 gold + `1cad4124b7` 的 ClinicServiceTests(文本引用改动类但符号级不相关)。剩余可提升空间集中在「文本引用 → 符号级调用」的语义缺口,而非图覆盖。
