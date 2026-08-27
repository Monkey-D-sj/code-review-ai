# 五类静态边全量语法一致性实施报告

> 面向对象：继续实现 `code-review-ai` P0 语法覆盖的编码 AI。
>
> 范围：Python、TypeScript、Java；`call`、`contains`、`import`、`extends`、`implements` 五类边。
>
> 纯语法报告不包含框架语义；Spring Boot / FastAPI 已在独立的
> [FRAMEWORK_CONFORMANCE_REPORT.md](FRAMEWORK_CONFORMANCE_REPORT.md) 中统计。
>
> 仍不在本轮纯语法范围：JavaScript `.js/.jsx/.mjs/.cjs`、其他框架 event 语义、历史仓库 Recall、Full Agent Eval。

## 1. 当前结论（2026-08-20 已更新）

本轮 `missing` 已全部清零。当前机器可校验的最终状态如下：

| 状态 | 数量 | 中文含义 |
|---|---:|---|
| `covered` | 142 | 已有正例、边界例和完整 Query E2E 证据 |
| `partial` | 52 | 已实现部分变体，或依赖工程配置/候选模型 |
| `dynamic` | 20 | 运行时才能决定，正确排除出 resolved 查询 |
| `missing` | 0 | 没有遗漏的 catalog 项 |
| `not_applicable` | 1 | Python 没有独立 implements 语法 |

按语言和边类型的明细以 [tests/p0/syntax-catalog.json](../tests/p0/syntax-catalog.json) 为准（原自动生成的明细表 `P0_EDGE_SYNTAX_CONFORMANCE_REPORT.md` 已删除）。机器校验结果为：`validate_syntax_catalog(...) == []`，公共 Query case 共 125 个。

本轮新增的真实实现包括：Java 声明式返回类型驱动的 `factory.create().run()` 返回链、抽象 receiver、泛型 receiver，以及 TypeScript tagged template 调用。getter/setter、Java method reference、TypeScript union/structural receiver 和 Gradle/JPMS/工作区配置边界没有伪装成 resolved，而是分别登记为 dynamic 或 partial。

下面的章节保留为原始实施基线和设计约束；其中旧的“当前基线”数字是任务开始时的快照，最终状态以 `tests/p0/syntax-catalog.json` 和上面的报告为准。

### 历史基线

任务开始时 `tests/p0` 已具备可用的 E2E 测试骨架，但只覆盖“最小 P0 契约”，不能代表全量语言语法。

原始基线：

| 语言 | 已登记 case | Gold resolved 邻居 | 当前结果 |
|---|---:|---:|---:|
| Python | 10 | 26 | 100% |
| TypeScript | 29 | 57 | 100% |
| Java | 12 | 41 | 100% |

这里的 100% 只表示现有 case 全部通过。全量语法分母尚未建立，因此禁止表述为“语言语法覆盖率 100%”。

本任务的最终目标是把本文第 6～8 节的每个原子语法项转成机器可读 catalog 和可执行 case，然后报告真实的：

```text
covered / partial / missing / dynamic / not_applicable
```

## 2. 不可改变的定义

### 2.1 五类边

| edge kind | 含义 |
|---|---|
| `call` | 一个可执行节点直接调用另一个可执行节点或构造目标 |
| `contains` | module/package/class/type 包含直接成员 |
| `import` | 一个 module/package 直接依赖仓库内另一个 module/package |
| `extends` | class/interface 的语言级继承关系 |
| `implements` | class/record/enum 对 interface 的语言级实现关系 |

Python没有独立 `implements` 语法，必须标记 `not_applicable`，不能用 ABC/Protocol 强行模拟。

### 2.2 分析结果

| 结果 | 条件 | 是否进入 `query_graph` 默认结果 |
|---|---|---:|
| `resolved` | 目标在当前静态上下文中唯一确定 | 是 |
| `candidate` | 存在多个合法仓库内目标 | 否 |
| `dynamic` | 目标依赖运行时对象或运行时值 | 否 |
| `unresolved` | 表达式明确但仓库内找不到目标 | 否 |
| `external` | 目标明确属于仓库外依赖 | 否 |

