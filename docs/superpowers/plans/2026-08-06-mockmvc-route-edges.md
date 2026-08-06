# MockMvc Route Edges Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建 MockMvc HTTP 路由边,把 Spring Controller 测试的 `mockMvc.perform(get("/path"))` 连到 controller 方法的 `@GetMapping("/path")`,改善 Spring PetClinic 基准的 Test Recall@All(基线 23.33%)。

**Architecture:** parser 捕获两类信息挂在 `ParsedNode` 上——controller 方法的路由 `mappings`(来自 mapping 注解)与测试方法的 `mockmvc_requests`(来自 `mockMvc.perform(...)`);新模块 `java_routing.py` 在 `resolve_edges` 里按「HTTP 方法 + 归一化路径」匹配两者,合成 `kind="call"` 的 resolved 边(test_method → controller_method),使 flow 能遍历到测试。Java 注解嵌在 `modifiers` 子节点里,decorator 提取需下钻。

**Tech Stack:** Python 3.14 / uv / tree-sitter-java 0.23.5 / pytest。运行基准:`scripts/run_swebench_suite.py` + `.benchmark-cache/repos/spring-projects__spring-petclinic`(已缓存)。

## Global Constraints

- **qname 一律走 `code_review_ai.qname`**;合成边 target 是 controller 方法 qname(如 `com.example::HomeController.list`)。
- **合成边必须 `kind="call"`**——`indexer._write_flows` 只取 `kind=='call'` 的边进 flow,`recompute_degrees` 也按 call 边算度。`resolution="resolved"`(target 在 nodes 里时)。
- **不动 flow 模型**;不动 Python/TS/JS 的 decorator 行为(单值 `decorator_node` 保持兼容)。
- **测试用 venv 直接跑**:`.venv/Scripts/python.exe -m pytest`(不用 `uv run`,避免撞 MCP server 锁定的 exe)。
- **代码规范**:函数体 ≤50 行、无单字母变量名、主控只编排。

---

### Task 1: Parser——Java 注解捕获 → 方法路由 `mappings`

**Files:**
- Modify: `code_review_ai/parser.py`
- Test: `tests/test_parser_java.py`(追加)

**Interfaces:**
- Produces:`ParsedNode.mappings: list[tuple[str, str]]`;`_decorator_types(lang)`;`_annotation_children(node, deco_types, lang)`;`_java_mappings(node, lang)`;`_annotation_strings(node)`;`_request_mapping_method(node)`;`LANG["java"]["decorator_node"] = {"marker_annotation", "annotation"}` + `"annotations_in_modifiers": True`。`_decorator_names` 改为集合感知 + modifiers 下钻。

- [ ] **Step 1: 写失败测试**

`tests/test_parser_java.py` 追加:
```python
def test_java_annotations_capture_mappings(tmp_path):
    src = tmp_path / "HomeController.java"
    src.write_text(
        "package com.example;\n"
        "@Controller\n"
        "class HomeController {\n"
        "    @GetMapping(\"/owners\")\n"
        "    public String list() { return null; }\n"
        "    @GetMapping(\"/owners/{ownerId}\")\n"
        "    public String show(int ownerId) { return null; }\n"
        "    @RequestMapping(value=\"/r\", method=RequestMethod.POST)\n"
        "    public String rm() { return null; }\n"
        "}\n",
        encoding="utf-8",
    )
    pf = parse_file(str(src), str(tmp_path))
    by = {n.qualified_name: n.mappings for n in pf.nodes}
    assert by["com.example::HomeController.list"] == [("GET", "/owners")]
    assert by["com.example::HomeController.show"] == [("GET", "/owners/{ownerId}")]
    # RequestMapping 带 method 元素 -> 具体方法;无则 ANY
    assert by["com.example::HomeController.rm"] == [("POST", "/r")]
    cls = next(n for n in pf.nodes if n.qualified_name == "com.example::HomeController")
    assert "Controller" in cls.decorators
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_parser_java.py::test_java_annotations_capture_mappings -v`
Expected: FAIL——`n.mappings` 属性不存在(AttributeError)。

- [ ] **Step 3: 实现**

`parser.py`:

