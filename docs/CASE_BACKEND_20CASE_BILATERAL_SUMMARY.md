# case-backend 20 用例评估总览：native vs core 双边轨迹

> 数据源：`.code-review-ai/full-agent-eval-cmp-20/report.json`（2026-08-26 夜跑，20 case × 2 mode × 1 rep = 40 run，
> workers=4，deepseek-v4-flash，uncapped budget，blind）。轨迹按 run 内工具序列压缩为 `序号. 工具 | 参数 | response=字数`。
> `grep` 列 = Bash 调用次数（本环境 grep/rg 是唯一读渠道外的检索）；`files` = 实际触碰文件数。

## 总览

| case | 难度 | native F1 | core F1 | native $ | core $ | n 工具 | c 工具 | n 文件 | c 文件 | n grep | c grep |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `case-backend-decrypt-password-alias` | hard | 1.00 | 1.00 | 0.13 | 0.15 | 16 | 13 | 10 | 6 | 5 | 1 |
| `case-backend-search-to-dict-between` | hard | 1.00 | 1.00 | 0.19 | 0.24 | 16 | 18 | 11 | 5 | 8 | 7 |
| `case3-execute-transfer-step-fail` | hard | 1.00 | 1.00 | 0.13 | 0.16 | 9 | 7 | 6 | 4 | 3 | 0 |
| `cb-medium-hierarchy-tree-orphan` | medium | 0.00 | 0.00 | 0.14 | 0.11 | 11 | 10 | 9 | 6 | 2 | 0 |
| `cb-medium-log-search-forwarded` | medium | 0.00 | 0.00 | 0.10 | 0.10 | 14 | 11 | 8 | 5 | 7 | 0 |
| `cb-medium-notice-create-unique` | medium | 0.00 | 0.00 | 0.19 | 0.13 | 20 | 13 | 20 | 8 | 13 | 2 |
| `cb-medium-notice-delete-existence` | medium | 0.00 | 0.00 | 0.11 | 0.12 | 14 | 8 | 6 | 4 | 8 | 1 |
| `cb-medium-storage-path-traversal` | medium | 1.00 | 1.00 | 0.16 | 0.15 | 16 | 15 | 14 | 8 | 4 | 2 |
| `cb-medium-workflow-node-code` | medium | 1.00 | 1.00 | 0.09 | 0.12 | 8 | 9 | 4 | 3 | 4 | 3 |
| `cb-recall-decrypt-password-exception` | hard | 1.00 | 1.00 | 0.12 | 0.15 | 15 | 14 | 7 | 6 | 5 | 1 |
| `cb-recall-encrypt-password` | medium | 1.00 | 1.00 | 0.10 | 0.10 | 8 | 11 | 4 | 4 | 4 | 0 |
| `cb-recall-file-get-source-default` | medium | 1.00 | 1.00 | 0.10 | 0.10 | 8 | 9 | 4 | 4 | 4 | 0 |
| `cb-recall-search-to-dict-fan-in` | hard | 0.00 | 0.00 | 0.29 | 0.45 | 20 | 24 | 14 | 8 | 8 | 6 |
| `cb-recall-transfer-build-config` | hard | 1.00 | 1.00 | 0.15 | 0.14 | 15 | 13 | 8 | 8 | 7 | 0 |
| `cb-trivial-dict-type-default-order` | trivial | 0.00 | 0.00 | 0.19 | 0.10 | 17 | 10 | 10 | 4 | 9 | 1 |
| `cb-trivial-hierarchy-parent-map` | trivial | 0.00 | 0.00 | 0.11 | 0.12 | 8 | 11 | 6 | 7 | 2 | 1 |
| `cb-trivial-notice-status-hardcoded` | trivial | 0.00 | 0.00 | 0.09 | 0.16 | 12 | 17 | 9 | 9 | 5 | 1 |
| `cb-trivial-position-page-offset` | trivial | 0.00 | 0.00 | 0.11 | 0.10 | 18 | 11 | 11 | 6 | 12 | 0 |
| `cb-trivial-transfer-dt-none-guard` | trivial | 1.00 | 1.00 | 0.15 | 0.16 | 11 | 10 | 7 | 6 | 5 | 0 |
| `cb-trivial-transfer-running-progress` | trivial | 0.00 | 0.00 | 0.12 | 0.11 | 15 | 11 | 2 | 3 | 13 | 4 |

**结果**：10/20 双 mode 均 F1=1.0；10/20 双 mode 均 F1=0.0（无单边胜出的 case）。
**F1=0 的全部是新增的 cb-recall / cb-trivial / cb-medium 族**，且双 mode 一致失败——不是 agent 能力差异，
而是 **gold 验收口径问题**：下面逐 case 归因显示，这 10 个 case 里 agent 在双 mode 都命中了正确 gold 文件（100%），
部分还给出了精确的 bug 诊断，但 finding 文本没能命中 `mechanism_terms`（多数 0-1/3-5，`min_matches=2` 够不到）。

### F1=0 的 10 个 case：agent 全命中正确文件，卡在 mechanism_terms

| case | mode | preds | 命中 gold 文件 | terms 命中 | 需 | gold terms |
|---|---|---|---|---|---|---|
| `cb-medium-hierarchy-tree-orphan` | n/c | 1/1 | 1/1 | 0/0 | 2 | get_dept_tree_controller、get_menu_tree_controller、get_current_user_info_controller |
| `cb-medium-log-search-forwarded` | n/c | 1/1 | 1/1 | 1/1 | 2 | get_log_list_controller、delete_log_controller、export_operation_log_list_controller |
| `cb-medium-notice-create-unique` | n/c | 1/1 | 1/1 | 1/1 | 2 | create_notice_controller、update_notice_controller、delete_notice_controller |
| `cb-medium-notice-delete-existence` | n/c | 1/2 | 1/2 | 0/1 | 2 | delete_notice_controller、get_notice_detail_controller、get_notice_list_controller |
| `cb-recall-search-to-dict-fan-in` | n/c | 1/1 | 1/1 | 0/0 | 2 | StorageSourceService、DictTypeService、DictDataService、RoleService、StorageTransferService |
| `cb-trivial-dict-type-default-order` | n/c | 1/1 | 1/1 | 0/1 | 2 | get_type_list_controller、get_data_list_controller、batch_set_available_dict_type_controller |
| `cb-trivial-hierarchy-parent-map` | n/c | 1/1 | 1/1 | 1/1 | 2 | batch_set_available、enable_ids、get_child_recursion |
| `cb-trivial-notice-status-hardcoded` | n/c | 1/1 | 1/1 | 0/1 | 2 | batch_set_available_notice_controller、get_notice_list_available_controller、delete_notice_controller |
| `cb-trivial-position-page-offset` | n/c | 1/1 | 1/1 | 0/1 | 2 | get_obj_list_controller、get_options、get_list |
| `cb-trivial-transfer-running-progress` | n/c | 1/1 | 1/1 | 0/0 | 2 | _step_payload、transfer_ws_manager、execute_transfer_task |

> 抽查 finding 文本（如 `cb-trivial-hierarchy-parent-map` 的 "mapping inverted, breaking ancestor recursion"、
> `cb-trivial-position-page-offset` 的 "pagination offset… page shift"、`cb-recall-search-to-dict-fan-in` 的 exclude_none 反了）
> 都精确描述了对的 bug。gold 的 terms 大多要求 finding 点名下游 controller/entry 名（如 `get_dept_tree_controller`、
> `batch_set_available`），但 agent 的 finding 只描述变更符号的契约破坏——这两者不一致，是 terms 口径问题。
> **修法建议**：要么把这 10 个 case 的 `mechanism_terms` 换成变更符号名/契约词（如 `get_parent_id_map`、`page_size`、`exclude_none`），
> 要么 `min_matches` 降为 1，或对命中正确文件+正确行的 finding 放行 terms。

## 逐 case 详情 + 双边轨迹

### 1. `case-backend-decrypt-password-alias`（hard）

- **变更符号**：app.api.v1.module_storage.core.encrypt::decrypt_password
- **bug**：`decrypt_password` 误用 `_get_fernet().encrypt()` 替代 `decrypt()`：解密被二次加密，存储源密码链路全坏
- **gold**：`app/api/v1/module_storage/core/encrypt.py`；terms(2): _resolve_storage_config, decrypt_storage_password, builtin_nodes, module_task

**native**：F1=1.0（P=1.0 R=1.0）· $0.130 · 16 calls · 10 files · 5 grep · 11 Read · 0 MCP · 49.0s · unknown_access=True

```text
01. Read | {"file_path":"app/api/v1/module_storage/core/encrypt.py"} | response=1128 chars
02. Bash | {"command":"rg -n \"decrypt_password|_get_fernet|def encrypt_password|def decrypt_password\" --no-ignore -g '!*.pyc'"… | response=1740 chars
03. Read | {"file_path":"tests/test_eval_storage_regressions.py"} | response=8149 chars
04. Read | {"file_path":"app/api/v1/module_storage/source/service.py","offset":1,"limit":170} | response=7288 chars
05. Read | {"file_path":"app/api/v1/module_storage/transfer/engine.py","offset":80,"limit":40} | response=1461 chars
06. Read | {"file_path":"app/api/v1/module_storage/file/service.py","offset":30,"limit":40} | response=1713 chars
07. Read | {"file_path":"app/api/v1/module_task/workflow/flows/handlers/builtin_nodes.py","offset":1,"limit":80} | response=2302 chars
08. Bash | {"command":"rg -n \"decrypt_storage_password\" --no-ignore","description":"Find decrypt_storage_password usages"} | response=290 chars
09. Bash | {"command":"rg -n \"StorageSourceService|_build_config|execute_transfer_task|StorageFileService|get_active_source|tes… | response=4552 chars
10. Read | {"file_path":"app/api/v1/module_task/workflow/flows/handlers/builtin_nodes.py","offset":95,"limit":60} | response=2476 chars
11. Read | {"file_path":"app/api/v1/module_storage/source/controller.py"} | response=5362 chars
12. Read | {"file_path":"app/api/v1/module_storage/file/controller.py"} | response=5360 chars
13. Read | {"file_path":"app/api/v1/module_storage/transfer/service.py"} | response=8024 chars
14. Bash | {"command":"rg -n \"_resolve_storage_config|storage_download|storage_delete|storage_url\" app/api/v1/module_task/work… | response=1534 chars
15. Bash | {"command":"rg -n \"StorageTransferService|/transfer|prefix=\" app/api/v1/module_storage/transfer/controller.py","des… | response=609 chars
16. Read | {"file_path":"app/api/v1/module_storage/transfer/controller.py","offset":19,"limit":35} | response=2137 chars
```

**core**：F1=1.0（P=1.0 R=1.0）· $0.151 · 13 calls · 6 files · 1 grep · 8 Read · 4 MCP · 64.4s · unknown_access=True

