# Java Type Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 Java 实例接收者调用(`owners.findByLastName(...)`、`this.owners.x()`)从 dynamic 解析为 resolved——parser 建立字段/参数/局部变量→声明类型表,resolver 按声明类型绑定接收者。

**Architecture:** parser 在 Java 文件解析时收集 `ParsedFile.var_types`(`method_qname → {变量: 基类名}`,字段∪参数∪局部变量);resolver 的 `_resolve_java` 在 CALL_ATTRIBUTE 的 import/同包查找**之前**查 var_types,把裸标识符接收者绑定到 `DeclaredType.method()`,目标存在则 resolved,否则维持原 dynamic。仅裸标识符与 `this.X` 接收者;外部类型解析失败不产生伪边。

**Tech Stack:** Python 3.14 / uv / tree-sitter-java 0.23.5 / pytest。基准:`scripts/run_swebench_suite.py` + `.benchmark-cache/repos/spring-projects__spring-petclinic`。

## Global Constraints

- **qname 一律走 `code_review_ai.qname`**;方法 qname 形如 `com.example::OwnerController.find`。
- **绑定只在 target 存在于 nodes 时才置 resolved**;类型解析失败或方法缺失 → 维持原 dynamic,不产生伪边。
- 只绑定**裸标识符**接收者与 `this.X`;链式/返回值接收者不做。
- `var_types` 是解析期内存结构(不进 DB);`update.py` 增量路径经 `resolve_edges(parsed, ...)` 自然复用。
- **测试用 venv 直接跑**:`.venv/Scripts/python.exe -m pytest`。
- **代码规范**:函数体 ≤50 行、无单字母变量名、主控只编排。

---

### Task 1: Parser——`var_types` 收集

**Files:**
- Modify: `code_review_ai/parser.py`
- Test: `tests/test_parser_java.py`(追加)

**Interfaces:**
- Produces:`ParsedFile.var_types: dict[str, dict[str, str]]`;`_collect_java_var_types(root, module_qname, lang)`;`_java_class_var_types(node, module_qname, lang, out)`;`_java_params(node, scope)`;`_java_locals(node, scope)`;`_type_base_name(type_node)`。parse_file 仅 `lang_name=="java"` 时填充。

- [ ] **Step 1: 写失败测试**

`tests/test_parser_java.py` 追加:
```python
def test_java_var_types_collected(tmp_path):
    src = tmp_path / "OwnerController.java"
    src.write_text(
        "package com.example;\n"
        "class OwnerController {\n"
        "    private final OwnerRepository owners;\n"
        "    private String a, b;\n"
        "    public OwnerController(OwnerRepository clinicService) {\n"
        "        this.owners = clinicService;\n"
        "    }\n"
        "    public String show(int ownerId, Model model) {\n"
        "        Owner owner = new Owner();\n"
        "        var repo = owners;\n"
        "        return owners.findByLastName(ownerId, model);\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    pf = parse_file(str(src), str(tmp_path))
    show = pf.var_types["com.example::OwnerController.show"]
    assert show["owners"] == "OwnerRepository"            # 类字段
    assert show["a"] == "String" and show["b"] == "String"  # 多 declarator
    assert show["model"] == "Model"                        # 参数
    assert show["owner"] == "Owner"                        # 局部变量
    assert "ownerId" not in show                           # 基元类型跳过
    assert "repo" not in show                              # var 跳过
    ctor = pf.var_types["com.example::OwnerController.OwnerController"]
    assert ctor["clinicService"] == "OwnerRepository"      # 构造器参数
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_parser_java.py::test_java_var_types_collected -v`
Expected: FAIL——`pf.var_types` 为空 dict(AttributeError 或 KeyError)。

- [ ] **Step 3: 实现**

`parser.py`:

1. `ParsedFile` 加字段:
```python
    imports: list[ImportEntry] = field(default_factory=list)
    inherits: list[RawInherit] = field(default_factory=list)
    var_types: dict[str, dict[str, str]] = field(default_factory=dict)
```

