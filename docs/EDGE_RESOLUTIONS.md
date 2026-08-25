# 边（Edge）类型与分辨率（Resolution）全解

代码图里每条边有**三个正交维度**,理解它们才能读对图:

1. **`kind`** —— 边的**关系类型**(是什么关系)
2. **`resolution`** —— 对 `target` 的**信任信号**(target 是否真实可依赖)
3. **`origin`** —— 边是**怎么推导出来的**(机制)

`resolution` 是灵魂:它决定这条边能不能进 flow/BFS、会不会被增量修复重判、以及 reviewer 要不要警惕它。

---

## 1. 边的字段(Edge dataclass)

| 字段 | 含义 |
|---|---|
| `source` / `target` | 起止 qname |
| `kind` | 关系类型(见 §2) |
| `resolution` | 信任信号(见 §3) |
| `origin` | `syntax` \| `module` \| `type` \| `framework` \| `heuristic`(默认 `syntax`) |
| `rule_id` | 框架/启发式规则 id,如 `JAVA-F01`/`JAVA-F04`/`JAVA-F05`;纯语法边为 `None` |
| `confidence` | 0.0–1.0,默认 1.0 |
| `evidence_json` | 结构化证据,如 dynamic 的 `{call_form, target_expr}`、candidate 的 `{candidates, truncated}` |
| `site_id` | 同一调用点的候选边分组;None 通常表示非候选边 |

---

## 2. `kind` —— 关系类型(5 种)

| kind | 语义 | 生产者 | 目标必在图? |
|---|---|---|---|
| `call` | 函数/方法/构造调用 | `_resolve_one` / `_resolve_java`(语法)、`_build_di_edges`(DI 注入)、`java_routing`(MockMvc→Controller) | 视 resolution |
| `contains` | 父子包含:模块→函数、类→方法 | `_build_contains` | 父在图中则 `resolved`,否则 `unresolved` |
| `import` | 模块→被导入模块 | `_build_imports`(`origin="module"`) | 模块在图则 `resolved`,否则 `unresolved` |
| `extends` | 类继承基类 | `_build_inherits`(`origin="type"`,relation="extends") | 同上 |
| `implements` | 类/接口实现接口 | `_build_inherits`(`origin="type"`,relation="implements") | 同上 |

> `call` 是所有边里**唯一会进入 flow 遍历**的 kind——但这还要看 resolution。`contains`/`import`/`extends`/`implements` 属于结构边(community 检测用它们,flow 不用)。

---

## 3. `resolution` —— 信任信号(6 个标签)

### `resolved` — ✅ 唯一可靠
**`target` 是图中真实存在的 qname**。产出来源:
- `_resolved()` 里 `_exists(target, existing)` 通过(语法解析出的调用)
- `_build_contains` / `_build_imports` / `_build_inherits` 目标命中 qnames
- 框架边:MockMvc 路由到图中的 Controller(`JAVA-F01`)、DI 命中唯一类(`JAVA-F04/F05`)

**唯一默认可遍历**的 resolution(见 §4)。会被 `repair_resolutions` 按 node 集重判。

### `unresolved` — target 不在图里
语义:**"目标符号不在已索引 repo"**(`impact.py` 的官方解释)。两条来源路径:

1. **裸 fallback**:名字在 local/imports/star 全查不到 → `return [base]`,`target` 存**原始表达式**。典型:builtin(`len`/`print`)、未导入的外部名、`from m import *` 无命中。
2. **推导了 qname 但不在图**:`import os; os.path.join(x)` → 拼出 `os::path.join`,但 os 是外部库没被索引 → `_resolved` 标记。`target` 存**推导出的真实 qname**。外部库主要走这条。

也会由 `repair_resolutions` 在节点删除/新增时与 `resolved` 互转(双向)。

### `dynamic` — receiver 类型静态不可知
**唯一来源 `_mark_dynamic`**(`obj.method()` 形式且所有静态绑定策略全失败)。receiver 是参数、无注解局部变量、`obj = factory()` 这类运行时值 → resolver **选择不猜**,保留原始表达式。
- `target` = 原始表达式(如 `plugin.run`)
- `evidence_json` = `{call_form, target_expr}`
- **从不被 `repair_resolutions` 重判**(它编码了"静态上真的不知道",node 存不存在都不能翻转)

### `candidate` — 有多个可枚举候选
**唯一来源 `_candidates`**:同一调用点能枚举出几个候选 target,静态定不了唯一,但也不是运行时才决定。**每个候选各一条独立边,共享同一 `site_id`**,`evidence_json` 记录完整候选列表 + 是否截断,上限 `_MAX_CANDIDATES = 20`。

