# MockMvc Route Edges + Java 注解捕获 Design

**日期**:2026-08-06
**范围**:改善 Spring PetClinic 基准的 Test Recall@All(基线 23.33%)——建 MockMvc HTTP 路由边把 Controller 测试连到 Controller 方法,并补 Java 注解捕获(含参数)。
**前置**:`benchmarks/SPRING_PETCLINIC_RECALL_ANALYSIS.md`(根因分析);`benchmarks/spring-petclinic-history-10.json`

## 目标

把 Controller 测试的 `mockMvc.perform(get("/path"))` 与 Controller 方法的路由注解 `@GetMapping("/path")` 在索引里连成 resolved 调用边,使「改动 Controller 方法 → 反向召回对应 *ControllerTests.java」成立。当前测试方法在 flow A、controller 方法在 flow B,两个 flow 不连通(无静态调用路径),导致 7/10 个 case 零命中。

预期:Test Recall@All 从 23.33% 提升到 ~70-80%;`40a41375e6`(提交级噪声)除外。

## ParsedNode 扩展

`ParsedNode` 加两个字段(默认空,Python/TS/JS 不受影响):

- `mappings: list[tuple[str, str]]` — controller 方法的路由 `(http_method, path)`。http_method 取 `"GET"`/`"POST"`/… 或 `"ANY"`(RequestMapping 未指定 method 时)。
- `mockmvc_requests: list[tuple[str, str]]` — 测试方法的请求 `(http_method, path)`。

## Parser 改动(`code_review_ai/parser.py`)

### 1. Java 注解捕获

`LANG["java"]` 加 `"decorator_node": {"marker_annotation", "annotation"}`;`_decorator_names` 改为接受单值或集合(现有 Python/TS/JS 的单值 `"decorator"` 保持兼容)。

`_walk_defs_typed` 创建方法节点时,调 `_java_mappings(node, lang)` 提取路由:

- `@GetMapping("/x")` → `("GET", "/x")`;`@PostMapping`/`@PutMapping`/`@DeleteMapping`/`@PatchMapping` 同理。
- `@RequestMapping` → 若参数含 `method = RequestMethod.GET` 等元素则取该值,否则 `("ANY", ...)`;路径取所有 string 参数(含 `{"/a","/b"}` 数组初始化形式)。
- 非 mapping 注解忽略。

辅助:
```python
_MAPPING_METHODS = {
    "RequestMapping": "ANY", "GetMapping": "GET", "PostMapping": "POST",
    "PutMapping": "PUT", "DeleteMapping": "DELETE", "PatchMapping": "PATCH",
}
def _java_mappings(node, lang) -> list[tuple[str, str]]  # 从注解子节点提取
def _annotation_strings(node) -> list[str]               # 参数里的 string_literal 值(含数组)
def _decorator_name(node) -> str                         # 注解名(复用/适配 Java name 字段)
```

### 2. MockMvc 请求捕获

`_walk_calls` Java 分支:遇到 `method_invocation` 的 `call_name_field` = `perform` 且 `call_object_field` 文本 == `mockMvc` 时,在其 `arguments` 子树找请求构建器调用(名 ∈ `{get, post, put, delete, patch, head, options}`,取链式调用的根),取首个 `string_literal` 参数作路径,记录 `(upper(name), path)` 到当前 scope。parse_file 收尾按 qualified_name 挂到方法节点。

## 路由匹配(`code_review_ai/java_routing.py`,新)

```python
def build_route_edges(parsed_files, existing_qnames) -> list[Edge]
```

1. 收集 controller 方法(有 `mappings`)与测试方法(有 `mockmvc_requests`)。
2. 归一化匹配:
   - `_normalize_path(path)`:去 `?query`/`#fragment`、按 `/` 分段、去空段。
   - `_segments_match(test_segs, ctrl_segs)`:段数相等;每段字面相等,或任一侧是 `{param}` 模板(`/owners/{id}` ↔ `/owners/1`)。
   - HTTP 方法:`test_method == ctrl_method` 或 `ctrl_method == "ANY"`。
3. 命中 → `Edge(source=test_qname, target=controller_qname, kind="call", resolution="resolved", file_path=test_file, call_line=0)`。
   - **kind 必须为 `"call"`** 才被 `_write_flows` 的 `call_edges` 过滤采纳,route 边才能参与 flow 遍历与 impact 反向召回。
   - `file_path` 取测试方法文件;`call_line=0`。

挂进 `resolve_edges`(非 Java 文件无 mappings/requests → 自然 no-op)。

## 边界与已知限制

- `40a41375e6`:OwnerRepository seed → PetValidatorTests gold 是真实提交的协同修改,与静态调用无关,救不了,预期仍 0%。
- 误配(测试路径命中非目标 controller)会轻微增加候选噪声、拉低 precision,但不影响 recall。
- 仅支持 Spring MockMvc 测试形态(`mockMvc.perform`);直接调用 controller 服务方法的测试原本就能解析,不受影响。

## 测试与验收

- `tests/test_parser_java.py` 追加:注解 → mappings;`mockMvc.perform(get("/owners?page=1"))` → 请求;链式 `post(...).param(...)`。
- `tests/test_resolver_java.py`(或新 `tests/test_java_routing.py`):`build_route_edges` 合成边、路径归一化、`{param}` 段匹配、HTTP 方法匹配、`ANY`。
- 夹具:新增小 controller + 测试的 Java 文件,断言 route 边存在且进 flow。
- **验收**:重跑 `scripts/run_swebench_suite.py --cases benchmarks/spring-petclinic-history-10.json` ,对比 `macro_test_file_recall_all`(基线 0.2333)与 per-case `patch_file_recall_all`。

## 实现备注(与设计文档的偏离)

- **Java 注解嵌在 `modifiers` 子节点里**,不是 def 的直接子节点也不是兄弟节点;decorator 提取(`_annotation_children`)需在 `lang.get("annotations_in_modifiers")` 时下钻 `modifiers`。`decorator_node` 由单值改为集合(`{"marker_annotation", "annotation"}`),`_decorator_types` 兼容旧单值配置。
- **`_decorator_name` 增加 `type_identifier` 匹配**(Java 注解名可能用 type_identifier)。
- **基准结果**:Test Recall@All 23.33% → **58.33%**(4 个零命中变非零,3 个到 100%)。详见 `benchmarks/SPRING_PETCLINIC_RECALL_ANALYSIS.md`「改进后结果」。
- `benchmark-results/` 被 .gitignore 忽略(生成物),只提交分析文档。

## 非目标(本次不做)

- Java 类型绑定(字段/参数/局部变量声明类型推断)——P1,后续单独做。
- Spring DI/Repository 接口实现边——P1。
- JUnit `@Test` 作为显式入口——当前 flow 已用「无 resolved 入边 = root」覆盖大部分。
- MockMvc 之外的其他框架路由(非 Spring)。
