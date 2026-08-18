# 静态解析覆盖与边界审计

> 审计日期：2026-08-18  
> 范围：`code_review_ai/parser.py`、`resolver.py`、`java_routing.py`、`config.py` 及其测试。  
> 结论：项目已具备 Python、TypeScript、JavaScript、Java 的**语法解析入口**，但“可解析”不等于“调用图可准确闭合”。ESM 相对导入与跨语言测试识别已修复（见 §4 P0），Java 注解/构造器注入、构造器语义与 Spring Controller Mapping 入口识别已建模（见 §4 P1）；目前最影响实际召回的是框架/运行时语义（未配置的框架入口、动态与反射分派、`@Bean`/component scan）和 TS/JS 模块解析的其余部分。

## 1. 判定口径

本报告把能力拆为四层，避免把 AST 能读出来误判为完整静态分析。

| 层级 | 含义 | 本报告中的标记 |
|---|---|---|
| 文件发现 | 文件是否进入索引 | 已覆盖 / 未覆盖 |
| 语法抽取 | 定义、调用、导入、继承等是否进入 IR | 已覆盖 / 部分覆盖 |
| 语义解析 | 原始调用是否连到仓库内真实 qname | resolved / dynamic / unresolved |
| 图产品 | flow、impact、testimpact、dead-code 是否能正确使用结果 | 可用 / 有漏报或误报风险 |

`dynamic` 与 `unresolved` 边会保留，而不是静默删除；这对人工审阅是有价值的，但下游的 flow、影响分析和测试选择只能沿 `resolved` 边可靠传播。

## 2. 文件与语言覆盖

文件类型由 [`code_review_ai/parser.py`](code_review_ai/parser.py) 的 `_EXT_MAP`（第 143 行）唯一决定。

| 语言 / 形态 | 已发现的扩展名 | 解析 grammar | 结论 |
|---|---|---|---|
| Python | `.py` | tree-sitter-python | 已覆盖 |
| TypeScript | `.ts`、`.tsx` | tree-sitter-typescript / TSX | 已覆盖 |
| JavaScript | `.js`、`.jsx`、`.mjs`、`.cjs` | TypeScript grammar / TSX grammar | 已覆盖基础语法，但没有 JavaScript 专用 grammar |
| Vue SFC | `.vue` | 抽取 `<script>` 后按 TypeScript | 仅 script，属 TS 附加形态，不是完整 Vue 分析 |
| Java | `.java` | tree-sitter-java | 已覆盖 |
| 其他 | 例如 `.pyi`、`.pyw`、`.mts`、`.cts`、Kotlin、Go、C#、C/C++、Rust、PHP、Ruby、Scala、Groovy、SQL | 无扩展映射或 grammar | 未覆盖，不会进入索引 |

说明：`.d.ts` 的文件名以 `.ts` 结尾，会被发现；但接口、类型别名和声明文件中的类型语义并未建模，不能把它等同于完整的 TypeScript declaration 支持。

## 3. 当前可靠覆盖

### 3.1 Python

- 定义：类、函数、嵌套作用域。
- 调用：裸调用、属性调用、其他调用形态均记录为 `RawCall`。
- 导入：`import`、`from … import`、别名、相对导入、通配符记录。
- 包语义：`__init__.py` 的包转发可沿 re-export 继续追踪。
- 继承：类基类可抽取为 `extends` 边。
- 装饰器：可保存名称，供配置式入口判断使用。

### 3.2 TypeScript / JavaScript

- 定义：函数、类、方法，以及赋值给变量的箭头函数或函数表达式。
- 调用：简单调用、属性调用、其他调用形态。
- ESM 抽取：具名、默认、命名空间、纯副作用 import，以及 `export { x } from "m"` 的 re-export 记录。
- 类型继承：`extends` / `implements` 可抽取。
- TS 路径别名：可从 `tsconfig.json` 的 `compilerOptions.paths` 读取并解析别名前缀。

### 3.3 Java

- package 作为 module；无 package 时按路径退化。
- class、interface、enum、record 归一为 class；method 与 constructor 归一为 method。
- 普通、通配、static import 均可抽取；同 package、普通 import、静态 import 的多种调用可解析。
- 对已收集声明类型的字段、参数和局部变量，可将 `owner.method()` 绑定到类型上的方法。
- Spring：抽取常见 Mapping 注解，并能为 `mockMvc.perform(get("/path"))` 与 Controller Mapping 合成调用边。