不得为了提高覆盖率把 candidate/dynamic/unresolved 标成 resolved。

### 2.3 覆盖状态

| 状态 | 完成条件 |
|---|---|
| `covered` | 正例、near-miss、公开 Query E2E、完整 gold 集合全部通过 |
| `partial` | 仅部分语法变体支持，或符号身份仍合并 |
| `missing` | 没有实现或没有最终 Query E2E 证据 |
| `dynamic` | 原理上不可唯一静态确定，且诚实降级测试通过 |
| `not_applicable` | 该语言不存在对应语法 |

`dynamic` 是正确结果，不计入 resolved Recall 分母；但要计入 Dynamic Honesty 分母。

## 3. 交付物

编码 AI 必须最终交付：

```text
tests/p0/syntax-catalog.json
tests/p0/python/...
tests/p0/typescript/...
tests/p0/java/...
tests/p0_conformance.py
tests/test_p0_syntax_catalog.py
```

其中：

- `syntax-catalog.json` 是完整分母；
- 各语言 fixture/case 是 gold；
- `p0_conformance.py` 从公开查询实时计算指标；
- `test_p0_syntax_catalog.py` 防止 catalog、case、coverage、metrics 漂移；
-最终报告只引用自动计算结果。

## 4. Catalog 设计

### 4.1 原子项格式

```json
{
  "id": "PY-CALL-SELF-METHOD",
  "language": "python",
  "edge_kind": "call",
  "syntax": "self.method()",
  "classification": "static",
  "status": "covered",
  "case_ids": ["PY-CALL-SELF-METHOD-POS", "PY-CALL-SELF-METHOD-NEG"],
  "expected_resolution": "resolved",
  "reason": "receiver is the enclosing class instance",
  "limitations": []
}
```

允许的 `classification`：

- `static`：仅语法/作用域即可确定；
- `module`：需要 module/import 规则；
- `type`：需要声明类型或签名；
- `ambiguous`：可能产生多个 candidate；
- `runtime`：无法可靠静态确定。

### 4.2 Catalog 门禁

测试必须保证：

1. ID 全局唯一；
2. language、edge_kind、status、classification 取值合法；
3. `covered` 至少有一个正例和一个 near-miss/负例；
4. `partial` 必须写 limitations；
5. `dynamic` 必须有公开负例；
6. `case_ids` 全部能从 case 文件加载；
7. case 的 `syntax_ids` 必须反向指回 catalog；
8. metrics 的分母来自 catalog，不能手填；
9. Python `implements` 只允许 `not_applicable`；
10. catalog 新增静态项时，如果没有测试，CI 必须失败或指标明确下降。

## 5. Case 设计

### 5.1 一个 case 只证明一个主要语法事实

```json
{
  "case_id": "PY-CALL-SELF-METHOD-POS",
  "syntax_ids": ["PY-CALL-SELF-METHOD"],
  "language": "python",
  "fixture": "fixture_repo",
  "qualified_name": "calls.methods::Worker.run",
  "edge_kind": "call",
  "direction": "out",
  "expected_in": [],
  "expected_out": ["calls.methods::Worker.save"]
}
```

禁止用一个大型 case 模糊证明十几种语法。可以共享 fixture，但每个主要语法必须有独立 case ID 和独立查询目标。

### 5.2 每个静态绑定项至少三种测试

1. `POS`：唯一正确目标被召回；
2. `NEAR`：存在同名相似目标，但不能串边；
3. `BOUNDARY`：目标外部、缺失、歧义或动态时不得伪 resolved。

### 5.3 完整集合断言

必须比较完整的 `expected_in`、`expected_out` 集合。以下断言不合格：

```python
assert expected in returned
```

必须使用：

```python
assert returned == expected
```

否则额外误边不会被检测。

## 6. Python 全量语法目录

### 6.1 Python `call`

