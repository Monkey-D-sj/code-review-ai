# 快速合成仓库:全部 9 个 case 清单 + 两个 agent 的完整决策

> 配套文档。主报告见 [`合成仓库为何无法证明graph价值.md`](./合成仓库为何无法证明graph价值.md),本文件是**逐 case 的完整清单**和 **native vs MCP-graph 两个 agent 的逐步决策轨迹**。

---

## 0. 评估设施速览

- **反向变异**:每个 case 是一个真实 git 分支(`fix-<slug>`)。eval 在 `fix-<slug>` 检出 worktree,把被改模块恢复到 `fix-<slug>^`(变异版 = buggy),留下 unstaged diff = agent 评审的对象。`detect_changed_symbols` 只看到这一个模块。
- **模式**(`FULL_EVAL_MODES`),全部**保留** Bash/Read/Glob/Grep,差异只在暴露哪些 MCP 工具:
  | 模式 | MCP 工具 |
  |---|---|
  | `native_agent` | 无(纯 Bash+Read+Grep) |
  | `full_project_querygraph` | `query_graph` |
  | `full_project_summary` | `get_change_summary` |
  | `full_project_search` | `search_symbol` |
  | `full_project_core` | `query_graph` + `get_change_summary` + `search_symbol` |
  | `full_project_agent` | 全部 MCP 工具 |
- **记分**:finding 的文件 ∈ `(gold.file, *alternate_files)`,且标题+描述里命中 **≥ min_matches 个** 不同 gold 关键词才计命中。bipartite 匹配,1 个 gold 最多被 1 条 finding 命中。
- **本轮大噪声 case 的 gold** 用了 `min_matches=2`(严格因果匹配),这是 2026-08-21 重设计的一部分。

---

## 1. 全部 9 个 case 完整清单

仓库:`benchmarks/fast-repo`。前 8 个在 `src/fast_bench/`(13 个模块,~10 行/文件,可整读);`large-noise` 在 `src/bigapp/`(~5500 行,14 个噪音模块 + dispatch/queue 手写)。

### 1.1 `caller-return-shape` — 返回类型契约变更

| 项 | 值 |
|---|---|
| 变异模块 | `src/fast_bench/pricing.py` |
| mutation(FIXED → BUGGY) | `return OrderTotal(...)` → `return (subtotal_cents, tax_cents, shipping_cents)` |
| 引入的回归 | `compute_total` 返回裸 tuple;调用方 `checkout.finalize_order` 做 `total.total_cents` → tuple 无该属性 → **AttributeError** |
| 调用链 | `checkout.finalize_order` → `pricing.compute_total` |
| gold | file=`pricing.py`,keywords=`tuple/total_cents/attributeerror/return type`,alternates=`checkout.py` |
| complexity | `cross-module` |
| 设计意图 | 返回形状变了,调用点不读返回值内容看不出;graph 的 in-edge 能一次给全调用方 |

结果(6x2,1 rep):native 1.0 / core 1.0。

### 1.2 `dropped-default-arg` — 丢默认参数

| 项 | 值 |
|---|---|
| 变异模块 | `src/fast_bench/inventory.py` |
| mutation | `reserve(item_id, qty=1)` → `reserve(item_id)` |
| 引入的回归 | 调用方 `api.create_order` 仍传 `reserve(item_id, qty)` → **TypeError(missing positional)** |
| 调用链 | `api.create_order` → `inventory.reserve` |
| gold | file=`inventory.py`,keywords=`missing/typeerror/qty/positional`,alternates=`api.py` |
| complexity | `cross-module` |
| 设计意图 | 参数契约变更,调用点在传实参,graph 的 call_site args 直接可见 |

结果:native 1.0 / core 1.0。

### 1.3 `feature-flag-inversion` — 布尔取反

| 项 | 值 |
|---|---|
| 变异模块 | `src/fast_bench/config.py` |
| mutation | `feature in _FEATURES` → `feature not in _FEATURES` |
| 引入的回归 | `is_enabled("billing")` 从 True 变 False;`app.startup` 不再 `_connect_billing_gateway()` → **计费网关静默不接线** |
| 调用链 | `app.startup` → `config.is_enabled` |
| gold | file=`config.py`,keywords=`billing/inverted/enabled/flag`,alternates=`app.py` |
| complexity | `cross-module` |
| 设计意图 | 无声逻辑反转,无崩溃;靠调用方语义判断 |