## 4. 已验证的高优先级缺口

### P0 — ESM 相对导入被抽取，但不会解析到仓库模块 ✅ 已修复（2026-08-18）：parse 期把相对说明符归一为 module qname（parser._esm_relative_module），跨文件调用/import 边 resolved

**现象**：`import { login } from "./auth"; login()` 会产生 `./auth::login` 的 `unresolved` 边，而不是当前仓库里的 `ts.auth::login`。

**原因**：`_extract_imports_esm` 只保存原始说明符；`resolver._module_of` 仅对配置的 `path_aliases` 做 canonicalize，不会按当前文件目录解析 `./`、`../`。实现位置：[`parser.py:1061`](code_review_ai/parser.py:1061)、[`resolver.py:64`](code_review_ai/resolver.py:64)。

**复现结果**：用仓库 `tests/fixtures/repo/ts/app.ts` 和 `auth.ts` 调用 `resolve_calls`，`ts.app::main → ./auth::login` 的 resolution 为 `unresolved`。

**影响**：这是 TS/JS 最常见的模块导入方式；跨文件调用图、impact、dead-code 入度和 re-export 追踪会出现系统性漏边。文档中“ESM 全形态”只能描述**语法抽取**，不能描述**跨文件调用解析**。

**建议**：在 `ImportEntry` 保存导入文件的相对目录，统一把相对说明符归一为模块 qname；随后再做扩展名、`index` 文件与别名解析。

### P0 — 测试识别默认仅覆盖 Python 命名习惯 ✅ 已修复（2026-08-18）：默认 test_globs 扩至 Java/TS/JS 惯例，新增 test_decorators（JUnit @Test/@ParameterizedTest）

**现象**：默认配置仅有 `test_globs=["*/tests/*", "test_*.py"]` 和 `test_names=["test_*"]`。

**未自动识别的常见形态**：

- Java：`src/test/java/**`、`*Test.java`、`*Tests.java`、JUnit 5 的 `@Test` 方法；
- TS/JS：`*.test.ts`、`*.spec.ts`、`*.test.js`、`*.spec.js`、`__tests__/`；
- Python：pytest 参数化测试虽可由文件规则覆盖，但非 `test_*` 命名、unittest 风格的测试依赖目录规则。

**原因与影响**：`is_test_node` 只依据配置 glob 与短名 glob，不读取语言注解或测试框架；`testimpact` 只查询 `is_test=1` 的节点。因此 Java 的 MockMvc 边即使合成成功，测试方法也不会自动进入“受影响测试”结果。实现位置：[`config.py:9`](code_review_ai/config.py:9)、[`parser.py:285`](code_review_ai/parser.py:285)、[`testimpact.py`](code_review_ai/testimpact.py)。

**建议**：按语言提供默认 test profile，并允许框架注解补充：JUnit `@Test`、Jest/Vitest/Mocha 的文件模式、Pytest/Unittest 的文件和类规则。

### P1 — 注解式 DI 并非跨语言通用 ✅ 已修复（2026-08-18）：新增 Java 注解字段/构造器注入的 `DiDecl` IR，resolver 按 `di_annotations` 把注入点连到依赖类（`_build_di_edges`）

**现象**：当前 DI 逻辑只对已经成为 `RawCall` 的调用，且调用目标匹配 `dependency_markers` 时，才把“裸标识符或点路径”的参数补成依赖边，例如 `Depends(get_db)`。

**未覆盖**：Java/Spring/JS 常见的 `@Inject Foo`、`@Autowired Foo`、构造器注入、字段注入、provider/token 注入，以及 TypeScript 的 decorator metadata。它们不是 `Depends(...)` 形式的调用参数，因此不会进入 `_resolve_di_args`。

**影响**：服务、Controller、Repository 之间由 DI 建立的调用关系不会进入图；相关 dead-code 结果和影响面会偏小。

**修复内容**：语法层新增 `DiDecl` IR（[`parser.py`](code_review_ai/parser.py)）：Java 的注解字段（owner = 类）与构造器参数（owner = 构造器 qname，无注解也收集，Spring 单构造器隐式注入）在 parse 期独立建模，不再复用 call arguments。解析层新增 `_build_di_edges`（[`resolver.py`](code_review_ai/resolver.py)）：字段按配置 `di_annotations`（默认 `["Autowired","Inject","Resource","MockBean"]`）过滤、构造器参数无条件，依赖类型经 `_resolve_java_type`（同 package / import）解析为仓库类后发 `kind="call"` 的 resolved 边 —— 与 `Depends()` 边同通道，flow / impact / 度计算 / dead-code 自动纳入。

