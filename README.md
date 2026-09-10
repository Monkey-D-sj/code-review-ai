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
the MCP server globally: the post-commit review hook (see *Review hooks*)
injects the graph tools on-demand via `--strict-mcp-config`, so everyday
interactive sessions never load the ~1.5k tokens of tool descriptions. To
register globally for interactive manual review, add `--register-mcp`:

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

```bash
code-review-ai rebuild --repo .                        # build index
code-review-ai query   --symbols auth::login           # impact for given symbols
code-review-ai query   --files path/to/file.py         # impact via git diff of files
code-review-ai search  "login" [--limit 50]             # full-text (FTS) or glob (*login*) symbol search
code-review-ai communities [--symbol auth::login]       # list communities / one symbol's community
```

`rebuild`/`query`/`search`/`communities` also accept no `--repo`/`--db` (defaults: `.` and `.code-review-ai/index.db`).

### Deterministic context plan (no LLM)

Route a change to `local` or `graph` and build one bounded evidence package
using only git diff, tree-sitter and the local SQLite index:

```bash
code-review-ai context-plan --max-chars 8000 -o eval-results/context-plan.json
```

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

### Visualization (`graph`)

Export interactive HTML graphs of the call structure:

```bash
code-review-ai graph -m communities -o communities.html   # community bubble chart (default)
code-review-ai graph -m graph       -o callgraph.html     # raw function-level force graph
code-review-ai graph -m flow        -o flows.html         # flow chart (BFS call chains)
```

Options: `-n` max items (200), `-m` mode (communities|graph|flow), `-o` output path.

## Eval: does the index find the bug, and what does it cost?

`benchmarks/review_loop_case_compare.py` runs the review loop over the 21
bug-injection cases in `benchmarks/case-backend-cases.json`, twice per run:

- **`graph`** — worksheet mode: the index's change summary (changed symbols →
  candidate rows) plus `get_impact`'s call graph, resolved through
  `update_review_item`.
- **`nograph`** — free-form with no index tooling: `read_file` / `search_code`
  plus `finish_review`, seeing only the diff. That is a no-graph reviewer's
  input.

Both arms share one model instance and one 25-turn / 150k-token budget, so the
cost columns are directly comparable. Scoring is one rule: a run hit if a
reported finding lands on the gold fix site (`fix_file`, or an alternate file —
the same regression can be repaired on either side of the broken contract).
Whether the index earns its keep is not a second score; it is read off the cost
columns — tokens, files read, tool calls. Finding the defect is the result;
paying less for the same result is the product.

```bash
uv run --no-sync python benchmarks/review_loop_case_compare.py --runs 3
uv run --no-sync python benchmarks/review_loop_case_compare.py \
  --case case-backend-decrypt-password-alias --runs 1 -o eval-results/smoke.json
```

`--no-sync` is not optional here: a bare `uv run` re-syncs the environment to the
project's *default* dependency set, which would uninstall the `deepseek` provider
package this harness needs (and `pytest` with it), and it can also collide with a
running `code-review-ai-mcp.exe` holding the venv's file lock.

Each case is materialized once (copy, patch, index, diff) and every run reuses
it — the loop is read-only, so all runs of a case see identical input. Arms
alternate inside each repetition rather than one arm draining the batch first,
so a provider that degrades mid-batch hits both equally. The output keeps every
run's raw findings and tool trace, so the numbers can be recomputed later
without re-running a model.

## Automating review

The index keeps itself fresh automatically (watcher + git hooks + MCP startup
catch-up); firing the *review* itself needs one extra hook. Two options, in
increasing order of automation.

### Review each commit (`install-hooks --review`)

One command, no global install needed — the hook self-bootstraps: at commit time
it prefers a PATH-installed `code-review-ai` and otherwise falls back to
`uvx --from <source>`:

```bash
uvx --from git+https://github.com/Monkey-D-sj/code-review-ai code-review-ai \
  install-hooks --repo . --db .code-review-ai/index.db --review
```

Writes the usual post-* sync hooks plus a review-enabled `post-commit`: it syncs
the index, summarizes the commit's change impact (`summary --files <changed>`
diffed against `HEAD^`, i.e. the commit itself, so it works before `origin/main`
exists), pipes that JSON into the review LLM, and writes the report to
`.code-review-ai/last-review.md`. Each review is also archived under
`.code-review-ai/reviews/<date>/<date>-<time>-<short-sha>.md` (with a concise
`.debug.log` trace — one line per tool/skill/MCP call plus its result — and,
for claude-code, a `.debug.jsonl` raw `stream-json` transcript for deeper
dives), so history is kept and `last-review.md` always points at the newest.
The review prompt first asks the LLM to classify the supplied local change.
Self-contained changes use no graph context; non-local changes call `get_impact`
once for direct call sites + affected entries, then use targeted native reads
only for missing evidence. The headless `claude -p` run injects the graph server
on-demand via `--strict-mcp-config` (only `get_impact` / `get_change_summary` /
`search_symbol`) and pre-authorizes those tools so they don't fail on permission
prompts — no global MCP registration needed, so everyday sessions carry no
tool-description overhead. The LLM
platform is selectable — `claude-code` (default, runs `claude -p
--output-format stream-json --verbose`, extracting the answer from the
transcript) or `codex` (runs `codex exec --full-auto`, which takes the summary
on stdin as prompt context). Tune the platform, output path, and fallback
source:

```bash
code-review-ai install-hooks --review \
  --platform codex \
  --review-out .code-review-ai/last-review.md \
  --from git+https://github.com/Monkey-D-sj/code-review-ai
```

`--review-launch "your command"` overrides the platform's review command
entirely (e.g. `--review-launch "codex exec"`).

`--review` only affects the post-commit hook; post-merge / post-checkout /
post-rewrite still sync only.

Hooks land wherever git actually reads them: `core.hooksPath` if set, else
`.git/hooks`. Under husky the hooks go to `.husky/` (its `core.hooksPath` points
at the auto-generated `.husky/_` shim dir, which sources the `.husky/*` files).

### Review each MR/PR in CI

Ready-to-adapt templates that run `sync -> summary (impact chain) -> LLM review`
and publish the report:

- `examples/ci/gitlab-ci.yml` — artifact `review.md`, merge_request pipelines
- `examples/ci/github-actions.yml` — artifact + PR comment, needs an `ANTHROPIC_API_KEY` secret

Both install the `claude` CLI in the runner via npm and require
`.code-review-ai/` in the target project's `.gitignore`.

## Automating test selection

The same always-fresh index powers `test-impact`: given the changed symbols
in a PR, it reverse-walks the call graph to the test functions that reach
them, so CI can run only the tests this change can actually break. Add
`--format paths` and the CLI prints space-separated, shell-ready test files
(forward slashes, no `./` prefix) - built for `pytest $(...)`:

```bash
code-review-ai test-impact --files <changed> --format paths
```

### Run only affected tests in CI

Ready-to-adapt templates that run `sync -> test-impact -> pytest` and fall
back to the full suite when there are no source changes, no test coverage,
or the query fails:

- `examples/ci/github-actions-test-select.yml` - pull_request pipelines
- `examples/ci/gitlab-ci-test-select.yml` - merge_request pipelines

Both need `.code-review-ai/` in the target project's `.gitignore`. The
fallback is deliberate: if TIA ever can't answer, CI still runs the full
suite rather than silently skipping. To skip instead of falling back when
no test covers the change, swap the empty-`$tests` branch for `exit 0`.

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
