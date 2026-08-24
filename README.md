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

This runs `claude mcp add` to register the MCP server at **user scope** (available in all your projects). Restart Claude Code or run `/mcp` to see the tools.

No install step - run it straight from git with `uvx`:

```bash
uvx --from git+https://github.com/Monkey-D-sj/code-review-ai code-review-ai install --platform claude-code
```

Options: `--scope user|project|local`, `--name <server-name>`, `--from <source>` (defaults to the git URL above; use `--from .` to register a local dev checkout).

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
- `get_impact` - impact chains for changed symbols (or derived from a git diff)
- `get_change_context` - compact, on-demand callers/callees for changes the LLM has already judged non-local; accepts qnames or changed files and resolves qnames server-side
- `search_symbol` - find symbols by name; plain-word queries run FTS token match + bm25 ranking with a substring fallback on 0 hits, while queries containing `*`/`?` keep the short-name glob behavior
- `get_symbol_detail` - node detail + direct callers/callees
- `list_entry_points` - designated entry points
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
code-review-ai context-plan --max-chars 8000 -o .code-review-ai/context-plan.json
```

## Agentic Eval

`agent-eval` compares the same review cases under four controlled, precomputed
context modes: `diff_only`, `search_baseline`, `graph_agent`, and
`hybrid_agent`. Despite the historical name, `graph_agent` does not let an
agent call tools; it is a component ablation that injects `get_impact` output.
Hybrid mode combines the diff with the changed-symbol source, up to three
direct callers/callees per symbol, and compact graph evidence under a 12,000
serialized-character hard budget. The agent command
reads a prompt from stdin and must write one JSON object to stdout. Runs retain
the full prompt, stdout, stderr, parsed answer, latency, reported/estimated
tokens, files read, tool calls, and deterministic finding scores.

Start from `examples/agent-eval-cases.example.json`, then run:

```bash
code-review-ai agent-eval --repo . \
  --cases examples/agent-eval-cases.example.json \
  --agent-command "your-agent --json" --repetitions 3 \
  --workers 4 \
  --runs-dir .code-review-ai/agent-eval \
  -o .code-review-ai/agent-eval-report.json
```

Use `--case-ids case-a case-b` to rerun provider failures without paying for
the rest of the suite again. Reports preserve provider model, uncached/cache
token categories, and total cost when the adapter exposes them. For the Claude
streaming adapter, files and tool calls come from observed provider events,
not model-authored telemetry fields.

Each gold finding has a stable `id`, repository-relative `file`, optional line
range, and optional matching keywords. A prediction matches only when every
provided constraint is satisfied. The aggregate report compares finding
Precision/Recall/F1, success rate, latency, tokens, files read, and tool calls
per mode. Token and file/tool metrics are marked or understood as agent-reported;
when usage is absent, token counts are explicitly estimated from text length.

The runner sets `CRAI_EVAL_MODE` and `CRAI_EVAL_CASE` for provider adapters.
For a fair experiment, keep the model, prompt policy, temperature, context
budget, and repetition count fixed, and prevent the Diff/Search agents from
using repository tools outside the supplied context.

A built-in Claude Code adapter normalizes `claude -p --output-format json`
into the eval contract and disables repository tools for controlled context
experiments:

```bash
code-review-ai agent-eval --repo . \
  --cases examples/agent-eval-cases.example.json \
  --agent-command "python -m code_review_ai.agent_adapter claude --model sonnet" \
  --repetitions 3 --workers 4 \
  -o .code-review-ai/agent-eval-example-r3.json
```

`examples/agent-eval-cases.example.json` is a single offline case (inline diff,
no clone needed). For repository-backed cases the manifest supplies a
`repo_url`/`source_commit`/`mutation_paths` triple; `agent-eval` clones/caches
each repository, creates an isolated worktree, restores the selected production
paths to the fix parent, detects changed symbols, and builds all four controlled
contexts from the same mutations used by `full-agent-eval`.
On Windows, commands with complex quoting can also be supplied as a JSON array.
Search currently has the best F1 point estimate; confidence intervals do not
establish that Graph or Hybrid improves F1 over Diff Only.

Before spending model budget, preflight the manifest, symbol coverage, context
sizes, and supplied files:

```bash
code-review-ai agent-eval --repo . \
  --cases examples/agent-eval-cases.example.json --dry-run \
  -o .code-review-ai/agent-eval-preflight.json
```

After a multi-repetition run, generate bootstrap confidence intervals and
paired comparisons against Diff Only:

```bash
code-review-ai agent-eval-analyze \
  --report .code-review-ai/agent-eval-report.json \
  -o .code-review-ai/agent-eval-analysis.json