| Syntax ID | 语法 | 期望 | 初始判断 |
|---|---|---|---|
| `PY-CALL-TOP` | 顶层 `f()` | resolved | covered |
| `PY-CALL-SAME-MODULE` | 同模块函数调用 | resolved | covered |
| `PY-CALL-CROSS-MODULE` | import 后跨模块调用 | resolved | covered |
| `PY-CALL-IMPORT-ALIAS` | alias 函数调用 | resolved | covered |
| `PY-CALL-MODULE-ATTR` | `module.f()` | resolved | covered |
| `PY-CALL-NESTED` | nested function | 遵守词法作用域 | partial |
| `PY-CALL-CLOSURE` | closure 调用捕获函数 | resolved | missing |
| `PY-CALL-RECURSIVE` | 直接递归 | resolved self-edge | covered |
| `PY-CALL-MUTUAL-RECURSIVE` | 互递归 | 两条 resolved edge | missing |
| `PY-CALL-SELF-METHOD` | `self.m()` | 当前类方法 | covered |
| `PY-CALL-CLS-METHOD` | `cls.m()` | 当前类方法 | covered |
| `PY-CALL-CLASS-METHOD` | `Class.m()` | 指定类方法 | covered |
| `PY-CALL-STATICMETHOD` | `@staticmethod` | 指定静态方法 | partial |
| `PY-CALL-CONSTRUCTOR` | `Class()` | class + `__init__` | covered |
| `PY-CALL-NEW` | `__new__` | 构造语义边 | partial |
| `PY-CALL-SUPER` | `super().m()` | MRO父类方法 | partial |
| `PY-CALL-ASYNC` | `await f()` | resolved | partial |
| `PY-CALL-CONTROL-IF` | if/else 内调用 | 保留全部静态调用 | covered |
| `PY-CALL-CONTROL-LOOP` | for/while 内调用 | resolved | covered |
| `PY-CALL-TRY` | try/except/finally 内调用 | 保留全部静态调用 | covered |
| `PY-CALL-COMPREHENSION` | comprehension 内调用 | 归属外层函数 | partial |
| `PY-CALL-LAMBDA` | lambda变量调用 | 稳定节点/目标 | missing |
| `PY-CALL-CALLABLE` | `obj()` → `__call__` | type/candidate | missing |
| `PY-CALL-PROPERTY` | property getter/setter | semantic/candidate | missing |
| `PY-CALL-MAGIC` | 运算符/迭代协议 | semantic/candidate | missing |
| `PY-CALL-WITH` | context manager | `__enter__/__exit__`候选 | missing |
| `PY-CALL-GENERATOR` | generator调用 | function edge | partial |
| `PY-CALL-DECORATOR` | decorator应用 | decorator关系 | partial |
| `PY-CALL-PARTIAL` | `functools.partial` | callable target candidate | missing |
| `PY-CALL-DYNAMIC-ATTR` | `getattr(obj,name)()` | dynamic | dynamic |
| `PY-CALL-SUBSCRIPT` | `mapping[name]()` | dynamic | dynamic |
| `PY-CALL-EVAL` | `eval/exec` | runtime boundary | dynamic |

### 6.2 Python `contains`

| Syntax ID | 语法 | 初始判断 |
|---|---|---|
| `PY-CONTAINS-MODULE-FUNCTION` | module→function | covered |
| `PY-CONTAINS-MODULE-CLASS` | module→class | covered |
| `PY-CONTAINS-CLASS-METHOD` | class→method | covered |
| `PY-CONTAINS-CLASS-NESTED-CLASS` | class→nested class | missing |
| `PY-CONTAINS-FUNCTION-NESTED` | function→nested function | partial |
| `PY-CONTAINS-DECORATED` | container→decorated definition | partial |
| `PY-CONTAINS-ASYNC` | container→async function/method | partial |

### 6.3 Python `import`