```text
01. MCP get_change_summary | {"files":["app/api/v1/module_storage/core/encrypt.py"]} | response=712 chars
02. MCP get_impact | {"files":["app/api/v1/module_storage/core/encrypt.py"]} | response=8748 chars
03. Read | {"file_path":"app/api/v1/module_storage/core/encrypt.py"} | response=1128 chars
04. MCP search_symbol | {"query":"decrypt_storage_password"} | response=15 chars
05. Bash | {"command":"rg -n \"decrypt_storage_password|decrypt_password|encrypt_password\" app/","description":"Find all usages… | response=1482 chars
06. Read | {"file_path":"app/api/v1/module_task/workflow/flows/handlers/builtin_nodes.py","offset":1,"limit":140} | response=4402 chars
07. MCP get_test_impact | {"files":["app/api/v1/module_storage/core/encrypt.py"]} | response=2262 chars
08. Read | {"file_path":"app/api/v1/module_storage/file/service.py","limit":75,"offset":1} | response=2923 chars
09. Read | {"file_path":"app/api/v1/module_storage/source/service.py","limit":160,"offset":1} | response=6966 chars
10. Read | {"file_path":"app/api/v1/module_storage/source/service.py","limit":40,"offset":160} | response=1010 chars
11. Read | {"file_path":"app/api/v1/module_storage/transfer/engine.py","limit":130,"offset":75} | response=5374 chars
12. Read | {"file_path":"tests/test_eval_storage_regressions.py","limit":140,"offset":50} | response=4245 chars
13. Read | {"file_path":"app/api/v1/module_storage/file/service.py","limit":130,"offset":73} | response=5566 chars
```
> **注（今天 08-27 新代码重跑）**：昨晚该 case 是别名索引落地前——core `search_symbol "decrypt_storage_password"` 返回 15 字符（0 命中）
> 才触发那 1 次 `rg`。今天实现 import 别名索引 + get_impact `aliases`/`via`/`level` 后重跑 core：**0 grep**，
> 7 calls、3 files、$0.289，`get_impact` 的 `aliases` 直接带 `decrypt_storage_password@builtin_nodes.py:27`。
> 见 `report-alias-core-replay.json` 与 `report-alias-core-replay-routes.md`。

> 点评：core grep 5→1；文件 少读 4 个；core 用 MCP 4 次（get_impact 主通道）；成本 高 $0.022。

---

### 2. `case-backend-search-to-dict-between`（hard）

- **变更符号**：app.utils.common_util::search_to_dict
- **bug**：`search_to_dict` 删除 `_time` 数组→`("between",[start,end])` 的包装：带时间范围数组参数的查询不再转换
- **gold**：`app/utils/common_util.py`；terms(2): BaseQueryParam, LoginLogService, OperationLogService, base_schema

**native**：F1=1.0（P=1.0 R=1.0）· $0.191 · 16 calls · 11 files · 8 grep · 8 Read · 0 MCP · 80.3s · unknown_access=True

```text
01. Read | {"file_path":"app/utils/common_util.py"} | response=14533 chars
02. Bash | {"command":"rg -n \"search_to_dict\" --glob '!*.pyc' .","description":"Find all references to search_to_dict"} | response=7179 chars
03. Read | {"file_path":"app/core/base_schema.py","offset":140,"limit":60} | response=2268 chars
04. Read | {"file_path":"app/core/base_crud.py","offset":280,"limit":80} | response=3456 chars
05. Read | {"file_path":"tests/test_eval_storage_regressions.py","offset":1,"limit":80} | response=2541 chars
06. Read | {"file_path":"app/core/base_crud.py","limit":60,"offset":358} | response=2529 chars
07. Bash | {"command":"rg -n \"BaseQueryParam|created_time|updated_time\" app/api app/core --glob '*.py' | rg -v \"base_schema.p… | response=10583 chars
08. Bash | {"command":"rg -n \"created_time|updated_time|between\" tests --glob '*.py' | head -60","description":"Search tests f… | response=299 chars
09. Read | {"file_path":"app/api/v1/module_storage/source/service.py","limit":50,"offset":60} | response=2220 chars
10. Bash | {"command":"rg -n \"def page|def get_list|search:\" app/core/base_crud.py | head -40","description":"Locate page and … | response=151 chars
11. Bash | {"command":"rg -n \"StorageSourceQueryParam|TransferTaskQueryParam|QueryParam\" app/api/v1/module_storage --glob '*.p… | response=2369 chars
12. Read | {"file_path":"app/core/base_crud.py","limit":70,"offset":110} | response=3249 chars
13. Bash | {"command":"ls tests && rg -n \"\\.page\\(|get_list\\(|/page|/list\" tests --glob '*.py' | head -40","description":"L… | response=1657 chars
14. Read | {"file_path":"tests/test_eval_system_regressions.py","limit":130,"offset":1} | response=4309 chars
15. Bash | {"command":"rg -n \"Router\\(|@router|prefix=|/list|/page\" app/api/v1/module_storage/source/controller.py app/api/v1… | response=791 chars
16. Bash | {"command":"rg -n \"include_router|StorageSourceRouter|UserRouter|prefix=\" app/api/v1/module_storage/__init__.py app… | response=1729 chars
```

**core**：F1=1.0（P=1.0 R=1.0）· $0.243 · 18 calls · 5 files · 7 grep · 6 Read · 5 MCP · 90.1s · unknown_access=True

```text
01. MCP get_change_summary | {} | response=841 chars
02. Read | {"file_path":"app/utils/common_util.py"} | response=14533 chars
03. MCP get_impact | {"symbols":["app.utils.common_util::search_to_dict"],"include_signatures":true} | response=18857 chars
04. MCP get_test_impact | {"symbols":["app.utils.common_util::search_to_dict"]} | response=2048 chars
05. MCP search_symbol | {"query":"search_to_dict"} | response=7444 chars
06. Bash | {"command":"rg -n \"between\" app --glob '*.py' | head -80","description":"Search for 'between' in app Python files"} | response=652 chars
07. Read | {"file_path":"app/core/base_crud.py","offset":300,"limit":120} | response=5092 chars
08. Read | {"file_path":"app/core/base_schema.py","offset":130,"limit":80} | response=3151 chars
09. Bash | {"command":"rg -n \"created_time|updated_time\" app --glob '*.py' -l | head -60","description":"List files referencin… | response=1040 chars
10. Read | {"file_path":"tests/test_eval_storage_regressions.py","offset":40,"limit":30} | response=866 chars
11. Read | {"file_path":"app/core/base_crud.py","limit":130,"offset":120} | response=6052 chars
12. Bash | {"command":"rg -n '\"q\"\\s*:' app --glob '*.py' | rg 'between' | head -40","description":"Search for json_schema_ext… | response=31 chars
13. Bash | {"command":"rg -n \"class .*QueryParam\" app --glob '*.py' | head -60","description":"List query param class definiti… | response=2446 chars
14. Bash | {"command":"rg -n \"created_time|updated_time\" app/core/base_model.py | head -20","description":"Check base model ti… | response=197 chars
15. Bash | {"command":"rg -n \"search_to_dict\" app --glob '*.py' | head -60","description":"List all search_to_dict call sites … | response=6250 chars
16. Bash | {"command":"rg -n \"_time\\s*:\" app --glob '*schema.py' | head -40","description":"Find _time fields in schema files"} | response=2686 chars
17. Read | {"file_path":"app/api/v1/module_system/log/schema.py","offset":40,"limit":30} | response=1202 chars
18. MCP get_symbol_detail | {"qualified_name":"app.core.base_crud::CRUDBase._build_conditions"} | response=549 chars
```
> 点评：core grep 8→7；文件 少读 6 个；core 用 MCP 5 次（get_impact 主通道）；成本 高 $0.052。

---

### 3. `case3-execute-transfer-step-fail`（hard）

- **变更符号**：app.api.v1.module_storage.transfer.engine::execute_transfer_task
- **bug**：transfer `execute_transfer_task` 失败分支 `break`→`continue`：单步失败后继续执行后续步骤，不再终止
- **gold**：`app/api/v1/module_storage/transfer/engine.py`；terms(2): StorageTransferService, _launch, create_task, asyncio

**native**：F1=1.0（P=1.0 R=1.0）· $0.134 · 9 calls · 6 files · 3 grep · 6 Read · 0 MCP · 67.1s · unknown_access=True

```text
01. Read | {"file_path":"app/api/v1/module_storage/transfer/engine.py"} | response=10596 chars
02. Bash | {"command":"rg -n \"execute_transfer_task|transfer_task_registry\" --type py","description":"Search for execute_trans… | response=1508 chars
03. Bash | {"command":"ls app/api/v1/module_storage/transfer/","description":"List transfer module directory"} | response=99 chars
04. Read | {"file_path":"app/api/v1/module_storage/transfer/service.py"} | response=8024 chars
05. Read | {"file_path":"app/api/v1/module_storage/transfer/registry.py"} | response=569 chars
06. Read | {"file_path":"tests/test_eval_storage_regressions.py"} | response=8149 chars
07. Read | {"file_path":"app/api/v1/module_storage/transfer/controller.py"} | response=6106 chars
08. Read | {"file_path":"app/api/v1/module_storage/transfer/schema.py"} | response=4276 chars
09. Bash | {"command":"rg -n \"stops_after_first_failed_step|execute_transfer_task|continue|break\" tests/ app/api/v1/module_sto… | response=744 chars
```

**core**：F1=1.0（P=1.0 R=1.0）· $0.158 · 7 calls · 4 files · 0 grep · 4 Read · 3 MCP · 78.8s

```text
01. MCP get_change_summary | {} | response=601 chars
02. Read | {"file_path":"app/api/v1/module_storage/transfer/engine.py"} | response=10596 chars
03. MCP get_impact | {"symbols":["app.api.v1.module_storage.transfer.engine::execute_transfer_task"],"include_call_sites":true} | response=9956 chars
04. MCP get_test_impact | {"symbols":["app.api.v1.module_storage.transfer.engine::execute_transfer_task"]} | response=784 chars
05. Read | {"file_path":"tests/test_eval_storage_regressions.py"} | response=8149 chars
06. Read | {"file_path":"app/api/v1/module_storage/transfer/controller.py"} | response=6106 chars
07. Read | {"file_path":"app/api/v1/module_storage/transfer/service.py"} | response=8024 chars
```
> 点评：core grep 3→0；文件 少读 2 个；core 用 MCP 3 次（get_impact 主通道）；成本 高 $0.024。

---

### 4. `cb-medium-hierarchy-tree-orphan`（medium）

- **变更符号**：app.utils.common_util::traversal_to_tree
- **bug**：`traversal_to_tree` 孤儿分支 `tree.append(node)`→`pass`：悬空节点被静默丢弃
- **gold**：`app/utils/common_util.py`；terms(2): get_dept_tree_controller, get_menu_tree_controller, get_current_user_info_controller

**native**：F1=0.0（P=0.0 R=0.0）· $0.139 · 11 calls · 9 files · 2 grep · 9 Read · 0 MCP · 63.5s · unknown_access=True

```text
01. Read | {"file_path":"app/utils/common_util.py"} | response=14728 chars
02. Bash | {"command":"rg -n \"traversal_to_tree\" C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-20/… | response=2381 chars
03. Read | {"file_path":"tests/test_eval_system_regressions.py"} | response=5745 chars
04. Read | {"file_path":"app/api/v1/module_system/dept/service.py","offset":1,"limit":70} | response=2623 chars
05. Read | {"file_path":"app/api/v1/module_system/menu/service.py","offset":1,"limit":100} | response=3985 chars
06. Read | {"file_path":"app/api/v1/module_system/user/service.py","offset":150,"limit":70} | response=3381 chars
07. Bash | {"command":"rg -n \"\\.tree|current_info\" C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-… | response=1572 chars
08. Read | {"file_path":"app/api/v1/module_system/dept/controller.py"} | response=3831 chars
09. Read | {"file_path":"app/api/v1/module_system/menu/controller.py"} | response=3827 chars
10. Read | {"file_path":"app/api/v1/module_system/user/controller.py","limit":60} | response=2789 chars
11. Read | {"file_path":"app/api/v1/module_system/user/service.py","limit":30,"offset":205} | response=1399 chars
```

