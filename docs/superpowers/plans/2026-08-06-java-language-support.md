# Java Language Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `code-review-ai` 能对 Java 仓库建索引(类/接口/枚举/记录/方法/构造函数、package、import、调用、extends/implements),增强 resolver 解析同包类、import 类属性调用、static import、`new Foo()`、裸同类方法调用、FQCN,并新增 `code-review-java` 审核 skill。

**Architecture:** 延续数据驱动设计——`parser.py` 的 `LANG` dict 加 `"java"` 条目 + `_EXT_MAP` 加 `.java`;对通用遍历器做最小泛化(`call_node` 改集合、`_call_target_for` 语言感知、`_walk_inherits` 支持每类型继承字段、module 推导加 Java 分支、import 提取加 Java 分支)。resolver 给 `RawCall` 加 `language` 字段并新增 `_resolve_java`,Python/TS 路径不变。审核 skill 加 `code-review-java` 并同步 installer / 路由表 / 结构测试。

**Tech Stack:** Python 3.14 / uv / tree-sitter-java 0.23.5 / pytest(沿用现有)。模块 qname = `package` 声明(如 `com.foo`)。

## Global Constraints

- **qname 一律走 `code_review_ai.qname`**——`join`/`short`/`module`,禁止手拼。Java 类 qname 形如 `com.foo::UserService`,方法 `com.foo::UserService.authenticate`。
- **module 推导**:Java 优先取 `package_declaration` 的 `name` 文本;无 package 回退 `src/main/java`、`src/test/java` 前缀剥离后的路径,再回退现有 `_module_qname`。
- **边 resolution 语义不变**:`resolved` 是唯一进 flow 的信任信号;`dynamic`(obj.method 未绑定)与 `unresolved` 保留不进 flow。
- **不要"修回" flow 模型**:flat-single-flow 行为由现有测试锁定,勿动 `flow_builder.py`。
- **测试用 venv 直接跑**:`.venv/Scripts/python.exe -m pytest ...`(不用 `uv run`,避免触发 `uv sync` 撞上 MCP server 锁定的 exe)。依赖已就绪:pyproject.toml 已加 `tree-sitter-java>=0.23`、uv.lock 已更新、venv 已装 0.23.5。
- **代码规范**:函数体 ≤50 行、无单字母变量名、主控只编排不写细节。

---

### Task 1: Java 夹具文件

**Files:**
- Create: `tests/fixtures/repo/java/com/foo/UserService.java`
- Create: `tests/fixtures/repo/java/com/foo/App.java`
- Create: `tests/fixtures/repo/java/com/foo/PasswordChecker.java`
- Create: `tests/fixtures/repo/java/com/foo/BaseService.java`
- Create: `tests/fixtures/repo/java/com/foo/Auth.java`
- Create: `tests/fixtures/repo/java/com/foo/util/Util.java`

**Interfaces:**
- Produces: fixture Java 源文件,供 Task 2/3 的 parser 测试与 Task 4 的 resolver 测试 `parse_file`。

- [ ] **Step 1: 创建 6 个夹具文件**

`com/foo/BaseService.java`:
```java
package com.foo;

public class BaseService {
    public static boolean boot() {
        return true;
    }
}
```

`com/foo/Auth.java`:
```java
package com.foo;

public interface Auth extends Marker {
    void run();
}
```

`com/foo/PasswordChecker.java`:
```java
package com.foo;

public class PasswordChecker {
    public static boolean check(String user) {
        return user.length() > 0;
    }
}
```

`com/foo/UserService.java`:
```java
package com.foo;

public class UserService extends BaseService implements Auth {
    public String name;

    public UserService(String n) {
        this.name = n;
    }

    public boolean authenticate(String user, String pw) {
        return check(user) && BaseService.boot();
    }

    public boolean check(String user) {
        return user.length() > 0;
    }

    public void run() {
    }
}
```

`com/foo/util/Util.java`:
```java
package com.foo.util;

public class Util {
    public static int compute() {
        return 42;
    }
}
```

`com/foo/App.java`:
```java
package com.foo;

import com.foo.UserService;
import com.foo.PasswordChecker;
import static com.foo.util.Util.compute;

public class App {
    public static void main(String[] args) {
        UserService svc = new UserService("n");
        PasswordChecker.check("u");
        svc.authenticate("u", "p");
        compute();
    }
}
```

- [ ] **Step 2: 冒烟验证 tree-sitter-java 能解析**

