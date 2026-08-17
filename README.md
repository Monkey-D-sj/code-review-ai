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

### Register with Codex

```bash
code-review-ai install --platform codex
```

This deploys the four review skills to `~/.codex/skills/` and appends the MCP
tool-usage docs to `~/.codex/AGENTS.md` (marker-guarded, idempotent). Codex
has no `codex mcp add` CLI, so MCP registration is manual — add a block to
`~/.codex/config.toml`:

```toml
[mcp_servers.code-review-ai]
command = "uvx"
args = ["--from", "git+https://github.com/Monkey-D-sj/code-review-ai", "code-review-ai-mcp"]
type = "stdio"
```

Both installs also deploy four user-scope code-review skills:
`code-review-langs` (entry/router) plus `code-review-python`,
`code-review-typescript`, and `code-review-javascript`, each carrying the
static review rules for its language. They coexist with any existing
`code-review` skill and never call the MCP graph tools.

## MCP tools

- `rebuild_index` - build/rebuild the index from the working tree
- `get_impact` - impact chains for changed symbols (or derived from a git diff)
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
  --cases benchmarks/agentic-eval-real-repos.json \
  --repos-dir .code-review-ai/external-repos \
  --agent-command "python -m code_review_ai.agent_adapter claude --model sonnet" \
  --repetitions 3 --workers 4 \
  -o .code-review-ai/agent-eval-real-repos-r3.json
```

`benchmarks/agentic-eval-real-repos.json` is the canonical case set shared by
`agent-eval` and `full-agent-eval`: twelve reverse mutations from real fixes in
itsdangerous, p-limit, Gson, FastAPI, and Spring PetClinic. Six cases are
explicitly context-heavy, covering cross-module alias pipelines, route-state
propagation, framework lifecycles, ORM/view boundaries, and database-backed
concurrency invariants. Each runner consumes the same repository URL,
fix commit, mutation paths, review task, and gold findings. `agent-eval`
automatically clones/caches each repository, creates an isolated worktree,
restores the selected production paths to the fix parent, detects changed
symbols, and builds all four controlled contexts from the same mutations used
by `full-agent-eval`. The older `benchmarks/agent-eval-real-10.json` remains a
project-local historical smoke suite, not the cross-evaluator baseline.
On Windows, commands with complex quoting can also be supplied as a JSON array.
The 120-run baseline and its limitations are documented in
`benchmarks/AGENT_EVAL_BASELINE.md`. Search currently has the best F1 point
estimate; confidence intervals do not establish that Graph or Hybrid improves
F1 over Diff Only.

Before spending model budget, preflight the manifest, symbol coverage, context
sizes, and supplied files:

```bash
code-review-ai agent-eval --repo . \
  --cases benchmarks/agentic-eval-real-repos.json --dry-run \
  --repos-dir .code-review-ai/external-repos \
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
  --cases benchmarks/agentic-eval-real-repos.json --dry-run \
  -o .code-review-ai/full-agent-preflight.json

code-review-ai full-agent-eval \
  --cases benchmarks/agentic-eval-real-repos.json \
  --agent-command "python -m code_review_ai.agent_adapter claude --model sonnet --max-budget-usd 1.00" \
  --repetitions 3 --workers 4 \
  -o .code-review-ai/full-agent-report.json
```

The current online-v2 result completed 36/36 calls. Compared with Native Agent,
Full Project changed the F1 point estimate from 90.4% to 91.3%, precision from
85.2% to 88.0%, and recall from 100.0% to 97.2%. The paired F1 interval is -9.8
to +8.5 points, so the small experiment does not establish a quality gain. With
index setup excluded from Agent timing, Full Project cost 3.0% more, was 2.3%
slower, and read 25.3% fewer files. Only 9/18 Full Project runs chose
`get_impact`; all 18 used project MCP. Methodology, artifacts, and limitations
are documented in `benchmarks/FULL_AGENT_EVAL_REAL_REPOS.md`.

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

### Reproducible SWE-bench suite

`benchmarks/swe-bench-verified-30.json` contains 30 real, fixed-revision cases:
Flask (1), Requests (8), pytest (11), and Xarray (10). Production patch ranges
are the change seeds; files from the official `test_patch` are the retrieval
targets, avoiding the trivial metric of predicting the seed file itself.

```bash
uv run python scripts/run_swebench_suite.py \
  --cases benchmarks/swe-bench-verified-30.json \
  --cache-dir .benchmark-cache --top-k 10 \
  --out benchmark-results/swe-bench-verified-30.json
```

The runner clones each repository once, checks out every case's pinned
`base_commit`, includes tests in indexing, and creates an isolated SQLite index
per case. Use `--limit 1` for a smoke test. The committed manifest can be
regenerated from Hugging Face rows JSON with
`scripts/generate_swebench_manifest.py`; raw dataset files are not vendored.

### FastAPI and Spring Boot historical suites

FastAPI is not part of classic SWE-bench Verified, so its cases are labelled
separately instead of being presented as Verified samples.
`benchmarks/fastapi-history-10.json` contains 10 commits from the official
FastAPI repository that changed both production Python and tests. Cases cover
routing, applications, SSE, compatibility, encoding, dependencies/OpenAPI,
headers, and responses.

`benchmarks/spring-petclinic-history-10.json` adds 10 Java commits from the
official Spring PetClinic repository. They cover Spring MVC controllers,
repositories, validators, entities, and JUnit tests. The production files are
under `src/main/java/`; the retrieval targets are the corresponding changed
files under `src/test/java/`. `benchmarks/historical-suite-50.json` combines
both history subsets with the 30 Verified cases.

```bash
uv run python scripts/run_swebench_suite.py \
  --cases benchmarks/historical-suite-50.json \
  --dataset-name "SWE-bench Verified + FastAPI + Spring PetClinic Git history" \
  --cache-dir .benchmark-cache --top-k 10 \
  --out benchmark-results/historical-suite-50.json
```

Regenerate the FastAPI subset from an official local clone with:

```bash
uv run python scripts/generate_git_history_manifest.py \
  --repo-path ../fastapi --count 10 \
  --out benchmarks/fastapi-history-10.json
```

Regenerate the Spring PetClinic subset from an official local clone with:

```bash
uv run python scripts/generate_git_history_manifest.py \
  --repo-path ../spring-petclinic \
  --repo spring-projects/spring-petclinic \
  --production-prefix src/main/java/ --test-prefix src/test/java/ \
  --suffix .java --count 10 --scan 3000 --prefer-recent \
  --max-gold-files 5 --max-production-files 5 \
  --exclude-subject upgrade --exclude-subject migrate \
  --exclude-subject copyright --exclude-subject formatting \
  --out benchmarks/spring-petclinic-history-10.json
```

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
The review prompt steers the LLM to prefer code-review-ai's MCP tools
(`get_impact` / `get_change_summary` / `search_symbol` / `query_graph`) and the
`code-review` skills over raw `git diff`/`grep`, and the headless run
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