**core**：F1=0.0（P=0.0 R=0.0）· $0.112 · 10 calls · 6 files · 0 grep · 6 Read · 4 MCP · 53.4s

```text
01. MCP get_change_summary | {} | response=649 chars
02. Read | {"file_path":"app/utils/common_util.py","offset":180,"limit":80} | response=2477 chars
03. MCP get_impact | {"symbols":["app.utils.common_util::traversal_to_tree"]} | response=3851 chars
04. MCP get_change_context | {"symbols":["app.utils.common_util::traversal_to_tree"]} | response=2726 chars
05. MCP get_test_impact | {"symbols":["app.utils.common_util::traversal_to_tree"]} | response=715 chars
06. Read | {"file_path":"app/api/v1/module_system/dept/service.py","offset":30,"limit":40} | response=1922 chars
07. Read | {"file_path":"app/api/v1/module_system/menu/service.py","offset":60,"limit":35} | response=1603 chars
08. Read | {"file_path":"tests/test_eval_system_regressions.py"} | response=5745 chars
09. Read | {"file_path":"app/api/v1/module_system/user/service.py","limit":70,"offset":160} | response=3331 chars
10. Read | {"file_path":"app/api/v1/module_system/user/controller.py","limit":45,"offset":15} | response=2094 chars
```
> 点评：双失败但 agent 命中正确文件+正确 bug，terms 只中 0/2（gold 口径问题）。

---

### 5. `cb-medium-log-search-forwarded`（medium）

- **变更符号**：app.api.v1.module_system.log.service::LoginLogService.page
- **bug**：`LoginLogService.page` 把 `search=search_to_dict(search)` 改成 `search=None`：查询条件被丢弃
- **gold**：`app/api/v1/module_system/log/service.py`；terms(2): get_log_list_controller, delete_log_controller, export_operation_log_list_controller

**native**：F1=0.0（P=0.0 R=0.0）· $0.103 · 14 calls · 8 files · 7 grep · 7 Read · 0 MCP · 45.1s · unknown_access=True

```text
01. Read | {"file_path":"app/api/v1/module_system/log/service.py"} | response=5775 chars
02. Bash | {"command":"git status --short","description":"Show working tree status"} | response=42 chars
03. Bash | {"command":"rg -n \"LoginLogService|login_log\" --glob \"*.py\" app | head -50","description":"Find callers of LoginL… | response=1952 chars
04. Bash | {"command":"rg -n \"search_to_dict\" --glob \"*.py\" app","description":"Find search_to_dict definition and usage"} | response=6166 chars
05. Read | {"file_path":"app/api/v1/module_system/log/controller.py"} | response=5516 chars
06. Read | {"file_path":"app/api/v1/module_system/log/schema.py"} | response=4481 chars
07. Read | {"file_path":"app/api/v1/module_system/log/crud.py"} | response=780 chars
08. Read | {"file_path":"app/utils/common_util.py","offset":80,"limit":50} | response=1604 chars
09. Bash | {"command":"rg -n \"async def page|def page\" app/core/base_crud.py","description":"Find page method in base_crud"} | response=23 chars
10. Read | {"file_path":"app/core/base_crud.py","offset":136,"limit":80} | response=3653 chars
11. Bash | {"command":"rg -n \"login/list|LoginLogService|login_log|LoginLogQueryParam\" --glob \"*.py\" tests app 2>/dev/null |… | response=1081 chars
12. Bash | {"command":"ls tests 2>/dev/null || echo \"no tests dir\"","description":"List tests directory"} | response=115 chars
13. Read | {"file_path":"tests/test_eval_system_regressions.py","offset":80,"limit":40} | response=1248 chars
14. Bash | {"command":"rg -n \"\\.page\\(\" app --glob \"*.py\" | rg -i \"LoginLog|login\" | head -20","description":"Find all c… | response=205 chars
```

**core**：F1=0.0（P=0.0 R=0.0）· $0.097 · 11 calls · 5 files · 0 grep · 6 Read · 5 MCP · 35.9s

```text
01. MCP get_change_summary | {} | response=899 chars
02. Read | {"file_path":"app/api/v1/module_system/log/service.py"} | response=5775 chars
03. MCP get_impact | {"symbols":["app.api.v1.module_system.log.service::LoginLogService.page"]} | response=2026 chars
04. MCP get_change_context | {"symbols":["app.api.v1.module_system.log.service::LoginLogService.page"]} | response=1162 chars
05. Read | {"file_path":"app/api/v1/module_system/log/controller.py"} | response=5516 chars
06. Read | {"file_path":"app/api/v1/module_system/log/crud.py"} | response=780 chars
07. MCP get_test_impact | {"symbols":["app.api.v1.module_system.log.service::LoginLogService.page"]} | response=756 chars
08. MCP search_symbol | {"query":"search_to_dict"} | response=906 chars
09. Read | {"file_path":"app/utils/common_util.py","offset":86,"limit":35} | response=1252 chars
10. Read | {"file_path":"tests/test_eval_system_regressions.py","offset":80,"limit":40} | response=1248 chars
11. Read | {"file_path":"app/api/v1/module_system/log/service.py","offset":38,"limit":14} | response=544 chars
```
> 点评：双失败但 agent 命中正确文件+正确 bug，terms 只中 1/2（gold 口径问题）。

---

### 6. `cb-medium-notice-create-unique`（medium）

- **变更符号**：app.api.v1.module_system.notice.service::NoticeService.create
- **bug**：`NoticeService.create` 删掉标题重复检查：重复标题可重复创建
- **gold**：`app/api/v1/module_system/notice/service.py`；terms(2): create_notice_controller, update_notice_controller, delete_notice_controller

**native**：F1=0.0（P=0.0 R=0.0）· $0.185 · 20 calls · 20 files · 13 grep · 7 Read · 0 MCP · 82.9s · unknown_access=True

```text
01. Bash | {"command":"pwd && ls -la","description":"Show current directory and list files"} | response=1117 chars
02. Read | {"file_path":"app/api/v1/module_system/notice/service.py"} | response=5406 chars
03. Bash | {"command":"ls -la app/api/v1/module_system/notice/","description":"List files in notice module directory"} | response=429 chars
04. Read | {"file_path":"app/api/v1/module_system/notice/crud.py"} | response=490 chars
05. Read | {"file_path":"app/api/v1/module_system/notice/schema.py"} | response=2431 chars
06. Read | {"file_path":"app/api/v1/module_system/notice/controller.py"} | response=4537 chars
07. Read | {"file_path":"app/api/v1/module_system/notice/model.py"} | response=864 chars
08. Bash | {"command":"rg -rn \"notice\" tests/ --iglob \"*.py\" -l 2>/dev/null; echo \"---\"; find tests -type f -name \"*.py\"… | response=219 chars
09. Bash | {"command":"rg -n \"notice\" tests/test_api_module_system.py tests/test_eval_system_regressions.py tests/test_eval_st… | response=2712 chars
10. Bash | {"command":"rg -rn \"sys_notice\" sql/ app/ --iglob \"*.sql\" --iglob \"*.py\" 2>/dev/null; echo \"---migrations---\"… | response=86 chars
11. Read | {"file_path":"tests/test_eval_system_regressions.py"} | response=5745 chars
12. Bash | {"command":"find app -name \"base_crud.py\" -o -name \"base_model.py\" | head; echo \"---\"; cat app/core/base_crud.p… | response=19510 chars
13. Bash | {"command":"ls -la sql/; echo \"---\"; rg -rn \"notice_title|sys_notice\" sql/ 2>/dev/null; echo \"---unique---\"; rg… | response=334 chars
14. Bash | {"command":"rg -rn \"NoticeService\" app/ tests/ --iglob \"*.py\" | rg -v \"service.py\" ; echo \"=== create callers … | response=1487 chars
15. Bash | {"command":"rg -rn \"NoticeService\" app/ tests/ --iglob \"*.py\"","description":"Find all NoticeService usages"} | response=1194 chars
16. Bash | {"command":"rg -rn \"NoticeCRUD\" app/ tests/ --iglob \"*.py\"","description":"Find all NoticeCRUD usages"} | response=2191 chars
17. Bash | {"command":"grep -rn \"NoticeCRUD\\|NoticeService\" app/api/v1/module_task/workflow/flows/handlers/builtin_nodes.py",… | response=130 chars
18. Read | {"file_path":"app/api/v1/module_task/workflow/flows/handlers/builtin_nodes.py","offset":250,"limit":60} | response=2210 chars
19. Bash | {"command":"grep -rn \"unique\\|unique=True\\|UniqueConstraint\" app/api/v1/module_system/notice/ sql/data/ 2>/dev/nu… | response=1188 chars
20. Bash | {"command":"grep -rn \"module_system\\|system\" app/api/__init__.py app/api/v1/__init__.py main.py 2>/dev/null | head… | response=841 chars
```

**core**：F1=0.0（P=0.0 R=0.0）· $0.126 · 13 calls · 8 files · 2 grep · 7 Read · 4 MCP · 49.9s · unknown_access=True

```text
01. MCP get_change_summary | {"files":["app/api/v1/module_system/notice/service.py"]} | response=1253 chars
02. MCP get_impact | {"files":["app/api/v1/module_system/notice/service.py"]} | response=2638 chars
03. Read | {"file_path":"app/api/v1/module_system/notice/service.py"} | response=5406 chars
04. Read | {"file_path":"app/api/v1/module_system/notice/crud.py"} | response=490 chars
05. Read | {"file_path":"app/api/v1/module_system/notice/controller.py"} | response=4537 chars
06. Bash | {"command":"ls \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-20/worktrees/cb-medium-not… | response=63 chars
07. Read | {"file_path":"app/api/v1/module_system/notice/schema.py"} | response=2431 chars
08. Read | {"file_path":"app/api/v1/module_system/notice/model.py"} | response=864 chars
09. Read | {"file_path":"app/core/base_crud.py"} | response=21242 chars
10. MCP get_test_impact | {"files":["app/api/v1/module_system/notice/service.py"]} | response=761 chars
11. MCP get_change_context | {"symbols":["app.api.v1.module_system.notice.service::NoticeService.create"]} | response=1171 chars
12. Read | {"file_path":"tests/test_eval_system_regressions.py","offset":100,"limit":80} | response=2445 chars
13. Bash | {"command":"rg -n \"sys_notice|unique\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-2… | response=11050 chars
```
> 点评：双失败但 agent 命中正确文件+正确 bug，terms 只中 1/2（gold 口径问题）。

---

### 7. `cb-medium-notice-delete-existence`（medium）

- **变更符号**：app.api.v1.module_system.notice.service::NoticeService.delete
- **bug**：`NoticeService.delete` 删掉「ID 不存在则报错」检查：删除对不存在 ID 静默
- **gold**：`app/api/v1/module_system/notice/service.py`；terms(2): delete_notice_controller, get_notice_detail_controller, get_notice_list_controller

**native**：F1=0.0（P=0.0 R=0.0）· $0.111 · 14 calls · 6 files · 8 grep · 6 Read · 0 MCP · 53.7s · unknown_access=True

