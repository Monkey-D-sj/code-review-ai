# Spring PetClinic Recall@All 偏低原因分析

日期：2026-08-06  
数据集：`benchmarks/spring-petclinic-history-10.json`  
运行结果：`benchmark-results/spring-petclinic-history-10.json`

## 结论

Spring PetClinic 的 Test Recall@All 为 **23.33%**，主要原因不是召回数量
上限，而是 Java/Spring 调用图在进入排序之前已经断开。

> **改进后（2026-08-06）**：接入 MockMvc 路由边（`docs/superpowers/specs/2026-08-06-mockmvc-route-edges-design.md`）后，Test Recall@All 从 **23.33% → 58.33%**。
> 10 个 case 中 6 个至少命中一个 gold test（原 3 个）：`bb37aad8c3`/`753d35c2f8`/`14af47d4e5` 0%→100%，`1cad4124b7` 0%→50%。
> 剩余 3 个零命中：`40a41375e6`（提交级噪声，OwnerRepository seed ↔ PetValidatorTests gold 无调用关系）、`e0db9b184e`（多 gold 含服务/并发测试，route 边只覆盖 Controller test）、`50866def72`（改动在实体/验证器，MockMvc test 仍不可达）。详见文末「改进后结果」。

10 个历史快照的调用解析统计合计如下：

| 调用边状态 | 数量 | 占比 | 是否参与 flow |
|---|---:|---:|---|
| resolved | 647 | 5.60% | 是 |
| dynamic | 7,554 | 65.36% | 否 |
| unresolved | 3,356 | 29.04% | 否 |

当前 flow 只遍历 `resolution="resolved"` 的边。因此 Recall@All 虽然把
查询上限扩展到全部索引节点，却不会遍历 dynamic/unresolved 边。它衡量的是
“现有已解析图的完整覆盖率”，不是无条件返回仓库内所有文件。

## 结果拆解

- 10 个 case 中只有 3 个 case 召回至少一个 gold test，7 个完全未命中。
- 15 个 gold test 文件中实际命中 3 个，micro recall 为 20%。
- 报告的 23.33% 是先计算每个 case 的 recall，再做 macro average。
- Symbol Found Rate 为 100%，说明生产代码改动范围可以定位到 Java 类和方法；
  问题主要发生在从生产符号走向调用者、测试和关联业务文件的阶段。

| 提交 | Gold 数 | Recall@All | 直接表现 |
|---|---:|---:|---|
| `bb37aad8c3` owner search | 1 | 0% | 只返回 `OwnerController.java` |
| `e0db9b184e` unique pet names | 3 | 0% | 只返回 `PetController.java` |
| `753d35c2f8` future visit dates | 1 | 0% | 返回部分实体及无关测试，漏掉目标测试 |
| `142321aa3e` findPetTypes | 3 | 33.33% | 只命中 `PetTypeFormatterTests.java` |
| `40a41375e6` PetValidator test | 1 | 0% | 生产 seed 与 gold test 缺少直接关系 |
| `1cad4124b7` owner logic refactor | 2 | 0% | Controller/Repository 之间部分可达，测试不可达 |
| `50866def72` pet validation | 1 | 0% | 实体继承可达，MockMvc 测试不可达 |
| `14af47d4e5` owner id mismatch | 1 | 0% | 只到 Controller 和 Owner 实体 |
| `405cdc635b` owner error message | 1 | 100% | 实体引用关系成功连接到 Controller test |
| `4926e29270` validation annotations | 1 | 100% | Validator test 与改动模型成功连通 |

## 具体原因

### 1. Java 接收者没有类型绑定

解析器会提取 `owners.findByLastName(...)`、`ownerRepository.findAll(...)` 这类
属性调用，但 resolver 目前主要能处理：

- 当前类内的简单方法调用；
- 显式 import 的类或静态成员；
- 已知类名/FQCN 的调用；
- 同 package 下可直接确认的类。