结果:native 1.0 / core 1.0。

### 1.4 `shipment-init-order` — 初始化丢字段

| 项 | 值 |
|---|---|
| 变异模块 | `src/fast_bench/shipping.py` |
| mutation | `__init__` 里删 `self.carrier`;`label()` 从 `f"{carrier}:{tracking}"` 变 `return tracking_code` |
| 引入的回归 | 调用方 `orders.ship` 拿 `shipment.label()`,标签静默丢 carrier(无崩溃) |
| 调用链 | `orders.ship` → `shipping.Shipment(...).label()` |
| gold | file=`shipping.py`,keywords=`carrier/label/tracking_code/initialized`,alternates=`orders.py` |
| complexity | `cross-module` |
| 设计意图 | 构造函数契约变更,属性访问在调用方 |

结果:native 0.67 / core 0.67(1 rep,重复发现 FP)。

### 1.5 `auth-swallow-exception` — 异常被吞

| 项 | 值 |
|---|---|
| 变异模块 | `src/fast_bench/auth.py` |
| mutation | `authenticate` 包上 `try/except Exception: return None` |
| 引入的回归 | 调用方 `session.open_session` 直接 `identity["user"]`,`authenticate` 失败返回 `None` → **TypeError(None subscript)** |
| 调用链 | `session.open_session` → `auth.authenticate` |
| gold | file=`auth.py`,keywords=`swallow/none/autherror/exception`,alternates=`session.py` |
| complexity | `cross-module` |
| 设计意图 | 静默失败;下游调用方期待抛异常而非 None |

结果:native 0.67 / core 0.67(重复发现 FP)。

### 1.6 `notify-required-arg` — 新增必选参数

| 项 | 值 |
|---|---|
| 变异模块 | `src/fast_bench/notify.py` |
| mutation | `send(recipient, message)` → `send(recipient, message, channel)` |
| 引入的回归 | 调用方 `service.send_order_confirmation` 只传两个实参 → **TypeError(missing positional)** |
| 调用链 | `service.send_order_confirmation` → `notify.send` |
| gold | file=`notify.py`,keywords=`missing/typeerror/channel/required`,alternates=`service.py` |
| complexity | `cross-module` |
| 设计意图 | 与 1.2 对称:这次是"加"参数而不是"丢"默认参数 |

结果:native **0.0** / core 1.0(1 rep;native 那次漏报)。

### 1.7 `same-name-callee` — 同名函数陷阱

| 项 | 值 |
|---|---|
| 变异模块 | `src/fast_bench/token.py` |
| mutation | `parse(token)` 返回 `token.strip()` → `token.strip() or None` |
| 引入的回归 | 调用方 `otp.validate_token` 对空 token 拿到 `None` → `None.startswith("sk-")` → **AttributeError** |
| 同名干扰 | `settings.parse(text) -> int`(另一个模块里的 `parse`,与 `token.parse` 同名)——grep `parse` 会命中两个 |
| 调用链 | `otp.validate_token` → `token.parse`(真调用方);`config_loader.load_timeout` → `settings.parse`(同名干扰,无关系) |
| gold | file=`token.py`,keywords=`none/attributeerror/startswith/blank/validate`,alternates=`otp.py` |
| complexity | `cross-module`, `same-name` |
| 设计意图 | graph 的 qname 精确解析出 `token::parse`,把同名 `settings::parse` 排除——这是 graph 的"歧义消解"卖点 |

结果(a-round,3 reps):native 1.0 / core 1.0 / summary 1.0 / search 1.0 / querygraph 1.0。

### 1.8 `deep-chain-contract` — 两跳深链

| 项 | 值 |
|---|---|
| 变异模块 | `src/fast_bench/storage.py` |
| mutation | `fetch(key)` 返回 `_blob_for(key)`(bytes)→ `f"blob:{key}"`(str) |
| 引入的回归 | `cache.load` → `decode(raw)` 对 str 调 `.decode("utf-8")` → **AttributeError**;调用点 `cache.load` 零线索(直接转发 `fetch` 返回值) |
| 调用链 | `portal.render_page` → `cache.load` → `storage.fetch` → `encoding.decode`(断点在第 3 跳) |
| gold | file=`storage.py`,keywords=`decode/attributeerror/bytes/str/fetch`,alternates=`cache.py`, `encoding.py` |
| complexity | `cross-module`, `deep-chain` |
| 设计意图 | 返回类型契约,断点离变更 2 跳;graph 的 out-edge 能沿链走 |

