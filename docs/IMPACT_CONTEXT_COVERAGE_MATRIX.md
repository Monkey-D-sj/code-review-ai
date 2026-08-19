# Impact 上下文召回覆盖矩阵

> 目标：给定一个发生变化的代码节点，找回评审该改动所需的直接代码上下文。
>
> 覆盖语言：Python、TypeScript/JavaScript、Java。
>
> 本文是目标覆盖目录，不表示当前实现已经支持全部条目。P0 只承诺可从源码语法、作用域和模块规则稳定得出的直接关系；框架、配置和运行时关联留在 P2，作为明确记录的后续增强，而不是当前能力承诺。后续每一项都应映射到自动化测试、Impact Benchmark case，或明确的“不支持/降级”契约。
>
> 当前发布所需的 P0 范围、跨语言公共契约、fixture 规范与 100% 门槛见 [Impact P0 直接代码上下文覆盖规范](IMPACT_CONTEXT_P0_COVERAGE.md)。本矩阵保留完整目录和 P1/P2 扩展项。

## 1. 评测对象

Impact 查询的输入是一个或多个 changed symbols，输出上下文分为五类：

| 上下文类型 | 定义 | 示例 |
|---|---|---|
| `caller` | 直接或传递调用 changed symbol 的节点 | `Controller` 调用 `Service` |
| `callee` | changed symbol 为理解行为必须读取的下游节点 | Service 调用 Repository |
| `reference` | 直接读取、写入、声明或导入 changed symbol 的节点 | 常量读取、字段访问、类型注解、import |
| `entry` | 可从外部触发、且能到达 changed symbol 的业务入口 | HTTP route、CLI command、consumer |
| `test` | 直接或间接覆盖 changed symbol 的测试节点/文件 | unit test、MockMvc test |
| `semantic` | 没有显式调用，但存在确定或高置信业务关系的节点 | DI provider、route binding、event handler |

不把“同一提交中碰巧修改的文件”直接等同于必要上下文。P0 的 strong gold 是人工确认的调用、直接引用和模块关系；框架关系与提交共变只能作为 P2 的弱 gold 或候选。

## 2. 可分析性分级

| 级别 | 含义 | 系统应有行为 |
|---|---|---|
| A：确定静态关系 | 从语法、作用域和模块规则可唯一确定 | 生成 `resolved` 边，参与 flow/impact/testimpact |
| B：类型/配置关系 | 读取类型声明、构建配置或模块配置后可确定 | 生成带证据的 `resolved` 边 |
| C：框架语义关系 | 需要理解注解、装饰器、注册 API 或框架约定 | 生成标明来源的 synthetic/semantic 边 |
| D：保守候选关系 | 存在多个合理目标，无法唯一确定 | 返回候选集、置信度和原因，不伪装成唯一 resolved 边 |
| E：运行时不可知 | 目标取决于外部数据、反射、动态修改或运行时容器 | 标记 dynamic/unresolved，并提示人工检查或结合运行时 trace |

评测必须包含正例和负例：不仅验证“应该召回的能召回”，还要验证“不确定关系不会被错误包装成确定关系”。

### 2.1 交付优先级

| 优先级 | 当前承诺 | 范围 |
|---|---|---|
| P0 | 必做 | 变更定位、显式调用、直接 symbol reference、import/re-export、可唯一确定的类/继承/构造关系，以及无法解析时的诚实降级 |
| P1 | 后续 | 类型辅助 receiver 绑定、override/接口分派候选、直接测试调用与 test-impact 排序 |
| P2 | 不阻塞当前发布 | 框架约定、DI、路由、事件/队列、回调数据流、前端模板、测试框架生命周期、外部配置和运行时行为 |

P0 的目标不是构建完整程序分析器，而是让评审者看到“这段源码直接调用或直接引用了谁、又被谁直接调用或直接引用”。各语言的定义/作用域/模块解析表中，可由语法唯一确定的项默认属于 P0；需要类型推断或存在多个合理目标的项按 P1 或 D/E 降级。

P2 典型例子包括：Spring `@Autowired`、FastAPI 路由装饰器、React JSX 父子组件、`EventEmitter`/Kafka topic、pytest fixture、`Promise.then`、反射和 `getattr(obj, name)`。它们很有价值，但不应以猜测结果污染 P0 的直接关系。

## 3. 三语言公共覆盖

### 3.1 节点与变更定位（P0）

| ID | 情况 | 应有行为 | 分级 |
|---|---|---|---|
| COM-N01 | 修改函数/方法体 | 定位到所属函数/方法 | A |
| COM-N02 | 修改函数/方法签名 | 定位节点，并召回全部调用方 | A |
| COM-N03 | 新增函数/方法 | 建立新节点与新调用边 | A |
| COM-N04 | 删除函数/方法 | 从旧索引/tombstone 恢复原节点及上游 | A |
| COM-N05 | 删除整个文件 | 恢复文件内被删节点及旧调用方 | A |
| COM-N06 | 修改类声明 | 定位类，同时保留受影响成员范围 | A/B |
| COM-N07 | 修改继承/实现列表 | 召回父类、接口、子类和实现类 | B/D |
| COM-N08 | 修改 import/package/module 声明 | 召回因此改变绑定的调用点 | B |
| COM-N09 | 修改装饰器/注解 | 重新计算入口、DI、route、test 等语义边 | B/C |
| COM-N10 | 修改模块级初始化代码 | 以 module 节点表示，召回 importers/entries | A/B |
| COM-N11 | 一个 hunk 跨多个节点 | 返回全部覆盖节点，并报告未覆盖行 | A |
| COM-N12 | 文件重命名/移动 | 尽量关联旧新节点；无法关联时按删除+新增 | B/D |
| COM-N13 | 仅注释/格式变化 | 不制造行为 Impact；可标记低风险 | A |
| COM-N14 | 生成文件、vendored、依赖目录 | 按配置排除并报告排除原因 | A |
| COM-N15 | 不支持的语言/二进制文件 | 进入 uncovered changes，不静默忽略 | A |

