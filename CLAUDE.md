# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`code-review-ai` parses a Python codebase with tree-sitter, persists a call graph + call flows to SQLite, and exposes impact-chain queries over MCP so an AI reviewer can pull only the relevant call chain instead of reading whole files. The core is a plain library; `mcp_server.py` (primary) and `cli.py` (optional/manual) are thin frontends. Python 3.14, managed with `uv`. Single-language (Python) today, but the parser is data-driven for future grammars.

## Commands

```bash
uv sync --extra dev              # install deps incl. pytest
uv run pytest                    # run all tests (testpaths = ["tests"])
uv run pytest tests/test_flow_builder.py                 # one file
uv run pytest tests/test_flow_builder.py::test_linear_chain   # one test

uv run code-review-ai rebuild --repo . --db .code-review-ai/index.db   # build index
uv run code-review-ai query   --symbols auth::login        # impact for given symbols
uv run code-review-ai query   --files path/to/file.py      # impact via git diff of files
uv run code-review-ai search  "login"                       # glob-match symbol short names
uv run code-review-ai-mcp                                    # run the MCP server (stdio)
```

`rebuild`/`query`/`search` also accept no `--repo`/`--db` (defaults: `.` and `.code-review-ai/index.db`). Tests import `from conftest import Q` (where `Q = qname.join`) and `FIXTURES` (path to the synthetic repo in `tests/fixtures/repo`); `tests/` is on `sys.path` via the root conftest.

## Architecture: the rebuild pipeline

One atomic SQLite transaction in `indexer.rebuild` (orchestration only — writes are delegated to `_write_*` helpers):

```
git ls-files *.py
  → parser.parse_file        (tree-sitter AST → ParsedNode / RawCall / ImportEntry per file)
  → resolver.resolve_calls   (import-aware → Edge with resolution label)
  → _write_nodes / _write_edges          (Phase A: persist graph)
  → flow_builder.build_flows → _write_flows   (Phase B: materialize flows)
```

- **Phase A vs B are decoupled.** Phase B builds flows from the in-memory nodes/edges already produced by Phase A — it does **not** re-read the DB. The flow algorithm can change without touching parsing.
- `db.transaction()` wraps the whole rebuild; on failure it rolls back and the old WAL-committed index survives. Readers during a rebuild see the previous committed index (atomic switch).

### Module responsibilities (one each, no cycles)

`parser.py` tree-sitter → nodes/raw-calls/imports · `resolver.py` import-aware call resolution → edges · `flow_builder.py` adjacency + BFS → flows · `indexer.py` rebuild orchestration · `changes.py` git diff / files / symbols → changed qnames · `impact.py` membership slicing + edge fallback → impact · `watcher.py` watchfiles debounce → trigger rebuild · `config.py` layered config · `db.py` SQLite schema/WAL/txn · `mcp_server.py` / `cli.py` frontends.

## Conventions you must follow

**Qualified names go through `qname.py` — never build/split them by hand.** Format: `module::scope.scope.name` — `::` separates the module from the first scope, `.` separates nested scopes (e.g. `auth::login`, `auth::UserService.authenticate`). Use `qname.join(module, name, scope_qname=None)` and `qname.short(qname)`. The `::`/`.` split is deliberately distinct from Python's own `.` attribute access.

**Edge `resolution` is the trust signal for `target`:**
- `resolved` — `target` is a real qname present in `nodes`; the **only** edges that participate in flow traversal.
- `dynamic` — `obj.method()` form, unbound to a concrete class; `target` stores the raw expression, no node.
- `unresolved` — builtin / external lib / `from m import *`; `target` stores the raw name, no node.

dynamic/unresolved edges are kept (so the AI can see resolution gaps) but never enter `flow_builder`.

**Flow model — current implementation deliberately differs from the design spec.** `flow_builder.build_flows` emits **one flow per entry point**, BFS-flattening *all* reachable nodes into a single ordered `path` (visited set prevents cycles/diamond re-expansion). The design spec (`docs/superpowers/specs/2026-07-24-code-review-ai-design.md` §4.4) describes "one flow per reachable node via BFS shortest path" — that was superseded by recent refactors (see commit `cc346c6`). `flows.depth` is now unused (0) and `criticality` is NULL. Do not "fix" this back to per-node flows; the tests (`test_flow_builder.py`) assert the flat-single-flow behavior.

**Impact query** (`impact.get_impact`): for a changed symbol, look up `flow_memberships WHERE node_id = symbol`; within each flow, `position <` symbol = upstream callers, `position >` = downstream callees, and that flow's `entry_point` = an affected business entry. If the symbol is on no flow, fall back to direct `edges` lookups (`target=` callers, `source=` callees).

## Config

Layered in `config.py`: `DEFAULTS` dict → `[tool.code-review-ai]` in `pyproject.toml` (or a standalone `cr-ai.toml`) → env `CRAI_<UPPER_KEY>` (highest priority). Notable keys: `diff_base` (default `origin/main`), `entry_names` (glob patterns matched against a function's short name to identify entry points), `entry_decorators` (loaded but **not yet consumed** — entry-point matching is name-glob only), `watch_debounce_ms`.

## Frontends

MCP is the primary interface (`code-review-ai-mcp`): tools `rebuild_index`, `get_impact`, `search_symbol`, `get_symbol_detail`, `list_entry_points`. On startup it runs a catch-up rebuild if the index is stale (`is_stale` compares file mtimes to `build_meta.built_at`), then a daemon thread runs `watchfiles` to debounce-rebuild on `.py` changes. CLI (`code-review-ai`) mirrors `rebuild`/`query`/`search` for manual use.

## Design spec & dev history

The authoritative design doc (in Chinese) is `docs/superpowers/specs/2026-07-24-code-review-ai-design.md` — read it for intent on data model, resolution semantics, and lifecycle. Note where the code has since diverged (flow model, see above). `.superpowers/sdd/` holds task briefs/reports from the spec-driven build; `progress.md` tracks the 11 completed tasks.

## 代码架构规范（强制）

### 核心原则
- **高内聚**：每个函数/类单一职责。函数体 ≤ 50行，类 ≤ 300行。
- **低耦合**：业务层禁止直接依赖外部库（DB、IO、第三方API），必须通过接口/抽象层隔离。
- **长逻辑拆分**：任何包含 ≥3个步骤、或嵌套 ≥2层的逻辑，强制拆分为语义清晰的子函数。

### 代码组织
主控函数只做三件事：
1. 参数准备/校验
2. 调用子函数（编排顺序）
3. 返回结果

禁止在主控函数中写具体实现细节。