| Syntax ID | 语法 | 初始判断 |
|---|---|---|
| `PY-IMPORT-MODULE` | `import a` | covered |
| `PY-IMPORT-SUBMODULE` | `import a.b` | covered |
| `PY-IMPORT-MODULE-ALIAS` | `import a as x` | covered |
| `PY-IMPORT-FROM` | `from a import f` | covered |
| `PY-IMPORT-FROM-ALIAS` | `from a import f as g` | covered |
| `PY-IMPORT-RELATIVE-ONE` | `from .a import f` | covered |
| `PY-IMPORT-RELATIVE-MULTI` | `from ..a import f` | covered |
| `PY-IMPORT-REEXPORT` | `__init__.py` re-export | covered |
| `PY-IMPORT-STAR-ALL` | wildcard + `__all__` | covered |
| `PY-IMPORT-STAR-AMBIGUOUS` | 多star同名 | candidate | covered/partial需核验 |
| `PY-IMPORT-SRC-LAYOUT` | src layout | covered |
| `PY-IMPORT-NAMESPACE-PACKAGE` | namespace package | partial |
| `PY-IMPORT-STUB` | `.pyi` | runtime不建边、type-only | partial |
| `PY-IMPORT-DYNAMIC-CONSTANT` | `import_module("a")` | 可静态候选 | missing |
| `PY-IMPORT-DYNAMIC-VARIABLE` | `import_module(name)` | dynamic | dynamic |
| `PY-IMPORT-EXTERNAL` | 外部package | external/unresolved | partial |

### 6.4 Python `extends/implements`

| Syntax ID | edge | 语法 | 初始判断 |
|---|---|---|---|
| `PY-EXTENDS-SAME-MODULE` | extends | 同模块父类 | covered |
| `PY-EXTENDS-IMPORTED` | extends | import父类 | covered |
| `PY-EXTENDS-ALIAS` | extends | alias父类 | partial |
| `PY-EXTENDS-MULTIPLE` | extends | 多重继承 | partial |
| `PY-EXTENDS-TRANSITIVE` | extends | 多级继承 | covered |
| `PY-EXTENDS-EXTERNAL` | extends | 外部父类 | unresolved/external |
| `PY-EXTENDS-DYNAMIC` | extends | 动态base表达式 | dynamic |
| `PY-IMPLEMENTS-NONE` | implements | Python无implements | not_applicable |

## 7. TypeScript 全量语法目录

### 7.1 TypeScript `call`

| Syntax ID | 语法 | 初始判断 |
|---|---|---|
| `TS-CALL-FUNCTION` | function declaration | covered |
| `TS-CALL-FUNCTION-EXPR` | function expression变量 | covered |
| `TS-CALL-ARROW` | arrow变量 | covered |
| `TS-CALL-ASYNC` | async/await | covered/partial |
| `TS-CALL-GENERATOR` | generator | partial |
| `TS-CALL-NESTED` | nested function | partial |
| `TS-CALL-IIFE` | IIFE | missing |
| `TS-CALL-TOP-LEVEL` | module顶层调用 | covered |
| `TS-CALL-CLASS-METHOD` | class method | covered |
| `TS-CALL-THIS` | `this.m()` | covered |
| `TS-CALL-SUPER` | `super.m()` | covered |
| `TS-CALL-STATIC` | `Class.m()` | covered |
| `TS-CALL-CONSTRUCTOR` | `new Class()` | covered |
| `TS-CALL-CLASS-FIELD-ARROW` | class field arrow | missing |
| `TS-CALL-OBJECT-METHOD` | object literal method | missing |
| `TS-CALL-OBJECT-FUNCTION` | object property function | missing |
| `TS-CALL-PRIVATE` | `#privateMethod()` | missing |
| `TS-CALL-GETTER-SETTER` | accessor | missing |
| `TS-CALL-OPTIONAL` | optional call/chaining | missing |
| `TS-CALL-COMPUTED-CONSTANT` | `obj["m"]()` | candidate/static | missing |
| `TS-CALL-COMPUTED-DYNAMIC` | `obj[name]()` | dynamic | dynamic |
| `TS-CALL-TAGGED-TEMPLATE` | tagged template | missing |
| `TS-CALL-CALLBACK` | callback直接参数 | 不当作直接执行；candidate | covered negative |
| `TS-CALL-OVERLOAD` | overload签名+实现 | 实现节点 | partial |
| `TS-CALL-RECURSION` | 递归/互递归 | resolved | missing/partial |
| `TS-CALL-INTERFACE-RECEIVER` | interface receiver | candidate | missing |
| `TS-CALL-UNION-RECEIVER` | union receiver | candidates | missing |
| `TS-CALL-GENERIC-RECEIVER` | generic约束 | candidate | missing |
| `TS-CALL-STRUCTURAL` | structural typing | candidate | missing |
| `TS-CALL-REFLECT` | Reflect动态目标 | dynamic | dynamic |