`COM-N09` 中“定位装饰器/注解变更”属于 P0；由此重新计算 route、DI、test 等框架语义边属于 P2。其余条目均为 P0。

### 3.2 显式调用图（P0）

| ID | 情况 | 应召回关系 | 分级 |
|---|---|---|---|
| COM-C01 | 同文件直接调用 | caller → changed | A |
| COM-C02 | 跨文件直接调用 | importer/caller → changed | A/B |
| COM-C03 | 三级以上传递调用 | entry → ... → changed | A/B |
| COM-C04 | 多个直接调用方 | 全部 caller，直接调用方优先 | A |
| COM-C05 | 多个业务入口 | 全部可达 entry | A/C |
| COM-C06 | 一个 caller 调用多个 changed symbols | 合并结果并保留 covers | A |
| COM-C07 | 菱形图 | 去重，不能路径爆炸 | A |
| COM-C08 | 调用环 | 有界遍历，不死循环 | A |
| COM-C09 | 直接递归/互递归 | 返回环证据且不重复 | A |
| COM-C10 | 同名符号位于不同模块/类 | 只连接作用域与模块匹配目标 | A/B |
| COM-C11 | 无调用方节点 | 不伪造 upstream；按入口规则判断 | A/C |
| COM-C12 | 调用外部库/builtin | 保留 external/unresolved，不跨仓库伪解析 | E |
| COM-C13 | 无法解析的接收者调用 | 保留 dynamic 边及原始表达式 | D/E |
| COM-C14 | 一次表达式产生多个候选目标 | 返回候选及依据，不任选一个 | D |
| COM-C15 | 条件分支中的调用 | 静态上保留所有可达分支调用 | A |
| COM-C16 | 异常/early return 后的不可达调用 | 基础模式可保守保留；CFG 模式应区分不可达 | A/D |

`COM-C05` 的调用图可达性属于 P0；将函数识别为 HTTP、CLI 或 consumer 等“业务入口”需要框架或配置建模时属于 P2。`COM-C11` 同理：P0 只要求不伪造 upstream。

### 3.3 模块、继承和类型关系（P0/P1）

| ID | 情况 | 应召回关系 | 分级 |
|---|---|---|---|
| COM-M01 | 普通 import | module/importer 与 imported symbol | A/B |
| COM-M02 | alias import | alias 正确绑定真实 symbol | A/B |
| COM-M03 | re-export/barrel/package forwarding | 沿转发链找到真实定义 | B |
| COM-M04 | wildcard import | 唯一可判定时解析，否则给候选 | D |
| COM-M05 | 跨包/跨模块继承 | class → base/interface | B |
| COM-M06 | 方法覆盖 override | base method ↔ overriding methods | B/D |
| COM-M07 | 接口/抽象方法分派 | 返回所有可行实现或基于类型缩小 | D |
| COM-M08 | 泛型/模板实例化 | 在类型信息充分时绑定，擦除后给候选 | B/D |
| COM-M09 | 多重继承/多接口 | 保留全部关系和语言特定解析顺序 | B/D |
| COM-M10 | 外部依赖中的父类/接口 | 保留 external type，不能假装仓库内闭合 | E |

P0：`COM-M01`–`COM-M05` 和 `COM-M10`，即直接 import/re-export、唯一可判定的模块绑定、源码可见的继承关系及外部边界。P1：`COM-M06`–`COM-M09`，即 override、接口分派、泛型和多继承等需要类型辅助或候选集的关系。

### 3.4 直接符号引用（P0）

调用图不足以解释常量、字段和类型声明的改动。本节只记录源码中可直接观察到的 read/write/type/import/instantiation 关系；不追踪运行时数据流。

| ID | 情况 | 应召回关系 | 分级 |
|---|---|---|---|
| COM-R01 | 模块/全局常量或变量被读取 | reader → changed value | A/B |
| COM-R02 | 模块/全局变量被写入 | writer → changed value | A/B |
| COM-R03 | 类字段/属性被读取或写入 | member access → changed field/property | A/B |
| COM-R04 | enum 成员/常量对象成员被引用 | use site → changed member | A/B |
| COM-R05 | 类、interface、type alias、DTO/schema 字段被声明或用作类型 | type use → changed type/member | A/B |
| COM-R06 | 类/接口被 extends、implements 或实例化 | subtype/implementation/constructor site → changed type | A/B |
| COM-R07 | exported symbol 被 import、alias 或 re-export | importer/forwarder → changed export | A/B |
| COM-R08 | 动态属性名、反射或运行时数据决定引用目标 | 保留 candidate/dynamic 与原始表达式，不伪造引用边 | D/E |

### 3.5 回调、异步和控制反转（P2）

| ID | 情况 | 应召回关系 | 分级 |
|---|---|---|---|
| COM-I01 | 函数/方法作为参数传入已知 API | registration/caller → callback | B/C |
| COM-I02 | 匿名函数/lambda 回调 | 建立可定位匿名节点或归属到外层节点 | A/B |
| COM-I03 | promise/future/task continuation | producer → callback/continuation | B/C |
| COM-I04 | 事件注册与触发 | emitter/topic → handlers | C/D |
| COM-I05 | 消息队列 producer/consumer | 根据确定 topic/queue 建语义边 | C/D |
| COM-I06 | 定时任务/cron/scheduler | handler 作为 entry | C |
| COM-I07 | hook/plugin 注册 | 注册点 → plugin/hook 实现 | C/D |
| COM-I08 | 依赖注入 provider → consumer | provider/type/token → injection point | B/C/D |
| COM-I09 | 路由声明 → handler | route 作为 entry 并连接 handler | C |
| COM-I10 | 测试框架收集 → test | test 节点/文件正确标记 | C |

