# Java Language Support Design

**日期**:2026-08-06
**范围**:为 `code-review-ai` 增加 Java 调用图解析能力 + `code-review-java` 审核 skill(ROADMAP 第 5 条,仅 Java)
**前置**:ROADMAP.md 第 5 条「更多语言(Go / Java)」→ 本次只做 Java

## 目标

让 `code-review-ai` 能对 Java 仓库建索引:解析类/接口/枚举/记录/方法/构造函数,提取 `package`、import、调用、extends/implements,并通过增强的 resolver 把仓库内的常见调用解析成真实边。产出跨 Python / TS / JS / Java 的调用图,配套一个 `code-review-java` 审核 skill 随 installer 部署。

## Java qname 模型

沿用现有 `module::scope.name` 约定;module 取自 `package` 声明(缺失时回退路径推导)。

```
package com.foo;  →  module = com.foo
class UserService →  com.foo::UserService            (kind=class)
authenticate()   →  com.foo::UserService.authenticate (kind=method)
UserService(...)  →  com.foo::UserService.UserService (kind=method, 构造函数,名=类名)
```

取 package 而非路径的理由:Java 的 `import a.b.C` 引用的正是类全名 `a.b::C`,模块模型与引用模型天然对齐。

## Parser 改动(`code_review_ai/parser.py`)

### 1. 新增 `LANG["java"]` 条目

- `def_nodes`:
  - `class_declaration` / `interface_declaration` / `enum_declaration` / `record_declaration` → `class`
  - `method_declaration` / `constructor_declaration` → `method`
- `scope_nodes`:以上集合
- `call_node`:**集合** `{method_invocation, object_creation_expression}`
- `import_nodes`:`import_declaration`
- `class_def` = `class_declaration`,`class_extends` = `superclass`,`class_implements` = `super_interfaces`

### 2. 现有条目最小泛化(同步改 Python/TS/JS 条目)

- `call_node` 从单值改为集合:Python `{"call"}`,TS/JS `{"call_expression"}`
- `_walk_calls` 判断改为 `child.type in lang["call_node"]`
- `_call_target` 泛化:读 `lang.get("call_name_field", "function")` 字段;若存在 `lang.get("call_object_field")`(Java = `"object"`),有 receiver 时拼 `obj.name` 的 attribute 形式
- 新增构造调用:node 类型 == `lang.get("constructor_node")`(= `object_creation_expression`)时,取 `lang.get("constructor_type_field", "type")` 字段文本,`call_form = CALL_CONSTRUCT`(新常量)

### 3. module 推导

`parse_file` 中 `lang_name == "java"` 时走 `_java_package(source, file_path, repo_root)`:
- 从根节点找 `package_declaration`,取 `name` 字段文本(如 `com.foo`)
- 无 package 时回退:识别 `src/main/java`、`src/test/java` 前缀并剥离后用 `_module_qname`;否则直接用现有 `_module_qname`

### 4. import 提取:新增 `_extract_imports_java`

| 源形式 | ImportEntry |
|---|---|
| `import a.b.C;` | `(local="C", module="a.b", imported="C")` |
| `import a.b.*;` | `(local="*", module="a.b", imported=None, is_star=True)` |
| `import static a.b.C.m;` | `(local="m", module="a.b::C", imported="m")` |

`RawCall` 增加 `language: str = "python"` 字段,`parse_file` 填充(供 resolver 分支)。

## Resolver 改动(`code_review_ai/resolver.py`)

新增辅助 `_join_target(mod, name)`:mod 含 `::` 时直接 `f"{mod}.{name}"`(静态 import 的 module 是类 qname,`qname.join` 会误插第二个 `::`),否则走 `qname.join`。

Java 分支(`RawCall.language == "java"`)解析顺序:

| 调用形式 | 规则 |
|---|---|
| 简单 `name()` | ① 本文件符号(`local`)→ ② import(name→类/静态方法)→ ③ 同包类(`mod_syms[源包].get(name)`)→ ④ 同类方法(由 `source_qname` 推出外层类,查 `class.method` 是否在 existing)→ 未命中 unresolved |
| 属性 `head.rest()` | ① `head` 是 class import → `_join_target(a.b::C, rest)` ② module import → `_join_target(mod, rest)` ③ 本文件类 → ④ FQCN 回退(按点逐级拆前缀,查 `mod_syms[前缀]` 里的类,兼容 `com.foo.Bar.create()` 与同包 `Bar.create()`)→ ⑤ dynamic |
| `new Foo()`(CALL_CONSTRUCT) | 按简单名解析到类节点 `mod::Foo`(影响单元=类本身;构造函数仍作为节点存在图中) |