Run: `.venv/Scripts/python.exe -c "import tree_sitter_java, tree_sitter; from tree_sitter import Language, Parser; p=Parser(Language(tree_sitter_java.language())); t=p.parse(b'class A { void m(){ new A(); } }'); print(t.root_node.type)"`
Expected: prints `program`,无异常。

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/repo/java
git commit -m "test(java): add Java call-graph fixture files"
```

---

### Task 2: Parser——节点 + 调用(核心)

**Files:**
- Modify: `code_review_ai/parser.py`
- Test: `tests/test_parser_java.py`(新建,本任务只写「节点 + 调用」用例;Task 3 追加「import + 继承」用例)

**Interfaces:**
- Consumes: Task 1 夹具。
- Produces: `LANG["java"]` 条目;`_EXT_MAP[".java"]`;`CALL_CONSTRUCT` 常量;`_call_target_for(node, lang)`;`_java_module_qname(tree, file_path, repo_root)` / `_java_path_module(file_path, repo_root)`;`RawCall.language` 字段。`parse_file` 对 `.java` 产出 module=`com.foo`、类节点 `com.foo::X`、方法节点 `com.foo::X.m`、构造调用 `new Foo()` → RawCall(call_form=CALL_CONSTRUCT)。

- [ ] **Step 1: 写失败测试(节点 + 调用)**

`tests/test_parser_java.py`:
```python
"""Tests for Java parsing."""
import os

from conftest import FIXTURES as FIX, Q
from code_review_ai.parser import (parse_file, _lang_for_path, CALL_ATTRIBUTE,
                                   CALL_CONSTRUCT, CALL_SIMPLE)


def _parse(rel: str):
    return parse_file(os.path.join(FIX, "java", rel), FIX)


def test_lang_for_path_java():
    assert _lang_for_path("Foo.java")[0] == "java"


def test_module_from_package_declaration():
    pf = _parse("com/foo/UserService.java")
    assert pf.language == "java"
    assert pf.module_qname == "com.foo"


def test_parse_class_methods_constructor():
    pf = _parse("com/foo/UserService.java")
    kinds = {n.qualified_name: n.kind for n in pf.nodes}
    assert kinds["com.foo::UserService"] == "class"
    assert kinds["com.foo::UserService.authenticate"] == "method"
    assert kinds["com.foo::UserService.check"] == "method"
    assert kinds["com.foo::UserService.UserService"] == "method"  # constructor
    method = next(n for n in pf.nodes
                  if n.qualified_name == "com.foo::UserService.authenticate")
    assert method.parent_qname == "com.foo::UserService"


def test_parse_interface_is_class_kind():
    pf = _parse("com/foo/Auth.java")
    kinds = {n.qualified_name: n.kind for n in pf.nodes}
    assert kinds["com.foo::Auth"] == "class"
    assert kinds["com.foo::Auth.run"] == "method"


def test_parse_calls_method_invocation_and_construct():
    pf = _parse("com/foo/App.java")
    calls = {(c.target_expr, c.call_form) for c in pf.raw_calls}
    assert ("new UserService", CALL_CONSTRUCT) in calls
    assert ("PasswordChecker.check", CALL_ATTRIBUTE) in calls
    assert ("svc.authenticate", CALL_ATTRIBUTE) in calls
    assert ("compute", CALL_SIMPLE) in calls
    for c in pf.raw_calls:
        assert c.source_qname == "com.foo::App.main"
        assert c.language == "java"


def test_parse_bare_and_dotted_call_targets():
    pf = _parse("com/foo/UserService.java")
    calls = {(c.target_expr, c.call_form) for c in pf.raw_calls}
    assert ("check", CALL_SIMPLE) in calls        # bare method call
    assert ("BaseService.boot", CALL_ATTRIBUTE) in calls


def test_module_fallback_path_when_no_package(tmp_path):
    src = tmp_path / "src" / "main" / "java" / "App.java"
    src.parent.mkdir(parents=True)
    src.write_text("class App {}\n", encoding="utf-8")
    pf = parse_file(str(src), str(tmp_path))
    assert pf.module_qname == "App"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_parser_java.py -v`
Expected: FAIL——`_lang_for_path` 抛 `ValueError: unsupported file extension: Foo.java`。

- [ ] **Step 3: 实现 parser 节点 + 调用**

在 `parser.py`:

1. 顶部 import 加 `import tree_sitter_java as tsjava`,并在现有 `TSX_LANGUAGE` 后加:
```python
JAVA_LANGUAGE = Language(tsjava.language())
```

2. 三个现有 LANG 条目的 `call_node` 从字符串改为集合:`"call": {...}` → `"call_node": {"call"}`(python);`"call_node": {"call_expression"}`(typescript/javascript)。加 java 条目:
```python
    "java": {
        "def_nodes": {
            "class_declaration": "class",
            "interface_declaration": "class",
            "enum_declaration": "class",
            "record_declaration": "class",
            "method_declaration": "method",
            "constructor_declaration": "method",
        },
        "scope_nodes": {
            "class_declaration", "interface_declaration", "enum_declaration",
            "record_declaration", "method_declaration", "constructor_declaration",
        },
        "call_node": {"method_invocation", "object_creation_expression"},
        "constructor_node": "object_creation_expression",
        "constructor_type_field": "type",
        "call_name_field": "name",
        "call_object_field": "object",
        "import_nodes": {"import_declaration"},
        "class_def_nodes": {
            "class_declaration", "interface_declaration",
            "enum_declaration", "record_declaration",
        },
        "inherit_fields": {
            "class_declaration": [("superclass", "extends"), ("super_interfaces", "implements")],
            "interface_declaration": [("extends_interfaces", "extends")],
            "enum_declaration": [("super_interfaces", "implements")],
            "record_declaration": [("super_interfaces", "implements")],
        },
    },