**仍未覆盖**：provider/token 注入、TS decorator metadata、`@Bean`/component scan/AOP（见 §5.5）；Python/TS 的注解式 DI 仍未建模。

### P1 — Java 构造函数不会获得文档所述的 `__init__` 边 ✅ 已修复（2026-08-18）：构造器补边按语言选成员名，Java 用真实构造函数 `Class.Class`（`resolver._init_member_qname`），含“实例化 → 构造器 → 构造器内部调用”端到端链测试

**现象**：`new Foo()` 可解析到类 `Foo`；随后 resolver 尝试补 `Foo.__init__`。但 Java parser 为构造函数生成的 qname 是 `Foo.Foo`，并不存在 `Foo.__init__`，所以补边条件永远不成立。

**影响**：构造函数内部调用不会因 `new Foo()` 而进入调用图；impact 无法从实例化点走到 Java 构造函数。

**修复内容**：`resolve_calls` 补构造器边时经 `_init_member_qname`（[`resolver.py`](code_review_ai/resolver.py:162)）按语言选择成员名——Python 仍用 `__init__`，Java 复用类短名得到真实构造函数 `Class.Class`。于是 `new Foo()` 额外补一条到 `Foo.Foo` 的 resolved 边；构造器体内部的裸调用经 `_enclosing_class` 解析到同类方法，`实例化 → 构造器 → 构造器内部调用` 全链进图（见 §10 端到端测试）。

## 5. 语义解析的部分覆盖与保守边界

### 5.1 所有动态语言均受影响

| 场景 | 当前行为 | 后果 |
|---|---|---|
| `obj.method()`，接收者无静态类型 | `dynamic` | 不会进入 resolved flow |
| `getattr`、反射、猴子补丁、运行时注册 | 多数不产生可解析目标 | 静态图天然不可见 |
| 回调注册、事件总线、消息队列、依赖容器 | 多数仅是普通调用或字符串/配置 | producer / consumer 不连边 |
| 字符串路由、插件发现、动态 import | `unresolved` 或无边 | 框架入口与实现不连边 |
| 外部库、builtin、通配导入成员 | 保留 unresolved | 不跨仓库解析，属于设计边界 |

这类场景不应以“没有边”理解为“没有依赖”。下游结论应展示 unresolved/dynamic 的数量，并将其视作可信度信号。

### 5.2 Python 的进一步缺口

- 同类裸方法调用（例如 `class A: def f(self): self.g()` 或 `g()`）不会像 Java 一样按当前类解析；`self.g()` 会是 dynamic，裸 `g()` 不能自动落到 `A.g`。
- `super().method()`、protocol/ABC 的运行时实现、descriptor、`__getattr__` 等多态机制无建模。
- `import package.submodule` 后以 `package.submodule.fn()` 访问的完整包层级缺少专门的 module-object 语义。
- `importlib.import_module`、entry point metadata、Celery 字符串 task、Flask 蓝图动态注册等框架机制无专属 bridge。

### 5.3 TypeScript / JavaScript 的进一步缺口

- CommonJS 的 `require()` 不是 import extractor 的输入；`.cjs` 虽会被发现和解析，但模块依赖通常不会成为 import/call 解析边。
- `baseUrl`、`extends`、project references、多个 `paths` target、Node 的 package `exports` / `imports`、目录 `index` 解析、扩展名省略均未实现。当前 path alias 只读取 `tsconfig.json` 的 `paths`，且每个 alias 只使用第一个 target。
- TypeScript 的 interface、type alias、enum/type-only import、泛型、联合/交叉类型、函数重载没有类型图；TS/JS 不会像 Java 一样根据参数或字段类型解析 receiver。
- 只识别声明、类方法和变量赋值的函数表达式。对象字面量中的函数、类字段函数、框架 options object 中的 handler 等常见形式缺少专门定义建模。
- JSX/TSX 可被 grammar 读取，但组件树、props、事件 handler、hook 依赖和模板到函数的调用边不在图内。

### 5.4 Vue 的进一步缺口

- 仅正则提取 `<script>`；`<template>`、`<style>`、模板事件、组件引用、slot、`ref` 和响应式依赖均不进图。
- 多个 `<script>` 块会被拼接，却统一使用第一个块的行号偏移；后续块的节点行号会失真。
- 没有按 `lang` 选择 JS/TS grammar，也不解析 `.vue` 外的模板语言或编译产物。