它尚未建立局部变量、字段、方法参数到声明类型的符号表。因此接收者是
`owners`、`ownerRepository`、`mockMvc` 或其他实例变量时，无法根据字段声明或
构造器注入推断出具体类，调用会被标记为 dynamic。这是 65.36% dynamic 边的
主要来源。

### 2. Spring 依赖注入关系没有建图

PetClinic 广泛使用构造器注入和 Spring Data Repository。当前图没有把下面的
关系转换为可遍历边：

- Controller 构造器参数类型 -> Repository/Service；
- 注入字段 -> 具体 Bean 类型；
- Repository 接口 -> Spring Data 运行时实现；
- `@Controller`、`@Service`、`@Repository` 之间的 Bean 依赖；
- `@ModelAttribute` 等隐式方法调用。

所以 Controller 调 Repository 的路径容易在实例接收者处断开。

### 3. MockMvc 测试不会直接调用 Controller 方法

Controller 测试通常调用 `mockMvc.perform(get(...))`，再由 Spring MVC 根据
`@GetMapping`、`@PostMapping` 等注解在运行时路由到 Controller。源码中不存在
`OwnerController.processFindForm()` 这种直接调用。

当前 parser 虽然能解析 Java 方法和普通调用，但没有建立：

`MockMvc request -> HTTP route -> Controller method`

这条框架语义边。因此 Controller 改动常常只能召回 Controller 自身，无法到达
对应的 `*ControllerTests.java`。前 8 个 case 中的大多数 0% 都受此影响。

### 4. JUnit/Spring 测试入口识别不足

flow 的入口判断基于配置的名称模式或“没有 resolved 入边的 method”。当前未将
`@Test`、`@ParameterizedTest`、Spring 测试注解作为明确入口，也没有消费 Java
测试注解来增强测试到业务代码的反向关系。

即使测试方法被当作图的根节点，只要其 `mockMvc`、Mockito 或注入对象调用仍是
dynamic，从测试到生产代码的 flow 也会在第一段断开。

### 5. 外部框架和继承方法产生 unresolved 边

29.04% 的调用为 unresolved，典型来源包括 Spring、JUnit、Mockito、Jakarta
Validation 和 Java 标准库。外部库本身没有被项目索引；此外，继承方法、默认
接口方法和泛型接口调用也没有完整的层级分派解析。

外部调用不一定都需要解析，但当它承担框架路由或回调语义时，直接丢弃会切断
测试与业务代码之间的桥梁。

### 6. Gold 文件是“同 commit 修改”，不一定是静态调用影响

本 benchmark 把真实提交中修改的测试文件当作 gold。这是可复现的客观代理，
但不等价于“测试文件必须静态调用每个生产 seed”。例如 `40a41375e6` 的生产
改动是 `OwnerRepository.java`，gold 却是 `PetValidatorTests.java`，两者没有
明显直接调用链。这种 case 即使静态解析完全正确，也不一定能从调用图召回。

多 gold case 也会放大这个问题：`e0db9b184e` 同时修改并发测试、Controller
测试和 Service 测试，从一个 Controller seed 要求召回全部三者，包含了调用图
之外的提交级关联。

## 为什么 Recall@All 和 Recall@10 相同

该 Java 子集的 Recall@10 与 Recall@All 都是 23.33%。原因是成功连通的候选集合
本身很小，平均只有 4.7 个文件；相关测试一旦进入已解析图，通常已经在前 10。
未命中的测试则根本不在可达图中。扩大结果窗口无法修复图上的断边。

因此当前瓶颈排序为：

1. Java 字段、参数、局部变量的类型推断；
2. MockMvc 路由与 Spring 注解语义边；
3. 构造器注入和 Bean 依赖边；
4. 接口、继承、泛型及动态分派；
5. benchmark gold 中非调用图关联的噪声；
6. Top-K 排序。

## 建议的改进顺序

### P0：Java 类型绑定

解析字段、参数、局部变量和构造器参数的声明类型，将
`receiver.method()` 绑定到 `DeclaredType.method()`。优先覆盖 `this.field`、字段
简称和构造器注入字段。完成后 dynamic 占比应显著下降。