2. 新增辅助(放在 `_type_base_name` 之前,`_collect_java_var_types` 附近——建议放在 `_java_module_qname` 之后):
```python
def _type_base_name(type_node) -> str | None:
    """Base type identifier of a Java type node; None for primitives and `var`.

    ``List<Owner>`` -> ``List``; ``int`` / ``boolean`` -> None (no class);
    ``var`` (Java 10 inference) -> None."""
    if type_node is None:
        return None
    if type_node.type == "type_identifier":
        text = type_node.text.decode("utf-8")
        return None if text == "var" else text
    for child in type_node.children:
        found = _type_base_name(child)
        if found is not None:
            return found
    return None


def _collect_java_var_types(root, module_qname, lang) -> dict[str, dict[str, str]]:
    """Build {method_qname: {var_name: base_type}} for Java receiver binding."""
    out: dict[str, dict[str, str]] = {}
    class_defs = lang.get("class_def_nodes")
    for child in root.children:
        if class_defs and child.type in class_defs:
            _java_class_var_types(child, module_qname, lang, out)
    return out


def _java_class_var_types(node, module_qname, lang, out) -> None:
    """Collect a class's fields and per-method params/locals into out."""
    fields: dict[str, str] = {}
    for child in node.children:
        if child.type != "field_declaration":
            continue
        type_name = _type_base_name(child.child_by_field_name("type"))
        if type_name is None:
            continue
        for decl in child.children:
            if decl.type == "variable_declarator":
                name_node = decl.child_by_field_name("name")
                if name_node is not None:
                    fields[name_node.text.decode("utf-8")] = type_name
    cls_name_node = node.child_by_field_name("name")
    cls_qname = (qname.join(module_qname, cls_name_node.text.decode("utf-8"))
                 if cls_name_node is not None else None)
    for child in node.children:
        if child.type not in ("method_declaration", "constructor_declaration"):
            continue
        method_name = child.child_by_field_name("name")
        if method_name is None:
            continue
        method_qn = qname.join(module_qname, method_name.text.decode("utf-8"), cls_qname)
        scope: dict[str, str] = dict(fields)
        _java_params(child, scope)
        _java_locals(child, scope)
        out[method_qn] = scope


def _java_params(node, scope) -> None:
    params_node = node.child_by_field_name("parameters")
    if params_node is None:
        return
    for param in params_node.children:
        if param.type != "formal_parameter":
            continue
        type_name = _type_base_name(param.child_by_field_name("type"))
        name_node = param.child_by_field_name("name")
        if type_name is not None and name_node is not None:
            scope[name_node.text.decode("utf-8")] = type_name


def _java_locals(node, scope) -> None:
    """Collect local_variable_declaration names in a method body (recursive)."""
    for child in node.children:
        if child.type == "local_variable_declaration":
            type_name = _type_base_name(child.child_by_field_name("type"))
            if type_name is None:
                continue
            for decl in child.children:
                if decl.type == "variable_declarator":
                    name_node = decl.child_by_field_name("name")
                    if name_node is not None:
                        scope[name_node.text.decode("utf-8")] = type_name
        _java_locals(child, scope)
```

3. `parse_file` 填充(在继承处理之后、返回之前):
```python
    if lang_name == "java":
        pf.var_types = _collect_java_var_types(root, module_qname, lang)
```

- [ ] **Step 4: 运行测试通过 + 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_parser_java.py tests/test_parser.py tests/test_parser_ts.py -q`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/parser.py tests/test_parser_java.py
git commit -m "feat(parser): collect Java var->type table (fields/params/locals)"
```

---

### Task 2: Resolver——接收者类型绑定