### 3.6 Test Impact 公共契约（P1/P2）

| ID | 情况 | 应有行为 | 分级 |
|---|---|---|---|
| COM-T01 | 测试直接调用 changed symbol | 召回该测试 | A |
| COM-T02 | 测试经业务层传递到 changed symbol | 召回该测试及证据路径 | A/B |
| COM-T03 | 一个测试覆盖多个 changed symbols | 合并 covers | A |
| COM-T04 | 多个测试覆盖同一 changed symbol | 召回全部相关测试 | A/C |
| COM-T05 | changed symbol 无静态测试覆盖 | 返回空并触发安全回退策略 | A/E |
| COM-T06 | changed symbol 不存在 | 返回 not_found，不误选测试 | A |
| COM-T07 | 测试文件存在但测试函数匿名/动态生成 | 至少召回测试文件；节点级可降级 | C/D |
| COM-T08 | 参数化/动态测试 | 以测试定义或文件为稳定单位 | C/D |
| COM-T09 | fixture/setup 间接覆盖 | fixture/setup → changed，召回消费它的测试 | C |
| COM-T10 | framework integration test | 通过 route/DI/事件等语义边召回 | C |
| COM-T11 | 测试只引用类型/常量而不执行行为 | 不应当作确定行为覆盖；可作弱候选 | D |
| COM-T12 | 查询失败、索引过期、unresolved 比例过高 | 明确降级到全量测试 | A |

## 4. Python 覆盖目录

### 4.1 定义、作用域和调用语法

| ID | Python 情况 | 期望上下文 | 分级 |
|---|---|---|---|
| PY-S01 | 顶层 `def` | 普通函数节点及调用关系 | A |
| PY-S02 | `async def` / `await f()` | async 函数及被等待调用 | A |
| PY-S03 | 类、实例方法 | class contains method；`self.m()` 绑定本类方法 | A/B |
| PY-S04 | `@classmethod` / `cls.m()` | 绑定本类方法 | A/B |
| PY-S05 | `@staticmethod` / `C.m()` | 绑定类静态方法 | A/B |
| PY-S06 | nested function/closure | 保留词法作用域；外层与内层调用关系 | A/B |
| PY-S07 | lambda 赋值 | 建立稳定匿名/变量函数节点 | A/B |
| PY-S08 | callable object `obj()` | 类型可知时连 `Class.__call__` | B/D |
| PY-S09 | 构造 `C()` | caller → class/`C.__init__`/`C.__new__` | B/D |
| PY-S10 | `super().m()` | 按 MRO 连接基类方法；歧义时给候选 | B/D |
| PY-S11 | property getter/setter/deleter | 属性访问与对应 descriptor 方法 | C/D |
| PY-S12 | magic method：迭代、比较、运算符 | 语法操作与 `__iter__`/`__eq__` 等方法 | C/D |
| PY-S13 | context manager `with` / `async with` | 连接 `__enter__`/`__exit__` 或异步对应方法 | C/D |
| PY-S14 | iterator/generator/yield | consumer 与 generator；不虚构运行次数 | B/D |
| PY-S15 | comprehension 内调用 | 归属外层函数并保留调用 | A |
| PY-S16 | decorator definition/application | decorated function ↔ decorator；wrapper 关系 | B/C |
| PY-S17 | 默认参数/annotation 中调用 | 区分定义时执行与调用时行为 | B |
| PY-S18 | `functools.partial` | 可确定函数目标与预绑定参数 | C/D |
| PY-S19 | `functools.singledispatch` | base function ↔ registered implementations | C/D |
| PY-S20 | 同名 nested/class/module 函数 | 遵守 LEGB/类作用域，不串边 | A/B |
| PY-S21 | `async for` | 记录迭代对象与循环体；异步迭代协议无法唯一解析时降级 | C/D |

### 4.2 Import、包和类型

| ID | Python 情况 | 期望上下文 | 分级 |
|---|---|---|---|
| PY-M01 | `import a` | `a.fn()` 绑定模块成员 | A/B |
| PY-M02 | `import a.b` | `a.b.fn()` 绑定子模块成员 | B |
| PY-M03 | `import a as x` | alias 绑定真实模块 | A/B |
| PY-M04 | `from a import f` | 裸 `f()` 绑定真实定义 | A/B |
| PY-M05 | `from a import f as g` | alias 绑定真实定义 | A/B |
| PY-M06 | 相对 import：`.x`/`..x` | 基于当前 package 归一 | B |
| PY-M07 | `__init__.py` re-export | 沿包转发找到定义 | B |
| PY-M08 | `from a import *` | 结合 `__all__` 时解析，否则给候选 | B/D |
| PY-M09 | src layout/namespace package | 正确推导 module qname | B |
| PY-M10 | `.pyi` stub/type-only import | 用于类型解析，不当作运行时调用 | B |
| PY-M11 | ABC/Protocol | 接口方法与实现候选 | D |
| PY-M12 | 类型注解 receiver | 利用参数/字段/局部变量注解缩小方法目标 | B/D |
| PY-M13 | union/generic/type alias | 展开为候选类型集合 | B/D |
| PY-M14 | 返回值链 `factory().run()` | 利用返回类型绑定 `run` | B/D |

### 4.3 Python 框架语义（P2）