结果(a-round,3 reps):native 1.0 / core 1.0 / summary 1.0 / search 1.0 / querygraph [1.0, 1.0, 0.67]。

### 1.9 `large-noise` — 大仓库 + 诱饵(2026-08-21 重设计 v2)

| 项 | 值 |
|---|---|
| 变异模块 | `src/bigapp/config.py`(305 行) |
| mutation | `parse_config` 里 `timeout=_to_int(payload, "timeout", default=30)` → `timeout=_to_int(payload, "timeout")` —— 所有配置的 `timeout` 变 `None` |
| 真 bug(跨两跳) | `dispatch.build_plan` 无守卫 `compute_wait(cfg.timeout)` → `queue._to_millis(None)` = `None * 1000.0` → **TypeError** |
| 调用链 | `dispatch.build_plan` → `queue.compute_wait` → `queue._to_millis`(断点第 3 跳) |
| 噪音 | 14 个模块(~210 行/个),轮换 9 种 None-safe 兜底,7 种带 `* 1000`(短路边/回退兜底,不炸) |
| 诱饵 | `alerts._wait_before_alert` 同样调 `compute_wait` 但在调用点兜底 `cfg.timeout or DEFAULT_ALERTS_TIMEOUT` |
| gold | file=`config.py`,keywords=`typeerror/compute_wait/multiply/multiplication/millis/none/dispatch`,**min_matches=2**,alternates=`dispatch.py`, `queue.py` |
| complexity | `cross-module`, `large-noise` |
| prompt | `Review this proposed change to the configuration parser for concrete regressions it could introduce.`(无导航词) |
| 设计意图 | 7 点批评全落实:删导航词/中性文件名/差异化噪音/诱饵消费者/跨两跳故障/严格因果 gold |

结果(r5,5 modes × 5 reps):native **1.0** > search 0.867 > core 0.733 = summary 0.733 > querygraph 0.657。详见主报告 §3.2。

---

## 2. 两个 agent 的完整决策

> **数据来源说明**:eval 的 transcript 只保留最终 JSON findings(adapter 的 `normalize_claude_stream` 把 stream 消化掉了),**原始逐条 tool_use 轨迹默认不落盘**。为写本段,我用与 eval 完全相同的命令(claude `-p --stream-json --verbose` + 相同 MCP 配置 + 相同 prompt/diff/worktree)重跑了一次 native_agent 和 full_project_core,把原始 stream 完整保留下来,逐条抽取决策。以下轨迹是**真实重放的**。

下面两条轨迹是在同一 worktree(节点 399)、同一 prompt、同一 diff 下逐条抽取的真实重放。**native_agent 是 13 个工具调用,full_project_core 是 27 个**——这不只是"有没有 graph 工具"的差别,而是两种决策风格。

### 2.1 native_agent:纯 Bash+Read+Grep,0 个 MCP 工具(13 步)

