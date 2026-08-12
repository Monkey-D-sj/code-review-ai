# Impact Routing 实现计划

> **已废弃(superseded,2026-08-12):** 本计划实现的数值 `risk` 评分已移除,深度判定并入决策表类别(跨服务/删除/被跨模块调用的接口变更 → get_impact;其他需上下文 → query_graph 上游;私有 → 只读直接调用点)。离线验证证伪了「risk 高分才值得升级 get_impact」(Pearson −0.21),`assess_symbol_risk`、`agent-eval-route-check` 子命令、`route_check_analysis` 及配套测试均已删除;最终设计见 `docs/superpowers/specs/2026-08-09-impact-routing-design.md`。本文件仅保留作历史。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给变更摘要加 per-node 风险评分,让评审 prompt 默认 query_graph 看上游、仅高风险才升级 get_impact,并用现有 baseline transcripts 离线验证风险信号。

**Architecture:** 风险评分是一个纯 SQL 的单符号函数(`assess_symbol_risk`),挂在 `changes.py` 的 change summary 上;评审行为通过 `hooks.py` 的 `_REVIEW_PROMPT` 文案引导;验证通过 `agent_eval_analysis.py` 新聚合函数 + `cli.py` 新子命令读取已有 transcripts,不跑新 agent。

**Tech Stack:** Python 3.14(uv)、SQLite(stdlib sqlite3)、pytest。无新依赖。

## Global Constraints

- qname 一律走 `code_review_ai.qname` 的 `join`/`short`,绝不手工拼接或拆分(`module::scope.name`)。
- `assess_symbol_risk` 返回 0–100 整数。语义:删除→90;有跨模块(file_path 不同)resolved 入边→`min(100, 60 + 10×n)`;只有同模块入边→`min(59, 30 + 5×n)`;解析到且零入边(叶子)→10;未解析→50。
- `query_graph` 方向语义:`in`=调用方(上游)、`out`=被调方(下游)、`both`=两者(`code_review_ai/graph.py` 的 `VALID_DIRECTIONS`)。
- prompt 必须保持 `get_change_summary` 出现在 `get_impact` 之前(`tests/test_hooks.py` 断言)。
- 不新增任何配置项、不新增依赖。route-check 只读 stdlib。

---

### Task 1: `assess_symbol_risk` 风险评分 + 单元测试

**Files:**
- Modify: `code_review_ai/changes.py`(在 `detect_changed_symbols` 之后新增函数)
- Test: `tests/test_changes.py`

**Interfaces:**
- Produces: `assess_symbol_risk(conn: sqlite3.Connection, symbol: str, deleted: bool = False) -> int` — Task 2/4 消费。

- [ ] **Step 1: 写失败测试**

在 `tests/test_changes.py` 追加:

```python
def _seed_risk_graph(conn):
    """a.py::target 有同模块+跨模块 caller; a.py::leaf 只有同模块 caller;
    b.py::external 无 caller。"""
    for qname, kind, file_path in [
            ("a::target", "function", "a.py"),
            ("a::leaf", "function", "a.py"),
            ("a::caller", "function", "a.py"),
            ("b::external", "function", "b.py"),
    ]:
        conn.execute("INSERT INTO nodes(qualified_name, kind, file_path) "
                     "VALUES(?,?,?)", (qname, kind, file_path))
    for source, target in [("a::caller", "a::target"),  # 同模块
                           ("b::external", "a::target"),  # 跨模块
                           ("a::caller", "a::leaf")]:    # 同模块
        conn.execute("INSERT INTO edges(source, target, kind, resolution) "
                     "VALUES(?,?,?,?)", (source, target, "call", "resolved"))


def test_assess_symbol_risk_rules(tmp_path):
    from code_review_ai.changes import assess_symbol_risk
    conn = _conn(str(tmp_path / "risk.db"))
    _seed_risk_graph(conn)
    assert assess_symbol_risk(conn, "a::target") == 70      # 跨模块入边 -> 60+10
    assert assess_symbol_risk(conn, "a::leaf") == 35        # 同模块入边 -> 30+5
    assert assess_symbol_risk(conn, "b::external") == 10    # 叶子
    assert assess_symbol_risk(conn, "nope::missing") == 50  # 未解析
    assert assess_symbol_risk(conn, "a::target", deleted=True) == 90  # 删除


def test_assess_symbol_risk_caps_cross_module(tmp_path):
    from code_review_ai.changes import assess_symbol_risk
    conn = _conn(str(tmp_path / "risk.db"))
    conn.execute("INSERT INTO nodes(qualified_name, kind, file_path) "
                 "VALUES('a::hub','function','a.py')")
    for index in range(1, 6):  # 5 个跨模块 caller -> 60+50=110 -> 截断 100
        conn.execute("INSERT INTO nodes(qualified_name, kind, file_path) "
                     "VALUES(?,?,?)", (f"b::c{index}", "function", "b.py"))
        conn.execute("INSERT INTO edges(source, target, kind, resolution) "
                     "VALUES(?,?,?,?)", (f"b::c{index}", "a::hub", "call", "resolved"))
    assert assess_symbol_risk(conn, "a::hub") == 100
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_changes.py -k assess_symbol_risk -v`
Expected: FAIL with `ImportError: cannot import name 'assess_symbol_risk'`

