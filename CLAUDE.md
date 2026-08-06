# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`code-review-ai` parses a Python codebase with tree-sitter, persists a call graph + call flows to SQLite, and exposes impact-chain queries over MCP so an AI reviewer can pull only the relevant call chain instead of reading whole files. The core is a plain library; `mcp_server.py` (primary) and `cli.py` (optional/manual) are thin frontends. Python 3.14, managed with `uv`. Supports Python, TypeScript, and JavaScript; the parser is data-driven — add a LANG entry + grammar to extend.

## Commands

```bash
uv sync --extra dev              # install deps incl. pytest
uv run pytest                    # run all tests (testpaths = ["tests"])
uv run pytest tests/test_flow_builder.py                 # one file
uv run pytest tests/test_flow_builder.py::test_linear_chain   # one test

uv run code-review-ai rebuild --repo . --db .code-review-ai/index.db   # build index
uv run code-review-ai query   --symbols auth::login        # impact for given symbols
uv run code-review-ai query   --files path/to/file.py      # impact via git diff of files
uv run code-review-ai test-impact --symbols auth::login    # which tests cover the changed symbols -> run only those
uv run code-review-ai summary --symbols auth::login   # change summary JSON (summary + changed_functions)
uv run code-review-ai summary                        # same, computed from the git diff of the whole tree
uv run code-review-ai query-graph auth::login                # graph neighborhood (in/out via resolved edges)
uv run code-review-ai query-graph auth::login --edge-kind call --direction both
uv run code-review-ai search  "login"                       # glob-match symbol short names
uv run code-review-ai communities [--symbol auth::login]    # list communities, or one symbol's community
uv run code-review-ai install --platform claude-code        # register MCP server with Claude Code (self-install)
uv sync --extra community                                    # opt: install leidenalg+igraph for Phase C
uv run code-review-ai-mcp                                    # run the MCP server (stdio)
```

`rebuild`/`query`/`search`/`communities` also accept no `--repo`/`--db` (defaults: `.` and `.code-review-ai/index.db`). Tests import `from conftest import Q` (where `Q = qname.join`) and `FIXTURES` (path to the synthetic repo in `tests/fixtures/repo`); `tests/` is on `sys.path` via the root conftest.

## Architecture: the rebuild pipeline

One atomic SQLite transaction in `indexer.rebuild` (orchestration only — writes are delegated to `_write_*` helpers):

```
git ls-files *.py
  → parser.parse_file        (tree-sitter AST → ParsedNode / RawCall / ImportEntry per file)
  → resolver.resolve_calls   (import-aware → Edge with resolution label)
  → _write_nodes / _write_edges          (Phase A: persist graph)
  → flow_builder.build_flows → _write_flows   (Phase B: materialize flows)
  → community.build_communities → _write_communities   (Phase C: communities + inter-community edges, opt-in)
```

- **Phase A vs B are decoupled.** Phase B builds flows from the in-memory nodes/edges already produced by Phase A — it does **not** re-read the DB. The flow algorithm can change without touching parsing.
- **Phase C (communities) is opt-in and degrades gracefully.** `community.build_communities` runs only when `config.community_detection` is set; it builds an undirected, edge-count-weighted graph from **structural** `resolution='resolved'` edges — `contains`, `import`, `inherits`, **not** call edges (symmetrized, self-loops dropped) — and partitions it via an injectable `partitioner` (default lazy-imports `leidenalg`/`igraph`, `seed=42`). Edge weighting is `config.community_weight`: `plain` (raw count), `degree_damped` (soft down-weight of edges incident to cross-module sink hubs like base classes / util modules), or `hub_pruned` (hard-cut the cross-module edges of cross-cutting sink hubs so they stop bridging communities while staying anchored to their home module via local edges; see `WeightMode`). If the libs are missing, `_write_communities` logs and skips — the rest of the index commits normally. Only nodes on a structural resolved edge get a community; isolates are excluded. `_write_communities` also persists the community graph's **inter-community edges** (`inter_community_edges` → `community_edges` table) so the visualization reads build output instead of re-deriving it.
- `db.transaction()` wraps the whole rebuild; on failure it rolls back and the old WAL-committed index survives. Readers during a rebuild see the previous committed index (atomic switch).

### Module responsibilities (one each, no cycles)

`parser.py` tree-sitter → nodes/raw-calls/imports · `resolver.py` import-aware call resolution → edges · `flow_builder.py` adjacency + BFS → flows · `community.py` Leiden community detection over structural edges (opt-in) → communities + `community_edges` · `indexer.py` rebuild orchestration · `export_graph.py` persisted graph/communities/flows → interactive HTML · `changes.py` git diff / files / symbols → changed qnames + change summary · `graph.py` resolved-edge neighborhood query → in/out neighbors · `impact.py` membership slicing + edge fallback → impact · `testimpact.py` reverse query filtered to test nodes → "run these tests" · `watcher.py` watchfiles debounce → trigger rebuild · `config.py` layered config · `db.py` SQLite schema/WAL/txn · `installer.py` self-install (register MCP via `claude mcp add`) · `mcp_server.py` / `cli.py` frontends.

## Conventions you must follow

**Qualified names go through `qname.py` — never build/split them by hand.** Format: `module::scope.scope.name` — `::` separates the module from the first scope, `.` separates nested scopes (e.g. `auth::login`, `auth::UserService.authenticate`). Use `qname.join(module, name, scope_qname=None)` and `qname.short(qname)`. The `::`/`.` split is deliberately distinct from Python's own `.` attribute access.