### 5.5 Java 的进一步缺口

- receiver 类型绑定只依赖显式收集的字段、参数、局部变量声明类型；`var`、返回值类型推断、泛型实际类型、多态分派、interface/abstract 的真实实现均不做 call target 选择。
- Spring Mapping + MockMvc bridge 只匹配常量路径、HTTP 方法和等长 path segment；变量路径构造、前缀来自配置、WebFlux、`RestTemplate`/`WebTestClient`、安全过滤器、异常处理器不覆盖。
- Spring Controller Mapping 方法现按 `entry_decorators`（默认含 `GetMapping`/`PostMapping`/`PutMapping`/`DeleteMapping`/`PatchMapping`/`RequestMapping`）被 `build_flows` 认定为入口并被 dead-code 排除；但 `@Bean`、component scan、`@RestControllerAdvice`、异常处理器、定时任务等其它框架入口仍未识别。
- 注解字段/构造器注入已建模（`@Autowired` 等按 `di_annotations` 过滤的字段、构造器参数无条件成边，见 §4 P1），但 `@Bean`、component scan、AOP、反射、JPA repository proxy 等框架依赖仍未建模。
- import 的通配符不会解析到具体类；外部 classpath/JAR 也不进入图。

## 6. 继承、入口与图产品的共性限制

### 6.1 继承边不按 import 解析

`_build_inherits` 仅尝试将简单基类名拼到当前 module；不会使用各语言的 import map。因此跨文件/跨 package 的 `extends Base` 或 `implements Contract` 即使 Base/Contract 位于当前仓库，也可能保留 unresolved。此问题主要影响社区分析和类型关系展示，不会自动补足方法调用分派。

### 6.2 入口推断偏保守且与框架脱节

flow root 由“短名命中 `entry_names`、装饰器命中 `entry_decorators`（如 Spring Mapping 注解），或没有 resolved 入边”的函数/方法产生。因此 Spring Controller handler、Python `app.route` / `click.command` / `celery.task` 等已配置的框架入口有稳定语义；未配置的 consumer、cron job、Lambda/Cloud Function 等仍需要配置或专用 bridge。

### 6.3 dead-code 是候选，不是删除依据

dead-code 以“无 resolved 调用者且不是已知入口/测试”为主条件。由于上面的 unresolved、测试识别和框架入口缺口，以下符号尤其易出现误报：路由处理器、回调、ORM hook、序列化/反射调用目标、插件实现和跨语言互调入口（注解/构造器注入的 DI 服务现在有边可依，误报风险已下降）。

## 7. 与 `Languages.md` 需要对齐的表述

建议将现有文档中的下列绝对表述改成分层描述：

| 当前容易造成的理解 | 建议表述 |
|---|---|
| “ESM 全形态” | “ESM 语法形态可抽取；相对说明符和 Node 模块解析目前不闭合。” |
| “注解 DI 边（跨语言通用）” | ✅ 已对齐：`LANGUAGES.md` 已改为“调用式 marker + Java 注解字段/构造器注入”；TS decorator、provider/token 注入仍未建模。 |
| “`new Foo()` 额外补到 `Foo::__init__`” | ✅ 已对齐：`LANGUAGES.md` 已改为“额外补到真实构造函数 `Foo::Foo.Foo`（Java 构造器以类名命名）”。 |
| “MockMvc 测试在 flow 里可见” | “可合成测试到 Controller 的边；要进入 testimpact，还需配置 Java 测试识别规则。” |
| “新增语言下游零改动” | “新增 grammar 后可复用 IR/图层；仍需完成文件发现、module/import 归一、测试约定、类型/框架语义和回归测试。” |

## 8. 修复优先级建议