| ID | 情况 | 应召回关系 | 分级 |
|---|---|---|---|
| PY-F01 | Flask `@app.route` | route entry → handler | C |
| PY-F02 | Flask Blueprint `@bp.route` / register | blueprint prefix + route → handler | C |
| PY-F03 | FastAPI `@app/router.get/post/...` | HTTP entry → handler | C |
| PY-F04 | FastAPI `include_router` + prefix | 组合完整路由并连接 handler | C |
| PY-F05 | FastAPI `Depends(provider)` | handler/consumer → provider；递归 DI | C |
| PY-F06 | FastAPI middleware/exception handler/lifespan | 注册点 → handler，handler 作为入口 | C |
| PY-F07 | Django URLConf | URL pattern → view | C |
| PY-F08 | Django class-based view | `as_view` → dispatch/HTTP method handlers | C/D |
| PY-F09 | Django signal | sender/signal → receiver | C/D |
| PY-F10 | Django ORM model manager/queryset | 类型明确时绑定自定义方法；动态生成保守处理 | C/D |
| PY-F11 | Celery `@task`/`shared_task` | task 作为 entry；`.delay/.apply_async` → task | C/D |
| PY-F12 | Celery 字符串 task 名 | 常量字符串可映射；运行时字符串降级 | C/E |
| PY-F13 | Click/Typer command | CLI entry → command handler | C |
| PY-F14 | argparse `set_defaults(func=...)` | CLI subcommand → callback | C |
| PY-F15 | asyncio `create_task/gather` | caller → coroutine | B/C |
| PY-F16 | logging/plugin handler 配置 | 静态配置可映射；外部配置降级 | C/E |
| PY-F17 | SQLAlchemy event/listener | event target → listener | C/D |
| PY-F18 | Pydantic validator/serializer | model lifecycle → validator/serializer | C |

### 4.4 Python 测试生态（P2）

| ID | 情况 | 应召回关系 | 分级 |
|---|---|---|---|
| PY-T01 | pytest `test_*` | 测试节点/文件识别 | C |
| PY-T02 | pytest fixture 参数注入 | test → fixture → production | C |
| PY-T03 | fixture 依赖 fixture | 递归 fixture 图 | C |
| PY-T04 | `autouse=True` fixture | 作用域内 tests → fixture | C/D |
| PY-T05 | `conftest.py` fixture/plugin | 跨目录作用域绑定 | C/D |
| PY-T06 | `@pytest.mark.parametrize` | 参数化实例归一到稳定测试定义 | C |
| PY-T07 | pytest hook `pytest_*` | hook 作为框架入口/测试上下文 | C |
| PY-T08 | unittest `TestCase.test_*` | 测试方法识别 | C |
| PY-T09 | `setUp/tearDown/setUpClass` | 生命周期方法 → tests | C |
| PY-T10 | mock/patch target string | 常量 qname 可映射为弱语义关系 | C/D |

### 4.5 Python 动态边界（P2：只要求诚实降级）

以下情况必须进入负向/降级测试，目标不是强行 resolved：

| ID | 情况 | 正确降级行为 | 分级 |
|---|---|---|---|
| PY-D01 | `getattr/setattr` 动态成员 | 常量名给候选；变量名 dynamic | D/E |
| PY-D02 | `importlib.import_module`/`__import__` | 常量模块名可解析；动态字符串 unresolved | C/E |
| PY-D03 | `eval/exec` | 标记运行时代码边界 | E |
| PY-D04 | monkey patch | 标记定义/赋值变化；目标行为不可可靠确定 | E |
| PY-D05 | metaclass 动态生成成员 | 标记动态类型边界 | E |
| PY-D06 | descriptor/`__getattr__`/`__getattribute__` | 返回候选或 dynamic，不伪解析 | D/E |
| PY-D07 | entry-point/plugin metadata | 配置可读时连接；环境插件未知时 external | C/E |
| PY-D08 | pickle/import path/字符串 qname | 常量可作为候选，外部数据不可知 | D/E |

## 5. TypeScript / JavaScript 覆盖目录

TypeScript 和 JavaScript共享语法/模块生态，但必须分别有真实 `.ts/.tsx` 与 `.js/.jsx/.mjs/.cjs` case，不能用 TypeScript 用例替代 JavaScript 验证。

### 5.1 定义、类和调用语法

| ID | TS/JS 情况 | 期望上下文 | 分级 |
|---|---|---|---|
| JS-S01 | function declaration | 普通函数及调用关系 | A |
| JS-S02 | async function/`await` | async caller → callee | A |
| JS-S03 | generator function | generator 定义与调用 | A/B |
| JS-S04 | arrow function 变量 | 变量名对应函数节点 | A |
| JS-S05 | function expression 变量 | 变量名对应函数节点 | A |
| JS-S06 | anonymous callback | 建匿名节点或归属调用点 | A/B |
| JS-S07 | nested function/closure | 保留词法作用域 | A/B |
| JS-S08 | object literal method | 对象成员函数节点 | A/B |
| JS-S09 | object property arrow/function | 属性对应函数节点 | A/B |
| JS-S10 | class method | class contains method | A |
| JS-S11 | constructor/`new C()` | caller → constructor/class | B |
| JS-S12 | `this.m()` | 绑定当前类/对象方法 | B/D |
| JS-S13 | `super.m()`/`super()` | 绑定父类方法/构造器 | B/D |
| JS-S14 | static method/field | `C.m()` 正确绑定 | B |
| JS-S15 | class field arrow | 字段对应可调用节点 | A/B |
| JS-S16 | private `#method/#field` | 类内绑定，不泄漏到其他类 | B |
| JS-S17 | getter/setter | 属性访问与 accessor | C/D |
| JS-S18 | optional call/chaining `obj?.m?.()` | 保留条件调用关系 | A/D |
| JS-S19 | computed property call `obj[name]()` | 常量名可解析，变量名 dynamic | D/E |
| JS-S20 | tagged template | tag function 调用 | A/B |
| JS-S21 | IIFE | 匿名函数及立即调用 | A/B |
| JS-S22 | decorator application | decorated target ↔ decorator | B/C |
| JS-S23 | top-level call | module 节点作为 caller | A |
| JS-S24 | 同名重载/声明合并 | 绑定实现签名，声明节点作为类型上下文 | B/D |
| JS-S25 | class static block | class 初始化节点中的直接调用/引用归属该类 | A/B |