- [ ] **Step 3: 实现**

在 `code_review_ai/changes.py` 的 `detect_changed_symbols` 之后新增:

```python
def assess_symbol_risk(conn, symbol: str, deleted: bool = False) -> int:
    """0-100 blast-radius score for one changed symbol (spec 2026-08-09).

    deleted -> 90; any cross-module resolved caller -> min(100, 60+10*n);
    same-module callers only -> min(59, 30+5*n); resolved leaf -> 10;
    unresolved (not in graph) -> 50. Cross-module means the caller node's
    file_path differs from the target's (module == file in this graph).
    """
    if deleted:
        return 90
    target = conn.execute(
        "SELECT file_path FROM nodes WHERE qualified_name=?", (symbol,)).fetchone()
    if target is None:
        return 50
    target_file = target["file_path"]
    incoming = conn.execute(
        "SELECT DISTINCT source FROM edges WHERE target=? AND resolution='resolved'",
        (symbol,)).fetchall()
    cross = same = 0
    for edge in incoming:
        source = conn.execute(
            "SELECT file_path FROM nodes WHERE qualified_name=?",
            (edge["source"],)).fetchone()
        if source is None:
            continue
        if source["file_path"] != target_file:
            cross += 1
        else:
            same += 1
    if cross:
        return min(100, 60 + 10 * cross)
    if same:
        return min(59, 30 + 5 * same)
    return 10
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_changes.py -k assess_symbol_risk -v`
Expected: PASS(2 tests)

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/changes.py tests/test_changes.py
git commit -m "feat(changes): assess_symbol_risk 0-100 blast-radius score"
```

---

### Task 2: 变更摘要记录附加 `risk`

**Files:**
- Modify: `code_review_ai/changes.py`(`_symbols_summary`、`_delete_change`、`build_change_summary`)
- Test: `tests/test_changes.py`(更新 `test_build_change_summary_diff_path` 的精确 dict 断言)

**Interfaces:**
- Consumes: `assess_symbol_risk`(Task 1)
- Produces: `build_change_summary` 的每条 `changed_functions[i]["risk"]`(int)与每条 `delete_change[i]["risk"]`(90)。

- [ ] **Step 1: 写失败测试**

更新 `test_build_change_summary_diff_path` 的断言(空 conn 下 `auth::login` 未解析 → 50):

```python
    assert out["changed_functions"] == [
        {"qname": Q("auth", "login"), "kind": "function",
         "file": "auth.py", "start_line": 6, "end_line": 7, "risk": 50}]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_changes.py::test_build_change_summary_diff_path -v`
Expected: FAIL(实际无 `risk` 键,`assert out["changed_functions"] == [...]` 不匹配)

- [ ] **Step 3: 实现**

在 `code_review_ai/changes.py` 三处附加 `risk`:

`_symbols_summary` 内,把两条 `records.append(...)` 前先算 risk——将循环体改为:

```python
        rel = _relative_to_repo(config, row["file_path"])
        files.add(rel)
        record = {"qname": symbol, "kind": row["kind"], "file": rel,
                  "start_line": row["start_line"], "end_line": row["end_line"],
                  "risk": assess_symbol_risk(conn, symbol)}
        records.append(record)
