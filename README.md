# code-review-ai

Parse a codebase with tree-sitter, persist a call graph + call flows to SQLite, and expose impact-chain queries over MCP - so an AI reviewer (Claude Code, etc.) can pull just the relevant call chain instead of reading whole files.

Supports **Python, TypeScript, JavaScript, and Java**. Requires Python 3.14 (auto-fetched by `uv`, so you don't install it yourself).

## Install

You need [`uv`](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/Monkey-D-sj/code-review-ai
```

With optional Leiden community detection (Phase C):

```bash
uv tool install "code-review-ai[community] @ git+https://github.com/Monkey-D-sj/code-review-ai"
```

## Register with Claude Code (one command)

```bash
code-review-ai install --platform claude-code
```

This deploys the review docs + skills. By default it does **not** register
the MCP server globally — pass `--register-mcp` to make the graph tools
available to interactive sessions:

```bash
code-review-ai install --platform claude-code --register-mcp
```

No install step - run it straight from git with `uvx`:

```bash
uvx --from git+https://github.com/Monkey-D-sj/code-review-ai code-review-ai install --platform claude-code
```

Options: `--register-mcp`, `--scope user|project|local`, `--name <server-name>`, `--from <source>` (defaults to the git URL above; use `--from .` to register a local dev checkout).

### Manual registration

```bash
claude mcp add code-review-ai -s user -- uvx --from git+https://github.com/Monkey-D-sj/code-review-ai code-review-ai-mcp
```

### Install into the Codex user environment

Run the project installer to register the MCP server in the installing user's
`~/.codex/config.toml`, deploy the six review skills to `~/.codex/skills`, and
refresh the global `~/.codex/AGENTS.md` usage instructions:

```bash
code-review-ai install --platform codex
```

Restart Codex after installation. The command uses Codex's supported
`codex mcp add` flow, so the ChatGPT desktop app, Codex CLI, and IDE extension
share the configured MCP server.

### Install the Codex plugin (optional)

The repository root is also the Codex plugin. Its `skills/` entry is a link to
the same `code_review_ai/skills` files that the Python installer deploys, so
there is only one skill source. Add its repo-local marketplace, then install
**Code Review AI** from the Codex plugin UI:

```bash
codex plugin marketplace add .
```

The plugin bundles six review skills (`code-review-langs`,
`code-review-methodology`, and the Python, TypeScript, JavaScript, and Java
language rules) and registers the MCP server through `uvx`. After installation,
start a new Codex task so the skills and tools are available together.

For manual or CI-only MCP registration without the skills, configure:

```toml
[mcp_servers.code-review-ai]
command = "uvx"
args = ["--from", "git+https://github.com/Monkey-D-sj/code-review-ai", "code-review-ai-mcp"]
```

## MCP tools

- `rebuild_index` - build/rebuild the index from the working tree
- `get_impact` - impact chains for changed symbols (or derived from a git diff); direct callers/callees carry `call_site` code snippets (opt-out via `include_call_sites=false`); default `max_level=1` returns direct neighbors plus a `depth` summary (pass `max_level=0` for the full transitive closure); responses are JSON by default (pass `toon=true` for the compact TOON text encoding)
- `get_change_summary` - change summary from the git diff: stats (`summary`), `changed_functions` with per-function diffs, `uncovered_changes`, `delete_change`; responses are JSON by default (pass `toon=true` for the compact TOON text encoding)
- `get_change_context` - manual multi-symbol / directional (in/out/both) graph expansion; largely superseded by `get_impact` (which already carries direct call-site code), kept for ad-hoc graph queries
- `search_symbol` - find symbols by name; plain-word queries run FTS token match + bm25 ranking with a substring fallback on 0 hits, while queries containing `*`/`?` keep the short-name glob behavior
- `get_communities` / `get_community` - Leiden communities (opt-in via `community_detection`)

## CLI (manual use)

Two commands, on purpose. Everything else the graph can answer is an MCP tool
above, which is the interface the reviewer actually uses.

```bash
code-review-ai review --repo . --db .code-review-ai/index.db -o review.json
code-review-ai install --platform claude-code    # deploy skills + register the MCP server
```

`review --arm` picks how the review is done:

- `--arm graph` (default) — index-backed: the change summary becomes a worksheet
  of changed symbols, and `get_impact` supplies the call graph.
- `--arm nograph` — review the diff alone with `read_file` / `search_code`. No
  index is needed or created, so this works on a repo that was never indexed.

Both write the same JSON payload. `--max-turns` / `--max-tokens` bound a run.

### Built-in review loop

The package also includes a provider-neutral, read-only review loop. It talks
to an OpenAI-compatible endpoint directly, builds the change summary before
the first model request, and exposes only bounded `get_impact`, `read_file`,
and literal `search_code` tools. The model must finish by submitting a
structured report.

```bash
# .env (kept out of Git): OPENAI_API_KEY=...
code-review-ai review --repo . --db .code-review-ai/index.db \
  --model your-model --base-url https://your-provider.example/v1 \
  -o .code-review-ai/review.json
```

The local `.env` template also accepts `CRAI_REVIEW_MODEL` and
`CRAI_REVIEW_BASE_URL`, so a fully configured file lets
`code-review-ai review --repo .` run without model/key flags. Every built-in
agent reads the single `OPENAI_API_KEY` entry by default. The process
environment takes precedence over `.env`, and no CLI option accepts a plaintext
API key.

A review run is bounded: at most 50 model turns, and the run fails after 2
consecutive turns that neither resolve a worksheet row nor call a tool. The loop
is hand-rolled — it calls an OpenAI-compatible endpoint directly, with no agent
framework in between — so a run spends no tokens on framework overhead.

`review` writes live index/model/tool progress to stderr while reserving stdout
for the final JSON payload. Pass `--no-progress` for a quiet automation run;
`--visual` and `--no-visual` are accepted for compatibility and have no effect.

## Eval: does the index find the bug, and what does it cost?

`benchmarks/review_loop_case_compare.py` runs the 21 bug-injection cases in
`benchmarks/case-backend-cases.json` through both arms of `review` — the same
command a user runs, so the harness cannot drift from the product:

- **`graph`** — `review --arm graph`: worksheet mode, the index's change summary
  (changed symbols → candidate rows) plus `get_impact`'s call graph, resolved
  through `update_review_item`.
- **`nograph`** — `review --arm nograph`: free-form with no index tooling,
  `read_file` / `search_code` plus `finish_review`, seeing only the diff.

The arm is the only difference between a run and its counterpart, prompt
included. Both run under the same 25-turn / 150k-token budget, so the cost
columns are directly comparable. Scoring is one rule: a run hit if a reported
finding lands on the gold fix site (`fix_file`, or an alternate file — the same
regression can be repaired on either side of the broken contract). Whether the
index earns its keep is not a second score; it is read off the cost columns —
tokens, files read, tool calls. Finding the defect is the result; paying less
for the same result is the product.

```bash
uv run python benchmarks/review_loop_case_compare.py --runs 3
uv run python benchmarks/review_loop_case_compare.py \
  --case case-backend-decrypt-password-alias --runs 1 -o eval-results/smoke.json
```

The DeepSeek provider and pytest are declared in the `dev` dependency group, so
`uv run` installs them by default and a bare `uv run` is all this needs. (They
used to be an *extra*, which `uv run` does not sync — every bare run pruned them
out of the venv.) Add `--no-sync` only when a running `code-review-ai-mcp.exe`
holds the venv's file lock.

Each case is materialized once (copy, patch, index, diff) and every run reuses
it — the loop is read-only, so all runs of a case see identical input. Arms
alternate inside each repetition rather than one arm draining the batch first,
so a provider that degrades mid-batch hits both equally. The output keeps every
run's raw findings and tool trace, so the numbers can be recomputed later
without re-running a model.

## Config

Layered: defaults -> `[tool.code-review-ai]` in `pyproject.toml` (or a standalone `cr-ai.toml`) -> env `CRAI_<UPPER_KEY>`. Notable keys: `diff_base` (default `origin/main`), `entry_names`, `community_detection` (bool, default `false`; set `CRAI_COMMUNITY_DETECTION=1` to enable Leiden communities), `summary_source` (default `"diff"` — attaches each changed function's unified diff to `get_change_summary`; `"none"` keeps the metadata-only shape).

## How it works

One atomic SQLite transaction per rebuild:

```
parse (tree-sitter) -> resolve calls -> write nodes/edges (Phase A)
                                    -> build flows (Phase B)
                                    -> detect communities (Phase C, opt-in)
```

Impact query: for a changed symbol, slice its flows into upstream callers / downstream callees / affected entry points. Community query: the symbol's Leiden cluster and co-members - the horizontal blast radius.