### 5.2 ESM、CommonJS 和工程模块解析

| ID | TS/JS 情况 | 期望上下文 | 分级 |
|---|---|---|---|
| JS-M01 | ESM named import | 绑定真实 export | A/B |
| JS-M02 | ESM default import/export | 绑定 default 定义 | A/B |
| JS-M03 | namespace import | `ns.f()` 绑定成员 | B |
| JS-M04 | side-effect import | importer → module initialization | A/B |
| JS-M05 | `export {x}` | export symbol关联本地定义 | A/B |
| JS-M06 | `export {x} from` | re-export 到真实定义 | B |
| JS-M07 | `export * from` | barrel 转发；冲突时给候选 | B/D |
| JS-M08 | 相对路径 `./`/`../` | 基于当前文件归一模块 | B |
| JS-M09 | 省略扩展名 | 按 Node/TS 规则解析 `.ts/.tsx/.js/.jsx` | B |
| JS-M10 | 目录 `index` | `./foo` → `foo/index.*` | B |
| JS-M11 | package.json `main/module/types` | 按环境/目标解析入口 | B/D |
| JS-M12 | package `exports/imports` | 条件 export 按配置返回目标/候选 | B/D |
| JS-M13 | tsconfig `baseUrl` | 非相对 import 归一 | B |
| JS-M14 | tsconfig `paths` 多 target | 按匹配顺序解析，保留候选 | B/D |
| JS-M15 | tsconfig `extends` | 合并继承配置 | B |
| JS-M16 | project references | 跨 TS project 解析 | B |
| JS-M17 | monorepo/workspace package | workspace package → 源码入口 | B |
| JS-M18 | CommonJS `require()` | 模块及成员绑定 | B |
| JS-M19 | `module.exports`/`exports.x` | 建立 CommonJS exports | B |
| JS-M20 | ESM/CJS interop | 按运行模式返回确定目标或候选 | B/D |
| JS-M21 | dynamic `import()` 常量 | 常量路径可解析 | C |
| JS-M22 | bundler alias/Vite/Webpack/Jest mapper | 读取配置后归一 | B/C |
| JS-M23 | `.d.ts`/type-only import | 用于类型关系，不生成运行时调用 | B |
| JS-M24 | TypeScript `import x = require()` / `export =` | 按 CommonJS 绑定规则连接真实 module/export | B |

### 5.3 TypeScript 类型辅助解析

| ID | TypeScript 情况 | 期望上下文 | 分级 |
|---|---|---|---|
| TS-Y01 | 参数显式类型 | `receiver.m()` 绑定声明类型 | B |
| TS-Y02 | 字段/局部变量类型 | receiver 绑定类型成员 | B |
| TS-Y03 | 构造赋值推断 | `const x = new C()` → C | B |
| TS-Y04 | 返回类型链 | `factory().m()` 通过返回类型绑定 | B/D |
| TS-Y05 | interface method | interface ↔ implementations 候选 | D |
| TS-Y06 | abstract class method | abstract declaration ↔ overrides | B/D |
| TS-Y07 | union/intersection | 返回所有可行成员目标 | D |
| TS-Y08 | generic type parameter | 约束充分时缩小，否则候选 | B/D |
| TS-Y09 | type alias | 展开别名后解析 | B |
| TS-Y10 | overload signatures | 调用绑定实现；可按参数缩小 overload | B/D |
| TS-Y11 | structural typing | 返回结构匹配候选时必须标低置信度 | D |
| TS-Y12 | enum/namespace declaration merge | 正确处理值空间与类型空间 | B/D |

### 5.4 异步、回调和事件（P2）

| ID | TS/JS 情况 | 应召回关系 | 分级 |
|---|---|---|---|
| JS-I01 | `.then/.catch/.finally` | Promise → callback | B/C |
| JS-I02 | `setTimeout/setInterval` | scheduler → callback | C |
| JS-I03 | EventEmitter `on/once/emit` | 常量 event → handlers | C/D |
| JS-I04 | DOM `addEventListener` | event target/type → handler | C/D |
| JS-I05 | array HOF：map/filter/reduce | caller → callback | B/C |
| JS-I06 | callback 通过变量传递 | 数据流可追踪时连接，否则候选 | D |
| JS-I07 | async queue/job processor | queue name → processor | C/D |
| JS-I08 | worker/child process | 静态入口文件可连接；动态路径降级 | C/E |
| JS-I09 | RxJS pipeline | operator/callback 链与 subscriber | C/D |

### 5.5 前端与 Node 框架语义（P2）

| ID | 情况 | 应召回关系 | 分级 |
|---|---|---|---|
| JS-F01 | Express/Koa route | HTTP route → handler/middleware chain | C |
| JS-F02 | NestJS Controller route | decorator route → method entry | C |
| JS-F03 | NestJS constructor/token DI | controller/service → provider | C/D |
| JS-F04 | NestJS module imports/providers | module wiring → provider/controller | C |
| JS-F05 | React function/class component | component 节点及 render/use 关系 | B/C |
| JS-F06 | JSX component `<Child/>` | Parent → Child component | C |
| JS-F07 | JSX event `onClick={handler}` | component/event → handler | C |
| JS-F08 | React hook callback/dependency | component → hook callback；dependency 作为弱关系 | C/D |
| JS-F09 | Next.js pages/app routes | 文件约定 → route entry | C |
| JS-F10 | Next.js server action/API handler | framework entry → handler | C |
| JS-F11 | Vue `<script setup>` | script 定义/调用进入模块图 | A/B |
| JS-F12 | Vue template component | parent template → child component | C |
| JS-F13 | Vue template event | template event → script handler | C |
| JS-F14 | Vue computed/watch/ref | reactive dependency和callback | C/D |
| JS-F15 | Angular component/service/DI | decorator metadata → component/provider | C |
| JS-F16 | Angular template event/binding | template → handler/property | C/D |
| JS-F17 | Redux action/reducer | action type → reducers/listeners | C/D |
| JS-F18 | GraphQL resolver | schema field → resolver entry | C/D |