### P0：Spring MVC 测试路由边

索引 Controller 的 mapping 注解以及 MockMvc 请求的 method/path，建立测试方法
到 Controller method 的 synthetic resolved edge。这会直接改善 Controller 类
case 的 Test Recall@All。

### P1：Spring Bean/Repository 语义

建立构造器参数、注入字段、接口实现和 Spring Data Repository 的依赖边；对
无法定位运行时实现的 Repository 接口，至少保留到接口方法的 resolved edge。

### P1：测试入口与注解

把 JUnit `@Test` 等注解识别为 entry point，并使用测试类字段类型、Mockito
`@Mock`/`@InjectMocks`、Spring `@Autowired` 信息辅助解析。

### P2：拆分评测口径

保留真实 commit gold，同时增加两组指标：

- **Direct-call gold**：人工或自动验证测试与 seed 存在直接/框架调用关系；
- **Commit co-change gold**：保留当前口径，衡量更宽泛的提交级关联。

这样可以区分“解析器确实漏边”和“真实提交包含非调用关系文件”，避免把两种
失败都归因于 Java resolver。

## 验收建议

每次改进后固定重跑这 10 条 case，并同时观察：

- Test Recall@All；
- resolved-call rate；
- dynamic/unresolved 的绝对数量和占比；
- 7 个零命中 case 中有多少变为非零；
- Recall@10 是否开始低于 Recall@All。

若 Recall@All 上升而 Recall@10 没有同步上升，才说明主要瓶颈已经从图覆盖转移到
排序和上下文预算。当前阶段优先优化排序不会解决 Java 子集的主要问题。

## 改进后结果（2026-08-06）

实现 MockMvc 路由边后重跑 `spring-petclinic-history-10`（`.venv/Scripts/python.exe scripts/run_swebench_suite.py`）：

| 指标 | 基线 | 改进后 |
|---|---:|---:|
| macro_test_file_recall_all | 23.33% | **58.33%** |
| macro_test_file_recall_at_k | 23.33% | 58.33% |
| symbol_found_rate | 100% | 100% |

| 提交 | Gold | 基线 recall@all | 改进后 | 说明 |
|---|---:|---:|---:|---|
| bb37aad8c3 | 1 | 0% | **100%** | OwnerControllerTests 经 `/owners?page=1` → `@GetMapping("/owners")` 连通 |
| e0db9b184e | 3 | 0% | 0% | 多 gold（Controller/Service/并发测试），route 边只覆盖 Controller test |
| 753d35c2f8 | 1 | 0% | **100%** | VisitControllerTests 经 visit 路由连通 |
| 142321aa3e | 3 | 33% | 33% | 不变（PetTypeFormatterTests 命中原路径） |
| 40a41375e6 | 1 | 0% | 0% | 提交级噪声：OwnerRepository seed ↔ PetValidatorTests 无调用关系 |
| 1cad4124b7 | 2 | 0% | **50%** | OwnerControllerTests 命中；另一 gold（Repository 直连）仍不可达 |
| 50866def72 | 1 | 0% | 0% | 改动在实体/验证器，MockMvc test 未达 |
| 14af47d4e5 | 1 | 0% | **100%** | OwnerControllerTests 经 owner 路由连通 |
| 405cdc635b | 1 | 100% | 100% | 保持 |
| 4926e29270 | 1 | 100% | 100% | 保持 |

**下一步**（按剩余零命中归因）：
- `e0db9b184e` / `1cad4124b7` 的未命中部分：Controller 之外的 Service/Repository gold → Java 类型绑定（P1）把 `owners.findByLastName()` 之类接收者解析到声明类型；
- `50866def72`：改动符号在实体/验证器，与 Controller test 无路由关系 → 需要测试入口/字段类型辅助；
- `40a41375e6`：静态图无法召回，建议拆分「Direct-call gold」与「Commit co-change gold」口径（P2）。