```

3. `_EXT_MAP` 加:
```python
    ".java": ("java", LANG["java"], JAVA_LANGUAGE),
```

4. 常量区(`CALL_OTHER` 之后)加:
```python
CALL_CONSTRUCT = "construct"  # new Foo()
```

5. `RawCall` 加字段:
```python
    language: str = "python"
```

6. `_call_target` 之后新增 `_call_target_for`,并让 `_walk_calls` 改用它(同时 `call_node` 判断改集合):
```python
def _call_target_for(node, lang):
    """Language-aware call-target extraction.

    Returns (target_expr, call_form); (None, None) when no usable target.
    Java's method_invocation splits receiver/name into separate fields; its
    object_creation_expression ('new Foo()') carries the type in a 'type' field.
    """
    if lang.get("constructor_node") and node.type == lang["constructor_node"]:
        ctor = node.child_by_field_name(lang.get("constructor_type_field", "type"))
        if ctor is None:
            return None, None
        return ctor.text.decode("utf-8"), CALL_CONSTRUCT
    name_field = lang.get("call_name_field", "function")
    func = node.child_by_field_name(name_field)
    if func is None:
        return None, None
    obj_field = lang.get("call_object_field")
    if obj_field:
        obj = node.child_by_field_name(obj_field)
        if obj is not None:
            return f"{obj.text.decode('utf-8')}.{func.text.decode('utf-8')}", CALL_ATTRIBUTE
    return _call_target(func)
```
`_walk_calls` 中:
```python
        if child.type in lang["call_node"]:
            expr, form = _call_target_for(child, lang)
            if expr is not None:
                out.append(RawCall(
                    source_qname=cur_scope or module_qname,
                    target_expr=expr, call_form=form,
                    file_path="", call_line=child.start_point[0] + 1,
                ))
```

7. `parse_file`:把 `module_qname` 计算移到 `tree`/`root` 之后,Java 走 `_java_module_qname`;并在批量填充 raw_calls 时补 `c.language = lang_name`:
```python
    tree = _parser(ts_lang).parse(source)
    root = tree.root_node
    if lang_name == "java":
        module_qname = _java_module_qname(tree, file_path, repo_root)
    else:
        module_qname = _module_qname(file_path, repo_root)
```
批量循环:
```python
    for c in pf.raw_calls:
        c.file_path = file_path
        c.language = lang_name
        if line_offset:
            c.call_line += line_offset
```
注意 `_module_qname(file_path, repo_root)` 原样保留,Java 分支在 parse_file 里单独处理(见 Step 3 新增的两个函数)。

8. 新增两个 Java module 推导函数(放在 `_module_qname` 之后):
```python
def _java_module_qname(tree, file_path: str, repo_root: str) -> str:
    """Java module qname: the package declaration when present, else path-derived."""
    root = tree.root_node
    for child in root.children:
        if child.type == "package_declaration":
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                return name_node.text.decode("utf-8")
    return _java_path_module(file_path, repo_root)


def _java_path_module(file_path: str, repo_root: str) -> str:
    """Path-derived module for a Java file with no package declaration."""
    rel = Path(file_path).resolve().relative_to(Path(repo_root).resolve())
    parts = list(rel.with_suffix("").parts)
    for marker in (("src", "main", "java"), ("src", "test", "java")):
        if parts[:len(marker)] == marker:
            return ".".join(parts[len(marker):])
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_parser_java.py -v`
Expected: 本任务 6 个用例 PASS。Task 3 的 import/继承用例尚未写。

- [ ] **Step 5: 回归确认现有语言不破**

Run: `.venv/Scripts/python.exe -m pytest tests/test_parser.py tests/test_parser_ts.py -v`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add code_review_ai/parser.py tests/test_parser_java.py
git commit -m "feat(parser): Java node/call extraction via data-driven LANG entry"
```