### 5.6 TS/JS 测试生态（P2）

| ID | 情况 | 应召回关系 | 分级 |
|---|---|---|---|
| JS-T01 | Jest/Vitest `test/it` callback | 测试节点或至少测试文件 → production | C |
| JS-T02 | `describe` 嵌套 | 保留 suite/test 层级 | C |
| JS-T03 | `beforeEach/afterEach/beforeAll/afterAll` | 生命周期 callback → 作用域内 tests | C/D |
| JS-T04 | `test.each`/参数化测试 | 归一到稳定测试定义 | C/D |
| JS-T05 | Jest mock module/factory | mock target 作为弱语义关系 | C/D |
| JS-T06 | spies/mocked methods | 常量属性目标可候选，不当作真实执行边 | D |
| JS-T07 | React Testing Library | render component → component；event → handler | C/D |
| JS-T08 | Supertest HTTP 测试 | method/path → Express/Nest route | C |
| JS-T09 | Playwright/Cypress | 静态 URL/selector 可作弱 route/component gold | C/D/E |
| JS-T10 | test filename/glob variants | `.test/.spec/__tests__` 等正确识别 | C |

### 5.7 TS/JS 动态边界（P2：只要求诚实降级）

| ID | 情况 | 正确降级行为 | 分级 |
|---|---|---|---|
| JS-D01 | `eval/new Function` | 标记运行时代码边界 | E |
| JS-D02 | Proxy 动态成员 | dynamic，不伪解析 | E |
| JS-D03 | computed property/动态字符串 | 常量可候选；变量 dynamic | D/E |
| JS-D04 | prototype monkey patch | 标记可能影响实例调用，不能唯一绑定 | D/E |
| JS-D05 | 动态 `require/import` 路径 | unresolved，并返回原始表达式 | E |
| JS-D06 | runtime module loader/plugin | 配置可读时连接，否则 external/dynamic | C/E |
| JS-D07 | DI string/symbol token 动态绑定 | 常量配置给候选，多 provider 保留歧义 | C/D/E |
| JS-D08 | bundler code generation/macros | 有产物/配置时分析，否则声明边界 | E |

## 6. Java 覆盖目录

### 6.1 类型、成员和调用语法

| ID | Java 情况 | 期望上下文 | 分级 |
|---|---|---|---|
| JAVA-S01 | class/interface/enum/record | 正确类型节点与 contains | A |
| JAVA-S02 | annotation type | 注解定义和使用关系 | A/B |
| JAVA-S03 | method/constructor | 独立成员节点 | A |
| JAVA-S04 | overloaded methods/constructors | qname 含可区分签名；按参数尽量绑定 | B/D |
| JAVA-S05 | bare same-class call | 绑定当前类成员 | A/B |
| JAVA-S06 | `this.m()` | 绑定当前类成员 | B |
| JAVA-S07 | `super.m()`/`super()` | 绑定父类方法/构造器 | B/D |
| JAVA-S08 | static call `C.m()` | 绑定类静态成员 | B |
| JAVA-S09 | static import | 裸调用绑定静态成员 | B |
| JAVA-S10 | `new C()` | caller → class/匹配构造器 | B/D |
| JAVA-S11 | anonymous class | 建匿名类型，连接 overridden callbacks | B/C |
| JAVA-S12 | local/nested/inner class | 保留外层与内部类型作用域 | A/B |
| JAVA-S13 | enum constant class body | 常量特定实现作为候选 | B/D |
| JAVA-S14 | record canonical/compact ctor | 建构造器及 accessor 语义 | B/C |
| JAVA-S15 | field/parameter/local declared type | receiver.method 绑定声明类型 | B |
| JAVA-S16 | `var` local inference | 从初始化表达式推断 receiver 类型 | B/D |
| JAVA-S17 | chained return `factory.create().run()` | 利用返回类型绑定 | B/D |
| JAVA-S18 | array/collection element receiver | 泛型信息充分时绑定 | B/D |
| JAVA-S19 | lambda | 建匿名可调用节点并连接目标函数式接口 | B/D |
| JAVA-S20 | method reference `x::m`/`C::new` | callback → 方法/构造器 | B/C |
| JAVA-S21 | initializer/static initializer | module/class 初始化节点及触发关系 | A/B |
| JAVA-S22 | try-with-resources | 连接 close/AutoCloseable 为语义候选 | C/D |
| JAVA-S23 | field/instance initializer | 初始化块或字段初始化表达式中的直接调用/引用归属类型 | A/B |

### 6.2 Package、module、继承和分派

| ID | Java 情况 | 期望上下文 | 分级 |
|---|---|---|---|
| JAVA-M01 | package 声明 | 正确 FQCN/qname | A |
| JAVA-M02 | 无 package 的路径回退 | 稳定模块名 | B |
| JAVA-M03 | 普通 import | 类型/静态成员绑定 | B |
| JAVA-M04 | wildcard import | 唯一命中时解析，否则候选 | D |
| JAVA-M05 | fully-qualified call/type | 精确绑定 | A/B |
| JAVA-M06 | Maven/Gradle source sets | main/test/generated/sourceSet 模块边界 | B |
| JAVA-M07 | multi-module Maven/Gradle | 模块依赖内跨模块解析 | B |
| JAVA-M08 | JPMS `module-info.java` | requires/exports/uses/provides 关系 | B/C |
| JAVA-M09 | class extends | class → base | B |
| JAVA-M10 | interface extends/implements | 类型关系闭合 | B |
| JAVA-M11 | default interface method | 调用目标与覆盖候选 | B/D |
| JAVA-M12 | abstract method dispatch | 所有可行实现候选 | D |
| JAVA-M13 | runtime polymorphism | 结合 declared/points-to 类型缩小，否则候选 | D |
| JAVA-M14 | sealed class/interface | 用 permits 集合约束候选 | B/D |
| JAVA-M15 | generic type/bridge method | 结合实例类型解析；擦除后保守候选 | B/D |
| JAVA-M16 | external JAR/classpath | 可选读取符号表；不索引源码时标 external | B/E |