```

`_delete_change` 内 `file_records` 的 dict 字面量加 `"risk": 90,`(放在 `"is_test"` 之后):

```python
            "signature": row["signature"], "is_test": row["is_test"],
            "risk": 90,
            "upstream": [{"source": u["source"], "kind": u["kind"],
```

`build_change_summary` 的 diff 路径,在 `functions, uncovered = _diff_coverage(...)` 之后加:

```python
    for record in functions:
        record["risk"] = assess_symbol_risk(conn, record["qname"])
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_changes.py tests/test_mcp_server.py::test_get_change_summary_tool -v`
Expected: PASS(MCP 测试只断言字段取值,不因新增键失败)

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/changes.py tests/test_changes.py
git commit -m "feat(changes): attach risk score to change summary records"
```

---

### Task 3: `_REVIEW_PROMPT` 修订为自包含测试 + 方向默认 + 风险门槛

**Files:**
- Modify: `code_review_ai/hooks.py`(`_REVIEW_PROMPT` 字面量)
- Test: `tests/test_hooks.py`

**Interfaces:**
- Produces: 新 `_REVIEW_PROMPT`(单行、无换行符、无单引号,保持 `get_change_summary` 先于 `get_impact`)。

- [ ] **Step 1: 写失败测试**

在 `tests/test_hooks.py` 追加:

```python
def test_install_hooks_review_prompt_contains_risk_routing(tmp_path):
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    written = install_hooks(str(repo), str(tmp_path / "i.db"), with_review=True)
    content = Path(written[HOOK_NAMES.index("post-commit")]).read_text(encoding="utf-8")
    assert "自包含" in content            # 重要性判据
    assert "query_graph" in content       # 默认动作
    assert "direction=in" in content      # 默认看上游
    assert "risk" in content              # 风险门槛
    assert content.index("get_change_summary") < content.index("get_impact")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_hooks.py::test_install_hooks_review_prompt_contains_risk_routing -v`
Expected: FAIL(`自包含` 不在 hook 脚本中)

- [ ] **Step 3: 实现**

替换 `code_review_ai/hooks.py` 中 `_REVIEW_PROMPT` 整个字面量(保持相邻字符串拼接、无实际换行):

```python
_REVIEW_PROMPT = (
    "对以下代码变更影响做代码评审。输入是 code-review-ai 生成的变更摘要 JSON(含各函数的 risk 评分)。"
    "1. get_change_summary 确认变更明细与各函数的 risk。"
    "2. 对每个变更函数,先判断它是否自包含:只凭 diff 与该函数自身的代码,"
    "能否完整判断这次改动的正确性与影响范围?能(纯注释/文档/改名/格式化、仅函数内部局部计算、"
    "不改对外签名/返回类型/异常语义、不改变调用方依赖的行为)→ 直接按 diff 评审,不查上下文;"
    "不能(改了对外签名/返回类型/新增异常、改变了调用方依赖的语义、新增/移除跨模块调用、"
    "路由/DI 装配、被其他模块调用且改动可能破坏它们)→ 需要上下文;拿不准按需要上下文处理。"
    "3. 需要上下文的真实改动 → 默认用 query_graph 看该函数的上游(direction=in,即调用方):"
    "改动一个函数,最可能的破坏在调用它的人。仅当改动涉及下游时才同时看下游(direction=out):"
    "改了传给被调方的入参/实参、新增或移除对某函数的调用、返回值被下游进一步消费等;"
    "函数自身签名入参变化(如新增必填参数)砸的是调用方,归入上游。"
    "4. 仅当该函数 risk ≥ 60(跨模块/删除)且改动重要 → 追加 get_impact 查完整影响链"
    "(上游调用方、受影响业务入口)。"
    "5. search_symbol / Read 按需补充;不要用 git diff / grep 自己重算。"
    "再按语言用 code-review 系列 skill 评审,按 error / warning / info 三级输出发现,"
    "每条给出文件、行号、问题描述与具体失败场景,用中文回答。"
)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_hooks.py -v`
Expected: PASS(含原有的 ordering 测试与新增测试)

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/hooks.py tests/test_hooks.py
git commit -m "feat(hooks): route impact context by importance and risk score"
```

---

### Task 4: `agent-eval-route-check` 离线验证

**Files:**
- Modify: `code_review_ai/agent_eval_analysis.py`
- Modify: `code_review_ai/cli.py`
- Test: `tests/test_agent_eval_analysis.py`

**Interfaces:**
- Consumes: `load_agent_cases(path) -> list[AgentEvalCase]`(from `code_review_ai.agent_eval`)、`assess_symbol_risk`(Task 1)。
- Produces: `route_check_analysis(conn, cases, runs_dir) -> dict`(per-case 表 + Pearson 相关 + ≥60/<60 分组);CLI 子命令 `agent-eval-route-check`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_agent_eval_analysis.py` 追加。文件头现有 import 为 `import pytest` 与 `from code_review_ai.agent_eval_analysis import analyze_agent_report`;需补 `import json` 与 `from pathlib import Path`:

```python
def _seed_transcript(runs_dir, case_id, mode, repetition, f1):
    path = Path(runs_dir) / case_id / mode
    path.mkdir(parents=True, exist_ok=True)
    (path / f"run-{repetition}.json").write_text(
        json.dumps({"result": {"f1": f1}}), encoding="utf-8")


def test_route_check_analysis_groups_by_risk(tmp_path):
    from code_review_ai.agent_eval import load_agent_cases
    from code_review_ai.agent_eval_analysis import route_check_analysis
    from code_review_ai.db import connect, init_schema

    conn = connect(str(tmp_path / "r.db"))
    init_schema(conn)
    for qname, file_path in [("a::target", "a.py"), ("b::external", "b.py")]:
        conn.execute("INSERT INTO nodes(qualified_name, kind, file_path) VALUES(?,?,?)",
                     (qname, "function", file_path))
    conn.execute("INSERT INTO edges(source, target, kind, resolution) "
                 "VALUES('b::external','a::target','call','resolved')")  # a::target 跨模块

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps([
        {"id": "case-high", "prompt": "p", "diff": "x",
         "changed_symbols": ["a::target"],
         "gold_findings": [{"id": "g", "file": "a.py", "keywords": ["k"]}]},
        {"id": "case-low", "prompt": "p", "diff": "x",
         "changed_symbols": ["b::external"],
         "gold_findings": [{"id": "g", "file": "b.py", "keywords": ["k"]}]},
    ]), encoding="utf-8")
    cases = load_agent_cases(str(manifest_path))

    runs_dir = tmp_path / "runs"
    _seed_transcript(runs_dir, "case-high", "diff_only", 1, 0.3)
    _seed_transcript(runs_dir, "case-high", "graph_agent", 1, 0.9)
    _seed_transcript(runs_dir, "case-high", "hybrid_agent", 1, 0.8)
    _seed_transcript(runs_dir, "case-low", "diff_only", 1, 0.8)
    _seed_transcript(runs_dir, "case-low", "graph_agent", 1, 0.4)
    _seed_transcript(runs_dir, "case-low", "hybrid_agent", 1, 0.5)

    analysis = route_check_analysis(conn, cases, str(runs_dir))
    by_case = {row["case_id"]: row for row in analysis["cases"]}
    assert by_case["case-high"]["max_risk"] == 70
    assert by_case["case-high"]["graph_delta_f1"] == 0.6
    assert by_case["case-low"]["max_risk"] == 10
    assert by_case["case-low"]["graph_delta_f1"] == -0.4
    assert analysis["correlation"]["graph_delta_f1"] > 0   # 高风险 -> 图更有利
    assert analysis["groups"]["high_risk"]["mean_graph_delta"] == 0.6
    assert analysis["groups"]["low_risk"]["mean_graph_delta"] == -0.4
```

文件头补 import:`import json` 与 `from pathlib import Path`(若未引入)。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_agent_eval_analysis.py::test_route_check_analysis_groups_by_risk -v`
Expected: FAIL(`ImportError: cannot import name 'route_check_analysis'`)

- [ ] **Step 3: 实现**

在 `code_review_ai/agent_eval_analysis.py` 顶部加 import(无循环:agent_eval/changes 均不 import 本模块):

```python
from code_review_ai.agent_eval import AgentEvalCase
from code_review_ai.changes import assess_symbol_risk
```

在文件末尾追加:

```python
def route_check_analysis(conn, cases: list[AgentEvalCase],
                         runs_dir: str) -> dict:
    """Per-case max risk vs impact-context F1 delta over existing transcripts.

    Reads <runs_dir>/<case_id>/<mode>/run-*.json (each record has
    result.f1), averages per mode, and compares graph/hybrid against
    diff_only. Confirms the risk signal: high-risk cases should benefit
    from impact context, low-risk cases should not.
    """
    per_case = []
    for case in cases:
        risks = [assess_symbol_risk(conn, symbol) for symbol in case.changed_symbols]
        if not risks:
            continue
        f1 = _case_mode_f1(runs_dir, case.case_id)
        baseline = _mean(f1.get("diff_only", []))
        per_case.append({
            "case_id": case.case_id,
            "max_risk": max(risks),
            "graph_delta_f1": round(_mean(f1.get("graph_agent", [])) - baseline, 4),
            "hybrid_delta_f1": round(_mean(f1.get("hybrid_agent", [])) - baseline, 4),
        })
    risk_values = [row["max_risk"] for row in per_case]
    return {
        "case_count": len(per_case),
        "cases": per_case,
        "correlation": {
            "graph_delta_f1": _pearson(risk_values,
                                       [row["graph_delta_f1"] for row in per_case]),
            "hybrid_delta_f1": _pearson(risk_values,
                                        [row["hybrid_delta_f1"] for row in per_case]),
        },
        "groups": {
            "high_risk": _group_summary([row for row in per_case if row["max_risk"] >= 60]),
            "low_risk": _group_summary([row for row in per_case if row["max_risk"] < 60]),
        },
    }


def _case_mode_f1(runs_dir: str, case_id: str) -> dict[str, list[float]]:
    """{mode: [f1,...]} from transcripts <runs_dir>/<case_id>/<mode>/run-*.json."""
    case_dir = Path(runs_dir) / case_id
    if not case_dir.is_dir():
        return {}
    by_mode: dict[str, list[float]] = defaultdict(list)
    for mode_path in sorted(case_dir.iterdir()):
        if not mode_path.is_dir():
            continue
        for run_file in sorted(mode_path.glob("run-*.json")):
            record = json.loads(run_file.read_text(encoding="utf-8"))
            f1 = record.get("result", {}).get("f1")
            if isinstance(f1, (int, float)):
                by_mode[mode_path.name].append(float(f1))
    return by_mode


def _group_summary(rows: list[dict]) -> dict:
    return {
        "n": len(rows),
        "mean_graph_delta": round(_mean([r["graph_delta_f1"] for r in rows]), 4) if rows else 0.0,
        "mean_hybrid_delta": round(_mean([r["hybrid_delta_f1"] for r in rows]), 4) if rows else 0.0,
        "graph_positive": sum(r["graph_delta_f1"] > 0 for r in rows),
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation; None when n<2 or zero variance."""
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denom_x = sum((x - mean_x) ** 2 for x in xs)
    denom_y = sum((y - mean_y) ** 2 for y in ys)
    if denom_x == 0 or denom_y == 0:
        return None
    return round(numerator / (denom_x * denom_y) ** 0.5, 4)
```

在 `code_review_ai/cli.py` 两处修改(均唯一,无歧义):

**A. 子命令定义** — 放在 `ae = sub.add_parser("agent-eval")` 那段(约 `--runs-dir`/`-o` 定义)之后:

```python
    rc = sub.add_parser("agent-eval-route-check")
    _add_common(rc)
    rc.add_argument("--cases", required=True)
    rc.add_argument("--runs-dir", required=True)
    rc.add_argument("-o", "--out")
```

**B. 处理分支** — 放在共享区 `conn = _conn(args.db)` 之后、`if args.cmd == "rebuild":` 之前(注意是带 `return` 的独立 `if`,不是 elif 链;它需要 cfg/conn 已建立):

```python
    if args.cmd == "agent-eval-route-check":
        from code_review_ai.agent_eval_analysis import route_check_analysis
        try:
            rebuild(cfg, conn)
            cases = load_agent_cases(args.cases)
            payload = route_check_analysis(conn, cases, args.runs_dir)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _write_json(payload, args.out)
        return 0
```

`load_agent_cases` 已在 cli.py 顶部从 `code_review_ai.agent_eval` 导入,无需局部重复导入。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_agent_eval_analysis.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add code_review_ai/agent_eval_analysis.py code_review_ai/cli.py tests/test_agent_eval_analysis.py
git commit -m "feat(eval): agent-eval-route-check offline risk correlation"
```

---

### Task 5: 对真实 baseline 运行 route-check 并记录

**Files:**
- Modify: `benchmarks/AGENT_EVAL_BASELINE.md`

- [ ] **Step 1: 运行离线验证**

Run:
```bash
uv run code-review-ai agent-eval-route-check \
  --cases benchmarks/agent-eval-real-10.json \
  --runs-dir .code-review-ai/agent-eval-real-10-r3 \
  --repo . --db .code-review-ai/index.db
```
Expected: 输出 JSON,含 10 个 case 的 `max_risk` 与 graph/hybrid delta、相关系数、分组表。符号未解析的 case 会显示 `max_risk: 50`(属预期,写进结果即可)。

- [ ] **Step 2: 记录到 baseline 文档**

在 `benchmarks/AGENT_EVAL_BASELINE.md` 的 `## Next experiment` 之前追加 `## Impact-routing offline validation` 一节:贴出 per-case 表 + 相关系数 + 分组表,并写 2–3 句解读——`max_risk ≥ 60` 组 graph/hybrid delta 是否为正、`< 60` 组是否为负/≈0,对应「高风险才值得升级 get_impact」是否成立。若数据不支持结论,如实说明(不为了好看改数字)。

- [ ] **Step 3: 提交**

```bash
git add benchmarks/AGENT_EVAL_BASELINE.md
git commit -m "docs(benchmark): record impact-routing offline validation"
```