---

### Task 3: Parser——import 提取 + 继承边

**Files:**
- Modify: `code_review_ai/parser.py`
- Modify: `tests/test_parser_java.py`(追加 import + 继承用例)

**Interfaces:**
- Consumes: Task 2 的 `LANG["java"]`(`import_nodes`/`class_def_nodes`/`inherit_fields`)、`_walk_inherits`。
- Produces: `_extract_imports_java(root, lang)`(regular/wildcard/static 三种 ImportEntry);`_walk_inherits` 泛化支持 per-type `inherit_fields` + `type_list` 下钻。

- [ ] **Step 1: 追加失败测试**

在 `tests/test_parser_java.py` 末尾追加:
```python
def test_extract_imports_regular_wildcard_static(tmp_path):
    src = tmp_path / "S.java"
    src.write_text(
        "package a.b;\n"
        "import com.foo.UserService;\n"
        "import java.util.*;\n"
        "import static com.foo.util.Util.compute;\n"
        "class S { void m() { compute(); } }\n",
        encoding="utf-8",
    )
    pf = parse_file(str(src), str(tmp_path))
    imp = {i.local_name: i for i in pf.imports}
    assert imp["UserService"].module == "com.foo"
    assert imp["UserService"].imported_name == "UserService"
    assert imp["*"].module == "java.util"
    assert imp["*"].is_star is True
    static = imp["compute"]
    assert static.module == "com.foo.util::Util"  # 静态 import:module 是类 qname
    assert static.imported_name == "compute"


def test_extract_inherits_extends_implements():
    pf = _parse("com/foo/UserService.java")
    ih = {(i.relation, i.base_expr) for i in pf.inherits}
    assert ("extends", "BaseService") in ih
    assert ("implements", "Auth") in ih


def test_extract_inherits_interface_extends_type_list():
    # interface extends 走 extends_interfaces 字段(包着 type_list)——验证下钻
    pf = _parse("com/foo/Auth.java")
    ih = {(i.relation, i.base_expr) for i in pf.inherits}
    assert ("extends", "Marker") in ih
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_parser_java.py::test_extract_imports_regular_wildcard_static -v`
Expected: FAIL——`pf.imports` 为空(`import_declaration` 未识别)。

- [ ] **Step 3: 实现 import 提取**

`_extract_imports` 加 Java 分派(在 python 分支后):
```python
    if lang_name == "java":
        return _extract_imports_java(root, lang)
```
新增:
```python
def _extract_imports_java(root, lang) -> list[ImportEntry]:
    """Extract Java imports: regular, wildcard, and static forms.

      import a.b.C;      -> local C  from module a.b (imported_name=C)
      import a.b.*;      -> star import of module a.b
      import static a.b.C.m; -> local m from class a.b::C (imported_name=m)
    """
    entries: list[ImportEntry] = []
    for node in root.children:
        if node.type not in lang["import_nodes"]:
            continue
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        full = name_node.text.decode("utf-8")
        child_types = {ch.type for ch in node.children}
        if "static" in child_types:
            module, _, member = full.rpartition(".")
            pkg, _, cls = module.rpartition(".")
            class_qn = f"{pkg}::{cls}" if cls else module
            entries.append(ImportEntry(member, class_qn, member, False))
        elif "asterisk" in child_types:
            entries.append(ImportEntry("*", full, None, True))
        else:
            pkg, _, cls = full.rpartition(".")
            entries.append(ImportEntry(cls, pkg, cls, False))
    return entries
```

- [ ] **Step 4: 运行 import 测试通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_parser_java.py::test_extract_imports_regular_wildcard_static -v`
Expected: PASS。

- [ ] **Step 5: 实现继承泛化**

把 `_walk_inherits` 替换为支持 per-type `inherit_fields` 的版本。先加模块级常量与辅助:
```python
_INHERIT_BASE_TYPES = ("identifier", "type_identifier", "property_identifier",
                       "attribute", "member_expression")


def _inherit_bases(clause):
    """Yield base type nodes from an inheritance clause, descending type_list
    (Java wraps interface extends in a type_list; classes/implements too)."""
    stack = list(clause.children)
    while stack:
        node = stack.pop()
        if node.type == "type_list":
            stack.extend(node.children)
        else:
            yield node