### 6.3 回调、并发和标准库语义（P2）

| ID | Java 情况 | 应召回关系 | 分级 |
|---|---|---|---|
| JAVA-I01 | Stream map/filter/forEach | pipeline → lambda/method reference | B/C |
| JAVA-I02 | Optional callbacks | caller → callback | B/C |
| JAVA-I03 | CompletableFuture continuation | future chain → callbacks | B/C |
| JAVA-I04 | Executor/Thread/Runnable | submit/start → run/call | C/D |
| JAVA-I05 | Timer/scheduler | scheduler → task callback | C/D |
| JAVA-I06 | listener registration | source/event → listener methods | C/D |
| JAVA-I07 | ServiceLoader | service interface → providers（配置可读时） | C/D |
| JAVA-I08 | serialization callbacks | framework lifecycle → read/write hooks | C/D |

### 6.4 Spring/Jakarta 语义（P2）

| ID | 情况 | 应召回关系 | 分级 |
|---|---|---|---|
| JAVA-F01 | `@Controller/@RestController` mapping | HTTP route → handler entry | C |
| JAVA-F02 | 类级+方法级 RequestMapping | 合并 path/method/consumes/produces | C |
| JAVA-F03 | path variable/wildcard/query | 路径规范化并保留模板匹配证据 | C/D |
| JAVA-F04 | constructor injection | consumer/constructor → dependency type | B/C |
| JAVA-F05 | field/setter injection | consumer → dependency type/provider | C |
| JAVA-F06 | qualifier/primary/named bean | 缩小具体 provider；冲突时给候选 | C/D |
| JAVA-F07 | `@Bean` method | configuration/provider → produced bean type | C |
| JAVA-F08 | component scan | scan config/package → components | C/D |
| JAVA-F09 | Spring Data Repository proxy | repository interface method作为稳定目标 | C |
| JAVA-F10 | derived query method | 调用绑定接口方法，不虚构运行时实现源码 | C |
| JAVA-F11 | `@Valid`/Validator | handler/model lifecycle → validator | C |
| JAVA-F12 | `@ModelAttribute/@InitBinder` | MVC lifecycle → binder/model method | C |
| JAVA-F13 | `@ControllerAdvice/@ExceptionHandler` | exception/type → handler entry | C/D |
| JAVA-F14 | filter/interceptor/security chain | route/request → applicable filters/interceptors | C/D |
| JAVA-F15 | `@EventListener`/publishEvent | event type → listeners | C/D |
| JAVA-F16 | `@Scheduled` | method作为定时 entry | C |
| JAVA-F17 | `@Async` | caller → async method，保留异步边 | C |
| JAVA-F18 | `@Transactional` | 标记事务边界；不伪造普通调用 | C |
| JAVA-F19 | AOP advice/pointcut | 可静态匹配的 join points → advice candidates | C/D |
| JAVA-F20 | JPA entity callbacks/listeners | entity lifecycle → callbacks | C/D |
| JAVA-F21 | WebFlux route/WebTestClient | route function/annotation → handler/test | C |
| JAVA-F22 | RestTemplate/WebClient/Feign | 常量目标可连跨客户端边；服务外部则 external | C/D/E |
| JAVA-F23 | Spring Integration/Kafka/JMS listener | topic/channel → consumer entry | C/D |
| JAVA-F24 | configuration properties/string bean names | 常量配置可候选；外部配置降级 | C/E |

### 6.5 Java 测试生态（P2）

| ID | 情况 | 应召回关系 | 分级 |
|---|---|---|---|
| JAVA-T01 | JUnit 4/5 `@Test` | 测试节点识别 | C |
| JAVA-T02 | `@ParameterizedTest` | 归一到测试方法 | C |
| JAVA-T03 | `@RepeatedTest/@TestFactory/@TestTemplate` | 测试节点/工厂识别 | C/D |
| JAVA-T04 | `@Nested` | 测试类层级与外层生命周期 | C |
| JAVA-T05 | Before/After lifecycle | 生命周期方法 → 作用域内 tests | C/D |
| JAVA-T06 | JUnit extension | extension callback → tests | C/D |
| JAVA-T07 | Mockito `@Mock/@Spy/@InjectMocks` | 测试 receiver 类型和注入候选 | C/D |
| JAVA-T08 | `when/verify` | 被引用方法作为弱测试上下文，不当作真实生产调用 | D |
| JAVA-T09 | Spring `@MockBean` | 测试 context → bean type | C |
| JAVA-T10 | MockMvc | method/path → Controller handler | C |
| JAVA-T11 | WebTestClient | method/path → WebFlux handler | C |
| JAVA-T12 | repository/service integration test | DI → target bean → changed symbol | C |
| JAVA-T13 | test source-set/file naming | Maven/Gradle/JUnit 惯例正确识别 | B/C |

### 6.6 Java 动态边界（P2：只要求诚实降级）

| ID | 情况 | 正确降级行为 | 分级 |
|---|---|---|---|
| JAVA-D01 | reflection `Class.forName/getMethod/invoke` | 常量类/方法名给候选，动态值 unresolved | D/E |
| JAVA-D02 | dynamic proxy | 接口可知时给实现/handler 候选，不唯一 resolved | D/E |
| JAVA-D03 | bytecode generation/instrumentation | 标记运行时增强边界 | E |
| JAVA-D04 | JNI/native method | 标记 native/external | E |
| JAVA-D05 | external DI/configuration | 配置不可见时列候选并降低可信度 | D/E |
| JAVA-D06 | runtime classpath/service provider | 构建环境可读时解析，否则 external | B/E |
| JAVA-D07 | expression language/SpEL 字符串 | 常量表达式可候选，外部输入 dynamic | C/E |