```text
01. Read | {"file_path":"app/api/v1/module_system/notice/service.py"} | response=5449 chars
02. Bash | {"command":"rg -n \"delete\\(\" \"C:\\Users\\44550\\Desktop\\code-review-ai\\.code-review-ai\\full-agent-eval-cmp-20\… | response=960 chars
03. Read | {"file_path":"app/api/v1/module_system/notice/crud.py"} | response=490 chars
04. Read | {"file_path":"app/api/v1/module_system/notice/controller.py"} | response=4537 chars
05. Bash | {"command":"rg -n \"async def delete\" \"C:\\Users\\44550\\Desktop\\code-review-ai\\.code-review-ai\\full-agent-eval-… | response=55 chars
06. Read | {"file_path":"app/core/base_crud.py","offset":240,"limit":60} | response=2756 chars
07. Bash | {"command":"rg -rn \"ɾ��ʧ�ܣ������ݲ�����|notice/delete|NoticeService|NoticeCRUD|\\.delete\\(ids\" \"C:\\Users\\44550\\… | response=856 chars
08. Bash | {"command":"rg -rn \"notice\" \"C:\\Users\\44550\\Desktop\\code-review-ai\\.code-review-ai\\full-agent-eval-cmp-20\\w… | response=329 chars
09. Bash | {"command":"rg -n \"notice|ɾ��\" \"C:\\Users\\44550\\Desktop\\code-review-ai\\.code-review-ai\\full-agent-eval-cmp-20… | response=1280 chars
10. Bash | {"command":"rg -n \"notice|ɾ��ʧ��|delete\" \"C:\\Users\\44550\\Desktop\\code-review-ai\\.code-review-ai\\full-agent-e… | response=939 chars
11. Read | {"file_path":"tests/test_eval_system_regressions.py","offset":100,"limit":90} | response=2445 chars
12. Bash | {"command":"rg -n \"async def get_list\" \"C:\\Users\\44550\\Desktop\\code-review-ai\\.code-review-ai\\full-agent-eva… | response=27 chars
13. Read | {"file_path":"app/core/base_crud.py","limit":45,"offset":114} | response=2093 chars
14. Bash | {"command":"rg -rn \"module_system.notice.controller|notice_router|NoticeRouter|/system/notice\" \"C:\\Users\\44550\\… | response=2706 chars
```

**core**：F1=0.0（P=0.0 R=0.0）· $0.119 · 8 calls · 4 files · 1 grep · 4 Read · 3 MCP · 62.5s · unknown_access=True

```text
01. Read | {"file_path":"app/api/v1/module_system/notice/service.py"} | response=5449 chars
02. MCP get_change_summary | {} | response=1173 chars
03. MCP get_impact | {"symbols":["app.api.v1.module_system.notice.service::NoticeService.delete"]} | response=3157 chars
04. MCP get_test_impact | {"symbols":["app.api.v1.module_system.notice.service::NoticeService.delete"]} | response=756 chars
05. Read | {"file_path":"tests/test_eval_system_regressions.py","offset":140,"limit":40} | response=1105 chars
06. Read | {"file_path":"app/api/v1/module_system/notice/controller.py","offset":55,"limit":30} | response=1461 chars
07. Read | {"file_path":"app/api/v1/module_system/notice/crud.py"} | response=490 chars
08. Bash | {"command":"rg -n \"NoticeRouter|APIRouter|prefix\" \"C:\\Users\\44550\\Desktop\\code-review-ai\\.code-review-ai\\ful… | response=9851 chars
```
> 点评：双失败但 agent 命中正确文件+正确 bug，terms 只中 0/2（gold 口径问题）。

---

### 8. `cb-medium-storage-path-traversal`（medium）

- **变更符号**：app.api.v1.module_storage.file.service::StorageFileService._validate_remote_path
- **bug**：`_validate_remote_path` 删掉 `..` 段检查（保留 NUL 检查）：远程路径可穿越
- **gold**：`app/api/v1/module_storage/file/service.py`；terms(2): copy_or_move, get_url, download

**native**：F1=1.0（P=1.0 R=1.0）· $0.163 · 16 calls · 14 files · 4 grep · 12 Read · 0 MCP · 60.9s · unknown_access=True

```text
01. Read | {"file_path":"app/api/v1/module_storage/file/service.py"} | response=8446 chars
02. Bash | {"command":"find app/api/v1/module_storage -type f | sort","description":"List files in module_storage directory"} | response=1416 chars
03. Bash | {"command":"rg -n \"_validate_remote_path|check_path_traversal|StorageFileService\" app --glob '*.py'","description":… | response=2388 chars
04. Read | {"file_path":"app/api/v1/module_storage/file/controller.py"} | response=5360 chars
05. Read | {"file_path":"app/api/v1/module_storage/core/local_adapter.py"} | response=4547 chars
06. Read | {"file_path":"app/api/v1/module_storage/core/base.py"} | response=3504 chars
07. Read | {"file_path":"app/utils/upload_util.py","offset":100,"limit":80} | response=2275 chars
08. Read | {"file_path":"app/api/v1/module_storage/core/ftp_adapter.py"} | response=7405 chars
09. Read | {"file_path":"app/api/v1/module_storage/core/sftp_adapter.py"} | response=5634 chars
10. Bash | {"command":"rg -n \"_validate_remote_path|StorageFileService|module_storage|_abs_path\" tests app --glob '*.py' 2>/de… | response=12136 chars
11. Read | {"file_path":"app/api/v1/module_storage/transfer/engine.py"} | response=10593 chars
12. Read | {"file_path":"tests/test_eval_storage_regressions.py","offset":220,"limit":60} | response=1385 chars
13. Read | {"file_path":"app/api/v1/module_storage/core/s3_adapter.py"} | response=6271 chars
14. Bash | {"command":"rg -n \"path_prefix|remote_path|full_prefix|\\.\\./\" app/api/v1/module_storage/transfer/service.py app/a… | response=31 chars
15. Read | {"file_path":"app/api/v1/module_storage/__init__.py"} | response=487 chars
16. Read | {"file_path":"app/init_app.py","offset":70,"limit":30} | response=1187 chars
```

**core**：F1=1.0（P=1.0 R=1.0）· $0.149 · 15 calls · 8 files · 2 grep · 8 Read · 5 MCP · 57.4s · unknown_access=True

```text
01. MCP get_change_summary | {} | response=1052 chars
02. Read | {"file_path":"app/api/v1/module_storage/file/service.py"} | response=8446 chars
03. MCP get_impact | {"symbols":["app.api.v1.module_storage.file.service::StorageFileService._validate_remote_path"],"max_nodes_… | response=8682 chars
04. MCP get_change_context | {"symbols":["app.api.v1.module_storage.file.service::StorageFileService._validate_remote_path"],"direction"… | response=6086 chars
05. Read | {"file_path":"app/api/v1/module_storage/file/controller.py"} | response=5360 chars
06. MCP get_test_impact | {"symbols":["app.api.v1.module_storage.file.service::StorageFileService._validate_remote_path"]} | response=788 chars
07. Bash | {"command":"ls \"C:\\Users\\44550\\Desktop\\code-review-ai\\.code-review-ai\\full-agent-eval-cmp-20\\worktrees\\cb-me… | response=161 chars
08. Bash | {"command":"ls \"C:\\Users\\44550\\Desktop\\code-review-ai\\.code-review-ai\\full-agent-eval-cmp-20\\worktrees\\cb-me… | response=49 chars
09. Read | {"file_path":"app/api/v1/module_storage/core/local_adapter.py"} | response=4547 chars
10. Read | {"file_path":"app/api/v1/module_storage/core/base.py"} | response=3504 chars
11. Read | {"file_path":"app/api/v1/module_storage/core/ftp_adapter.py"} | response=7405 chars
12. Read | {"file_path":"app/api/v1/module_storage/core/sftp_adapter.py"} | response=5634 chars
13. Read | {"file_path":"tests/test_eval_storage_regressions.py","offset":220,"limit":60} | response=1385 chars
14. Read | {"file_path":"app/api/v1/module_storage/core/s3_adapter.py","offset":1,"limit":60} | response=2283 chars
15. MCP get_symbol_detail | {"qualified_name":"app.api.v1.module_storage.file.service::StorageFileService._validate_remote_path"} | response=1016 chars
```
> 点评：core grep 4→2；文件 少读 6 个；core 用 MCP 5 次（get_impact 主通道）；成本 低 $0.014。

---

### 9. `cb-medium-workflow-node-code`（medium）

- **变更符号**：app.api.v1.module_task.workflow.flows.handlers.builtin_nodes::get_builtin_node
- **bug**：`get_builtin_node` 从 `.get(code)` 改成 `.get(code.upper())`：小写 code 查不到节点
- **gold**：`app/api/v1/module_task/workflow/flows/handlers/builtin_nodes.py`；terms(2): execute_workflow, storage_upload, ai_chat

**native**：F1=1.0（P=1.0 R=1.0）· $0.090 · 8 calls · 4 files · 4 grep · 4 Read · 0 MCP · 43.2s · unknown_access=True

```text
01. Read | {"file_path":"app/api/v1/module_task/workflow/flows/handlers/builtin_nodes.py"} | response=12737 chars
02. Bash | {"command":"rg -n \"get_builtin_node|BUILTIN_NODES|builtin_node\\(\" --glob '!*.pyc' .","description":"Find usages of… | response=1833 chars
03. Read | {"file_path":"app/api/v1/module_task/workflow/flows/service.py","offset":100,"limit":90} | response=3851 chars
04. Read | {"file_path":"tests/test_eval_storage_regressions.py","offset":240,"limit":40} | response=713 chars
05. Bash | {"command":"rg -n \"execute_workflow|WorkflowExecuteSchema|run_workflow_sync\" app tests --glob '!*.pyc'","descriptio… | response=1443 chars
06. Bash | {"command":"rg -n \"@builtin_node\\(\" app --glob '!*.pyc'","description":"Find all builtin_node registrations"} | response=670 chars
07. Read | {"file_path":"app/api/v1/module_task/workflow/flows/controller.py","offset":80,"limit":20} | response=727 chars
08. Bash | {"command":"rg -n \"execute_workflow|storage_upload|builtin\" tests --glob '*.py'","description":"Find workflow/built… | response=489 chars
```

**core**：F1=1.0（P=1.0 R=1.0）· $0.121 · 9 calls · 3 files · 3 grep · 3 Read · 3 MCP · 53.9s · unknown_access=True

```text
01. MCP get_change_summary | {} | response=661 chars
02. MCP get_impact | {"files":["app/api/v1/module_task/workflow/flows/handlers/builtin_nodes.py"]} | response=1822 chars
03. Read | {"file_path":"app/api/v1/module_task/workflow/flows/handlers/builtin_nodes.py"} | response=12737 chars
04. MCP get_test_impact | {"files":["app/api/v1/module_task/workflow/flows/handlers/builtin_nodes.py"]} | response=798 chars
05. Read | {"file_path":"app/api/v1/module_task/workflow/flows/service.py","offset":110,"limit":80} | response=3409 chars
06. Bash | {"command":"rg -n \"builtin_node\\(|BUILTIN_NODES|get_builtin_node\" C:\\Users\\44550\\Desktop\\code-review-ai\\.code… | response=308 chars
07. Read | {"file_path":"tests/test_eval_storage_regressions.py","offset":240,"limit":40} | response=713 chars
08. Bash | {"command":"rg -n \"builtin_node\\(|BUILTIN_NODES|get_builtin_node\" \"C:/Users/44550/Desktop/code-review-ai/.code-re… | response=3645 chars
09. Bash | {"command":"rg -n \"get_builtin_node|builtin_node_options\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/f… | response=400 chars
```
> 点评：core grep 4→3；文件 少读 1 个；core 用 MCP 3 次（get_impact 主通道）；成本 高 $0.032。