**Edge `resolution` is the trust signal for `target`:**
- `resolved` — `target` is a real qname present in `nodes`; the **only** edges that participate in flow traversal.
- `dynamic` — `obj.method()` form, unbound to a concrete class; `target` stores the raw expression, no node.
- `unresolved` — builtin / external lib / `from m import *`; `target` stores the raw name, no node.

dynamic/unresolved edges are kept (so the AI can see resolution gaps) but never enter `flow_builder`.

**Flow model — current implementation deliberately differs from the design spec.** `flow_builder.build_flows` emits **one flow per entry point**, BFS-flattening *all* reachable nodes into a single ordered `path` (visited set prevents cycles/diamond re-expansion). The design spec (`docs/superpowers/specs/2026-07-24-code-review-ai-design.md` §4.4) describes "one flow per reachable node via BFS shortest path" — that was superseded by recent refactors (see commit `cc346c6`). `flows.depth` is now unused (0) and `criticality` is NULL. Do not "fix" this back to per-node flows; the tests (`test_flow_builder.py`) assert the flat-single-flow behavior.

**Impact query** (`impact.get_impact`): for a changed symbol, look up `flow_memberships WHERE node_id = symbol`; within each flow, `position <` symbol = upstream callers, `position >` = downstream callees, and that flow's `entry_point` = an affected business entry. If the symbol is on no flow, fall back to direct `edges` lookups (`target=` callers, `source=` callees). The `tests` param (`"exclude"` default = business impact, drops test nodes; `"only"` = keep only test nodes; `"include"` = all) filters upstream/downstream/entries by `nodes.is_test`. `sym_pos` is always derived from the **unfiltered** membership so the changed symbol stays locatable in `"only"` mode; only the up/down node sets and entry query are filtered (so `_node_brief` is never called on a filtered-out id, which would hit its `str(node_id)` fallback).

**Test impact analysis** (`testimpact.get_test_impact`): given changed symbols, the tests that reach them (directly or transitively) -> "run only these tests". Built on `get_impact(tests="only")` - the reverse flow query restricted to `is_test=1` nodes - then grouped by test file. Test files must be indexed for this to work: by default `exclude` no longer drops `*/test*` (toggled via `test_globs`/`test_names`). A test function is a root (nothing calls it), so `build_flows` makes each test a flow entry point whose path covers everything it reaches - TIA reuses that reachability with no separate BFS. `list_entry_points` filters `is_test=0` to keep the business-entry contract once tests enter the graph.

**Community detection** (`community.build_communities` / `community.get_community`): communities persist to `communities` + `community_memberships`, and the community graph's cross-community edges to `community_edges` — all mirrors of `flows`/`flow_memberships`. `inter_community_edges` computes the edge table from the same structural (non-call) resolved edge set detection used, so the visualizer renders exactly what build produced. `get_community(qname)` returns the symbol's community label, modularity, and co-members — the *horizontal* blast radius, complementing impact's *vertical* caller/callee chains. A symbol on no structural edge returns `not in any community`.

## Config

Layered in `config.py`: `DEFAULTS` dict → `[tool.code-review-ai]` in `pyproject.toml` (or a standalone `cr-ai.toml`) → env `CRAI_<UPPER_KEY>` (highest priority). Notable keys: `diff_base` (default `origin/main`), `entry_names` (glob patterns matched against a function's short name to identify entry points), `entry_decorators` (loaded but **not yet consumed** — entry-point matching is name-glob only), `watch_debounce_ms`, `community_detection` (bool, default `false` — gates Phase C; env `CRAI_COMMUNITY_DETECTION`), `community_weight` (str, default `"plain"` - Phase C edge weighting; `"degree_damped"` soft-down-weights edges incident to cross-module sink hubs; `"hub_pruned"` hard-cuts the cross-module edges of cross-cutting sink hubs so they stop bridging communities while staying with their home module; env `CRAI_COMMUNITY_WEIGHT`), `exclude` (default no longer contains `*/test*` - test files are indexed so test-impact analysis works; re-add `*/test*` to opt out), `test_globs` (path globs tagging test files, default `["*/tests/*", "test_*.py"]` - directory or filename-prefix, deliberately not `*/test*` which would mis-tag a production module whose name merely starts with "test", e.g. `testimpact.py`) and `test_names` (short-name globs tagging test functions, default `["test_*"]`) - both feed `nodes.is_test` (via `parser.is_test_node`, matched against the repo-relative path) and are part of `config_hash`.

## Frontends

MCP is the primary interface (`code-review-ai-mcp`): tools `rebuild_index`, `get_impact`, `get_test_impact`, `get_change_summary`, `query_graph`, `search_symbol`, `get_symbol_detail`, `list_entry_points`, `get_communities`, `get_community`. On startup it runs a catch-up rebuild if the index is stale (`is_stale` compares file mtimes to `build_meta.built_at`), then a daemon thread runs `watchfiles` to debounce-rebuild on `.py` changes. CLI (`code-review-ai`) mirrors `rebuild`/`query`/`test-impact`/`search`/`communities` for manual use, plus `install --platform claude-code` which self-registers the MCP server (shells out to `claude mcp add` with a `uvx --from <git-url> code-review-ai-mcp` launch command; see `installer.py`). `graph` (`export_graph.py`) renders the index as interactive HTML: `-m communities` draws the persisted community graph (bubbles sized by node count, cross-community edges read straight from `community_edges` — never re-derived), `-m graph` the raw function-level call graph, `-m flow` the BFS flow chains.

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

### 代码编写
- 禁止使用单字母变量名（如 i、j、k、x、y）作为变量，除非它明确表示数学索引。
- 禁止使用内置函数名作为变量名（如 id、list、dict、str）
- 循环变量必须使用有意义的英文单词或业界通用缩写（如 user_id、item、student）
- 示例：❌ `for i in ids` → ✅ `for source in ids`

