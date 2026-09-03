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
  --runs-dir eval-results/agent-eval \
  -o eval-results/agent-eval-report.json
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
  -o eval-results/agent-eval-example-r3.json
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
  -o eval-results/agent-eval-preflight.json
```

After a multi-repetition run, generate bootstrap confidence intervals and
paired comparisons against Diff Only:

```bash
code-review-ai agent-eval-analyze \
  --report eval-results/agent-eval-report.json \
  -o eval-results/agent-eval-analysis.json
```

### Full-project tool-use eval

### Built-in LangGraph review agent

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
agent reads the single `OPENAI_API_KEY` entry by default.
`CRAI_EVAL_MODEL` and `CRAI_BASE_URL` configure the eval adapter. The process
environment takes precedence over `.env`, and no CLI option accepts a plaintext
API key. For the existing full-project evaluator, use the
same runtime through `python -m code_review_ai.agent_adapter langgraph
--model your-model`; its `native_agent` arm gets `read_file + search_code`,
and full-project arms also get `get_impact`.

`review` writes live index/model/tool progress and elapsed time to stderr while
reserving stdout for the final JSON payload. In an interactive terminal it now
uses an append-only timeline: index/context phases and each model round remain
visible with their tool requests and completions. Pass `--visual` to force this
timeline (for terminals where TTY detection is unavailable), `--no-visual` for
the simpler one-line event log, or `--no-progress` for a quiet automation run.

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
  -o eval-results/full-agent-preflight.json

code-review-ai full-agent-eval \
  --cases benchmarks/case-backend-cases.json \
  --model deepseek-v4-flash \
  --agent-command "python -m code_review_ai.agent_adapter claude" \
  --repetitions 3 --workers 4 \
  -o eval-results/full-agent-report.json
```

`--model` locks every arm to the same model (forwarded to the agent via
`CRAI_EVAL_MODEL`, so per-mode cost/token comparisons stay apples-to-apples);
omit it to use the CLI's current default model. A `--model sonnet` inside
`--agent-command` works too, but a standalone `--model` keeps all modes uniform.

`benchmarks/case-backend-cases.json` holds the business-shaped project cases
(`source_dir`-anchored under `full_agent_eval/case-backend`, no clone or
network needed), and `benchmarks/fast-cases.json` is the fast single-repo
regression set against `benchmarks/fast-repo` (`--local-repo`). Both run the
same `full-agent-eval` harness and share the `examples/agent-eval-cases.example.json`
offline shape.

#### Run without an LLM (`scripted` agent)

The real agent command above needs a logged-in `claude` CLI and spends tokens.
For a deterministic, no-network wiring regression that runs in CI, the same
harness accepts a scripted agent that replaces the model with a fixed script:

```bash
code-review-ai full-agent-eval \
  --cases benchmarks/fast-cases.json \
  --local-repo benchmarks/fast-repo \
  --agent-command "python -m code_review_ai.agent_adapter scripted" \
  --modes native_agent full_project_core \
  -o eval-results/scripted-report.json
```

The `scripted` adapter walks the exact same pipeline as the real one — CLI
subprocess, eval env vars, transcript persistence, scoring, and aggregation —
and, in the `full_project_core` arm, opens a real MCP server subprocess over
stdio and calls `get_change_summary` / `get_impact`, so the graph tools
genuinely answer against the case index. It makes no model call, so
it needs no claude login, tokens, or network. The scenario (native vs core) is
derived from `CRAI_EVAL_MODE`, so one `--agent-command` serves both arms. This
is a capability-and-wiring oracle, not a behavior substitute: it proves the
harness wiring and that the graph tools answer on the index, but it cannot say
how a real LLM agent would use those tools. Keep real `claude` runs for
behavioral native-vs-core comparison; run the scripted arm in CI for regressions.
Coverage is `tests/test_scripted_full_agent_eval.py`.

Cases are graded **blind**: the prompt states the deliverable (what broke, which
callers / entry points / tests are affected) and shows the diff, but never names a
symbol. Per-case prose lives in each case's `hint` field and reaches the model only
under `--hinted`, which exists as an ablation arm — such prose is symmetric input to
both arms but asymmetric benefit, since naming the affected callers hands the native
arm the traversal the graph tools exist to do. Gold keywords are therefore restricted
to identifiers only traversal surfaces (a keyword visible in the diff or the hint can
be paraphrased instead of traced), and each answer is capped at 3 findings so f1 does
not turn into a verbosity measure.

Each case has one structured `gold` object shared by two independent score layers:

```json
{
  "gold": {
    "root_causes": [{
      "id": "bug-id", "fix_file": "app/service.py",
      "mechanism_terms": ["Caller", "failure"], "min_matches": 2
    }],
    "context": {
      "symbols": [], "files": ["app/service.py"],
      "entries": [], "tests": [],
      "hard_negatives": {"symbols": [], "files": []}
    }
  }
}
```

`graph_retrieval` scores symbols/files/entries/tests and explicit hard negatives.
`agent_review` scores root causes plus the structured affected context returned by
the agent. Empty gold dimensions are not applicable rather than zero. Legacy
`gold_findings`/`gold_files` manifests remain loadable for old reports.

See [docs/EVALUATION_AUTHORING_GUIDE.md](docs/EVALUATION_AUTHORING_GUIDE.md) for
the case-backend authoring workflow, Gold annotation rules, validation commands,
and report interpretation.

For the current repository-specific expansion order and Native/Graph balancing
quota, follow [docs/CASE_BACKEND_EXPANSION_PLAYBOOK.md](docs/CASE_BACKEND_EXPANSION_PLAYBOOK.md).

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