def _walk_inherits(node, module_qname, lang, out: list):
    """Walk AST for class inheritance: extends / implements clauses."""
    class_defs = lang.get("class_def_nodes")
    for child in node.children:
        t = child.type
        is_class_def = (t in class_defs) if class_defs else (t == lang.get("class_def"))
        if is_class_def:
            cls_name_node = child.child_by_field_name("name")
            if cls_name_node is None:
                continue
            cls_qname = qn_join(module_qname, cls_name_node.text.decode("utf-8"))
            if lang.get("inherit_fields"):
                pairs = lang["inherit_fields"].get(t, ())
            else:
                pairs = []
                for field_key, rel in (("class_extends", "extends"),
                                       ("class_implements", "implements")):
                    field_name = lang.get(field_key)
                    if field_name:
                        pairs.append((field_name, rel))
            for field_name, rel in pairs:
                clause = child.child_by_field_name(field_name)
                if clause is None:
                    continue
                for base in _inherit_bases(clause):
                    if base.type in _INHERIT_BASE_TYPES:
                        out.append(RawInherit(
                            class_qname=cls_qname,
                            base_expr=base.text.decode("utf-8"),
                            relation=rel,
                        ))
        _walk_inherits(child, module_qname, lang, out)
```

- [ ] **Step 6: 运行继承测试通过 + 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_parser_java.py tests/test_parser.py tests/test_parser_ts.py -v`
Expected: 全 PASS(现有 Python/TS 继承用例继续通过)。

- [ ] **Step 7: Commit**

```bash
git add code_review_ai/parser.py tests/test_parser_java.py
git commit -m "feat(parser): Java import extraction + per-type inherit fields"
```

---

### Task 4: Resolver——Java 调用解析

**Files:**
- Modify: `code_review_ai/resolver.py`
- Test: `tests/test_resolver_java.py`(新建)

**Interfaces:**
- Consumes: Task 2/3 的 `RawCall.language`、`CALL_CONSTRUCT`、夹具。
- Produces: `_join_target(mod, name)`;`_enclosing_class(qname)`;`_resolve_java_dotted(expr, mod_syms, existing)`;`_resolve_java(c, local, imports, existing, mod_syms, source_module, base)`;`_resolve_one` 增加可选参数 `mod_syms`/`source_module` 并对 `language=="java"` 分派;`_module_symbols` 改为按 module 合并(支持 Java 同包多文件);`_build_imports` 跳过 `"::" in imp.module`(静态 import 指向类而非模块)。

- [ ] **Step 1: 写失败测试**

`tests/test_resolver_java.py`:
```python
"""Tests for Java call resolution."""
import os

from conftest import FIXTURES as FIX
from code_review_ai.parser import parse_file
from code_review_ai.resolver import resolve_calls


def _java_files():
    names = (
        "java/com/foo/UserService.java",
        "java/com/foo/App.java",
        "java/com/foo/PasswordChecker.java",
        "java/com/foo/BaseService.java",
        "java/com/foo/Auth.java",
        "java/com/foo/util/Util.java",
    )
    return [parse_file(os.path.join(FIX, name), FIX) for name in names]


def _resolve():
    files = _java_files()
    qnames = {n.qualified_name for f in files for n in f.nodes}
    return resolve_calls(files, qnames)


def test_new_construct_resolves_to_class():
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.foo::App.main", "com.foo::UserService", "resolved") in by


def test_import_class_attribute_call_resolves():
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.foo::App.main", "com.foo::PasswordChecker.check", "resolved") in by


def test_static_import_resolves():
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.foo::App.main", "com.foo.util::Util.compute", "resolved") in by


def test_bare_same_class_method_resolves():
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.foo::UserService.authenticate", "com.foo::UserService.check", "resolved") in by


def test_fqcn_resolves():
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    assert ("com.foo::UserService.authenticate", "com.foo::BaseService.boot", "resolved") in by


def test_local_var_method_stays_dynamic():
    edges = _resolve()
    dyn = [e for e in edges if e.target == "svc.authenticate"]
    assert dyn and dyn[0].resolution == "dynamic"


def test_inherit_edges_resolved():
    edges = _resolve()
    by = {(e.source, e.target, e.kind, e.resolution) for e in edges}
    assert ("com.foo::UserService", "com.foo::BaseService", "extends", "resolved") in by
    assert ("com.foo::UserService", "com.foo::Auth", "implements", "resolved") in by
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_resolver_java.py -v`
Expected: FAIL——`new UserService` / 同包 / 静态 import / 裸同类调用等解析不到,`resolve_calls` 不识别 `CALL_CONSTRUCT`(走 CALL_OTHER → unresolved)。

- [ ] **Step 3: 实现 resolver**

1. import 加 `CALL_CONSTRUCT`:
```python
from code_review_ai.parser import (ParsedFile, RawCall, CALL_SIMPLE,
                                   CALL_ATTRIBUTE, CALL_CONSTRUCT)
```