典型场景:barrel re-export 歧义、star import 同名、`super().x()` 多父命中、声明类型 union(`A | B`)、star-import 接收者多命中、Java DI 通配。

### `semantic` — 保留(框架规则边)
设计上留给**框架规则产生的、经过审核的**边。`traversal.py` 里 `is_traversable` 只有在 rule_id 被 `register_semantic_rule` 登记进 allow-list 时才会放行。**当前没有任何生成器产出该标签**(taxonomy 和遍历策略已就位,Phase 6 语义适配器预留)。

### `external` — 保留(显式外部依赖)
设计上用于显式标记外部依赖 target。`impact.py` 的 `_uncertainty` 排序/`_REASON_BY_RESOLUTION` 已包含它,但**当前无生成器产出**。

> ⚠️ 现实:**索引里实际只出现 `resolved` / `unresolved` / `dynamic` / `candidate` 四种**。`semantic`/`external` 是 taxonomy 中的预留位,处理代码都在,但还没被写入。

---

## 4. 行为差异 —— 一张表看懂

| 特性 | `resolved` | `unresolved` | `dynamic` | `candidate` | `semantic` | `external` |
|---|---|---|---|---|---|---|
| target 可信度 | ✅ 图中真实存在 | ❌ 不在图 | ⚠️ 静态不可知 | ⚠️ 多个候选 | 规则审核后 | 外部依赖 |
| 进 flow/BFS 遍历 | ✅ **总是** | ❌ | ❌ | ❌ | 仅 allow-list | ❌ |
| `repair_resolutions` 重判 | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| 被 `impact.uncertainty` 列出 | 否 | 是(优先级 2) | 是(优先级 0) | 是(优先级 1) | — | 是(优先级 3) |
| TIA 建议全量跑测试 | 否 | 否 | ✅ | ✅ | — | 否 |
| 进社区检测(结构边) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |

**逐条解释:**

- **遍历**(`traversal.is_traversable`):`resolved` 无条件可遍历(含结构边);`semantic` 需 allow-list;`candidate`/`dynamic`/`unresolved`/`external` 一律不遍历——**flow 绝不建立在猜测上**。
- **增量修复**(`update.py repair_resolutions`):只重判 `resolved`↔`unresolved`(依据当前 node 集),跳过 `call` 边 target 无 `::` 的行;`dynamic`/`candidate`/`semantic`/`external` 永不重判——它们编码的是推导过程,节点存在与否不该翻转。
- **不确定性清单**(`impact._uncertainty`):把变更符号一跳内的非 resolved 边按 `dynamic(0) > candidate(1) > unresolved(2) > external(3)` 排序列出,让 reviewer 看到解析缺口。
- **测试影响断点**(`testimpact._BREAKPOINT_RESOLUTIONS = {"dynamic", "candidate"}`):路径上出现这两种边时,TIA 建议回退全量跑,因为 target 不确定,只跑命中测试可能漏。

---

## 5. 信任层级

```
resolved  ──── 最可靠:target 就是那个符号
  ↑
semantic  ──── 框架规则边,登记审核后才可遍历
  ↑
candidate ──── 候选集可枚举(≤20),reviewer 逐一核对
  ↑
unresolved ─── 名字/推导出的 qname 不在图(外部库、builtin)
  ↑
external  ──── 显式外部依赖
  ↑
dynamic   ──── 最不可靠:receiver 运行时才知道,resolver 不猜
```

> 注意层级不是严格的:比如 `unresolved`(推导出真实 qname 但没索引)比 `dynamic`(完全不可知)"更确定",但两者都不参与遍历。真正影响使用的只有两件事:**能否遍历**(resolved / allow-listed semantic)和**是否被不确定性清单/测试断点标记**(后四种)。

---

## 6. 常见疑问速答

- **为什么保留不遍历的边?** 让 reviewer 看到解析缺口而非静默丢失。`get_impact.uncertainty` / `query_graph` 的 `coverage` 就是为此存在的。
- **`obj = ABC()` 为什么是 dynamic?** 无注解赋值不进 `var_types`(只收注解),resolver 不信赋值右值推断。
- **加注解就能 resolved?** 前提是注解能**唯一命中图中 qname 且成员直接声明**;union → candidate,成员在父类(不走继承链)→ dynamic。
- **Java/TS 命中率为什么高?** 语言强制类型,`var_types` 天然全覆盖;Python 靠开发纪律(全注解)。