---

### 10. `cb-recall-decrypt-password-exception`（hard）

- **变更符号**：app.api.v1.module_storage.core.encrypt::decrypt_password
- **bug**：`decrypt_password` 异常分支从 `raise CustomException` 改成 `return ""`：解密失败静默返回空串
- **gold**：`app/api/v1/module_storage/core/encrypt.py`；terms(2): _resolve_storage_config, decrypt_storage_password, StorageSourceService, StorageFileService, _build_config, test_config

**native**：F1=1.0（P=1.0 R=1.0）· $0.120 · 15 calls · 7 files · 5 grep · 10 Read · 0 MCP · 55.1s · unknown_access=True

```text
01. Read | {"file_path":"app/api/v1/module_storage/core/encrypt.py"} | response=1075 chars
02. Bash | {"command":"rg -n \"decrypt_password\" --type py","description":"Search for decrypt_password callers"} | response=1331 chars
03. Read | {"file_path":"app/api/v1/module_storage/transfer/engine.py","offset":70,"limit":50} | response=1848 chars
04. Read | {"file_path":"app/api/v1/module_storage/file/service.py","offset":30,"limit":40} | response=1713 chars
05. Read | {"file_path":"app/api/v1/module_storage/source/service.py","offset":30,"limit":40} | response=1657 chars
06. Read | {"file_path":"app/api/v1/module_storage/source/service.py","offset":130,"limit":40} | response=1685 chars
07. Read | {"file_path":"app/api/v1/module_task/workflow/flows/handlers/builtin_nodes.py","offset":1,"limit":60} | response=1848 chars
08. Read | {"file_path":"tests/test_eval_storage_regressions.py"} | response=8149 chars
09. Bash | {"command":"rg -n \"decrypt_storage_password|decrypt_password|CustomException\" app/api/v1/module_task/workflow/flows… | response=162 chars
10. Bash | {"command":"rg -n \"test_config|test_connection|_build_config|_get_source\" app/api/v1/module_storage -g \"*.py\"","d… | response=4521 chars
11. Read | {"file_path":"app/api/v1/module_task/workflow/flows/handlers/builtin_nodes.py","limit":50,"offset":95} | response=1928 chars
12. Read | {"file_path":"app/api/v1/module_storage/transfer/engine.py","limit":120,"offset":115} | response=5351 chars
13. Read | {"file_path":"app/api/v1/module_storage/source/controller.py","limit":40,"offset":75} | response=1548 chars
14. Bash | {"command":"rg -n \"StorageFileRouter\\.(get|post|put|delete)|_get_source\" app/api/v1/module_storage/file/controller… | response=1753 chars
15. Bash | {"command":"rg -n \"transfer|execute_transfer_task|StorageTransferRouter|create_transfer\" app/api/v1/module_storage/… | response=5109 chars
```

**core**：F1=1.0（P=1.0 R=1.0）· $0.149 · 14 calls · 6 files · 1 grep · 8 Read · 5 MCP · 63.5s · unknown_access=True

```text
01. MCP get_change_summary | {"files":["app/api/v1/module_storage/core/encrypt.py"]} | response=767 chars
02. MCP get_impact | {"files":["app/api/v1/module_storage/core/encrypt.py"]} | response=7719 chars
03. Read | {"file_path":"app/api/v1/module_storage/core/encrypt.py"} | response=1075 chars
04. MCP get_symbol_detail | {"qualified_name":"app.api.v1.module_storage.core.encrypt::decrypt_password"} | response=928 chars
05. MCP get_test_impact | {"files":["app/api/v1/module_storage/core/encrypt.py"]} | response=2263 chars
06. Read | {"file_path":"tests/test_eval_storage_regressions.py"} | response=8149 chars
07. Read | {"file_path":"app/api/v1/module_storage/source/service.py","offset":120,"limit":60} | response=2565 chars
08. Read | {"file_path":"app/api/v1/module_storage/file/service.py","offset":30,"limit":70} | response=2925 chars
09. Read | {"file_path":"app/api/v1/module_storage/source/service.py","offset":40,"limit":20} | response=782 chars
10. Read | {"file_path":"app/api/v1/module_storage/transfer/engine.py","offset":80,"limit":40} | response=1461 chars
11. Read | {"file_path":"app/api/v1/module_task/workflow/flows/handlers/builtin_nodes.py","limit":40,"offset":95} | response=1467 chars
12. MCP search_symbol | {"query":"decrypt_storage_password"} | response=15 chars
13. Bash | {"command":"rg -n \"decrypt_storage_password|decrypt_password\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-… | response=162 chars
14. Read | {"file_path":"app/api/v1/module_storage/source/service.py","limit":10,"offset":1} | response=585 chars
```
> 点评：core grep 5→1；文件 少读 1 个；core 用 MCP 5 次（get_impact 主通道）；成本 高 $0.029。

---

### 11. `cb-recall-encrypt-password`（medium）

- **变更符号**：app.api.v1.module_storage.core.encrypt::encrypt_password
- **bug**：`encrypt_password` 误用 `decrypt()` 替代 `encrypt()`：加密变解密
- **gold**：`app/api/v1/module_storage/core/encrypt.py`；terms(2): StorageSourceService, StorageSourceCRUD, create, update

**native**：F1=1.0（P=1.0 R=1.0）· $0.097 · 8 calls · 4 files · 4 grep · 4 Read · 0 MCP · 47.6s · unknown_access=True

```text
01. Read | {"file_path":"app/api/v1/module_storage/core/encrypt.py"} | response=1128 chars
02. Bash | {"command":"rg -n \"encrypt_password|decrypt_password|_get_fernet\" --type py","description":"Search for encrypt_pass… | response=2155 chars
03. Read | {"file_path":"app/api/v1/module_storage/source/service.py"} | response=7959 chars
04. Read | {"file_path":"tests/test_eval_storage_regressions.py"} | response=8149 chars
05. Bash | {"command":"rg -n \"StorageSourceService|\\.create\\(|\\.update\\(\" app/api/v1/module_storage --type py -g \"!*servi… | response=1491 chars
06. Bash | {"command":"rg -n \"encrypt_password\" app --type py","description":"Search all encrypt_password usages in ap"} | response=463 chars
07. Read | {"file_path":"app/api/v1/module_storage/source/controller.py"} | response=5362 chars
08. Bash | {"command":"rg -n \"router|include_router|storage/source|module_storage\" app/api/v1/module_storage --type py | head … | response=7534 chars
```

**core**：F1=1.0（P=1.0 R=1.0）· $0.098 · 11 calls · 4 files · 0 grep · 5 Read · 6 MCP · 40.4s

```text
01. MCP get_change_summary | {} | response=700 chars
02. MCP get_impact | {"symbols":["module_storage::encrypt_password"]} | response=300 chars
03. MCP search_symbol | {"query":"encrypt_password"} | response=7262 chars
04. MCP get_impact | {"symbols":["app.api.v1.module_storage.core.encrypt::encrypt_password"]} | response=3477 chars
05. Read | {"file_path":"app/api/v1/module_storage/core/encrypt.py"} | response=1128 chars
06. MCP get_test_impact | {"symbols":["app.api.v1.module_storage.core.encrypt::encrypt_password"]} | response=1021 chars
07. Read | {"file_path":"app/api/v1/module_storage/source/service.py","offset":80,"limit":50} | response=2231 chars
08. Read | {"file_path":"app/api/v1/module_storage/source/controller.py","limit":40,"offset":40} | response=2247 chars
09. Read | {"file_path":"tests/test_eval_storage_regressions.py","limit":60,"offset":55} | response=1757 chars
10. MCP search_symbol | {"query":"decrypt_password"} | response=302 chars
11. Read | {"file_path":"app/api/v1/module_storage/source/controller.py","limit":30,"offset":1} | response=1456 chars
```
> 点评：core grep 4→0；core 用 MCP 6 次（get_impact 主通道）；成本 高 $0.000。

---

### 12. `cb-recall-file-get-source-default`（medium）

- **变更符号**：app.api.v1.module_storage.file.service::StorageFileService._get_source
- **bug**：`_get_source` 从 `get_active_source(source_id)` 改成 `get_active_source(None)`：永远取默认存储源
- **gold**：`app/api/v1/module_storage/file/service.py`；terms(2): copy_or_move, get_url, is_default, target_id

**native**：F1=1.0（P=1.0 R=1.0）· $0.104 · 8 calls · 4 files · 4 grep · 4 Read · 0 MCP · 43.8s · unknown_access=True

```text
01. Read | {"file_path":"app/api/v1/module_storage/file/service.py"} | response=8474 chars
02. Bash | {"command":"rg -n \"get_active_source\" .","description":"Search for get_active_source usages"} | response=737 chars
03. Bash | {"command":"rg -n \"_get_source\" .","description":"Search for _get_source usages"} | response=1069 chars
04. Read | {"file_path":"app/api/v1/module_storage/source/service.py","offset":150,"limit":80} | response=1469 chars
05. Read | {"file_path":"tests/test_eval_storage_regressions.py"} | response=8149 chars
06. Bash | {"command":"rg -n \"StorageFileService|_get_source|\\.upload\\(|\\.download\\(|\\.delete\\(|\\.exists\\(|\\.list\\(|g… | response=20488 chars
07. Read | {"file_path":"app/api/v1/module_storage/file/controller.py"} | response=5360 chars
08. Bash | {"command":"rg -n \"\\.exists\\(|\\.list\\(|\\.get_url\\(|\\.copy_or_move\\(|\\.upload\\(|\\.download\\(|\\.delete\\(… | response=3873 chars
```

**core**：F1=1.0（P=1.0 R=1.0）· $0.098 · 9 calls · 4 files · 0 grep · 4 Read · 5 MCP · 34.9s

```text
01. Read | {"file_path":"app/api/v1/module_storage/file/service.py"} | response=8474 chars
02. MCP get_change_summary | {} | response=1154 chars
03. MCP get_change_summary | {"symbols":["app.api.v1.module_storage.file.service::StorageFileService._get_source"]} | response=461 chars
04. MCP get_impact | {"symbols":["app.api.v1.module_storage.file.service::StorageFileService._get_source"],"max_nodes_per_direct… | response=11002 chars
05. MCP search_symbol | {"query":"get_active_source"} | response=700 chars
06. Read | {"file_path":"app/api/v1/module_storage/source/service.py","offset":150,"limit":50} | response=1469 chars
07. Read | {"file_path":"tests/test_eval_storage_regressions.py"} | response=8149 chars
08. Read | {"file_path":"app/api/v1/module_storage/file/controller.py"} | response=5360 chars
09. MCP get_test_impact | {"symbols":["app.api.v1.module_storage.file.service::StorageFileService._get_source"]} | response=786 chars
```
> 点评：core grep 4→0；core 用 MCP 5 次（get_impact 主通道）；成本 低 $0.005。

---

### 13. `cb-recall-search-to-dict-fan-in`（hard）

- **变更符号**：app.utils.common_util::search_to_dict
- **bug**：`search_to_dict` 从 `model_dump(exclude_none=True)` 改成 `False`：空值字段进入查询条件
- **gold**：`app/utils/common_util.py`；terms(2): StorageSourceService, DictTypeService, DictDataService, RoleService, StorageTransferService

**native**：F1=0.0（P=0.0 R=0.0）· $0.294 · 20 calls · 14 files · 8 grep · 12 Read · 0 MCP · 106.1s · unknown_access=True