2. `_module_symbols` 改为合并(同包多文件共享 module qname):
```python
def _module_symbols(parsed_files: list[ParsedFile]) -> dict:
    """module_qname -> {local_name: qualified_name}, merged across files that
    share a module (Java classes in the same package)."""
    out: dict[str, dict[str, str]] = {}
    for pf in parsed_files:
        syms = out.setdefault(pf.module_qname, {})
        for n in pf.nodes:
            if n.kind in ("function", "class"):
                syms[qname.short(n.qualified_name)] = n.qualified_name
    return out
```

3. 新增辅助 `_join_target` / `_enclosing_class` / `_resolve_java_dotted` / `_resolve_java`:
```python
def _join_target(mod: str, name: str) -> str:
    """Join a module/class prefix with a member into a qualified name.

    When mod already contains '::' (a class qname — e.g. a Java static-import
    target), append with SCOPE_SEP; otherwise the standard module::name form."""
    if "::" in mod:
        return f"{mod}.{name}"
    return qname.join(mod, name)


def _enclosing_class(qualified_name: str) -> str | None:
    """The first scope of a qname (the class containing a method), or None."""
    mod = qname.module(qualified_name)
    rest = qualified_name[len(mod) + len(qname.MODULE_SEP):]
    if not rest:
        return None
    first_scope = rest.split(qname.SCOPE_SEP, 1)[0]
    return qname.join(mod, first_scope)


def _resolve_java_dotted(expr: str, mod_syms: dict, existing: set[str]) -> str | None:
    """Resolve a dotted call by longest module prefix (FQCN / same-package).

    e.g. com.foo.Bar.create() or Bar.create() where Bar is in a known module.
    """
    parts = expr.split(".")
    for i in range(len(parts) - 1, 0, -1):
        mod = ".".join(parts[:i])
        syms = mod_syms.get(mod)
        if not syms:
            continue
        head = parts[i]
        if head not in syms:
            continue
        class_qn = syms[head]
        member = ".".join(parts[i + 1:])
        if member:
            target = _join_target(class_qn, member)
            if target in existing:
                return target
        elif class_qn in existing:
            return class_qn
    return None


def _resolve_java(c, local: dict, imports: dict, existing: set[str],
                  mod_syms: dict | None, source_module: str | None, base: Edge) -> Edge:
    """Java-aware call resolution: simple / attribute / construct forms."""
    if c.call_form == CALL_SIMPLE:
        name = c.target_expr
        if name in local:
            return _resolved(base, local[name], existing)
        if name in imports:
            mod, imported, _star = imports[name]
            if imported:
                return _resolved(base, _join_target(mod, imported), existing)
            return _resolved(base, mod, existing)
        if mod_syms and source_module:
            same_pkg = mod_syms.get(source_module, {})
            if name in same_pkg:
                return _resolved(base, same_pkg[name], existing)
        enclosing = _enclosing_class(c.source_qname)
        if enclosing:
            target = _join_target(enclosing, name)
            if target in existing:
                return _resolved(base, target, existing)
        return base
    if c.call_form == CALL_ATTRIBUTE:
        head, sep, rest = c.target_expr.partition(".")
        if not sep:
            base.resolution = "dynamic"
            return base
        if head in imports:
            mod, imported, _star = imports[head]
            if imported:
                class_qn = _join_target(mod, imported)
                return _resolved(base, _join_target(class_qn, rest), existing)
            return _resolved(base, _join_target(mod, rest), existing)
        if head in local and local[head] in existing:
            return _resolved(base, _join_target(local[head], rest), existing)
        if mod_syms:
            target = _resolve_java_dotted(c.target_expr, mod_syms, existing)
            if target:
                return _resolved(base, target, existing)
        base.resolution = "dynamic"
        return base
    if c.call_form == CALL_CONSTRUCT:
        name = c.target_expr
        candidates: list[str] = []
        if name in local:
            candidates.append(local[name])
        if name in imports:
            mod, imported, _star = imports[name]
            candidates.append(_join_target(mod, imported) if imported else mod)
        if mod_syms and source_module:
            same_pkg = mod_syms.get(source_module, {})
            if name in same_pkg:
                candidates.append(same_pkg[name])
        for candidate in candidates:
            if candidate in existing:
                return _resolved(base, candidate, existing)
        return base
    return base  # CALL_OTHER -> unresolved
```

4. `_resolve_one` 加可选参数 + Java 分派:
```python
def _resolve_one(c, local, imports, existing, all_import_maps,
                 mod_syms=None, source_module=None) -> Edge:
    base = Edge(source=c.source_qname, target=c.target_expr, kind="call",
                file_path=c.file_path, call_line=c.call_line, resolution="unresolved")
    if c.language == "java":
        return _resolve_java(c, local, imports, existing, mod_syms, source_module, base)
    if c.call_form == CALL_SIMPLE:
        ...原有逻辑不变...
```