**Files:**
- Modify: `code_review_ai/resolver.py`
- Test: `tests/test_resolver_java.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 `ParsedFile.var_types`。
- Produces:`_resolve_java_type(type_name, source_module, imports, mod_syms) -> str | None`;`_resolve_java` 增 `var_types` 参数并在 CALL_ATTRIBUTE 先做接收者绑定;`_resolve_one`/`resolve_calls` 线程化 `var_types`。

- [ ] **Step 1: 写失败测试**

`tests/test_resolver_java.py` 追加:
```python
def _type_binding_repo(tmp_path):
    files = []
    for name, body in (
        ("Owner.java",
         "package com.example;\nclass Owner {}\n"),
        ("OwnerRepository.java",
         "package com.example;\n"
         "interface OwnerRepository {\n"
         "    Owner findByLastName(String lastName);\n"
         "}\n"),
        ("OwnerController.java",
         "package com.example;\n"
         "class OwnerController {\n"
         "    private final OwnerRepository owners;\n"
         "    OwnerController(OwnerRepository owners) { this.owners = owners; }\n"
         "    Owner find(String name) { return owners.findByLastName(name); }\n"
         "    Owner findThis(String name) { return this.owners.findByLastName(name); }\n"
         "    void unknown() { mysteryObj.call(); }\n"
         "}\n"),
    ):
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        files.append(parse_file(str(path), str(tmp_path)))
    return files


def test_receiver_type_binding_resolves(tmp_path):
    files = _type_binding_repo(tmp_path)
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    # 字段接收者 owners -> OwnerRepository.findByLastName
    assert ("com.example::OwnerController.find",
            "com.example::OwnerRepository.findByLastName", "resolved") in by
    # this. 前缀
    assert ("com.example::OwnerController.findThis",
            "com.example::OwnerRepository.findByLastName", "resolved") in by
    # 未知接收者仍 dynamic
    dyn = [e for e in edges if e.target == "mysteryObj.call"]
    assert dyn and dyn[0].resolution == "dynamic"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_resolver_java.py::test_receiver_type_binding_resolves -v`
Expected: FAIL——`owners.findByLastName` / `this.owners.findByLastName` 仍是 dynamic。

- [ ] **Step 3: 实现**

`resolver.py`:

1. `_resolve_java_type`(放在 `_resolve_java_dotted` 后):
```python
def _resolve_java_type(type_name: str, source_module: str | None,
                       imports: dict, mod_syms: dict | None) -> str | None:
    """Resolve a Java type name to a class qname: same-package class, then import."""
    if mod_syms and source_module:
        same_pkg = mod_syms.get(source_module, {})
        if type_name in same_pkg:
            return same_pkg[type_name]
    if type_name in imports:
        mod, imported, _star = imports[type_name]
        return _join_target(mod, imported) if imported else mod
    return None
```

2. `_resolve_java` 签名加 `var_types: dict | None = None`,CALL_ATTRIBUTE 分支开头插入绑定:
```python
    if c.call_form == CALL_ATTRIBUTE:
        expr = c.target_expr
        if expr.startswith("this."):
            expr = expr[len("this."):]
        head, sep, rest = expr.partition(".")
        if not sep:
            base.resolution = "dynamic"
            return base
        # Java receiver type binding: bare identifier whose declared type we know
        if var_types:
            scope_types = var_types.get(c.source_qname, {})
            receiver_type = scope_types.get(head)
            if receiver_type:
                class_qn = _resolve_java_type(
                    receiver_type, source_module, imports, mod_syms)
                if class_qn:
                    target = _join_target(class_qn, rest)
                    if target in existing:
                        return _resolved(base, target, existing)
        if head in imports:
            ...
```

3. `_resolve_one` 加 `var_types: dict | None = None`,Java 分派传入:
```python
    if c.language == "java":
        return _resolve_java(c, local, imports, existing, mod_syms,
                             source_module, base, var_types)
```

4. `resolve_calls` 汇总并传入:
```python
    var_types = {qn: types for pf in parsed_files
                 for qn, types in pf.var_types.items()}