1. `ParsedNode` 加字段:
```python
    decorators: list[str] = field(default_factory=list)
    mappings: list[tuple[str, str]] = field(default_factory=list)
    mockmvc_requests: list[tuple[str, str]] = field(default_factory=list)
```

2. `LANG["java"]` 加:
```python
        "decorator_node": {"marker_annotation", "annotation"},
        "annotations_in_modifiers": True,
        "mockmvc_capture": True,
```

3. 新增辅助(放在 `_decorator_names` 附近):
```python
def _decorator_types(lang) -> set[str]:
    """decorator_node as a set; a single-string config (Python/TS/JS) works too."""
    node_type = lang.get("decorator_node")
    if not node_type:
        return set()
    if isinstance(node_type, (set, tuple, frozenset)):
        return set(node_type)
    return {node_type}


def _annotation_children(node, deco_types: set[str], lang) -> list:
    """Annotation nodes decorating a def: direct children of the given types,
    plus those nested in a `modifiers` child (tree-sitter-java nests Java
    annotations there — not siblings, not direct children)."""
    found = [child for child in node.children if child.type in deco_types]
    if lang.get("annotations_in_modifiers"):
        for child in node.children:
            if child.type == "modifiers":
                found.extend(c for c in child.children if c.type in deco_types)
    return found
```

4. 改 `_decorator_names`:
```python
def _decorator_names(node, lang) -> list[str]:
    """Collect decorator names from a node's annotation/decorator children.
    A lang without ``decorator_node`` configured is a no-op."""
    deco_types = _decorator_types(lang)
    if not deco_types:
        return []
    return [_decorator_name(c) for c in _annotation_children(node, deco_types, lang)]
```

5. 改 `_walk_defs_typed` 开头:`deco_type = lang.get("decorator_node")` → `deco_types = _decorator_types(lang)`;循环内 `if deco_type and t == deco_type:` → `if deco_types and t in deco_types:`;`if deco_type:` → `if deco_types:`。创建节点时加 mappings:
```python
            mappings = _java_mappings(child, lang) if lang.get("annotations_in_modifiers") else []
            output.append(ParsedNode(
                qualified_name=qn, kind=kind, file_path="",
                start_line=child.start_point[0] + 1, end_line=child.end_point[0] + 1,
                signature=_sig(source, child), parent_qname=scope_qname,
                decorators=decorators, mappings=mappings,
            ))
```

6. 新增映射提取(放在 `_decorator_name` 后):
```python
_MAPPING_METHODS = {
    "RequestMapping": "ANY",
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}


def _java_mappings(node, lang) -> list[tuple[str, str]]:
    """Extract (http_method, path) pairs from Spring mapping annotations.

    Reads a def's annotation nodes (including those in a modifiers child).
    RequestMapping without an explicit method element maps to 'ANY'."""
    out: list[tuple[str, str]] = []
    for ann in _annotation_children(node, _decorator_types(lang), lang):
        method = _MAPPING_METHODS.get(_decorator_name(ann))
        if method is None:
            continue
        paths = _annotation_strings(ann)
        if method == "ANY":
            method = _request_mapping_method(ann) or "ANY"
        for path in paths:
            out.append((method, path))
    return out


def _annotation_strings(node) -> list[str]:
    """Collect quoted string values in an annotation's arguments (descendants),
    e.g. @GetMapping(\"/owners\") -> ['/owners']; { \"/a\", \"/b\" } -> both."""
    return [s.text.decode("utf-8").strip("\"'")
            for s in _collect_by_type(node, "string_literal")]


def _collect_by_type(node, node_type: str) -> list:
    out = []
    for child in node.children:
        if child.type == node_type:
            out.append(child)
        out.extend(_collect_by_type(child, node_type))
    return out


def _request_mapping_method(node) -> str | None:
    """Extract the HTTP method from @RequestMapping(method=RequestMethod.GET)."""
    for access in _collect_by_type(node, "field_access"):
        text = access.text.decode("utf-8")
        if text.startswith("RequestMethod."):
            return text.split(".")[-1].upper()
    return None
```

- [ ] **Step 4: 运行测试通过 + 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_parser_java.py tests/test_parser.py tests/test_parser_ts.py -q`
Expected: 全 PASS(现有 decorator 测试——Python/TS——不变)。

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/parser.py tests/test_parser_java.py
git commit -m "feat(parser): Java annotation capture -> method route mappings"
```

