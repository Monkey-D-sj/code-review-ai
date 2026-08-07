# 类级 @RequestMapping 前缀合并 Design

**日期**:2026-08-07
**范围**:把 Spring 类级 `@RequestMapping("/owners/{ownerId}")` 前缀合并进方法级 mapping 路径,修复 PetController 相关 case 的 route 匹配(Test Recall@All 目标从 65% 提升)。
**前置**:`benchmarks/SPRING_PETCLINIC_RECALL_ANALYSIS.md`;诊断见 Task D0——`e0db9b184e`/`50866def72` 的 PetController 用类级前缀 + 方法短路径,`_java_mappings` 只取方法级路径导致 route 边缺失。

## 背景

PetController 是 `@RequestMapping("/owners/{ownerId}")` 类级前缀 + 方法级 `@GetMapping("/pets/new")`,完整路由 `/owners/{ownerId}/pets/new`。当前 `_java_mappings` 只产出 `/pets/new`,与测试 `mockMvc.perform(get("/owners/{ownerId}/pets/new"))` 匹配失败 → PetController 相关 case 全部 0% 命中。

## Parser 改动(`code_review_ai/parser.py`)

把 mappings 计算从 `_walk_defs_typed` 挪到专用 Java pass(与 `_collect_java_var_types` 同模式,因需要类上下文):

1. 新增 `_collect_java_mappings(root, module_qname, lang) -> dict[method_qname, list[(http_method, path)]]`。
2. `_java_class_mappings(node, module_qname, lang, out)`:对每个 class——
   - 读类级 `@RequestMapping` 注解的字符串参数作为前缀列表(可能多个路径);
   - 对 body 里每个方法,`_java_mappings(member, lang)` 取方法级映射,若前缀存在则每个 (method, path) 拼接每个前缀。
3. `_join_mapping_path(prefix, path) -> str`:`prefix.rstrip("/") + path`(`/owners/{ownerId}` + `/pets/new` → `/owners/{ownerId}/pets/new`;prefix 为空或 `/` 时原样)。
4. `parse_file`(java)用结果覆盖 `ParsedNode.mappings`;**移除 `_walk_defs_typed` 里对 `_java_mappings` 的直接调用**(顺带消除类节点上可能的伪映射——类本身不产生方法路由)。

## 测试与验收

- `tests/test_parser_java.py`:类级 `@RequestMapping("/owners/{ownerId}")` + 方法 `@GetMapping("/pets/new")` → mappings 含 `("GET", "/owners/{ownerId}/pets/new")`。
- `tests/test_java_routing.py`:带类级前缀的 controller,测试请求 `/owners/1/pets/new` 命中完整路由。
- 既有 `test_java_annotations_capture_mappings`(类无前缀)与 OwnerController 相关 case 不受影响。
- **验收**:重跑 `scripts/run_swebench_suite.py --cases benchmarks/spring-petclinic-history-10.json`,对比 `macro_test_file_recall_all`(65%)与 `e0db9b184e`/`50866def72` per-case。

## 非目标

- 接口→实现方法派发边(诊断显示 PetClinic 无 Service 实现,低回报)。
- 1cad4124b7 的 ClinicServiceTests gold(co-change 噪声,静态不可达)。