| # | 调用 | 结果 / 学到了什么 |
|---|---|---|
| 1 | `Bash: git diff HEAD -- src/bigapp/config.py` | 看到唯一改动:`timeout=_to_int(payload, "timeout", default=30)` → `timeout=_to_int(payload, "timeout")`。**先看 diff,锁定根因候选。** |
| 2 | `Read config.py`(305 行全文) | 确认 `_to_int` 无 default 时返回 `None`;`timeout: int` 类型注解会被打破。 |
| 3 | `Bash: rg -n "parse_config\|load_config_from_file\|\.timeout\b\|timeout_seconds" --glob '!*.lock' .` | **用 grep 当自己的图**:枚举全部 `.timeout` / `parse_config` 消费者,输出命中 alerts.py:39(`cfg.timeout and cfg.timeout * 1000 or DEFAULT`)、analytics.py:38 等 `* 1000` 诱饵。 |
| 4 | `Read dispatch.py` | 看到 `build_plan` 里 `compute_wait(cfg.timeout)` **无守卫**——立即形成崩溃假设。 |
| 5 | `Bash: rg -n "def compute_wait" --glob '*.py'` | 定位崩溃函数在 queue.py。 |
| 6 | `Read queue.py` | 看到 `compute_wait` → `_to_millis(seconds)` = `seconds * 1000.0`。`None * 1000.0` → TypeError,链路闭合。 |
| 7 | `Read backfill.py` | **验证噪音模块**:`_resolve_chunk_timeout` 返回 `str(cfg.timeout)`——不乘 1000,无崩溃。放行。 |
| 8 | `Read export.py` | **验证另一个噪音模块**:`_effective_timeout(value, fallback)` 有兜底。放行。 |
| 9 | `Bash: rg -n "timeout" --glob '*test*' tests/` | 无 tests 目录(exit 2)。 |
| 10 | `Bash: ls -la && find . -name '*test*'` | 确认无测试。 |
| 11 | `Bash: python -c "…"` | **决定性一步:实际执行变异代码。** `parse_config({'name':…})` → `timeout=None`;`compute_wait(None)` → 打印 `dispatch path error: TypeError unsupported operand type(s) for *: 'NoneType' and 'float'`。崩溃被运行证实,而非推断。 |
| 12 | `Bash: rg -n "DEFAULT_TIMEOUT_SECONDS" .` | 确认 `DEFAULT_TIMEOUT_SECONDS = 30` 常量现在没有任何调用方(变相的孤儿常量)。 |
| 13 | `StructuredOutput` | **2 条 finding**:config.py:290(根因:删默认值使 timeout 变 None)+ dispatch.py:49(崩溃点:`compute_wait(None)` TypeError)。两条是**同一个 bug 的两面**,都在 gold/alternate 里。 |

**决策模式:假设驱动 + 定向验证 + 执行裁决。** 第 3 步的 grep 已经把 `* 1000` 诱饵(alerts:39/analytics:38)和所有消费者扫出来摆在眼前,但 native **没有去读 alerts.py / analytics.py**——因为它先有了"崩溃在 dispatch→queue"的假设,那些 `and…or` 短路模式不崩溃,直接从 grep 输出里排除;第 7、8 步只验证了它顺路撞见的两个噪音模块。最后用**运行代码**替代了一切推理上的不确定性。

### 2.2 full_project_core:graph 三件套 + Bash/Read(27 步)