```text
01. Read | {"file_path":"app/utils/common_util.py"} | response=14759 chars
02. Bash | {"command":"rg -n \"search_to_dict\" C:\\Users\\44550\\Desktop\\code-review-ai\\.code-review-ai\\full-agent-eval-cmp-… | response=308 chars
03. Bash | {"command":"rg -n \"search_to_dict\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-20/w… | response=15369 chars
04. Read | {"file_path":"app/core/base_crud.py"} | response=21242 chars
05. Read | {"file_path":"tests/test_eval_storage_regressions.py"} | response=8149 chars
06. Bash | {"command":"rg -n \"get_obj_list_crud\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-2… | response=3025 chars
07. Bash | {"command":"rg -n \"json_schema_extra=.*q|\\\"q\\\"|'q'\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/ful… | response=19556 chars
08. Read | {"file_path":"app/api/v1/module_task/cronjob/job/crud.py"} | response=2742 chars
09. Read | {"file_path":"app/api/v1/module_task/cronjob/node/crud.py"} | response=3123 chars
10. Read | {"file_path":"app/api/v1/module_task/workflow/flows/crud.py"} | response=2650 chars
11. Read | {"file_path":"app/api/v1/module_storage/transfer/service.py","offset":120,"limit":60} | response=2489 chars
12. Read | {"file_path":"app/api/v1/module_generator/gencode/crud.py","offset":60,"limit":60} | response=1942 chars
13. Read | {"file_path":"app/api/v1/module_task/workflow/node_type/service.py","offset":40,"limit":80} | response=3240 chars
14. Bash | {"command":"rg -n \"q.*None|None.*q|not None|\\\"not None\\\"|'not None'\" \"C:/Users/44550/Desktop/code-review-ai/.c… | response=2270 chars
15. Bash | {"command":"rg -n \"\\\"q\\\"\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-20/worktr… | response=31 chars
16. Read | {"file_path":"app/common/enums.py","offset":70,"limit":50} | response=1105 chars
17. Bash | {"command":"rg -n \"json_schema_extra=.*not None|\\\"not None\\\"|'not None'|q=.*None\" \"C:/Users/44550/Desktop/code… | response=382 chars
18. Read | {"file_path":"app/api/v1/module_generator/gencode/service.py","offset":255,"limit":45} | response=1522 chars
19. Read | {"file_path":"templates/python/service.py.jinja2"} | response=10694 chars
20. Bash | {"command":"rg -n \"search_to_dict|exclude_none\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-… | response=16389 chars
```

**core**：F1=0.0（P=0.0 R=0.0）· $0.445 · 24 calls · 8 files · 6 grep · 10 Read · 8 MCP · 203.4s · unknown_access=True

```text
01. MCP get_change_summary | {} | response=612 chars
02. Read | {"file_path":"app/utils/common_util.py","offset":80,"limit":80} | response=2412 chars
03. MCP get_impact | {"symbols":["app.utils.common_util::search_to_dict"],"include_signatures":true} | response=19866 chars
04. MCP search_symbol | {"query":"def get_list","limit":20} | response=6268 chars
05. MCP search_symbol | {"query":"CurdBase","limit":10} | response=15 chars
06. MCP get_symbol_detail | {"qualified_name":"app.core.base_crud::CRUDBase.get_list"} | response=779 chars
07. Read | {"file_path":"app/core/base_crud.py","offset":80,"limit":120} | response=5478 chars
08. Read | {"file_path":"app/core/base_crud.py","limit":80,"offset":1} | response=3392 chars
09. MCP get_symbol_detail | {"qualified_name":"app.core.base_crud::CRUDBase._build_conditions"} | response=549 chars
10. Read | {"file_path":"app/core/base_crud.py","limit":120,"offset":300} | response=5092 chars
11. Bash | {"command":"rg -n '\"q\"\\s*:\\s*\"None\"|\"q\"\\s*:\\s*\"not None\"' app --glob '*.py'","description":"Find query pa… | response=31 chars
12. Bash | {"command":"rg -n '\"q\"\\s*:' app --glob '*.py' | head -80","description":"List all json_schema_extra q operators u"} | response=10121 chars
13. Bash | {"command":"rg -n 'json_schema_extra=.*\"q\"\\s*:\\s*\"(None|not None)\"' . --glob '*.py'","description":"Search whol… | response=31 chars
14. MCP get_test_impact | {"symbols":["app.utils.common_util::search_to_dict"]} | response=2040 chars
15. Read | {"file_path":"tests/test_eval_storage_regressions.py","limit":80,"offset":1} | response=2541 chars
16. Read | {"file_path":"tests/test_eval_system_regressions.py","limit":130,"offset":1} | response=4309 chars
17. Bash | {"command":"rg -n 'search_to_dict' . --glob '*.py'","description":"Find all usages of search_to_dict across"} | response=6838 chars
18. Read | {"file_path":"app/core/base_schema.py","limit":40,"offset":165} | response=1383 chars
19. MCP search_symbol | {"query":"get_obj_list_crud"} | response=1946 chars
20. Read | {"file_path":"app/api/v1/module_task/cronjob/job/crud.py","limit":60,"offset":1} | response=1851 chars
21. Read | {"file_path":"app/api/v1/module_system/user/schema.py","limit":30,"offset":280} | response=821 chars
22. Bash | {"command":"rg -n 'not None|\"None\"' app --glob '*.py' -g '!*test*'","description":"Search for not None / None null-… | response=6847 chars
23. Read | {"file_path":"app/common/enums.py","limit":30,"offset":80} | response=650 chars
24. Bash | {"command":"rg -n 'Null|QueryOp|not_none|\\.none|\"None\"' app --glob '*.py' -g '!common/enums.py' | rg -i 'schema|qu… | response=321 chars
```
> 点评：双失败但 agent 命中正确文件+正确 bug，terms 只中 0/2（gold 口径问题）。

---

### 14. `cb-recall-transfer-build-config`（hard）

- **变更符号**：app.api.v1.module_storage.transfer.engine::_build_config
- **bug**：`_build_config` 从 `password=decrypt_password(source.password)` 改成明文 `source.password`：密码未解密
- **gold**：`app/api/v1/module_storage/transfer/engine.py`；terms(2): _run_step, execute_transfer_task, StorageTransferService, _launch

**native**：F1=1.0（P=1.0 R=1.0）· $0.152 · 15 calls · 8 files · 7 grep · 8 Read · 0 MCP · 59.0s · unknown_access=True

```text
01. Read | {"file_path":"app/api/v1/module_storage/transfer/engine.py"} | response=10575 chars
02. Bash | {"command":"rg -n \"decrypt_password\" C:\\Users\\44550\\Desktop\\code-review-ai\\.code-review-ai\\full-agent-eval-cm… | response=308 chars
03. Bash | {"command":"rg -n \"decrypt_password\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-20… | response=2639 chars
04. Bash | {"command":"rg -n \"encrypt_password|password\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-ev… | response=102 chars
05. Read | {"file_path":"app/api/v1/module_storage/core/encrypt.py"} | response=1128 chars
06. Read | {"file_path":"app/api/v1/module_storage/core/base.py"} | response=3504 chars
07. Read | {"file_path":"app/api/v1/module_storage/source/service.py"} | response=7959 chars
08. Read | {"file_path":"app/api/v1/module_storage/file/service.py","offset":30,"limit":40} | response=1713 chars
09. Bash | {"command":"rg -n \"config.password|self.config.password|password\" \"C:/Users/44550/Desktop/code-review-ai/.code-rev… | response=8040 chars
10. Bash | {"command":"rg -n \"execute_transfer_task|_build_config\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/ful… | response=2205 chars
11. Read | {"file_path":"app/api/v1/module_storage/transfer/service.py"} | response=8024 chars
12. Bash | {"command":"rg -n \"transfer\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-20/worktre… | response=9932 chars
13. Bash | {"command":"rg -n \"transfer|engine|decrypt_password|StorageAdapterConfig\" \"C:/Users/44550/Desktop/code-review-ai/.… | response=4655 chars
14. Read | {"file_path":"tests/test_eval_storage_regressions.py","offset":78,"limit":40} | response=1133 chars
15. Read | {"file_path":"app/api/v1/module_storage/transfer/controller.py"} | response=6106 chars
```

**core**：F1=1.0（P=1.0 R=1.0）· $0.135 · 13 calls · 8 files · 0 grep · 8 Read · 5 MCP · 52.0s

```text
01. MCP get_change_summary | {"files":["app/api/v1/module_storage/transfer/engine.py"]} | response=655 chars
02. MCP get_impact | {"files":["app/api/v1/module_storage/transfer/engine.py"]} | response=4475 chars
03. Read | {"file_path":"app/api/v1/module_storage/transfer/engine.py"} | response=10575 chars
04. MCP search_symbol | {"query":"decrypt_password"} | response=301 chars
05. MCP search_symbol | {"query":"StorageAdapterConfig"} | response=2546 chars
06. Read | {"file_path":"app/api/v1/module_storage/core/encrypt.py"} | response=1128 chars
07. Read | {"file_path":"app/api/v1/module_storage/source/service.py"} | response=7959 chars
08. Read | {"file_path":"app/api/v1/module_storage/core/base.py"} | response=3504 chars
09. MCP get_test_impact | {"files":["app/api/v1/module_storage/transfer/engine.py"]} | response=1370 chars
10. Read | {"file_path":"app/api/v1/module_storage/core/factory.py"} | response=1682 chars
11. Read | {"file_path":"tests/test_eval_storage_regressions.py","offset":60,"limit":160} | response=4812 chars
12. Read | {"file_path":"app/api/v1/module_storage/transfer/service.py","limit":60,"offset":60} | response=2991 chars
13. Read | {"file_path":"app/api/v1/module_storage/transfer/controller.py"} | response=6106 chars
```
> 点评：core grep 7→0；core 用 MCP 5 次（get_impact 主通道）；成本 低 $0.017。

---

### 15. `cb-trivial-dict-type-default-order`（trivial）

- **变更符号**：app.api.v1.module_system.dict.service::DictTypeService.page
- **bug**：`DictTypeService.page` 默认排序 `id asc`→`desc`：列表顺序倒置
- **gold**：`app/api/v1/module_system/dict/service.py`；terms(2): get_type_list_controller, get_data_list_controller, batch_set_available_dict_type_controller

**native**：F1=0.0（P=0.0 R=0.0）· $0.192 · 17 calls · 10 files · 9 grep · 8 Read · 0 MCP · 84.8s · unknown_access=True