### 7.2 TypeScript `contains`

| Syntax ID | 语法 | 初始判断 |
|---|---|---|
| `TS-CONTAINS-MODULE-FUNCTION` | module→function | covered |
| `TS-CONTAINS-MODULE-ARROW` | module→arrow | covered |
| `TS-CONTAINS-MODULE-CLASS` | module→class | covered |
| `TS-CONTAINS-CLASS-CONSTRUCTOR` | class→constructor | covered |
| `TS-CONTAINS-CLASS-METHOD` | class→method | covered |
| `TS-CONTAINS-CLASS-STATIC-BLOCK` | class→static block | covered |
| `TS-CONTAINS-CLASS-FIELD-FUNCTION` | class→field function | missing |
| `TS-CONTAINS-NAMESPACE` | namespace→member | missing |
| `TS-CONTAINS-INTERFACE-MEMBER` | interface→method/property | missing |
| `TS-CONTAINS-ENUM-MEMBER` | enum→member | missing |
| `TS-CONTAINS-OBJECT-METHOD` | object→method | missing |

### 7.3 TypeScript `import`

| Syntax ID | 语法 | 初始判断 |
|---|---|---|
| `TS-IMPORT-NAMED` | named import | covered |
| `TS-IMPORT-DEFAULT` | default import | covered |
| `TS-IMPORT-NAMESPACE` | namespace import | covered |
| `TS-IMPORT-ALIAS` | named alias | covered |
| `TS-IMPORT-SIDE-EFFECT` | side-effect import | covered |
| `TS-IMPORT-RELATIVE` | `./`/`../` | covered |
| `TS-IMPORT-EXTENSION` | 显式/省略扩展名 | covered |
| `TS-IMPORT-INDEX` | directory index | covered |
| `TS-IMPORT-EXPORT-LOCAL` | `export {x}` | partial |
| `TS-IMPORT-REEXPORT-NAMED` | `export {x} from` | covered |
| `TS-IMPORT-REEXPORT-STAR` | `export * from` | covered |
| `TS-IMPORT-BARREL-CONFLICT` | 多barrel同名 | candidate | partial |
| `TS-IMPORT-BASEURL` | tsconfig baseUrl | partial/missing |
| `TS-IMPORT-PATHS` | tsconfig paths | covered |
| `TS-IMPORT-PATHS-MULTI` | paths多个target | missing |
| `TS-IMPORT-TSCONFIG-EXTENDS` | config继承 | missing |
| `TS-IMPORT-PROJECT-REFERENCE` | project references | missing |
| `TS-IMPORT-WORKSPACE` | monorepo workspace | missing |
| `TS-IMPORT-PACKAGE-EXPORTS` | package exports/imports | missing |
| `TS-IMPORT-IMPORT-EQUALS` | `import x = require()` | covered/partial |
| `TS-IMPORT-EXPORT-EQUALS` | `export =` | covered/partial |
| `TS-IMPORT-REQUIRE` | `require()` | covered/partial |
| `TS-IMPORT-MODULE-EXPORTS` | `module.exports` | covered/partial |
| `TS-IMPORT-DYNAMIC-CONSTANT` | `import("./x")` | missing |
| `TS-IMPORT-DYNAMIC-VARIABLE` | `import(path)` | dynamic |
| `TS-IMPORT-TYPE-ONLY` | `import type` | type-only，不产生runtime call | partial |
| `TS-IMPORT-EXTERNAL` | 外部package | external/unresolved | partial |