`import a.b.*` 的解析由 FQCN 回退覆盖(前缀拆到 `a.b` 即命中同包符号表)。

## 审核 skill + installer + 文档

### `code_review_ai/skills/code-review-java/SKILL.md`(新)

沿用现有语言 skill 格式:frontmatter(`name`/`description`)+ `## 审核方式` + 五个必选章节(安全 / 正确性 / 性能 / 架构 / 语言特有),每条规则标 `error/warning/info`。Java 特有规则示例:

- **安全**:硬编码密钥/连接串;字符串拼 SQL / JDBC 未参数化;日志输出敏感字段;`ObjectInputStream.readObject` 反序列化不可信数据;XXE(`DocumentBuilder` 未禁 DTD);反射执行不可信输入(`Class.forName`/`Method.invoke`)
- **正确性**:空 `catch` 吞异常;资源未 try-with-resources / finally 关闭(流/`Connection`/`ResultSet`);`==` 比较对象(应用 `equals`);可变共享状态(`static` 非 final 字段、`SimpleDateFormat` 并发)缺同步;整数溢出
- **性能**:循环内 N+1 查询/网络;热点路径 `+` 拼字符串(应用 `StringBuilder`);无界集合/缓存;同步块过大
- **架构**:类 >300 行/方法 >50 行;有状态 `static` 万能类;≥3 步逻辑未拆子函数;接口未抽象领域边界
- **语言特有**:优先不可变集合(`List.of`/`Map.of`);`Optional` 避免裸 `get()`;`equals`/`hashCode` 成对覆写;try-with-resources 替代手写 close;`@Override` 标记;命名规范

### 同步改动

- `installer.py`:`SKILL_NAMES` 加 `"code-review-java"`
- `code-review-langs/SKILL.md`:路由表加一行 `| Java | .java | code-review-java |`
- `test_skills.py`:`SKILL_NAMES` 加 java;`test_entry_lists_exactly_the_three_language_skills` 正则与断言改为四种语言(测试名同步改)
- `README.md`:"Supports Python, TypeScript, and JavaScript" → 加 Java

## 测试与夹具

新增 `tests/fixtures/repo/java/`(镜像现有 ts 夹具,便于对齐断言):

- `auth.java`:`package com.foo;` — `UserService` 类 + `authenticate()`(含裸方法调用 `check(pw)`)+ 同包类 `PasswordChecker.check`
- `app.java`:`import com.foo.UserService;` — `main()` 里 `svc.authenticate()`、`new UserService()`、`PasswordChecker.check()`(覆盖 import / 构造 / 同包三种解析)
- `util.java`:静态方法(供 `import static` 用例)

测试文件:

- `tests/test_parser_java.py`:节点 qname/kind、package→module 推导、三种 import 提取、method_invocation(带/不带 receiver)、`new Foo()`、extends/implements 边
- `tests/test_resolver_java.py`:同包类、import 类属性调用、静态 import、`new Foo()`→类节点、裸同类方法调用、FQCN
- 运行全套 `uv run pytest` 确认无回归

## 错误处理

- tree-sitter 对残缺代码容忍(产 `ERROR` 节点不抛异常),walkers 只认已知 node type,天然免疫畸形 Java —— 无新增错误处理
- `.java` 文件在 `git ls-files` 后消失 → 现有 `changes.py` / `indexer` 的 `OSError` 捕获已覆盖,不变

## 非目标(本次不做)

- **Java 基准样本**:`benchmarks/` 现有 SWE-bench 数据是 Python 专用格式,新增 Java 基准集需要真实 Java 仓库的 changed_ranges,单独立项
- **Go 语言**:ROADMAP 第 5 条含 Go,本次只做 Java,Go 后续按同一套方案接入
- **注解类型**(`@interface`)、lambda、匿名类、字段(field)的解析:不做,保持 YAGNI
- **`code-review` skill 报告框架**:不涉及,只新增语言规则 skill