5. `resolve_calls` 传入 `mod_syms` 与 `source_module`:
```python
def resolve_calls(parsed_files, existing_qnames):
    mod_syms = _module_symbols(parsed_files)
    all_import_maps = {...}
    class_qnames = {...}
    edges = []
    for pf in parsed_files:
        local = mod_syms.get(pf.module_qname, {})
        imports = _import_map(pf)
        for c in pf.raw_calls:
            edge = _resolve_one(c, local, imports, existing_qnames, all_import_maps,
                                mod_syms=mod_syms, source_module=pf.module_qname)
            edges.append(edge)
            ...__init__ 特殊处理原样保留...
```

6. `_build_imports` 跳过静态 import(module 是类 qname,不是模块):
```python
        for imp in pf.imports:
            if imp.is_star or "::" in imp.module:
                continue
```

- [ ] **Step 4: 运行 Java resolver 测试通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_resolver_java.py -v`
Expected: 7 个用例全 PASS。

- [ ] **Step 5: 回归现有 resolver / 全量**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全 PASS(含 `test_resolver.py` 的 `__init__` 链、reexport 等)。

- [ ] **Step 6: Commit**

```bash
git add code_review_ai/resolver.py tests/test_resolver_java.py
git commit -m "feat(resolver): Java call resolution (same-package/import/static/FQCN/construct)"
```

---

### Task 5: `code-review-java` 审核 skill + installer + 路由 + 结构测试

**Files:**
- Create: `code_review_ai/skills/code-review-java/SKILL.md`
- Modify: `code_review_ai/installer.py`(SKILL_NAMES)
- Modify: `code_review_ai/skills/code-review-langs/SKILL.md`(路由表)
- Modify: `tests/test_skills.py`
- Modify: `tests/test_installer.py`("Deployed 4 review skills" → 5,两处)

**Interfaces:**
- Produces: 语言审核 skill 套件新增 Java 成员,installer 部署时自动带上;结构测试与路由表同步。

- [ ] **Step 1: 创建 `code-review-java/SKILL.md`**

```markdown
---
name: code-review-java
description: 按 Java 审核规范审查 Java 代码（.java）。含安全、正确性、性能、架构与 Java 语言特有检查点，发现标注 error/warning/info。
---

# Java 代码审核规范

## 审核方式

通读待审代码，逐类对照以下检查点；每条发现标注严重级（error / warning / info）+ `文件:行号` + 修复建议。本 skill 只输出规则与发现，不生成报告框架（报告交给 `code-review`）。

## 安全

- error：硬编码密码、密钥、Token、数据库连接串。
- error：字符串拼接 SQL 或 JDBC 未参数化查询（SQL 注入）。
- error：日志输出密码、身份证、银行卡、Token 等敏感信息。
- error：对不可信输入做反序列化（`ObjectInputStream.readObject`）未做类型白名单。
- error：XML 解析未禁用外部实体/DTD（XXE）——`DocumentBuilderFactory` 未 `setFeature` 禁用 DOCTYPE/外部实体。
- error：基于不可信输入反射执行（`Class.forName` + `Method.invoke`）。

## 正确性

- error：空 `catch`，捕获后不处理不抛出。
- error：资源（`InputStream`/`Connection`/`ResultSet`）未用 try-with-resources 或 finally 关闭。
- error：对象相等用 `==`（应用 `equals`，如字符串比较）。
- error：可变共享状态缺同步（非 final 的 `static` 字段、并发使用 `SimpleDateFormat`）。
- warning：`catch (Exception e)` 过宽 / 吞异常。
- warning：整数运算未考虑溢出（大数乘法/自增）。

## 性能

- warning：循环内查询数据库或发起网络请求（N+1）。
- warning：热点路径用 `+` 拼接字符串（应用 `StringBuilder`）。
- warning：无界集合/缓存；同步块过大（应缩小锁范围或用并发集合）。

## 架构

- warning：类超过 300 行、方法超过 50 行。
- warning：逻辑 ≥3 步或嵌套 ≥2 层未拆分为语义清晰的子函数。
- warning：有状态的 `static` 万能类（工具类应无状态）。
- info：魔法数字、未使用变量/导入、冗余或注释掉的旧代码、缺必要注释。

## 语言特有