1. ✅ **已完成（2026-08-18）—— 先修 ESM 相对导入**：parse 期把相对说明符归一为模块 qname（`parser._esm_relative_module`），跨文件调用/import 边直接 resolved。
2. ✅ **已完成（2026-08-18）—— 提供按语言的默认测试 profile**：`test_globs` 覆盖 Java `*/test/*` / `*Test.java` / `*Tests.java` 与 TS/JS `*.test.*`、`*.spec.*`、`__tests__`；`test_decorators` 覆盖 JUnit `@Test` / `@ParameterizedTest`。
3. ✅ **已完成（2026-08-18）—— 修正 Java constructor 语义**，并新增“实例化 → 构造函数 → 构造函数内部调用”的端到端测试（`test_instantiation_to_constructor_internal_call_end_to_end`）。
4. ✅ **已完成（2026-08-18）—— 将 DI 和框架入口显式建模**：Spring `@Autowired` 字段注入与构造器注入已建边（`di_annotations` / `DiDecl` / `_build_di_edges`）；Spring Controller Mapping 方法现被 `entry_decorators` 认定为入口——`build_flows` 新增装饰器入口通道，dead-code 自动排除，`list_entry_points` 可见。
5. ⬜ **待做 —— 完善 TS/JS 模块解析**：CommonJS、`baseUrl`、`extends`、index/package exports；再考虑类型辅助的 receiver 绑定。
6. ⬜ **待做 —— 补齐回归矩阵**：每项语言能力都至少覆盖“文件发现 → parse IR → resolve edge → index → impact/testimpact/dead-code”链路，而不只测试 parser。

## 9. 验证记录

- 已运行：

  ```text
  .venv\\Scripts\\python.exe -m pytest \
    tests/test_parser.py tests/test_parser_ts.py tests/test_parser_java.py \
    tests/test_resolver.py tests/test_resolver_java.py tests/test_java_routing.py \
    -q -p no:cacheprovider
  ```

  结果：`58 passed`。

- 额外用现有 TS fixture 验证了相对 ESM import 的解析结果：`ts.app::main → ./auth::login` 为 `unresolved`。这说明现有 parser 单测通过，并不覆盖跨文件 ESM resolver 的正确性。

本报告描述的是当前实现的静态分析边界，不表示这些场景在运行时一定无调用关系；相反，涉及动态语言、框架魔法或外部依赖时，应把未解析边当作人工复核的优先信号。

## 10. 修复后验证（2026-08-18）

- P0 / P1 / 入口识别修复后全量回归：`uv run pytest -q` -> **325 passed**（P0 阶段 316 + P1 阶段 6 个新单测 + Spring 入口识别 3 个新单测）。
- ESM 相对导入端到端：`tests/test_resolver.py::test_resolve_esm_relative_imports` 断言
  `ts.app::main -> ts.auth::login`（具名与命名空间导入）均为 `resolved`，且不再存在
  以 `./auth` 为 target 的边 -- 第 4 节 P0-1 的复现用例已翻绿。
- 测试识别：`tests/test_parser.py::test_is_test_node_default_globs_cover_java_and_js` 与
  `test_is_test_node_matches_test_decorators` 覆盖 Java/TS/JS 文件模式与 JUnit `@Test`。
- Java 构造器（P1-1）：`tests/test_resolver_java.py::test_new_construct_also_links_constructor`
  断言 `com.foo::App.main -> com.foo::UserService.UserService` 为 `resolved`（不再出现不存在的
  `Foo.__init__`）；`test_instantiation_to_constructor_internal_call_end_to_end` 走通
  `App.main -> new Service() -> Service.Service -> Service.helper` 全链，并断言构造器与其内部
  调用均出现在 App.main 的 flow path 中 —— §8 #3 要求的端到端链测试。
- Java 注解/构造器 DI（P1-2）：`test_java_di_decls_collected`（parse 期 `DiDecl` IR：注解字段
  owner=类、构造器参数 owner=构造器 qname、无注解字段跳过）、
  `test_di_field_injection_resolves`（`@Autowired` 字段 -> `OwnerController -> OwnerRepository`
  resolved 边，`@SuppressWarnings` 字段不建边）、
  `test_di_field_injection_off_without_matching_annotation`（未配置 `di_annotations` 时不建边）、
  `test_di_constructor_param_resolves`（构造器参数无条件成边，`构造函数 -> OwnerRepository`）。
- Spring 入口识别（§8 #4 收尾）：默认 `entry_decorators` 增加 Spring Mapping 注解；`build_flows`
  新增 `entry_decorators` 参数，装饰器命中即产生 flow root（`NodeRow.decorators` 纳入
  `flow_input_hash`，只改注解也会触发 flow 重建）。验证：
  `test_flow_builder.py::test_decorator_marked_method_is_entry`（有入边仍为入口）、
  `test_deadcode.py::test_find_dead_code_excludes_spring_mapping`（`@GetMapping` 不在 dead-code 候选）、
  `test_parser_java.py::test_java_annotations_capture_mappings`（方法级注解进 decorators）。