---

### Task 2: Parser——MockMvc 请求捕获 → `mockmvc_requests`

**Files:**
- Modify: `code_review_ai/parser.py`
- Test: `tests/test_parser_java.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 `ParsedNode.mockmvc_requests`、`LANG["java"]["mockmvc_capture"]`。
- Produces:`_mockmvc_request(node, lang)`;`_find_builder_call(node)`;`_first_string_literal(node)`;`_walk_calls(..., mockmvc_requests=None)` 线程化;parse_file 把请求挂到方法节点。

- [ ] **Step 1: 写失败测试**

`tests/test_parser_java.py` 追加:
```python
def test_java_mockmvc_request_capture(tmp_path):
    src = tmp_path / "HomeControllerTests.java"
    src.write_text(
        "package com.example;\n"
        "class HomeControllerTests {\n"
        "    void listOk() {\n"
        "        mockMvc.perform(get(\"/owners?page=1\"));\n"
        "    }\n"
        "    void createOk() {\n"
        "        mockMvc.perform(post(\"/owners/new\").param(\"x\", \"y\"));\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    pf = parse_file(str(src), str(tmp_path))
    by = {n.qualified_name: n.mockmvc_requests for n in pf.nodes}
    assert by["com.example::HomeControllerTests.listOk"] == [("GET", "/owners?page=1")]
    # 链式 .param(...) 也能取到根请求构建器
    assert by["com.example::HomeControllerTests.createOk"] == [("POST", "/owners/new")]
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_parser_java.py::test_java_mockmvc_request_capture -v`
Expected: FAIL——`mockmvc_requests` 为空列表。

- [ ] **Step 3: 实现**

`parser.py`:

1. 常量与辅助(放在 `_call_target_for` 后):
```python
_MOCKMVC_BUILDERS = {"get", "post", "put", "delete", "patch", "head", "options"}


def _mockmvc_request(node, lang) -> tuple[str, str] | None:
    """From a mockMvc.perform(...) method_invocation, return (HTTP_METHOD, path)
    or None when the call isn't a MockMvc request."""
    obj = node.child_by_field_name(lang.get("call_object_field", "object"))
    if obj is None or obj.text.decode("utf-8") != "mockMvc":
        return None
    name_node = node.child_by_field_name(lang.get("call_name_field", "name"))
    if name_node is None or name_node.text.decode("utf-8") != "perform":
        return None
    args = node.child_by_field_name("arguments")
    builder = _find_builder_call(args) if args is not None else None
    if builder is None:
        return None
    builder_name = builder.child_by_field_name("name").text.decode("utf-8")
    path = _first_string_literal(builder)
    if path is None:
        return None
    return builder_name.upper(), path


def _find_builder_call(node) -> object | None:
    """First method_invocation in the subtree whose name is a MockMvc request
    builder (the root of a get/post/... chain, possibly wrapped in .param())."""
    if node is None:
        return None
    if node.type == "method_invocation":
        name_node = node.child_by_field_name("name")
        if name_node is not None and name_node.text.decode("utf-8") in _MOCKMVC_BUILDERS:
            return node
    for child in node.children:
        found = _find_builder_call(child)
        if found is not None:
            return found
    return None


def _first_string_literal(node) -> str | None:
    for literal in _collect_by_type(node, "string_literal"):
        return literal.text.decode("utf-8").strip("\"'")
    return None
```

2. `_walk_calls` 加 `mockmvc_requests` 参数并线程化:
```python
def _walk_calls(node, module_qname, cur_scope, lang, out,
                mockmvc_requests: list | None = None):
    for child in node.children:
        if child.type in lang["call_node"]:
            expr, form = _call_target_for(child, lang)
            if expr is not None:
                out.append(RawCall(
                    source_qname=cur_scope or module_qname,
                    target_expr=expr, call_form=form,
                    file_path="", call_line=child.start_point[0] + 1,
                ))
            if mockmvc_requests is not None and lang.get("mockmvc_capture"):
                request = _mockmvc_request(child, lang)
                if request is not None:
                    mockmvc_requests.append((cur_scope or module_qname, request))
        if _is_scope(child.type, lang):
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                name = name_node.text.decode("utf-8")
                new_scope = qname.join(module_qname, name, cur_scope)
                _walk_calls(child, module_qname, new_scope, lang, out, mockmvc_requests)
            else:
                _walk_calls(child, module_qname, cur_scope, lang, out, mockmvc_requests)
        elif lang.get("detect_arrow_in_vars") and child.type == "variable_declarator":
            _maybe_arrow_scope(child, module_qname, cur_scope, lang, out, mockmvc_requests)
        else:
            _walk_calls(child, module_qname, cur_scope, lang, out, mockmvc_requests)
```
`_maybe_arrow_scope` 同样加 `mockmvc_requests=None` 参数并传给 `_walk_calls`(两处调用)。

3. `parse_file`:
```python
    mockmvc_requests: list[tuple[str, tuple[str, str]]] = []
    _walk_calls(root, module_qname, None, lang, pf.raw_calls, mockmvc_requests)
```
收尾(在 raw_calls 批量填充后)把请求挂到节点:
```python
    mockmvc_map: dict[str, list[tuple[str, str]]] = {}
    for scope_qname, request in mockmvc_requests:
        mockmvc_map.setdefault(scope_qname, []).append(request)
    for n in pf.nodes:
        if n.qualified_name in mockmvc_map:
            n.mockmvc_requests = mockmvc_map[n.qualified_name]
```

- [ ] **Step 4: 运行测试通过 + 回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_parser_java.py tests/test_parser.py tests/test_parser_ts.py -q`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/parser.py tests/test_parser_java.py
git commit -m "feat(parser): capture MockMvc perform requests on Java methods"
```

---

### Task 3: `java_routing.py` + 接入 `resolve_edges`

**Files:**
- Create: `code_review_ai/java_routing.py`
- Modify: `code_review_ai/resolver.py`
- Test: `tests/test_java_routing.py`(新建)

**Interfaces:**
- Consumes: Task 1/2 的 `ParsedNode.mappings` / `.mockmvc_requests`;`resolver.Edge`。
- Produces:`build_route_edges(parsed_files, existing_qnames) -> list[Edge]`(合成 `kind="call"` 边);`_normalize_path(path) -> list[str]`;`_segments_match(test_segs, ctrl_segs) -> bool`。挂进 `resolve_edges`。

- [ ] **Step 1: 写失败测试**

`tests/test_java_routing.py`:
```python
"""Tests for Spring MockMvc route-edge synthesis."""
from code_review_ai.parser import parse_file
from code_review_ai.resolver import resolve_edges


def _routing_repo(tmp_path):
    ctrl = tmp_path / "HomeController.java"
    ctrl.write_text(
        "package com.example;\n"
        "class HomeController {\n"
        "    @GetMapping(\"/owners\")\n"
        "    public String list() { return null; }\n"
        "    @GetMapping(\"/owners/{ownerId}\")\n"
        "    public String show(int ownerId) { return null; }\n"
        "    @PostMapping(\"/owners\")\n"
        "    public String create() { return null; }\n"
        "}\n",
        encoding="utf-8",
    )
    test = tmp_path / "HomeControllerTests.java"
    test.write_text(
        "package com.example;\n"
        "class HomeControllerTests {\n"
        "    void listOk() { mockMvc.perform(get(\"/owners?page=1\")); }\n"
        "    void showOk() { mockMvc.perform(get(\"/owners/7\")); }\n"
        "    void createOk() { mockMvc.perform(post(\"/owners\")); }\n"
        "}\n",
        encoding="utf-8",
    )
    return [parse_file(str(ctrl), str(tmp_path)),
            parse_file(str(test), str(tmp_path))]


def test_route_edges_synthesized(tmp_path):
    files = _routing_repo(tmp_path)
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_edges(files, qnames)
    by = {(e.source, e.target, e.kind, e.resolution) for e in edges}
    assert ("com.example::HomeControllerTests.listOk",
            "com.example::HomeController.list", "call", "resolved") in by
    # {ownerId} 模板段匹配字面段 7
    assert ("com.example::HomeControllerTests.showOk",
            "com.example::HomeController.show", "call", "resolved") in by
    assert ("com.example::HomeControllerTests.createOk",
            "com.example::HomeController.create", "call", "resolved") in by


def test_path_normalization_and_mismatch(tmp_path):
    from code_review_ai.java_routing import _normalize_path, _segments_match
    assert _normalize_path("/owners?page=1") == ["owners"]
    assert _normalize_path("/owners#top") == ["owners"]
    assert _segments_match(["owners", "1"], ["owners", "{ownerId}"]) is True
    assert _segments_match(["owners"], ["owners", "new"]) is False
    assert _segments_match(["owners", "1", "edit"], ["owners", "1"]) is False
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_java_routing.py -v`
Expected: FAIL——`ModuleNotFoundError: code_review_ai.java_routing` / 边不存在。

- [ ] **Step 3: 实现**

`code_review_ai/java_routing.py`:
```python
"""Synthetic Spring MockMvc route edges.

Bridges the gap between MockMvc-based controller tests and controller methods:
a test method that performs ``get("/owners")`` is connected to the controller
method whose ``@GetMapping("/owners")`` matches, so a changed controller method
is reachable from its test in flow traversal. The edge carries kind='call' and
resolution='resolved' so flow_builder traverses it like any real call.
"""

from code_review_ai.parser import ParsedFile


def build_route_edges(parsed_files: list[ParsedFile],
                      existing_qnames: set[str]) -> list:
    """Match test MockMvc requests to controller mappings; emit synthetic
    resolved call edges test_method -> controller_method."""
    from code_review_ai.resolver import Edge  # lazy — avoid import cycle

    controllers = [(node.qualified_name, node.mappings)
                   for pf in parsed_files for node in pf.nodes if node.mappings]
    tests = [(node.qualified_name, node.mockmvc_requests, node.file_path)
             for pf in parsed_files for node in pf.nodes if node.mockmvc_requests]
    edges: list = []
    seen: set[tuple[str, str]] = set()
    for test_qn, requests, test_file in tests:
        for request_method, request_path in requests:
            test_segs = _normalize_path(request_path)
            for ctrl_qn, mappings in controllers:
                for ctrl_method, ctrl_path in mappings:
                    if ctrl_method != "ANY" and ctrl_method != request_method:
                        continue
                    if not _segments_match(test_segs, _normalize_path(ctrl_path)):
                        continue
                    key = (test_qn, ctrl_qn)
                    if key not in seen:
                        seen.add(key)
                        edges.append(Edge(
                            source=test_qn, target=ctrl_qn, kind="call",
                            file_path=test_file, call_line=0,
                            resolution="resolved" if ctrl_qn in existing_qnames
                            else "unresolved",
                        ))
                    break
    return edges


def _normalize_path(path: str) -> list[str]:
    """Strip query/fragment and split a URL path into non-empty segments."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    return [segment for segment in path.split("/") if segment]


def _segments_match(test_segs: list[str], ctrl_segs: list[str]) -> bool:
    if len(test_segs) != len(ctrl_segs):
        return False
    for test_segment, ctrl_segment in zip(test_segs, ctrl_segs):
        if test_segment == ctrl_segment:
            continue
        template = lambda segment: segment.startswith("{") and segment.endswith("}")
        if template(test_segment) or template(ctrl_segment):
            continue
        return False
    return True
```

`code_review_ai/resolver.py` 的 `resolve_edges` 末尾加:
```python
    from code_review_ai.java_routing import build_route_edges
    edges.extend(build_route_edges(parsed, existing_qnames))
```

- [ ] **Step 4: 运行测试通过 + 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/test_java_routing.py tests/test_resolver.py tests/test_resolver_java.py -q`
Expected: 全 PASS。再跑 `.venv/Scripts/python.exe -m pytest -q` 确认无回归。

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/java_routing.py code_review_ai/resolver.py tests/test_java_routing.py
git commit -m "feat(java): synthetic MockMvc route edges test->controller"
```

---

### Task 4: 端到端验证——PetClinic 索引里出现 route 边

**Files:**
- 无代码改动(验证用)

**Interfaces:**
- Consumes: Task 3 产物。

- [ ] **Step 1: 重建 PetClinic 一个 case 的索引并验证 route 边**

Run:
```bash
.venv/Scripts/python.exe -c "
import tempfile
from pathlib import Path
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
repo = Path('.benchmark-cache/repos/spring-projects__spring-petclinic')
cfg = load_config(str(repo))
cfg.repo_path = str(repo)
db = str(Path(tempfile.mkdtemp()) / 'route-check.db')
conn = connect(db); init_schema(conn)
rebuild(cfg, conn)
rows = conn.execute(\"SELECT source,target FROM edges WHERE kind='call' AND target LIKE '%Controller.%' AND source LIKE '%Tests%'\").fetchall()
print('route edges test->controller:', len(rows))
for r in rows[:8]: print(' ', r[0].split('::')[-1], '->', r[1].split('::')[-1])
"
```
Expected: 打印出若干 `...Tests.<method> -> ...Controller.<method>` 的 route 边(PetClinic 是 `src/test/java/...` 仓库,需先 `git checkout` 到某个 base_commit 或直接用当前 HEAD)。

- [ ] **Step 2: Commit(如无代码改动则跳过本步 commit,直接进 Task 5)**

---

### Task 5: 重跑 PetClinic 基准对比

**Files:**
- Modify: `benchmark-results/spring-petclinic-history-10.json`(结果落盘)
- Modify: `benchmarks/SPRING_PETCLINIC_RECALL_ANALYSIS.md`(更新结论)

**Interfaces:**
- Consumes: Task 1-3 全部。

- [ ] **Step 1: 重跑基准**

Run:
```bash
.venv/Scripts/python.exe scripts/run_swebench_suite.py \
  --cases benchmarks/spring-petclinic-history-10.json \
  --cache-dir .benchmark-cache --top-k 10 \
  --out benchmark-results/spring-petclinic-history-10.json
```

- [ ] **Step 2: 对比指标**

Run: `.venv/Scripts/python.exe -c "import json; d=json.load(open('benchmark-results/spring-petclinic-history-10.json')); a=d['aggregate']; print('recall_all', a['macro_test_file_recall_all']); print('recall_at_k', a['macro_test_file_recall_at_k'])"`
Expected:`macro_test_file_recall_all` > 0.2333(基线)。记录新旧数字、per-case `patch_file_recall_all`、resolved-call rate。

- [ ] **Step 3: 更新分析文档**

在 `benchmarks/SPRING_PETCLINIC_RECALL_ANALYSIS.md` 顶部或「结论」补一段「改进后结果」:新 Test Recall@All、零命中 case 变化、剩余未命中原因(如 40a41375e6 提交级噪声)。若 `recall_all` 无提升,如实记录并分析。

- [ ] **Step 4: Commit**

```bash
git add benchmark-results/spring-petclinic-history-10.json benchmarks/SPRING_PETCLINIC_RECALL_ANALYSIS.md
git commit -m "bench(java): re-run petclinic after MockMvc route edges"
```

---

### Task 6: 全量回归 + 设计文档实现备注

**Files:**
- Modify: `docs/superpowers/specs/2026-08-06-mockmvc-route-edges-design.md`(实现备注)

- [ ] **Step 1: 全量测试**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全 PASS。

- [ ] **Step 2: 实现备注**

在 spec 末尾补「实现备注」:注解实际嵌在 `modifiers` 子节点(非设计假设的直接子节点);`decorator_node` 改集合 + `annotations_in_modifiers`;基准前后数字。

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-08-06-mockmvc-route-edges-design.md
git commit -m "docs(spec): implementation notes for MockMvc route edges"
```

---

## Self-Review 备忘

- Spec 覆盖对照:`mappings`/`mockmvc_requests` 字段 → Task 1/2;`decorator_node` 集合 + modifiers 下钻 → Task 1;`_mockmvc_request` → Task 2;`build_route_edges` + 路径归一化 + `{param}` 匹配 + kind='call' → Task 3;基准验收 → Task 5;文档 → Task 6。非目标(类型绑定/DI/@Test 入口/非 Spring 路由)不涉及任务。
- 类型一致性:`_java_mappings` 返回 `list[tuple[str,str]]`;`_mockmvc_request` 返回 `tuple[str,str] | None`;`build_route_edges(parsed_files, existing_qnames) -> list[Edge]`;`ParsedNode.mappings`/`.mockmvc_requests` 默认空列表。
- 关键风险点:注解在 `modifiers` 里(已验证);`_walk_calls` 参数线程化需连 `_maybe_arrow_scope`;合成边 kind 必须 `call`。