## 7. 每个覆盖项的测试层级

不是每一行都需要一个昂贵的真实仓库 Agent Eval。每项至少落入下列一种测试，关键能力应贯穿全部层级。

| 层级 | 目的 | 最低要求 |
|---|---|---|
| Parser 单测 | 证明节点、调用、import、类型、注解被抽取 | 正例、语法变体、行号 |
| Resolver 单测 | 证明边目标和 resolution 正确 | resolved/dynamic/unresolved 正反例 |
| Impact contract | 证明节点进入索引后能找回上下文 | upstream/downstream/entry/test |
| Incremental contract | 证明新增、修改、删除后结果与全量 rebuild 一致 | 三种语言都覆盖 |
| Historical benchmark | 证明真实仓库上的召回与排序 | Recall@K/All、Precision@K、MRR、失败分类 |
| Full Agent Eval | 证明上下文最终对 Agent 有价值 | 质量、Token、成本、延迟、工具采用 |

对于 P0 条目，至少应有 Resolver + Impact contract；仅有 Parser 单测不算 Impact 能力已经覆盖。P1 项目按排期进入同一套契约；P2 项目至少要有负向测试，验证系统会暴露不确定性并安全降级，而非作为 P0 的 resolved 边。

## 8. Impact Benchmark case 规范

建议统一使用节点级 strong gold，并允许文件级 weak gold：

```json
{
  "id": "java-spring-controller-service",
  "language": "java",
  "repository": "owner/repo",
  "base_commit": "...",
  "changed_symbols": ["com.example::OwnerService.save"],
  "gold_context": [
    {
      "symbol": "com.example::OwnerController.create",
      "file": "src/main/java/com/example/OwnerController.java",
      "relation": "caller",
      "strength": "strong",
      "reason": "direct typed call"
    },
    {
      "file": "src/test/java/com/example/OwnerControllerTests.java",
      "relation": "test",
      "strength": "strong",
      "reason": "MockMvc route reaches controller and service"
    }
  ],
  "expected_uncertainty": []
}
```

动态负例应显式声明：

```json
{
  "id": "python-dynamic-getattr",
  "language": "python",
  "changed_symbols": ["plugins::handler"],
  "gold_context": [],
  "expected_uncertainty": [
    {
      "source": "dispatch::run",
      "kind": "dynamic",
      "reason": "getattr target comes from runtime input"
    }
  ]
}
```

## 9. 数据集覆盖要求

### 9.1 P0 最低可发布规模

| 语言 | 强 gold cases | 真实仓库 | 必须包含 |
|---|---:|---:|---|
| Python | ≥20 | ≥3 | 普通调用、作用域、import/包转发、常量/字段/类型引用、动态负例 |
| TypeScript/JavaScript | ≥20 | ≥3 | TS+JS、ESM+CJS、barrel/alias、class/static block、类型/成员引用 |
| Java | ≥20 | ≥3 | 重载/类型绑定、继承/实例化、field initializer、成员/类型引用 |

P1/P2 可使用同一数据集补充 case，但不以框架覆盖率作为 P0 发布门槛。

### 9.2 分层约束

- P0 的 A 级直接关系：不少于 60%。
- P0 的 B 级模块/类型辅助关系：不少于 20%。
- P0 的 D/E 级歧义和运行时负例：不少于 20%。
- caller、callee、reference 三类 P0 strong gold 都必须出现；`entry` 与 `test` 是 P1/P2 的补充维度。
- 既要有成功召回，也要有正确返回空结果或不确定性的 case。
- 同一仓库不能占全部 case 的一半以上。
- 每项结果必须绑定数据集版本、代码 commit、配置和运行命令。

## 10. 验收指标

| 指标 | 说明 |
|---|---|
| Changed Symbol Found Rate | changed symbol 能否在历史快照中定位 |
| Strong Context Recall@K | 前 K 个结果召回多少必要节点/文件；报告必须固定 K=5、10、20 |
| Strong Context Precision@K | 前 K 个结果中多少是真正必要上下文；candidate 不能按 resolved 计入 precision |
| Recall@All | 区分候选生成缺口和排序缺口 |
| MRR | 第一个必要上下文出现的位置 |
| Relation Recall | caller/callee/reference 分项召回；entry/test/semantic 另按 P1/P2 报告 |
| Resolution Calibration | resolved/dynamic/unresolved 是否与 gold 不确定性一致 |
| Unsupported Honesty | E 级 case 是否避免错误确定性结论 |
| Query Latency | 索引已存在时的查询耗时 |
| Index Cost | 冷启动与增量索引耗时、体积 |

报告必须按优先级、语言、可分析性级别和关系类型分层。总平均值不能掩盖某一种语言的 P0 直接关系完全没有覆盖。

## 11. 完成定义

P0 条目只有同时满足以下条件才标记为“Impact 已覆盖”：

1. 语法形态能进入统一 IR；
2. resolver 能产生正确的 resolved/candidate/dynamic 结果；
3. 至少一个索引级测试验证 `get_impact` 或 `get_test_impact` 的最终输出；
4. 存在容易误匹配的负例；
5. 增量更新后结果与全量 rebuild 一致；
6. 至少有一个历史仓库 benchmark case；
7. 不可静态确定时，系统明确展示不确定性并触发安全降级，而不是静默漏掉或伪造确定关系。

P1/P2 条目可以标记为 `partial`、`missing` 或 `unsupported`，不影响 P0 完成定义。这里的“全量覆盖”仅指 P0 直接代码关系：支持范围内每种关系都有端到端证据；支持范围外的重要动态边界都有明确、可测试、不会误导用户的降级行为。