### 7.4 TypeScript `extends/implements`

| Syntax ID | edge | 语法 | 初始判断 |
|---|---|---|---|
| `TS-EXTENDS-SAME-MODULE` | extends | 同模块base | covered |
| `TS-EXTENDS-IMPORTED` | extends | 跨模块base | covered |
| `TS-EXTENDS-ALIAS` | extends | alias base | partial |
| `TS-EXTENDS-TRANSITIVE` | extends | 多级extends | covered |
| `TS-EXTENDS-GENERIC` | extends | generic base | partial |
| `TS-EXTENDS-MIXIN` | extends | mixin表达式 | dynamic/candidate |
| `TS-EXTENDS-EXTERNAL` | extends | 外部base | external/unresolved |
| `TS-IMPLEMENTS-ONE` | implements | 单interface | covered |
| `TS-IMPLEMENTS-MULTIPLE` | implements | 多interface | covered |
| `TS-IMPLEMENTS-IMPORTED` | implements | 跨模块interface | covered |
| `TS-IMPLEMENTS-ALIAS` | implements | alias interface | partial |
| `TS-IMPLEMENTS-GENERIC` | implements | generic interface | partial |
| `TS-IMPLEMENTS-EXTERNAL` | implements | 外部interface | external/unresolved |

## 8. Java 全量语法目录

### 8.1 Java `call`

| Syntax ID | 语法 | 初始判断 |
|---|---|---|
| `JAVA-CALL-BARE` | 同类裸方法 | covered |
| `JAVA-CALL-THIS` | `this.m()` | covered |
| `JAVA-CALL-STATIC` | `Class.m()` | covered |
| `JAVA-CALL-STATIC-IMPORT` | static import裸调用 | covered |
| `JAVA-CALL-FQCN` | FQCN调用 | covered |
| `JAVA-CALL-CONSTRUCTOR` | `new Class()` | covered |
| `JAVA-CALL-CONSTRUCTOR-OVERLOAD` | 构造器重载 | partial |
| `JAVA-CALL-SUPER` | `super.m()`/`super()` | missing/partial |
| `JAVA-CALL-FIELD-TYPE` | 字段receiver | covered |
| `JAVA-CALL-PARAM-TYPE` | 参数receiver | covered |
| `JAVA-CALL-LOCAL-TYPE` | 局部变量receiver | covered |
| `JAVA-CALL-VAR` | `var`推断 | missing |
| `JAVA-CALL-RETURN-CHAIN` | `factory.create().run()` | missing |
| `JAVA-CALL-OVERLOAD` | 方法重载 | partial，当前qname合并 |
| `JAVA-CALL-RECURSION` | 递归 | covered |
| `JAVA-CALL-MUTUAL-RECURSION` | 互递归 | missing |
| `JAVA-CALL-INNER-CLASS` | inner class方法 | partial |
| `JAVA-CALL-ANONYMOUS-CLASS` | anonymous override | missing |
| `JAVA-CALL-LOCAL-CLASS` | local class | missing |
| `JAVA-CALL-ENUM-BODY` | enum constant body | missing |
| `JAVA-CALL-RECORD-CTOR` | record ctor/accessor | missing |
| `JAVA-CALL-LAMBDA` | lambda body | missing |
| `JAVA-CALL-METHOD-REFERENCE` | `x::m`/`C::new` | missing |
| `JAVA-CALL-INTERFACE` | interface receiver | candidate | missing |
| `JAVA-CALL-ABSTRACT` | abstract receiver | candidate | missing |
| `JAVA-CALL-GENERIC` | generic receiver | candidate | missing |
| `JAVA-CALL-REFLECTION-CONSTANT` | 常量反射名 | candidate | missing |
| `JAVA-CALL-REFLECTION-DYNAMIC` | 动态反射 | dynamic | dynamic |
| `JAVA-CALL-PROXY` | dynamic proxy | dynamic | dynamic |

### 8.2 Java `contains`

