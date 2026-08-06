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
- `search_symbol` - find symbols by name glob
- `get_symbol_detail` - node detail + direct callers/callees
- `list_entry_points` - designated entry points
- `get_communities` / `get_community` - Leiden communities (opt-in via `community_detection`)

## CLI (manual use)

```bash
code-review-ai rebuild --repo .                        # build index
code-review-ai query   --symbols auth::login           # impact for given symbols
code-review-ai query   --files path/to/file.py         # impact via git diff of files
code-review-ai search  "login"                          # glob-match symbol short names
code-review-ai communities [--symbol auth::login]       # list communities / one symbol's community
```

`rebuild`/`query`/`search`/`communities` also accept no `--repo`/`--db` (defaults: `.` and `.code-review-ai/index.db`).

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

### FastAPI historical suite

FastAPI is not part of classic SWE-bench Verified, so its cases are labelled
separately instead of being presented as Verified samples.
`benchmarks/fastapi-history-10.json` contains 10 commits from the official
FastAPI repository that changed both production Python and tests. Cases cover
routing, applications, SSE, compatibility, encoding, dependencies/OpenAPI,
headers, and responses. `benchmarks/historical-suite-40.json` combines these
with the 30 Verified cases.

```bash
uv run python scripts/run_swebench_suite.py \
  --cases benchmarks/historical-suite-40.json \
  --dataset-name "SWE-bench Verified + FastAPI Git history" \
  --cache-dir .benchmark-cache --top-k 10 \
  --out benchmark-results/historical-suite-40.json
```

Regenerate the FastAPI subset from an official local clone with:

```bash
uv run python scripts/generate_git_history_manifest.py \
  --repo-path ../fastapi --count 10 \
  --out benchmarks/fastapi-history-10.json
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
exists), pipes that JSON into `claude -p`, and writes the report to
`.code-review-ai/last-review.md`. Tune the LLM command, output path, and fallback
source:

```bash
code-review-ai install-hooks --review \
  --review-launch "claude -p" \
  --review-out .code-review-ai/last-review.md \
  --from git+https://github.com/Monkey-D-sj/code-review-ai
```

`--review` only affects the post-commit hook; post-merge / post-checkout /
post-rewrite still sync only.

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

Layered: defaults -> `[tool.code-review-ai]` in `pyproject.toml` (or a standalone `cr-ai.toml`) -> env `CRAI_<UPPER_KEY>`. Notable keys: `diff_base` (default `origin/main`), `entry_names`, `community_detection` (bool, default `false`; set `CRAI_COMMUNITY_DETECTION=1` to enable Leiden communities).

## How it works

One atomic SQLite transaction per rebuild:

```
parse (tree-sitter) -> resolve calls -> write nodes/edges (Phase A)
                                    -> build flows (Phase B)
                                    -> detect communities (Phase C, opt-in)
```

Impact query: for a changed symbol, slice its flows into upstream callers / downstream callees / affected entry points. Community query: the symbol's Leiden cluster and co-members - the horizontal blast radius.