```

### Full-project tool-use eval

`full-agent-eval` tests the installed product on isolated real repositories.
It checks out a real fix commit, restores selected production files to the
parent revision, keeps the fixed tests available, and pairs a Native Agent
(`Read`/`Glob`/`Grep`) with a Full Project Agent using the same native tools
plus this project's MCP server.

Both evaluators use the same review policy. The controlled runner varies only
the supplied context, while the full-project runner varies only available
context tools.

For Full Project mode, each historical snapshot is indexed before the Agent
timer starts. The evaluated MCP server reuses that index without startup sync
or a watcher, and `rebuild_index` is not available to the Agent. The Prompt
mirrors the installed review policy: start from the change summary, use graph
neighbors when context is needed, and expand to `get_impact` only when the
blast radius remains uncertain or the change crosses an important boundary.

```bash
code-review-ai full-agent-eval \
  --cases benchmarks/case-backend-cases.json --dry-run \
  -o .code-review-ai/full-agent-preflight.json

code-review-ai full-agent-eval \
  --cases benchmarks/case-backend-cases.json \
  --agent-command "python -m code_review_ai.agent_adapter claude --model sonnet --max-budget-usd 1.00" \
  --repetitions 3 --workers 4 \
  -o .code-review-ai/full-agent-report.json
```

`benchmarks/case-backend-cases.json` holds the business-shaped project cases
(`source_dir`-anchored under `full_agent_eval/case-backend`, no clone or
network needed), and `benchmarks/fast-cases.json` is the fast single-repo
regression set against `benchmarks/fast-repo` (`--local-repo`). Both run the
same `full-agent-eval` harness and share the `examples/agent-eval-cases.example.json`
offline shape.

Cases are graded **blind**: the prompt states the deliverable (what broke, which
callers / entry points / tests are affected) and shows the diff, but never names a
symbol. Per-case prose lives in each case's `hint` field and reaches the model only
under `--hinted`, which exists as an ablation arm — such prose is symmetric input to
both arms but asymmetric benefit, since naming the affected callers hands the native
arm the traversal the graph tools exist to do. Gold keywords are therefore restricted
to identifiers only traversal surfaces (a keyword visible in the diff or the hint can
be paraphrased instead of traced), and each answer is capped at 3 findings so f1 does
not turn into a verbosity measure.

## Historical-change benchmark

Measure whether impact queries recover the files touched by known historical
fixes. A manifest is a JSON array; each case identifies the symbols changed by
the fix and the production/test files from the real patch:

```json
[
  {
    "id": "pallets__flask-5014",
    "changed_symbols": ["flask.app::Flask.make_response"],
    "gold_files": ["src/flask/app.py", "tests/test_basic.py"]
  }
]
```

Check out the repository at the historical base commit, then run:

```bash
code-review-ai benchmark --repo ../flask --cases cases/flask.json \
  --db .code-review-ai/flask.db --top-k 10 -o results/flask.json
```

The report includes indexing time and database size, call-edge resolution
distribution, symbol-found rate, per-case query latency, and historical patch
file Recall@K/Precision@K. `examples/benchmark-cases.example.json` is a starter manifest.
Patch files are an observable proxy for impact, not a complete ground truth;
label the metric **historical patch file recall** when reporting results.

For commits changing two or more production files, the same run also performs
leave-one-production-file-out evaluation. Each fold uses one changed file's
symbols as the only seed, removes that seed file from candidates, and treats
the commit's other changed production files as hidden targets. Reports expose
`production_file_eligible_cases`, `production_file_folds`, and macro related-
production-file Recall@K/Precision@K. Both test and production evaluations also
report Recall@All, Precision@All, and full candidate counts. Recall@All measures
graph coverage using a benchmark query limit equal to the full indexed node
count; Top-K measures whether ordering and context budgets surface the answer
early. Single-production-file commits are not included in production metric
denominators.

### Visualization (`graph`)

Export interactive HTML graphs of the call structure:

```bash
code-review-ai graph -m communities -o communities.html   # community bubble chart (default)
code-review-ai graph -m graph       -o callgraph.html     # raw function-level force graph
code-review-ai graph -m flow        -o flows.html         # flow chart (BFS call chains)
```

Options: `-n` max items (200), `-m` mode (communities|graph|flow), `-o` output path.

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
Self-contained changes use no graph context; non-local changes call the compact
`get_change_context` once with qnames or affected files, then use targeted
native reads only for missing evidence. The headless run
pre-authorizes those tools so they don't fail on permission prompts. The LLM
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