| Syntax ID | 语法 | 初始判断 |
|---|---|---|
| `JAVA-CONTAINS-PACKAGE-CLASS` | package→class | covered |
| `JAVA-CONTAINS-PACKAGE-INTERFACE` | package→interface | covered/partial |
| `JAVA-CONTAINS-PACKAGE-ENUM` | package→enum | covered/partial |
| `JAVA-CONTAINS-PACKAGE-RECORD` | package→record | covered/partial |
| `JAVA-CONTAINS-CLASS-METHOD` | class→method | covered |
| `JAVA-CONTAINS-CLASS-CONSTRUCTOR` | class→constructor | covered |
| `JAVA-CONTAINS-CLASS-INNER` | class→inner class | covered |
| `JAVA-CONTAINS-INTERFACE-METHOD` | interface→method | partial |
| `JAVA-CONTAINS-ENUM-MEMBER` | enum→constant/member | missing |
| `JAVA-CONTAINS-RECORD-COMPONENT` | record→component | missing |
| `JAVA-CONTAINS-INITIALIZER` | class→initializer/static initializer | missing |

### 8.3 Java `import`

| Syntax ID | 语法 | 初始判断 |
|---|---|---|
| `JAVA-IMPORT-REGULAR` | 普通import | covered |
| `JAVA-IMPORT-STATIC` | static import | covered |
| `JAVA-IMPORT-WILDCARD` | package wildcard | covered |
| `JAVA-IMPORT-STATIC-WILDCARD` | static wildcard | partial |
| `JAVA-IMPORT-SAME-PACKAGE` | 同package隐式可见 | covered |
| `JAVA-IMPORT-FQCN` | FQCN引用 | covered |
| `JAVA-IMPORT-CROSS-PACKAGE` | 跨package | covered |
| `JAVA-IMPORT-MAVEN-SOURCESET` | Maven main/test | covered/partial |
| `JAVA-IMPORT-GRADLE-SOURCESET` | Gradle source set | missing |
| `JAVA-IMPORT-MULTI-MODULE` | Maven/Gradle多模块 | missing |
| `JAVA-IMPORT-JPMS-REQUIRES` | module-info requires | missing |
| `JAVA-IMPORT-JPMS-USES-PROVIDES` | uses/provides | missing |
| `JAVA-IMPORT-EXTERNAL` | 外部classpath | external/unresolved | partial |

### 8.4 Java `extends/implements`

| Syntax ID | edge | 语法 | 初始判断 |
|---|---|---|---|
| `JAVA-EXTENDS-CLASS` | extends | class extends class | covered |
| `JAVA-EXTENDS-INTERFACE` | extends | interface extends interface | covered |
| `JAVA-EXTENDS-MULTI-INTERFACE` | extends | interface extends多个interface | covered/partial |
| `JAVA-EXTENDS-CROSS-PACKAGE` | extends | 跨package/import | covered |
| `JAVA-EXTENDS-WILDCARD` | extends | wildcard import base | covered |
| `JAVA-EXTENDS-TRANSITIVE` | extends | 多级继承 | partial |
| `JAVA-EXTENDS-GENERIC` | extends | generic base | partial |
| `JAVA-EXTENDS-SEALED` | extends | sealed/permits | missing |
| `JAVA-EXTENDS-EXTERNAL` | extends | 外部base | external/unresolved |
| `JAVA-IMPLEMENTS-ONE` | implements | class implements interface | covered |
| `JAVA-IMPLEMENTS-MULTIPLE` | implements | 多interface | covered |
| `JAVA-IMPLEMENTS-IMPORTED` | implements | 跨packageinterface | covered |
| `JAVA-IMPLEMENTS-WILDCARD` | implements | wildcard import interface | covered |
| `JAVA-IMPLEMENTS-ENUM` | implements | enum implements | partial/missing |
| `JAVA-IMPLEMENTS-RECORD` | implements | record implements | partial/missing |
| `JAVA-IMPLEMENTS-GENERIC` | implements | generic interface | partial |
| `JAVA-IMPLEMENTS-SEALED` | implements | sealed interface/permits | missing |
| `JAVA-IMPLEMENTS-EXTERNAL` | implements | 外部interface | external/unresolved |