```text
01. Read | {"file_path":"app/api/v1/module_system/dict/service.py"} | response=20004 chars
02. Bash | {"command":"rg -n \"order_by\" app/api/v1/module_system/dict/","description":"Search for order_by usage in dict module"} | response=1747 chars
03. Read | {"file_path":"app/api/v1/module_system/dict/controller.py"} | response=9392 chars
04. Read | {"file_path":"app/api/v1/module_system/dict/crud.py"} | response=2667 chars
05. Bash | {"command":"rg -n \"async def page|def page|order_by\" app/core/base_crud.py","description":"Search for page and orde… | response=529 chars
06. Bash | {"command":"rg -n \"order_by\" app/core/","description":"Search for order_by in core"} | response=1406 chars
07. Read | {"file_path":"app/core/base_crud.py","offset":105,"limit":80} | response=3749 chars
08. Read | {"file_path":"app/core/base_schema.py","offset":130,"limit":40} | response=1764 chars
09. Bash | {"command":"rg -n \"dict_type|DictType|type/list|type_list\" --glob \"*test*\" -l","description":"Search for dict typ… | response=69 chars
10. Bash | {"command":"rg -n \"pydantic\" requirements*.txt pyproject.toml setup.py 2>/dev/null | head -50","description":"Check… | response=254 chars
11. Bash | {"command":"rg -n \"type/list|type_list|DictTypeService|dict_type|order_by|asc|desc\" tests/test_api_module_system.py… | response=2145 chars
12. Read | {"file_path":"tests/test_eval_system_regressions.py"} | response=5745 chars
13. Bash | {"command":"rg -n \"\\.page\\(\" app/api/v1/module_system/dict/ && rg -n \"DictTypeService\\(\" app/ --glob \"*.py\""… | response=1228 chars
14. Bash | {"command":"rg -n \"DictRouter|include_router.*dict|/dict\" app/api/v1/module_system/dict/__init__.py app/api/v1/rout… | response=2728 chars
15. Read | {"file_path":"app/core/base_crud.py","offset":385,"limit":45} | response=2015 chars
16. Bash | {"command":"rg -n \"validate_default|model_config|ConfigDict|BaseModel\" app/core/base_schema.py | head -40","descrip… | response=1136 chars
17. Read | {"file_path":"app/core/base_schema.py","limit":40} | response=1405 chars
```

**core**：F1=0.0（P=0.0 R=0.0）· $0.097 · 10 calls · 4 files · 1 grep · 4 Read · 5 MCP · 46.7s · unknown_access=True

```text
01. MCP get_change_summary | {} | response=1019 chars
02. MCP get_impact | {"symbols":["app.api.v1.module_system.dict.service::DictTypeService.page"]} | response=2431 chars
03. MCP get_change_context | {"symbols":["app.api.v1.module_system.dict.service::DictTypeService.page"]} | response=1166 chars
04. MCP get_test_impact | {"symbols":["app.api.v1.module_system.dict.service::DictTypeService.page"]} | response=764 chars
05. Read | {"file_path":"app/api/v1/module_system/dict/service.py","offset":60,"limit":50} | response=1739 chars
06. Read | {"file_path":"app/api/v1/module_system/dict/controller.py"} | response=9392 chars
07. Read | {"file_path":"tests/test_eval_system_regressions.py","limit":120,"offset":1} | response=3992 chars
08. Read | {"file_path":"app/api/v1/module_system/dict/crud.py"} | response=2667 chars
09. MCP get_symbol_detail | {"qualified_name":"app.api.v1.module_system.dict.controller::get_type_list_controller"} | response=1026 chars
10. Bash | {"command":"rg -n \"async def page|order_by\" \"C:\\Users\\44550\\Desktop\\code-review-ai\\.code-review-ai\\full-agen… | response=529 chars
```
> 点评：双失败但 agent 命中正确文件+正确 bug，terms 只中 0/2（gold 口径问题）。

---

### 16. `cb-trivial-hierarchy-parent-map`（trivial）

- **变更符号**：app.utils.common_util::get_parent_id_map
- **bug**：`get_parent_id_map` 键值对倒置（`id→parent_id` 变 `parent_id→id`）
- **gold**：`app/utils/common_util.py`；terms(2): batch_set_available, enable_ids, get_child_recursion

**native**：F1=0.0（P=0.0 R=0.0）· $0.112 · 8 calls · 6 files · 2 grep · 6 Read · 0 MCP · 54.7s · unknown_access=True

```text
01. Read | {"file_path":"app/utils/common_util.py"} | response=14758 chars
02. Bash | {"command":"rg -n \"get_parent_id_map|get_parent_recursion|parent_id_map|parent_recursion\" --glob '!**/node_modules/… | response=1360 chars
03. Read | {"file_path":"app/api/v1/module_system/menu/service.py","offset":120,"limit":60} | response=1703 chars
04. Read | {"file_path":"app/api/v1/module_system/dept/service.py","offset":70,"limit":60} | response=1447 chars
05. Read | {"file_path":"tests/test_eval_system_regressions.py"} | response=5745 chars
06. Bash | {"command":"rg -n \"set_available|batch_set_available|BatchSetAvailable\" --glob '!**/node_modules/**' --glob '!**/.g… | response=7436 chars
07. Read | {"file_path":"app/api/v1/module_system/menu/controller.py","limit":30,"offset":60} | response=952 chars
08. Read | {"file_path":"app/api/v1/module_system/dept/controller.py","limit":25,"offset":60} | response=956 chars
```

**core**：F1=0.0（P=0.0 R=0.0）· $0.119 · 11 calls · 7 files · 1 grep · 7 Read · 3 MCP · 62.1s · unknown_access=True

```text
01. MCP get_change_summary | {"files":["app/utils/common_util.py"]} | response=749 chars
02. MCP get_impact | {"files":["app/utils/common_util.py"]} | response=2279 chars
03. Read | {"file_path":"app/utils/common_util.py","offset":110,"limit":60} | response=1932 chars
04. Read | {"file_path":"app/api/v1/module_system/dept/service.py","offset":70,"limit":50} | response=1447 chars
05. Read | {"file_path":"app/api/v1/module_system/menu/service.py","offset":90,"limit":80} | response=3244 chars
06. Read | {"file_path":"app/api/v1/module_system/dept/controller.py","limit":40,"offset":55} | response=1122 chars
07. Read | {"file_path":"app/api/v1/module_system/menu/controller.py","limit":40,"offset":40} | response=1943 chars
08. MCP get_test_impact | {"symbols":["app.utils.common_util::get_parent_id_map"]} | response=707 chars
09. Read | {"file_path":"tests/test_eval_system_regressions.py","limit":45,"offset":1} | response=1634 chars
10. Bash | {"command":"rg -n \"DeptRouter|MenuRouter\" C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp… | response=937 chars
11. Read | {"file_path":"app/api/v1/module_system/__init__.py"} | response=1555 chars
```
> 点评：双失败但 agent 命中正确文件+正确 bug，terms 只中 1/2（gold 口径问题）。

---

### 17. `cb-trivial-notice-status-hardcoded`（trivial）

- **变更符号**：app.api.v1.module_system.notice.service::NoticeService.set_available
- **bug**：`NoticeService.set_available` 把 `data.status` 硬编码成 `1`
- **gold**：`app/api/v1/module_system/notice/service.py`；terms(2): batch_set_available_notice_controller, get_notice_list_available_controller, delete_notice_controller

**native**：F1=0.0（P=0.0 R=0.0）· $0.087 · 12 calls · 9 files · 5 grep · 7 Read · 0 MCP · 40.8s · unknown_access=True

```text
01. Bash | {"command":"rg -n \"batch_set_available|status=data.status|status=1\" app/api/v1/module_system/notice/service.py","de… | response=76 chars
02. Read | {"file_path":"app/api/v1/module_system/notice/service.py","offset":100,"limit":80} | response=2332 chars
03. Bash | {"command":"rg -n \"set_available|BatchSetAvailable\" app --glob '*.py'","description":"Find callers of set_available… | response=6792 chars
04. Bash | {"command":"rg -n \"class NoticeCRUD|def set\\b|async def set\\b\" app --glob '*.py'","description":"Find NoticeCRUD … | response=347 chars
05. Read | {"file_path":"app/core/base_schema.py","offset":35,"limit":30} | response=1221 chars
06. Read | {"file_path":"app/api/v1/module_system/notice/controller.py","offset":60,"limit":35} | response=1586 chars
07. Read | {"file_path":"app/core/base_crud.py","offset":280,"limit":30} | response=1424 chars
08. Bash | {"command":"rg -n \"status\" app/api/v1/module_system/notice/model.py app/api/v1/module_system/notice/*.py 2>/dev/nul… | response=4266 chars
09. Bash | {"command":"rg -n \"status/batch|batch_set_available|set_available\" app/api/v1/module_system/notice tests 2>/dev/nul… | response=1244 chars
10. Read | {"file_path":"tests/test_eval_system_regressions.py","offset":100,"limit":55} | response=1854 chars
11. Read | {"file_path":"tests/test_api_module_system.py","offset":393,"limit":12} | response=516 chars
12. Read | {"file_path":"app/api/v1/module_system/notice/service.py","offset":60,"limit":40} | response=1374 chars
```

**core**：F1=0.0（P=0.0 R=0.0）· $0.161 · 17 calls · 9 files · 1 grep · 9 Read · 7 MCP · 67.3s · unknown_access=True

```text
01. MCP get_change_summary | {} | response=1096 chars
02. Read | {"file_path":"app/api/v1/module_system/notice/service.py"} | response=5570 chars
03. MCP get_impact | {"symbols":["app.api.v1.module_system.notice.service::NoticeService.set_available"]} | response=1995 chars
04. MCP get_impact | {"symbols":["app.api.v1.module_system.notice.service::NoticeService"]} | response=5109 chars
05. Read | {"file_path":"app/api/v1/module_system/notice/controller.py"} | response=4537 chars
06. Read | {"file_path":"app/api/v1/module_system/notice/crud.py"} | response=490 chars
07. MCP search_symbol | {"query":"BatchSetAvailable"} | response=8937 chars
08. Read | {"file_path":"app/core/base_schema.py","offset":40,"limit":15} | response=599 chars
09. Read | {"file_path":"app/api/v1/module_system/notice/model.py"} | response=864 chars
10. Read | {"file_path":"app/api/v1/module_system/notice/schema.py"} | response=2431 chars
11. MCP get_symbol_detail | {"qualified_name":"app.core.base_crud::CRUDBase.set"} | response=539 chars
12. Read | {"file_path":"app/core/base_crud.py"} | response=21242 chars
13. MCP get_test_impact | {"symbols":["app.api.v1.module_system.notice.service::NoticeService.set_available"]} | response=797 chars
14. Read | {"file_path":"app/plugin/module_example/demo/service.py","offset":80,"limit":15} | response=655 chars
15. MCP get_symbol_detail | {"qualified_name":"app.api.v1.module_system.menu.service::MenuService.set_available"} | response=780 chars
16. Read | {"file_path":"tests/test_eval_system_regressions.py","offset":100,"limit":40} | response=1339 chars
17. Bash | {"command":"rg -n \"NoticeRouter\" --glob \"*.py\" -g \"!*worktrees*\" app | head -20","description":"Find where Noti… | response=1393 chars
```
> 点评：双失败但 agent 命中正确文件+正确 bug，terms 只中 0/2（gold 口径问题）。

---

### 18. `cb-trivial-position-page-offset`（trivial）

- **变更符号**：app.api.v1.module_system.position.service::PositionService.page
- **bug**：`PositionService.page` 分页 offset 从 `(page_no-1)*page_size` 改成 `page_no*page_size`：翻页错位
- **gold**：`app/api/v1/module_system/position/service.py`；terms(2): get_obj_list_controller, get_options, get_list

**native**：F1=0.0（P=0.0 R=0.0）· $0.110 · 18 calls · 11 files · 12 grep · 6 Read · 0 MCP · 48.0s · unknown_access=True

