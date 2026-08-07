# Java 类型绑定 Design(receiver → 声明类型)

**日期**:2026-08-07
**范围**:把 Java 实例接收者调用从 dynamic 解析为 resolved——建立字段/参数/局部变量到声明类型的符号表,`receiver.method()` 绑定到 `DeclaredType.method()`。
**前置**:`benchmarks/SPRING_PETCLINIC_RECALL_ANALYSIS.md`(MockMvc 路由边后 58.33%,剩余瓶颈 #1 即类型绑定)

## 目标

把 `owners.findByLastName(...)`、`this.owners.x()`、测试里 `@Autowired`/`@MockBean` 字段直连 Repository/Service 的调用,从 dynamic 转为 resolved,改善:

- `e0db9b184e` / `1cad4124b7` 中 Controller → Repository/Service 的关系(Service test 未命中部分);
- Production Recall@All(Controller 到 Service/Repository 的业务链);
- 整体 resolved-call rate(当前 7.85%)。

## ParsedFile 扩展

`ParsedFile.var_types: dict[str, dict[str, str]]` = `method_qname → {变量名: 基类名}`。仅 Java 填充;Python/TS 为空。

## Parser 改动(`code_review_ai/parser.py`)

新增 Java 专用遍历器 `_collect_java_var_types(root, module_qname, lang) -> dict[str, dict[str, str]]`,在 `parse_file` 中仅 `lang_name == "java"` 时调用:

1. 遍历类/接口/枚举/记录:`field_declaration` 的 `type` 字段 + 每个 `variable_declarator` 的 `name` → `{字段名: 基类名}`。
2. 遍历方法/构造器:自身 `formal_parameter`(参数)与 `local_variable_declaration`(局部变量)→ 各自 `{名: 基类名}`;再并入所在类的字段表。
3. 结果以方法 qname 为键存入 `ParsedFile.var_types`。

辅助 `_type_base_name(type_node) -> str | None`:

- `type_identifier` → 文本(排除字面 `var`);
- `generic_type`(`List<Owner>`)→ 取首个 `type_identifier` 子节点文本(`List`);
- 基元(`integral_type`/`boolean_type`/…)、无类型标识 → `None`(跳过)。

## Resolver 改动(`code_review_ai/resolver.py`)

`resolve_calls` 从 parsed_files 汇总全局 `var_types = {method_qname: {var: type}}` 传入 `_resolve_java`。

`_resolve_java` 的 CALL_ATTRIBUTE 分支,**在现有 import/同包/本地查找之前**加接收者绑定:

1. `target_expr` 以 `this.` 开头 → 剥离前缀(`this.owners.findByLastName` → `owners.findByLastName`)。
2. `head`(首个点前的段)是裸标识符,且 `var_types[source_qname][head]` 命中 → 得 `type_name`。
3. `_resolve_java_type(type_name, source_module, imports, mod_syms, existing) -> str | None`:类型名 → 类 qname。
   - 同包类:`mod_syms[source_module][type_name]`;
   - import 类:`imports[type_name]` → `_join_target(module, imported)`。
4. 成功 → `_join_target(class_qn, rest)`;`target in existing` → **resolved**。类型解析失败或方法不在节点里 → 走原 dynamic。

新增辅助:
```python
def _resolve_java_type(type_name, source_module, imports, mod_syms, existing) -> str | None
def _var_types_map(parsed_files) -> dict[str, dict[str, str]]   # 汇总全局表
```

## 边界

- 只绑定**裸标识符**接收者(`owners.x`、`this.owners.x`);链式 `a.b.x()`、方法返回值接收者 `getX().y()` 不做。
- 外部类型(`Model`/`Pageable` 等,不在 nodes 里)解析失败 → 保持 unresolved/dynamic,不产生伪 resolved 边。
- 构造器 `this.owners = clinicService` 的赋值追踪不需要——字段声明类型已足够。
- `var` 推断类型不做(Java 10 局部变量推断需右值分析)。

## 测试与验收

- `tests/test_parser_java.py` 追加:`var_types` 收集(字段多 declarator、参数、局部变量、`this`、`var` 跳过、基元跳过、generic_type)。
- `tests/test_resolver_java.py` 追加:字段接收者、`this.` 前缀、参数接收者、外部类型不绑定、未知接收者仍 dynamic。
- 新增夹具或在 tmp_path 内联:controller 构造器注入字段 + 测试 `@Autowired` 字段直连 repository,断言 resolved 边。
- **验收**:重跑 `scripts/run_swebench_suite.py --cases benchmarks/spring-petclinic-history-10.json`,对比 `macro_test_file_recall_all`(58.33%)、`macro_related_production_file_recall_all`(17.19%)、resolved-call rate(7.85%)与 `e0db9b184e`/`1cad4124b7` per-case。

## 实现备注(与设计文档的偏离)

- **字段/方法在 `body` 成员里**:tree-sitter-java 0.23.5 把 class 成员(字段/方法)放在 `class_body`/`interface_body`/`enum_body`(四种声明的 `body` 字段)内,不是 class_declaration 的直接子节点;`_java_class_var_types` 遍历 `body` 成员。
- **类型绑定的附带效果**:app.java 夹具里 `UserService svc = new UserService(...); svc.authenticate()` 原来断言 dynamic,现经局部变量类型绑定正确 resolved(`test_local_var_method_stays_dynamic` 更新为 `test_local_var_method_resolves_via_declared_type`)。
- **基准结果**:Test Recall@All 58.33% → **65%**;Production Recall@All 17.19% → **34.9%**;resolved-call rate 7.85% → **10.53%**;`142321aa3e` 33%→100%。详见 `benchmarks/SPRING_PETCLINIC_RECALL_ANALYSIS.md`「类型绑定后结果」。

## 非目标(本次不做)

- Spring DI/Repository 接口实现边、`@Autowired` 字段语义单独成边——P0 后续。
- Validation 语义边(`@Valid`/Validator)——P1。
- 链式/返回值接收者、`var` 右值推断。