| # | 调用 | 结果 / 学到了什么 |
|---|---|---|
| 1 | `get_change_summary {}` | **工具输出爆炸:169,745 字符,超出模型 token 预算,被截断成一个错误 + 指向保存文件的指针。** 这次调用基本失败了(agent 后来用 Bash `git diff` 绕开)。 |
| 2 | `Read config.py` | 读变异模块本体。 |
| 3 | `query_graph bigapp.config.parse_config`(dir=in) | **qname 格式错误**(用了 `.` 而不是 `::`)→ `"found": false`。 |
| 4 | `query_graph bigapp.config.AppConfig`(dir=in) | 同样格式错误 → `"found": false`。 |
| 5 | `search_symbol "timeout_seconds"` | 学到正确 qname 格式:`bigapp.config::AppConfig.timeout_seconds`。 |
| 6 | `search_symbol "timeout"` | **枚举出所有 `* 1000` 诱饵**:`bigapp.export::_effective_timeout`、`bigapp.alerts::_resolve_alert_timeout`、`bigapp.analytics::_resolve_metric_timeout`… 从这里开始,7 个诱饵解析器全部进入视野。 |
| 7 | `query_graph bigapp.config::parse_config`(dir=in) | 14 个调用方(每个带 call_site 与实参):alerts/analytics/archive/backfill/dashboard/export/gateway/…/dispatch 全在。 |
| 8–10 | `query_graph ::AppConfig.timeout_seconds`(out/in)、`::AppConfig`(in) | 补查属性与方法,只拿到 parse_config 一处。 |
| 11 | `Read gateway.py` | `_resolve_request_timeout` 用 `max(cfg.timeout or DEFAULT, 1)`——有守卫,放行。 |
| 12 | `Read alerts.py` | 看到 `_resolve_alert_timeout` = `cfg.timeout and cfg.timeout * 1000 or DEFAULT_ALERTS_TIMEOUT`(line 39)——**`* 1000` 诱饵。** |
| 13 | `Read export.py` | `_resolve_file_timeout` 同款 `* 1000` 模式。 |
| 14 | `Read dispatch.py` | **`build_plan` 无守卫 `compute_wait(cfg.timeout)`(line 49)——真崩溃点。** |
| 15 | `search_symbol "compute_wait"` | 定位 `bigapp.queue::compute_wait`。 |
| 16 | `Read queue.py` | `_to_millis` = `seconds * 1000.0`。 |
| 17 | `Bash: rg -n "cfg\.timeout\|\.timeout\b\|timeout_seconds" src` | 全仓库 `* 1000` 大扫除:alerts:39 / analytics:38(诱饵)、archive:36 / backfill:38、alerts:222(守卫)。 |
| 18 | `Bash: rg -n "_resolve_.*timeout\|DEFAULT_.*TIMEOUT" src` | 把所有 7 个 `_resolve_*_timeout` 与 `DEFAULT_*_TIMEOUT` 常量全部列出来。 |
| 19 | `Read backfill.py` | `_resolve_chunk_timeout` 返回 `str(cfg.timeout)`。 |
| 20 | `Bash: rg timeout tests/` | 无 tests 目录。 |
| 21 | `Bash: ls + find` | 确认无测试。 |
| 22 | `Bash: git log --oneline -10` | 看提交历史(buggy/fix 分支)。 |
| 23 | `Bash: rg -n '"timeout"\|timeout\s*[:=]' src` | 确认 config.py:290 的变异行 + 各模块 `timeout = _resolve_*_timeout(cfg)` 调用点。 |
| 24 | `Bash: git diff HEAD -- src/bigapp/config.py` | 补上被 get_change_summary 截断掉的 diff。 |
| 25 | `query_graph bigapp.dispatch::build_plan`(dir=in) | 无 in-edge。 |
| 26 | `query_graph bigapp.queue::compute_wait`(dir=in) | **graph 给出的最高价值调用**——两个 `compute_wait` 调用点带 call_site 实参一目了然:<br>`alerts._wait_before_alert:222` `cfg.timeout or DEFAULT_ALERTS_TIMEOUT`(守卫·诱饵)<br>`dispatch.build_plan:49` `cfg.timeout`(无守卫·真崩溃)<br>**判别器就在这一次调用里,agent 已经看到了。** |
| 27 | `StructuredOutput` | **3 条 finding**,全在 config.py:290:<br>① "…crashes dispatch.build_plan with TypeError"(**真 bug,命中 gold**)✓<br>② "Effective alerts/analytics timeouts drop from 30000 to 30"(alerts:39 / analytics:38 的 `* 1000` 诱饵)<br>③ "AppConfig public API/serialization emits null timeout and returns None from timeout_seconds"(API 序列化副作用) |

**决策模式:穷举枚举 + 全量报告。** search_symbol + query_graph + 两轮 rg 把所有 `timeout` 相关符号摊在桌面上,core 读的比 native 还多(gateway/alerts/export/backfill 全读),最后把**观察到的每一条后果都报成 finding**。它没有像 native 那样只抓崩溃假设然后验证——而是在第 26 步已经握着判别器的情况下,仍然报了 3 条。

### 2.3 对比:同一个 bug,为什么 native 1.0 而 core 0.733

**先澄清一个容易误读的点:两个 agent 都找到了真 bug。** native 通过"grep 枚举 + 定向读 + 执行",core 通过 `query_graph compute_wait(dir=in)` 一次拿到无守卫调用点。差距不在召回,在**报告的克制**:

| 维度 | native_agent | full_project_core |
|---|---|---|
| 工具调用 | 13(0 个 MCP) | 27(12 个 MCP:1 summary + 8 query_graph + 3 search) |
| 读文件 | 5 个(config/dispatch/queue/backfill/export) | 7 个(config/gateway/alerts/export/dispatch/queue/backfill)+ get_change_summary 被截断的大 dump |
| 是否执行代码 | **是**——`python -c` 复现 `TypeError: NoneType * float` | **否**——7 次 Bash 全是 rg/ls/git,一次都没跑代码 |
| 最终 finding | 2 条,同一个 bug 的两面(根因+崩溃点) | 3 条:1 真 bug + 2 个"真实但次要"的后果 |
| 每条 finding 的验证方式 | 读代码 + **运行证实** | 枚举 + 推理,未运行 |
| 决策风格 | 假设 → 走真实链路 → 执行裁决 | 枚举全部 → 每条后果都上报 |