```text
01. Bash | {"command":"pwd && ls -la","description":"Show current directory and contents"} | response=1118 chars
02. Read | {"file_path":"app/api/v1/module_system/position/service.py"} | response=4717 chars
03. Bash | {"command":"rg -n \"\\.page\\(|PositionService|position\" app/api/v1/module_system/position --type py","description":… | response=5096 chars
04. Bash | {"command":"rg -rn \"PositionService|\\.page\\(\" app --type py -l","description":"List files referencing PositionSer… | response=1306 chars
05. Read | {"file_path":"app/api/v1/module_system/position/controller.py"} | response=5515 chars
06. Bash | {"command":"rg -n \"async def page|def page\" app/api/v1/module_system/position/crud.py","description":"Find page met… | response=31 chars
07. Bash | {"command":"rg -n \"def page|offset|limit\" app/api/v1/module_system/position/crud.py","description":"Search page met… | response=31 chars
08. Read | {"file_path":"app/api/v1/module_system/position/crud.py"} | response=786 chars
09. Bash | {"command":"rg -n \"def page|offset|limit\" app/core/base_crud.py","description":"Find page method in CRUDBase"} | response=362 chars
10. Bash | {"command":"rg -n \"class PaginationQueryParam|page_no|page_size\" app/core/base_schema.py","description":"Find Pagin… | response=348 chars
11. Read | {"file_path":"app/core/base_crud.py","offset":130,"limit":60} | response=2773 chars
12. Read | {"file_path":"app/core/base_schema.py","offset":125,"limit":25} | response=1092 chars
13. Bash | {"command":"rg -n \"offset = .*page_no\" app --type py","description":"Search for offset calculations using pag"} | response=1269 chars
14. Bash | {"command":"rg -n \"page_no.*page_size|\\(page_no\" app --type py","description":"Search for page_no*page_size patter… | response=2438 chars
15. Bash | {"command":"rg -ln \"position\" tests --type py 2>/dev/null; echo \"---\"; find tests -iname \"*position*\" 2>/dev/nu… | response=73 chars
16. Bash | {"command":"rg -n \"position|/list|page_no|page_size|offset\" tests/test_eval_system_regressions.py | head -60","desc… | response=801 chars
17. Read | {"file_path":"tests/test_eval_system_regressions.py","limit":60,"offset":40} | response=1805 chars
18. Bash | {"command":"rg -n \"PositionRouter|position\" app/api/v1/module_system/__init__.py app/api/v1/router.py app/api/v1/__… | response=208 chars
```

**core**：F1=0.0（P=0.0 R=0.0）· $0.102 · 11 calls · 6 files · 0 grep · 6 Read · 5 MCP · 39.4s

```text
01. MCP get_change_summary | {} | response=945 chars
02. Read | {"file_path":"app/api/v1/module_system/position/service.py"} | response=4717 chars
03. MCP get_impact | {"symbols":["app.api.v1.module_system.position.service::PositionService.page"]} | response=2461 chars
04. MCP get_test_impact | {"symbols":["app.api.v1.module_system.position.service::PositionService.page"]} | response=781 chars
05. Read | {"file_path":"app/api/v1/module_system/position/controller.py","offset":1,"limit":45} | response=2215 chars
06. Read | {"file_path":"app/api/v1/module_system/position/crud.py"} | response=786 chars
07. Read | {"file_path":"tests/test_eval_system_regressions.py","offset":1,"limit":80} | response=2728 chars
08. MCP search_symbol | {"query":"CRUDBase.page"} | response=617 chars
09. MCP search_symbol | {"query":"PaginationQueryParam"} | response=14294 chars
10. Read | {"file_path":"app/core/base_crud.py","offset":136,"limit":50} | response=2263 chars
11. Read | {"file_path":"app/core/base_schema.py","offset":136,"limit":30} | response=1296 chars
```
> 点评：双失败但 agent 命中正确文件+正确 bug，terms 只中 0/2（gold 口径问题）。

---

### 19. `cb-trivial-transfer-dt-none-guard`（trivial）

- **变更符号**：app.api.v1.module_storage.transfer.engine::_dt
- **bug**：`_dt` 删掉 None 守卫：时间为 None 时 `.isoformat()` 直接崩
- **gold**：`app/api/v1/module_storage/transfer/engine.py`；terms(2): _task_payload, _broadcast, transfer_ws_manager

**native**：F1=1.0（P=1.0 R=1.0）· $0.153 · 11 calls · 7 files · 5 grep · 6 Read · 0 MCP · 65.1s · unknown_access=True

```text
01. Read | {"file_path":"app/api/v1/module_storage/transfer/engine.py"} | response=10574 chars
02. Bash | {"command":"rg -n \"_dt\\(\" \"C:\\Users\\44550\\Desktop\\code-review-ai\\.code-review-ai\\full-agent-eval-cmp-20\\wo… | response=1634 chars
03. Bash | {"command":"rg -n \"_step_payload|_task_payload|_broadcast|execute_transfer_task\" \"C:\\Users\\44550\\Desktop\\code-… | response=3389 chars
04. Bash | {"command":"rg -n \"started_at|finished_at\" \"C:\\Users\\44550\\Desktop\\code-review-ai\\.code-review-ai\\full-agent… | response=517 chars
05. Read | {"file_path":"app/api/v1/module_storage/transfer/service.py"} | response=8024 chars
06. Bash | {"command":"rg -n \"execute_transfer_task|transfer|broadcast|task_update|send_to_user\" \"C:\\Users\\44550\\Desktop\\… | response=13685 chars
07. Read | {"file_path":"app/api/v1/module_storage/transfer/controller.py"} | response=6106 chars
08. Bash | {"command":"rg -n \"_dt|_step_payload|_task_payload|transfer.*engine|execute_transfer_task\" \"C:\\Users\\44550\\Desk… | response=4190 chars
09. Read | {"file_path":"tests/test_eval_storage_regressions.py","offset":130,"limit":110} | response=3464 chars
10. Read | {"file_path":"tests/test_eval_storage_regressions.py","limit":130} | response=3988 chars
11. Read | {"file_path":"app/api/v1/module_storage/transfer/ws_manager.py"} | response=1069 chars
```

**core**：F1=1.0（P=1.0 R=1.0）· $0.156 · 10 calls · 6 files · 0 grep · 7 Read · 3 MCP · 69.8s

```text
01. Read | {"file_path":"app/api/v1/module_storage/transfer/engine.py"} | response=10574 chars
02. MCP get_change_summary | {} | response=601 chars
03. MCP get_impact | {"symbols":["app.api.v1.module_storage.transfer.engine::_dt"]} | response=3262 chars
04. Read | {"file_path":"app/api/v1/module_storage/transfer/service.py"} | response=8024 chars
05. Read | {"file_path":"app/api/v1/module_storage/transfer/controller.py"} | response=6106 chars
06. MCP get_test_impact | {"symbols":["app.api.v1.module_storage.transfer.engine::_dt"]} | response=1594 chars
07. Read | {"file_path":"app/api/v1/module_storage/transfer/model.py"} | response=4033 chars
08. Read | {"file_path":"app/api/v1/module_storage/transfer/ws_manager.py"} | response=1069 chars
09. Read | {"file_path":"tests/test_eval_storage_regressions.py","offset":100,"limit":120} | response=3691 chars
10. Read | {"file_path":"tests/test_eval_storage_regressions.py","limit":80,"offset":219} | response=1411 chars
```
> 点评：core grep 5→0；文件 少读 1 个；core 用 MCP 3 次（get_impact 主通道）；成本 高 $0.003。

---

### 20. `cb-trivial-transfer-running-progress`（trivial）

- **变更符号**：app.api.v1.module_storage.transfer.engine::_run_step
- **bug**：`_run_step` 把 `step.progress` 从 `_STEP_RUNNING_PROGRESS` 硬编码成 `100`：运行中显示 100%
- **gold**：`app/api/v1/module_storage/transfer/engine.py`；terms(2): _step_payload, transfer_ws_manager, execute_transfer_task

**native**：F1=0.0（P=0.0 R=0.0）· $0.120 · 15 calls · 2 files · 13 grep · 2 Read · 0 MCP · 59.0s · unknown_access=True

```text
01. Read | {"file_path":"app/api/v1/module_storage/transfer/engine.py"} | response=10574 chars
02. Bash | {"command":"rg -n \"_STEP_RUNNING_PROGRESS|_STEP_COMPLETE_PROGRESS|progress\" C:\\Users\\44550\\Desktop\\code-review-… | response=396 chars
03. Bash | {"command":"rg -n \"_STEP_RUNNING_PROGRESS|step\\.progress|progress\" \"C:/Users/44550/Desktop/code-review-ai/.code-r… | response=2591 chars
04. Bash | {"command":"rg -n \"progress\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-20/worktre… | response=1604 chars
05. Bash | {"command":"rg -n \"progress|_STEP_RUNNING_PROGRESS|running\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai… | response=207 chars
06. Bash | {"command":"rg -n \"task_update|step.*progress|progress\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/ful… | response=2632 chars
07. Read | {"file_path":"tests/test_eval_storage_regressions.py"} | response=8149 chars
08. Bash | {"command":"rg -n \"send_to_user|task_update|progress\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-… | response=77 chars
09. Bash | {"command":"rg -n \"_run_step\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-20/worktr… | response=1400 chars
10. Bash | {"command":"ls \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-20/worktrees/cb-trivial-tr… | response=99 chars
11. Bash | {"command":"rg -n \"progress\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-20/worktre… | response=2381 chars
12. Bash | {"command":"rg -n \"step|progress\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-20/wo… | response=668 chars
13. Bash | {"command":"rg -n \"progress|step\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-20/wo… | response=31 chars
14. Bash | {"command":"rg -n \"progress\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-20/worktre… | response=1067 chars
15. Bash | {"command":"rg -n \"progress\" \"C:/Users/44550/Desktop/code-review-ai/.code-review-ai/full-agent-eval-cmp-20/worktre… | response=129 chars
```

**core**：F1=0.0（P=0.0 R=0.0）· $0.115 · 11 calls · 3 files · 4 grep · 4 Read · 3 MCP · 55.7s · unknown_access=True

```text
01. Bash | {"command":"rg -n \"_STEP_RUNNING_PROGRESS|progress\" app/api/v1/module_storage/transfer/engine.py","description":"Fi… | response=300 chars
02. Read | {"file_path":"app/api/v1/module_storage/transfer/engine.py"} | response=10574 chars
03. MCP get_change_summary | {"files":["app/api/v1/module_storage/transfer/engine.py"]} | response=635 chars
04. MCP get_impact | {"files":["app/api/v1/module_storage/transfer/engine.py"]} | response=9322 chars
05. Bash | {"command":"rg -n \"step\\.progress|_STEP_RUNNING_PROGRESS|\\.progress\" app --glob \"!**/engine.py\" | head -100","d… | response=31 chars
06. Bash | {"command":"rg -n \"progress\" app tests 2>/dev/null | head -100","description":"Search all progress references in ap… | response=1455 chars
07. Bash | {"command":"rg -n \"progress\" --type-add 'web:*.{ts,tsx,js,jsx,vue}' --type web . 2>/dev/null | grep -v node_modules… | response=2342 chars
08. Read | {"file_path":"tests/test_eval_storage_regressions.py","offset":180,"limit":60} | response=1889 chars
09. MCP get_test_impact | {"files":["app/api/v1/module_storage/transfer/engine.py"]} | response=1071 chars
10. Read | {"file_path":"tests/test_eval_storage_regressions.py","offset":1,"limit":60} | response=1954 chars
11. Read | {"file_path":"tests/test_eval_storage_regressions.py","limit":70,"offset":110} | response=2107 chars
```
> 点评：双失败但 agent 命中正确文件+正确 bug，terms 只中 0/2（gold 口径问题）。

---