- 资源管理优先 try-with-resources，避免手写 `close`。
- 覆写 `equals` 必须同步覆写 `hashCode`。
- `Optional` 避免裸 `get()`，用 `orElse` / `orElseThrow`。
- 优先不可变集合 `List.of` / `Map.of`。
- 用 `@Override` 标记覆写方法。
- 命名：类 `UpperCamelCase`、方法/变量 `camelCase`、常量 `UPPER_SNAKE`。
```

- [ ] **Step 2: installer SKILL_NAMES 加 java**

`code_review_ai/installer.py`:
```python
SKILL_NAMES = (
    "code-review-langs",
    "code-review-python",
    "code-review-typescript",
    "code-review-javascript",
    "code-review-java",
)
```

- [ ] **Step 3: 路由表加 Java 行**

`code_review_ai/skills/code-review-langs/SKILL.md` 表格加:
```markdown
| Java | `.java` | `code-review-java` |
```

- [ ] **Step 4: 更新结构测试**

`tests/test_skills.py`:
```python
SKILL_NAMES = (
    "code-review-langs",
    "code-review-python",
    "code-review-typescript",
    "code-review-javascript",
    "code-review-java",
)
```
并改 `test_entry_lists_exactly_the_three_language_skills` 为(函数名同步改):
```python
def test_entry_lists_exactly_the_language_skills():
    entry = _read("code-review-langs")
    referenced = set(re.findall(r"code-review-(?:python|typescript|javascript|java)", entry))
    assert referenced == set(LANGUAGE_SKILLS)
```

- [ ] **Step 5: 更新 installer 测试计数**

`tests/test_installer.py` 两处 `"Deployed 4 review skills"` → `"Deployed 5 review skills"`(行 ~69 与 ~199)。

- [ ] **Step 6: 运行测试**

Run: `.venv/Scripts/python.exe -m pytest tests/test_skills.py tests/test_installer.py -v`
Expected: 全 PASS(新 skill 有 5 个 `## ` 章节、路由表恰好引用 4 个语言 skill、deploy 计数 5)。

- [ ] **Step 7: Commit**

```bash
git add code_review_ai/skills/code-review-java code_review_ai/skills/code-review-langs/SKILL.md code_review_ai/installer.py tests/test_skills.py tests/test_installer.py
git commit -m "feat(skills): code-review-java review skill + installer/routing wiring"
```

---

### Task 6: 文档 + 端到端回归

**Files:**
- Modify: `README.md`(支持语言列表)
- Modify: `docs/superpowers/specs/2026-08-06-java-language-support-design.md`(如实现偏离,回填备注)

**Interfaces:**
- Consumes: 全部前序任务。

- [ ] **Step 1: README 支持语言列表加 Java**

`README.md` 第 5 行附近:`Supports **Python, TypeScript, and JavaScript**` → `Supports **Python, TypeScript, JavaScript, and Java**`。同时检索 README 中其他列语言的地方(如 AI 接入层/skill 列表),同步补 Java。

- [ ] **Step 2: 端到端验证——对夹具目录建索引**

Run:
```bash
.venv/Scripts/python.exe -m code_review_ai.cli rebuild --repo tests/fixtures/repo --db /tmp/cr-ai-java.db
.venv/Scripts/python.exe -m code_review_ai.cli query --repo tests/fixtures/repo --db /tmp/cr-ai-java.db --symbols com.foo::UserService.authenticate
```
Expected: rebuild 成功(含 java 文件的节点/边);query 返回 `com.foo::UserService.authenticate` 的影响链含上游调用者 `com.foo::App.main`(经动态边不保证,至少能查到方法自身的 callers)。

- [ ] **Step 3: 全量测试**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全部 PASS。

- [ ] **Step 4: 更新设计文档偏离备注(如有)**

若实现与设计文档有出入(如字段名、解析顺序),在 spec 末尾补一条「实现备注」。没有偏离则跳过。

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/specs/2026-08-06-java-language-support-design.md
git commit -m "docs: document Java support in README"
```

---

## Self-Review 备忘

- Spec 覆盖对照:`LANG["java"]` 条目 → Task 2;`_EXT_MAP`/`SOURCE_GLOBS` 自动派生 → Task 2;module 取 package → Task 2;三种 import → Task 3;method_invocation 带/不带 receiver + `new Foo()` → Task 2;extends/implements(interface 走 extends_interfaces + type_list)→ Task 3;resolver 五条规则 + `_join_target` → Task 4;`code-review-java` skill + installer + 路由 + test_skills → Task 5;README → Task 6。非目标(Go / 基准样本 / 注解/lambda/匿名类 / code-review 报告框架)不涉及任何任务。
- 类型一致性:`_call_target_for` 返回 `(str|None, str|None)`;`_resolve_java` 签名 `(c, local, imports, existing, mod_syms, source_module, base)`;`_join_target(mod, name)`;`_resolve_java_dotted(expr, mod_syms, existing)` 返回 `str | None`。