**三个决定性差异:**

1. **执行是分水岭。** 两个 agent 都有 Bash、都能跑 python,但只有 native 跑了。`python -c` 把"会不会崩"从推理变成事实:parse_config 给出 `timeout=None`、`compute_wait(None)` 当场抛 TypeError。core 在第 24 步已经用 git diff 确认了变异,却停在推理层面,没有去跑那行代码。

2. **graph 给了 core 精确判别器,core 没用它收敛。** `query_graph compute_wait(dir=in)` 返回的两个调用点——alerts 带 `or DEFAULT` 守卫、dispatch 裸传——**正是**区分"诱饵"和"真崩溃"的全部信息。core 看到了,但它的 2 条 FP 来自更早的第 6/17/18 步:`search_symbol "timeout"` 和两轮 rg 把 7 个 `* 1000` 解析器全列出来后,core 把 alerts/analytics 的 30000→30 和 API null 也当 finding 报了出去。

3. **core 的两条 FP 不是幻觉,是"真实但不在 gold 里"的后果。** 诚实说明:core 第②条"alerts/analytics 30000→30"**算术上是对的**——`cfg.timeout and cfg.timeout * 1000 or DEFAULT` 在 timeout=None 时短路到 `DEFAULT`(30)而非固定版的 30000,真实值变化 1000 倍。但它命中的是一个**设计上"None 安全(不崩溃)"、却并不"保值"** 的诱饵:这种 `and…or` 短路是 case 设计里的噪音,严格 gold(`min_matches=2`,必须点名 TypeError 崩溃路径)据此判它超报。native 在第 3 步 grep 输出里同样看到 alerts:39,但因为持有"崩溃在 dispatch"假设 + 执行证实,把它当不崩溃的短路模式直接放过了——它甚至没去读 alerts.py。**诱饵防线恰好测出了两种审稿姿态:native 用假设+执行过滤噪音,core 用枚举把噪音也上报。**

**记分口径。** r5 聚合(5 次重复):native 1.0 > core 0.733。本次重放单跑:native pred=2(F1 0.667,config+dispatch 两条未合并)、core pred=3(F1 0.5,1 命中 + 2 超报)。native 的"合并成 1 条还是报 2 条"是模型波动——两条都是同一个 bug 的两面、都在 alternate 里,怎么报都不算错;core 的超报则来自可枚举的次要后果,与 r5 的机制一致。

### 2.4 附带发现(关于工具本身的)

- **`get_change_summary` 在本 case 的体量下输出爆炸**:169,745 字符,超出模型 token 预算,被截断成错误 + 文件指针。agent 被迫用 `Bash git diff` 绕过。工具没有按消费端预算裁剪输出——这是真实仓库也要面对的可用性问题。
- **`query_graph` 有 qname 学习成本**:core 前两次调用用 `.` 分隔(`bigapp.config.parse_config`)返回 `found: false`,靠 `search_symbol` 才学到 `::` 格式。图工具的 qname 契约对 agent 不是零成本。
- **`query_graph compute_wait(dir=in)` 是全场单次最高价值调用**:一次给全调用方 + call_site 实参,直接区分守卫与裸传。问题不在"图没用",在于**枚举模式把图的精确输出稀释成了 3 条**。
- **token 成本**:core 流 6320 行 vs native 3005(约 2.1× 事件数);Q1 分析里 core 平均 cache-read 194,867 tokens vs native 66,252,query_graph 的 payload 是最大成本杠杆。

---

## 3. 结论要点

- 前 8 个 case 全部 native ≥ graph:同一批 agent、同一个仓库,8 个 case 里没有一档 MCP 模式把 native 甩开。
- `large-noise` v2 重设计后 native 首次明确击败所有 MCP 模式(1.0 vs 0.66–0.87),机制是 graph 穷举枚举在诱饵压力下误报(见 §2 轨迹对比)。
- 两个方向都不裁决工具价值;真实仓库表(native 0.631 → core 0.833)才是唯一裁决。

*证据:`.code-review-ai/fast-eval/a-round.json`、`report-6x2.json`、`large-noise-v2-r5/report.json`、`.code-review-ai/trace-decision/{native_agent,full_project_core}.stream.jsonl`(原始决策轨迹,本次重放生成)*
