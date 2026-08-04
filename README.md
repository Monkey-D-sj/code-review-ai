# code-review-ai

Parse a codebase with tree-sitter, persist a call graph + call flows to SQLite, and expose impact-chain queries over MCP - so an AI reviewer (Claude Code, etc.) can pull just the relevant call chain instead of reading whole files.

Supports **Python, TypeScript, and JavaScript**. Requires Python 3.14 (auto-fetched by `uv`, so you don't install it yourself).

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

### Visualization (`graph`)

Export interactive HTML graphs of the call structure:

```bash
code-review-ai graph -m communities -o communities.html   # community bubble chart (default)
code-review-ai graph -m graph       -o callgraph.html     # raw function-level force graph
code-review-ai graph -m flow        -o flows.html         # flow chart (BFS call chains)
```

Options: `-n` max items (200), `-m` mode (communities|graph|flow), `-o` output path.

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