## 9. 实现顺序

### Phase A：建立完整分母，不改产品代码

1. 把第 6～8 节全部条目写入 `tests/p0/syntax-catalog.json`；
2. 初始状态按本文判断填写，但必须通过现有测试复核；
3. 创建 `tests/test_p0_syntax_catalog.py`；
4. 将当前 case 添加 `syntax_ids`；
5. 没有真实 case 证据的条目不得标 `covered`；
6. 生成第一版真实覆盖率，即使数值较低也不能删除 missing 项。

Phase A 完成后，才能回答“五类边目前覆盖多少语法情况”。

### Phase B：闭合确定性静态语法

优先级：

1. `contains` 缺口；
2. 普通 import/re-export/alias/relative；
3. 同作用域、显式receiver和构造调用；
4. 普通extends/implements；
5. 工程module配置。

这些通常能产生唯一 resolved target，适合先提高可靠覆盖率。

### Phase C：修复符号身份

在继续Java overload前先实现签名级 `symbol_key`：

```text
com.example::Service.save(java.lang.String,int)
```

要求同步修改：

- node唯一性；
- edge source/target；
- search与query disambiguation；
- changes和tombstone；
- incremental update；
- FTS；
- MCP兼容输出。

未完成symbol_key前，Java overload保持`partial`。

### Phase D：类型辅助和候选集

实现：

- Python annotation receiver；
- TypeScript interface/union/generic/structural候选；
- Java interface/abstract/generic/runtime override候选；
- return-type chain；
- `var`/assignment推断。

存在多个目标时写 candidate，不进入默认resolved query。

### Phase E：动态边界

为每种 runtime语法增加负例，验证：

- 不产生phantom resolved edge；
- 原始表达式/evidence被保留；
- public uncertainty可见；
- candidate数量有限；
- dynamic边不进入flow/testimpact。

### Phase F：生成最终报告

报告必须分语言、分edge展示：

| 语言 | Edge | Covered | Partial | Missing | Dynamic | N/A | Static Coverage |
|---|---|---:|---:|---:|---:|---:|---:|

建议计算：

```text
Static Coverage = covered / (covered + partial + missing)
Dynamic Honesty = 正确降级dynamic case / 全部dynamic case
```

`not_applicable`不进入分母；`partial`不能按covered计算。

## 10. 单项实现流程

编码 AI 每次只领取1～5个相邻Syntax ID，并严格执行：

1. 读取catalog和现有fixture；
2. 把目标项状态设为`missing`或`partial`，先不改成covered；
3. 添加独立POS case；
4. 添加同名/近似目标NEAR case；
5. 添加external/ambiguous/dynamic BOUNDARY case；
6. 运行测试，确认至少一个先失败；
7. 最小修改Parser/Resolver；
8. 通过公开`query_graph`验证完整集合；
9. 验证candidate/dynamic不进入resolved query；
10. 运行incremental/full rebuild等价测试；
11. 将catalog改为covered；
12. 重新生成metrics/report；
13. 运行全量测试。

禁止一次领取整个语言并大规模重写Parser。

## 11. 每次提交门禁

每次提交必须满足：

- 新增/修改的Syntax ID都有case；
- case有完整gold集合；
- coverage状态有证据；
- metrics由实际查询计算；
- near-miss无额外resolved边；
- dynamic/candidate不污染resolved图；
- `all`仍等于五种边去重并集；
- limited query结果确定性排序；
- P0套件全绿；
- 全量测试全绿；
- `git diff --check`通过。

## 12. AI最终答复模板

```text
本次实现Syntax IDs：
新增正例：
新增near-miss：
新增动态/歧义边界：
产品代码改动：
状态变化（missing/partial→covered）：
分语言/分edge覆盖率变化：
P0测试结果：
全量测试结果：
仍未覆盖项：
```

如果没有完成完整POS+NEAR+BOUNDARY链路，不得把状态改成covered。