```
并把 `var_types=var_types` 传给 `_resolve_one`。

- [ ] **Step 4: 运行测试通过 + 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_resolver_java.py tests/test_java_routing.py tests/test_resolver.py -q`
Expected: 全 PASS。再 `.venv/Scripts/python.exe -m pytest -q` 确认无回归。

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/resolver.py tests/test_resolver_java.py
git commit -m "feat(resolver): bind Java receiver calls to declared types"
```

---

### Task 3: 端到端验证——PetClinic 出现类型绑定边

**Files:**
- 无代码改动(验证)

- [ ] **Step 1: 重建 PetClinic 索引,检查类型绑定边**

Run:
```bash
.venv/Scripts/python.exe -c "
import tempfile
from pathlib import Path
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
repo = Path('.benchmark-cache/repos/spring-projects__spring-petclinic')
cfg = load_config(str(repo)); cfg.repo_path = str(repo)
conn = connect(str(Path(tempfile.mkdtemp()) / 'tb.db')); init_schema(conn)
rebuild(cfg, conn)
rows = conn.execute(\"SELECT source,target FROM edges WHERE kind='call' AND resolution='resolved' AND target LIKE '%Repository.%'\").fetchall()
print('resolved edges into Repository methods:', len(rows))
for r in rows[:10]: print(' ', r[0].split('::')[-1], '->', r[1].split('::')[-1])
"
```
Expected:打印出 Controller/Service/Test → `...Repository.<method>` 的 resolved 边(如 `OwnerController.findOwner -> OwnerRepository.findById`)。

- [ ] **Step 2: Commit(如无代码改动跳过)**

---

### Task 4: 重跑 PetClinic 基准对比

**Files:**
- Modify: `benchmarks/SPRING_PETCLINIC_RECALL_ANALYSIS.md`(追加类型绑定后的结果)

- [ ] **Step 1: 重跑基准**

Run:
```bash
.venv/Scripts/python.exe scripts/run_swebench_suite.py \
  --cases benchmarks/spring-petclinic-history-10.json \
  --cache-dir .benchmark-cache --top-k 10 \
  --out benchmark-results/spring-petclinic-history-10-rerun.json
```
(结果文件被 gitignore,仅本地。)

- [ ] **Step 2: 对比指标**

Run: `.venv/Scripts/python.exe -c "import json; d=json.load(open('benchmark-results/spring-petclinic-history-10-rerun.json')); a=d['aggregate']; print('test_recall_all', a['macro_test_file_recall_all']); print('prod_recall_all', a['macro_related_production_file_recall_all']); print('resolved_rate', a.get('mean_resolved_call_rate'))"`
Expected:记录前后数字;若 Test Recall@All > 58.33% 或 Production Recall@All > 17.19%,说明类型绑定生效。

- [ ] **Step 3: 更新分析文档**

在 `benchmarks/SPRING_PETCLINIC_RECALL_ANALYSIS.md` 追加/更新「类型绑定后结果」段落:前后 macro 指标、`e0db9b184e`/`1cad4124b7`/`50866def72` per-case 变化、剩余未命中归因。如实记录,若无提升也写明。

- [ ] **Step 4: Commit**

```bash
git add benchmarks/SPRING_PETCLINIC_RECALL_ANALYSIS.md
git commit -m "bench(java): petclinic after type binding"
```

---

### Task 5: 全量回归 + spec 备注 + push

- [ ] **Step 1: 全量测试**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全 PASS。

- [ ] **Step 2: spec 实现备注**

在 `docs/superpowers/specs/2026-08-07-java-type-binding-design.md` 补「实现备注」:实际收集范围、基准前后数字。

- [ ] **Step 3: Commit + push**

```bash
git add docs/superpowers/specs/2026-08-07-java-type-binding-design.md
git commit -m "docs(spec): implementation notes for Java type binding"
git push origin master
```

---

## Self-Review 备忘

- Spec 覆盖:`var_types` 收集(字段/参数/局部/this/var/基元)→ Task 1;resolver 绑定(`this.` 剥离、裸标识符、`_resolve_java_type`、target-in-existing 才 resolved)→ Task 2;验收基准 → Task 4;文档 → Task 5。非目标(链式/返回值/var 右值/DI 边)不涉及任务。
- 类型一致性:`_collect_java_var_types -> dict[str, dict[str,str]]`;`_type_base_name -> str | None`;`_resolve_java_type(type_name, source_module, imports, mod_syms) -> str | None`;`_resolve_java(..., var_types=None)`;方法 qname 与 `_walk_calls` 的 `new_scope` 一致(`qname.join(module, name, class_qn)`)。
