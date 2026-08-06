# Performance Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reproducible performance benchmark (roadmap item 7) that measures index build time vs repo scale, query p99 latency, and build peak RSS, producing hard numbers as JSON + a rendered Markdown report.

**Architecture:** A testable core module `code_review_ai/perf.py` (synthetic repo generator, per-repo measurement, percentile, Markdown renderer) + a thin orchestration script `scripts/run_perf_benchmark.py` that runs real cached repos + the current repo + synthetic tiers under one fixed config. `psutil` is an optional extra for peak-RSS polling; without it the report degrades to DB-size proxy.

**Tech Stack:** Python 3.14, tree-sitter/SQLite pipeline already in repo (`rebuild`, `get_impact`, `load_config`), `psutil` (optional), pytest. Windows primary (no stdlib `resource`).

## Global Constraints

- Design spec: `docs/superpowers/specs/2026-08-06-performance-benchmark-design.md`
- Repo conventions: functions ≤50 lines; main functions only prep params / orchestrate / return; no single-letter variable names; no builtin names as variables.
- Fixed benchmark config for ALL repos: `community_detection` from `--community` (default on), `community_weight="hub_pruned"`, `exclude=list(DEFAULTS["exclude"])` from `code_review_ai.config`.
- Never per-repo `load_config()` for benchmarked repos — one fixed Config, `dataclasses.replace` to swap `repo_path`/`db_path`.
- Synthetic repos are deterministic (no RNG) and reported as reference-only.
- Tests must be deterministic WITHOUT psutil installed (inject an `rss_monitor` callable into `measure_repo`).
- Existing helpers reused: `from code_review_ai.indexer import rebuild` (returns `RebuildStats` with `.node_count/.edge_count/.flow_count/.stage_timings`), `from code_review_ai.impact import get_impact`, `from code_review_ai.db import connect, init_schema`, `from code_review_ai.config import DEFAULTS, load_config`.

---

### Task 1: `percentile` helper in `code_review_ai/perf.py`

**Files:**
- Create: `code_review_ai/perf.py`
- Test: `tests/test_perf.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `percentile(sorted_values: list[float], q: float) -> float` — linear-interpolated percentile; input must be ascending (caller sorts). Raises `ValueError` on empty list.

- [ ] **Step 1: Write the failing test**

```python
from code_review_ai.perf import percentile

import pytest


def test_percentile_interpolates_linearly():
    assert percentile([1.0, 2.0, 3.0, 4.0], 0) == 1.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 95) == 3.85
    assert percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0


def test_percentile_rejects_empty_list():
    with pytest.raises(ValueError, match="non-empty"):
        percentile([], 50)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_perf.py -v`
Expected: FAIL — `ImportError: cannot import name 'percentile'` (module/file not created yet).

- [ ] **Step 3: Write minimal implementation**

Create `code_review_ai/perf.py`:

```python
"""Reproducible performance benchmark core (roadmap item 7)."""


def percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile. `sorted_values` must be ascending."""
    if not sorted_values:
        raise ValueError("percentile requires a non-empty list")
    rank = q / 100.0 * (len(sorted_values) - 1)
    lower = int(rank)
    fraction = rank - lower
    if lower + 1 >= len(sorted_values):
        return float(sorted_values[-1])
    return sorted_values[lower] + fraction * (
        sorted_values[lower + 1] - sorted_values[lower]
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_perf.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/perf.py tests/test_perf.py
git commit -m "feat(perf): percentile helper for query-latency stats"
```

---

### Task 2: Deterministic synthetic repo generator

**Files:**
- Modify: `code_review_ai/perf.py`
- Test: `tests/test_perf.py`

**Interfaces:**
- Consumes: `Path`.
- Produces:
  - `SyntheticStats(files: int, nodes: int)` — frozen dataclass.
  - `build_synthetic_repo(target_dir: Path, file_count: int) -> SyntheticStats` — writes `m0.py`..`m{file_count-1}.py` into `target_dir` (created if missing). Each file `m{i}` imports `m{(i+1) % n}` and `m{(i+2) % n}`; each file defines 10 functions `f0`..`f9` (~8 lines each); on files where `i % 10 == 0` the `f0` function is named `main` (so `entry_names=["main"]` matches). Each function calls its own module's `f{(j+1) % 10}` plus one function in each imported module (all `resolved` call edges). Raises `ValueError` if `file_count < 1`. Returns `SyntheticStats(files=file_count, nodes=file_count * 10)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_perf.py`:

```python
from code_review_ai.perf import build_synthetic_repo


def test_synthetic_repo_has_expected_shape(tmp_path):
    stats = build_synthetic_repo(tmp_path, 20)
    assert stats.files == 20
    assert stats.nodes == 200
    py_files = sorted(tmp_path.glob("m*.py"))
    assert len(py_files) == 20
    first = py_files[0].read_text(encoding="utf-8")
    assert "import m1" in first
    assert "def main" in first
    assert "def f9" in first


def test_synthetic_repo_is_deterministic(tmp_path):
    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    build_synthetic_repo(first_dir, 12)
    build_synthetic_repo(second_dir, 12)
    first_files = sorted(first_dir.glob("m*.py"))
    second_files = sorted(second_dir.glob("m*.py"))
    assert len(first_files) == len(second_files)
    for left, right in zip(first_files, second_files):
        assert left.name == right.name
        assert left.read_text(encoding="utf-8") == right.read_text(encoding="utf-8")


def test_synthetic_repo_rejects_zero_files(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="file_count"):
        build_synthetic_repo(tmp_path, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_perf.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_synthetic_repo'`.

- [ ] **Step 3: Write minimal implementation**

Extend `code_review_ai/perf.py`. Add imports and the generator:

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SyntheticStats:
    files: int
    nodes: int


def build_synthetic_repo(target_dir: Path, file_count: int) -> SyntheticStats:
    """Generate a deterministic cyclic-call Python repo of `file_count` files."""
    if file_count < 1:
        raise ValueError("file_count must be at least 1")
    target_dir.mkdir(parents=True, exist_ok=True)
    for index in range(file_count):
        next_index = (index + 1) % file_count
        next2_index = (index + 2) % file_count
        lines = [f"import m{next_index}", f"import m{next2_index}", ""]
        for function_index in range(10):
            name = "main" if function_index == 0 and index % 10 == 0 else f"f{function_index}"
            lines.append(_function_source(name, function_index, next_index, next2_index))
            lines.append("")
        (target_dir / f"m{index}.py").write_text("\n".join(lines), encoding="utf-8")
    return SyntheticStats(files=file_count, nodes=file_count * 10)


def _function_source(name: str, function_index: int,
                     next_index: int, next2_index: int) -> str:
    next_function = (function_index + 1) % 10
    return (
        f"def {name}(argument):\n"
        f"    direct = f{next_function}(argument)\n"
        f"    left = m{next_index}.f{function_index}(argument)\n"
        f"    right = m{next2_index}.f{function_index}(argument)\n"
        f"    return direct + left + right\n"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_perf.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/perf.py tests/test_perf.py
git commit -m "feat(perf): deterministic synthetic repo generator"
```

---

### Task 3: Peak-RSS poller and `measure_repo`

**Files:**
- Modify: `code_review_ai/perf.py`
- Test: `tests/test_perf.py`

**Interfaces:**
- Consumes: `connect`, `init_schema` (`code_review_ai.db`), `rebuild` (`code_review_ai.indexer`), `get_impact` (`code_review_ai.impact`), `percentile`, `SyntheticStats` from earlier tasks.
- Produces:
  - `_import_psutil() -> object | None` — returns psutil module or `None`.
  - `_peak_rss_during(work: Callable[[], object], interval_s: float = 0.02) -> tuple[object, float | None]` — runs `work()` while a daemon thread samples this process's RSS every `interval_s` seconds; returns `(work_result, peak_rss_mb)` or `(work_result, None)` when psutil is missing.
  - `measure_repo(config: Config, db_path: str, query_samples: int = 200, rss_monitor: Callable[[Callable[[], object]], tuple[object, float | None]] | None = None) -> dict` — fresh DB at `db_path`, `init_schema`, run `rebuild` inside `rss_monitor` (default `_peak_rss_during`), sample `get_impact` latencies, return dict:
    `{"source_files": int, "nodes": int, "edges": int, "flows": int, "build_ms": dict, "query_ms": {"p50","p95","p99","samples"}, "peak_rss_mb": float|None, "database_bytes": int}`.
  - `_sample_query_latencies(conn: sqlite3.Connection, query_samples: int) -> dict` — every-`k`th node by id where `k = ceil(node_count / query_samples)`, up to `query_samples` samples; times `get_impact(conn, [symbol])` per sample; returns sorted-latency `{"p50","p95","p99","samples"}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_perf.py`:

```python
import importlib.util
import sqlite3

import pytest

from conftest import FIXTURES

from code_review_ai.config import load_config
from code_review_ai.perf import _peak_rss_during, build_synthetic_repo, measure_repo


def _fixture_config(tmp_path):
    config = load_config(FIXTURES)
    config.repo_path = FIXTURES
    config.db_path = str(tmp_path / "perf.db")
    config.community_detection = False
    return config


def test_measure_repo_reports_build_and_query_metrics(tmp_path):
    config = _fixture_config(tmp_path)
    payload = measure_repo(config, config.db_path,
                           query_samples=50,
                           rss_monitor=lambda work: (work(), None))
    assert payload["nodes"] > 0
    assert payload["source_files"] > 0
    assert payload["build_ms"]["total"] > 0
    for key in ("list_files", "parse", "resolve", "write_db", "total"):
        assert key in payload["build_ms"]
    assert payload["query_ms"]["p99"] >= payload["query_ms"]["p50"]
    assert payload["query_ms"]["samples"] <= 50
    assert payload["database_bytes"] > 0
    assert payload["peak_rss_mb"] is None


def test_measure_repo_records_peak_rss_when_monitor_reports(tmp_path):
    config = _fixture_config(tmp_path)
    payload = measure_repo(config, config.db_path,
                           rss_monitor=lambda work: (work(), 123.4))
    assert payload["peak_rss_mb"] == 123.4


def test_measure_repo_on_synthetic_repo_has_resolved_calls_and_flows(tmp_path):
    repo_dir = tmp_path / "synthetic-repo"
    build_synthetic_repo(repo_dir, 20)
    config = load_config(str(repo_dir))
    config.repo_path = str(repo_dir)
    config.db_path = str(tmp_path / "synth.db")
    config.community_detection = False
    payload = measure_repo(config, config.db_path,
                           query_samples=20,
                           rss_monitor=lambda work: (work(), None))
    assert payload["nodes"] == 200
    assert payload["source_files"] == 20
    assert payload["edges"] > 0
    assert payload["flows"] > 0


def test_peak_rss_during_returns_none_without_psutil():
    if importlib.util.find_spec("psutil") is not None:
        pytest.skip("psutil installed; none-path not applicable")
    result, peak_mb = _peak_rss_during(lambda: 7)
    assert result == 7
    assert peak_mb is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_perf.py -v`
Expected: FAIL — `ImportError: cannot import name 'measure_repo'` / `_peak_rss_during`.

- [ ] **Step 3: Write minimal implementation**

Extend `code_review_ai/perf.py`. Add imports and the measurement code:

```python
import os
import sqlite3
import threading
import time
from collections.abc import Callable

from code_review_ai.config import Config
from code_review_ai.db import connect, init_schema
from code_review_ai.impact import get_impact
from code_review_ai.indexer import rebuild


def _import_psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def _peak_rss_during(work: Callable[[], object],
                     interval_s: float = 0.02) -> tuple[object, float | None]:
    """Run `work` while polling this process's RSS; return (result, peak_mb)."""
    psutil_module = _import_psutil()
    if psutil_module is None:
        return work(), None
    process = psutil_module.Process()
    peak_bytes = process.memory_info().rss
    stop_event = threading.Event()

    def _poll():
        nonlocal peak_bytes
        while not stop_event.is_set():
            try:
                peak_bytes = max(peak_bytes, process.memory_info().rss)
            except OSError:
                pass
            time.sleep(interval_s)

    poller = threading.Thread(target=_poll, daemon=True)
    poller.start()
    try:
        result = work()
    finally:
        stop_event.set()
        poller.join(timeout=2.0)
    return result, round(peak_bytes / (1024 * 1024), 1)


def measure_repo(config: Config, db_path: str, query_samples: int = 200,
                 rss_monitor: Callable[[Callable[[], object]],
                                       tuple[object, float | None]] | None = None) -> dict:
    """Rebuild `config` into a fresh DB at `db_path` and measure build/query/RSS."""
    monitor = rss_monitor if rss_monitor is not None else _peak_rss_during
    conn = connect(db_path)
    try:
        init_schema(conn)
        stats, peak_rss_mb = monitor(lambda: rebuild(config, conn))
        source_files = conn.execute(
            "SELECT COUNT(DISTINCT file_path) AS count FROM nodes"
        ).fetchone()["count"]
        return {
            "source_files": source_files,
            "nodes": stats.node_count,
            "edges": stats.edge_count,
            "flows": stats.flow_count,
            "build_ms": stats.stage_timings,
            "query_ms": _sample_query_latencies(conn, query_samples),
            "peak_rss_mb": peak_rss_mb,
            "database_bytes": os.path.getsize(db_path) if os.path.exists(db_path) else 0,
        }
    finally:
        conn.close()


def _sample_query_latencies(conn: sqlite3.Connection,
                            query_samples: int) -> dict:
    rows = conn.execute(
        "SELECT qualified_name FROM nodes "
        "WHERE kind IN ('function','method') ORDER BY id"
    ).fetchall()
    step = max(1, (len(rows) + query_samples - 1) // query_samples)
    sampled = [row["qualified_name"] for row in rows[::step]][:query_samples]
    latencies_ms = []
    for symbol in sampled:
        started = time.perf_counter()
        get_impact(conn, [symbol])
        latencies_ms.append(round((time.perf_counter() - started) * 1000, 3))
    latencies_ms.sort()
    return {
        "p50": percentile(latencies_ms, 50) if latencies_ms else 0.0,
        "p95": percentile(latencies_ms, 95) if latencies_ms else 0.0,
        "p99": percentile(latencies_ms, 99) if latencies_ms else 0.0,
        "samples": len(sampled),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_perf.py -v`
Expected: PASS (9 passed; the psutil-none test runs since psutil is not installed).

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/perf.py tests/test_perf.py
git commit -m "feat(perf): measure_repo with peak-RSS poller and query percentiles"
```

---

### Task 4: Markdown report renderer

**Files:**
- Modify: `code_review_ai/perf.py`
- Test: `tests/test_perf.py`

**Interfaces:**
- Consumes: a report dict of the shape produced by the script (Task 5): `date`, `environment{platform,python,psutil_available}`, `config{community_detection,community_weight,query_samples,exclude}`, `repos[]` where each repo has `name,kind,source_files,nodes,edges,flows,build_ms{...6 stage keys...},query_ms{p50,p95,p99,samples},peak_rss_mb,database_bytes`.
- Produces: `render_markdown(report: dict) -> str` — Markdown with environment/config block, repo table (Repo|Kind|Source files|Nodes|Build (s)|Query p99 (ms)|Peak RSS (MB)|DB (MB)), stage-breakdown table, and a scaling-observations note. `peak_rss_mb is None` renders as `n/a`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_perf.py`:

```python
from code_review_ai.perf import render_markdown


def _minimal_report():
    return {
        "date": "2026-08-06",
        "environment": {"platform": "win32", "python": "3.14", "psutil_available": False},
        "config": {"community_detection": True, "community_weight": "hub_pruned",
                   "query_samples": 200, "exclude": ["*/test*"]},
        "repos": [{
            "name": "synthetic-1000", "kind": "synthetic", "source_files": 1000,
            "nodes": 10000, "edges": 41000, "flows": 100,
            "build_ms": {"list_files": 10.0, "parse": 900.0, "resolve": 450.0,
                         "write_db": 600.0, "communities": 300.0, "total": 2260.0},
            "query_ms": {"p50": 1.2, "p95": 4.5, "p99": 9.8, "samples": 200},
            "peak_rss_mb": None, "database_bytes": 3145728,
        }],
    }


def test_render_markdown_contains_repo_table_and_metrics():
    rendered = render_markdown(_minimal_report())
    assert "| Repo |" in rendered
    assert "synthetic-1000" in rendered
    assert "p99" in rendered
    assert "9.8" in rendered
    assert "n/a" in rendered
    assert "hub_pruned" in rendered
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_perf.py -v`
Expected: FAIL — `ImportError: cannot import name 'render_markdown'`.

- [ ] **Step 3: Write minimal implementation**

Extend `code_review_ai/perf.py`:

```python
def render_markdown(report: dict) -> str:
    """Render a benchmark report dict as Markdown."""
    environment = report["environment"]
    psutil_note = "available" if environment["psutil_available"] else "unavailable"
    lines = [
        "# Code Review AI Performance Benchmark",
        "",
        f"Date: {report['date']} · Platform: {environment['platform']} · "
        f"Python: {environment['python']} · psutil: {psutil_note}",
        "",
        "## Fixed configuration",
        "",
        f"- Community detection: "
        f"{'on' if report['config']['community_detection'] else 'off'} "
        f"(weight: {report['config']['community_weight']})",
        f"- Query samples per repo: {report['config']['query_samples']}",
        f"- Exclude: `{', '.join(report['config']['exclude'])}`",
        "",
        "## Repos",
        "",
        "| Repo | Kind | Source files | Nodes | Build (s) | Query p99 (ms) | "
        "Peak RSS (MB) | DB (MB) |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for repo in report["repos"]:
        rss = f"{repo['peak_rss_mb']:.1f}" if repo["peak_rss_mb"] is not None else "n/a"
        lines.append(
            f"| {repo['name']} | {repo['kind']} | {repo['source_files']} | "
            f"{repo['nodes']} | {repo['build_ms']['total'] / 1000:.2f} | "
            f"{repo['query_ms']['p99']} | {rss} | "
            f"{repo['database_bytes'] / 1048576:.1f} |"
        )
    lines += [
        "",
        "## Build stage breakdown (ms)",
        "",
        "| Repo | list_files | parse | resolve | write_db | communities | total |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for repo in report["repos"]:
        timing = repo["build_ms"]
        lines.append(
            f"| {repo['name']} | {timing['list_files']} | {timing['parse']} | "
            f"{timing['resolve']} | {timing['write_db']} | {timing['communities']} | "
            f"{timing['total']} |"
        )
    lines += [
        "",
        "## Scaling observations",
        "",
        "- Seconds per 1000 nodes: "
        + ", ".join(
            f"{repo['name']} {repo['build_ms']['total'] / 1000 / max(repo['nodes'] / 1000, 1e-9):.3f}"
            for repo in report["repos"]
        ),
        "- Synthetic repos are generated deterministically and are for "
        "reference only, not a claim about real-world parsing.",
        "",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_perf.py -v`
Expected: PASS (10 passed).

- [ ] **Step 5: Commit**

```bash
git add code_review_ai/perf.py tests/test_perf.py
git commit -m "feat(perf): Markdown report renderer"
```

---

### Task 5: Orchestration script + `perf` extra + generated report

**Files:**
- Create: `scripts/run_perf_benchmark.py`
- Modify: `pyproject.toml` (add `perf = ["psutil"]` to `[project.optional-dependencies]`)
- Create: `benchmarks/PERF.md` (generated by the script on the real run)
- Report: `benchmark-results/perf-<date>.json` (gitignored)

**Interfaces:**
- Consumes: `DEFAULTS, load_config` (`code_review_ai.config`), `build_synthetic_repo, measure_repo, render_markdown, _import_psutil` (`code_review_ai.perf`).
- Produces: `scripts/run_perf_benchmark.py` CLI. Entry list = cached repos under `<cache-dir>/repos/*` (sorted, `__`→`/` display) unless `--skip-cached`, then the current repo (`Path(__file__).resolve().parents[1]`), then each `--repos` extra, then each `--synthetic` tier. One fixed `Config` (`community_detection=--community`, `community_weight="hub_pruned"`, `exclude=list(DEFAULTS["exclude"])`), `dataclasses.replace` per repo for `repo_path`/`db_path` (DBs under `<cache-dir>/indexes/<name>.db`). Writes report JSON to `--out` (default `benchmark-results/perf-<date>.json`) and rendered Markdown to `--report` (default `benchmarks/PERF.md`).

- [ ] **Step 1: Write the script**

Create `scripts/run_perf_benchmark.py`:

```python
"""Run the reproducible performance benchmark (roadmap item 7).

Measures index build time vs repo scale, query p99 latency, and build peak
RSS over real cached repos + the current repo + synthetic scaling tiers.
"""

import argparse
import dataclasses
import datetime
import json
import platform
from pathlib import Path

from code_review_ai.config import DEFAULTS, load_config
from code_review_ai.perf import (
    _import_psutil,
    build_synthetic_repo,
    measure_repo,
    render_markdown,
)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos", action="append", default=[])
    parser.add_argument("--synthetic", default="500,1000,2000,4000")
    parser.add_argument("--community", action=argparse.BooleanOptionalAction,
                        default=True)
    parser.add_argument("--query-samples", type=int, default=200)
    parser.add_argument("--skip-cached", action="store_true")
    parser.add_argument("--cache-dir", default=".benchmark-cache")
    parser.add_argument("--out", default=None)
    parser.add_argument("--report", default="benchmarks/PERF.md")
    return parser.parse_args(argv)


def _cached_repos(cache_dir: Path) -> list[Path]:
    repos_dir = cache_dir / "repos"
    if not repos_dir.is_dir():
        return []
    return sorted(path for path in repos_dir.iterdir() if path.is_dir())


def _display_name(repo_path: Path, kind: str) -> str:
    if kind in ("current", "extra"):
        return repo_path.resolve().name
    if kind == "synthetic":
        return repo_path.name
    return repo_path.name.replace("__", "/")


def _index_db_path(cache_dir: Path, name: str) -> str:
    index_dir = cache_dir / "indexes"
    index_dir.mkdir(parents=True, exist_ok=True)
    return str(index_dir / f"{name}.db")


def _fixed_config(community_detection: bool):
    config = load_config()
    config.community_detection = community_detection
    config.community_weight = "hub_pruned"
    config.exclude = list(DEFAULTS["exclude"])
    return config


def _measure(base_config, cache_dir: Path, repo_path: Path, name: str,
             kind: str, query_samples: int) -> dict:
    db_path = _index_db_path(cache_dir, name)
    config = dataclasses.replace(base_config, repo_path=str(repo_path),
                                 db_path=db_path)
    return {"name": name, "kind": kind,
            **measure_repo(config, db_path, query_samples)}


def _synthetic_tiers(raw: str) -> list[int]:
    return [int(value) for value in raw.split(",") if value.strip()]


def _build_synthetic(cache_dir: Path, file_count: int) -> Path:
    target = cache_dir / "synthetic" / f"synthetic-{file_count}"
    build_synthetic_repo(target, file_count)
    return target


def _collect_entries(args, cache_dir: Path, project_root: Path, base_config):
    entries = []
    if not args.skip_cached:
        for repo_dir in _cached_repos(cache_dir):
            name = _display_name(repo_dir, "real")
            entries.append(_measure(base_config, cache_dir, repo_dir, name,
                                    "real", args.query_samples))
    entries.append(_measure(base_config, cache_dir, project_root,
                            project_root.name, "current", args.query_samples))
    for extra in args.repos:
        extra_path = Path(extra).resolve()
        entries.append(_measure(base_config, cache_dir, extra_path,
                                extra_path.name, "extra", args.query_samples))
    for file_count in _synthetic_tiers(args.synthetic):
        target = _build_synthetic(cache_dir, file_count)
        entries.append(_measure(base_config, cache_dir, target,
                                f"synthetic-{file_count}", "synthetic",
                                args.query_samples))
    return entries


def _build_report(args, entries: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "date": datetime.date.today().isoformat(),
        "environment": {
            "platform": platform.system().lower(),
            "python": platform.python_version(),
            "psutil_available": _import_psutil() is not None,
        },
        "config": {
            "community_detection": args.community,
            "community_weight": "hub_pruned",
            "query_samples": args.query_samples,
            "exclude": list(DEFAULTS["exclude"]),
        },
        "repos": entries,
    }


def main(argv=None) -> int:
    args = _parse_args(argv)
    cache_dir = Path(args.cache_dir).resolve()
    project_root = Path(__file__).resolve().parents[1]
    base_config = _fixed_config(args.community)
    entries = _collect_entries(args, cache_dir, project_root, base_config)
    report = _build_report(args, entries)
    out_path = Path(args.out or f"benchmark-results/perf-{report['date']}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    report_path = Path(args.report)
    report_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"wrote {out_path}")
    print(f"wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Add the `perf` optional extra**

In `pyproject.toml`, change:

```toml
[project.optional-dependencies]
dev = ["pytest>=8"]
community = ["leidenalg", "igraph"]
```

to:

```toml
[project.optional-dependencies]
dev = ["pytest>=8"]
community = ["leidenalg", "igraph"]
perf = ["psutil"]
```

- [ ] **Step 3: Install deps and smoke-run the script**

Run:
```bash
uv sync --extra dev --extra perf
uv run python scripts/run_perf_benchmark.py --skip-cached --synthetic 20,40 --no-community --out <tmp>/perf-smoke.json --report <tmp>/PERF.md
```
Expected: exit 0; both files written; JSON parses; `repos` has 3 entries (current + synthetic-20 + synthetic-40). Verify with:
```bash
uv run python -c "import json,sys; d=json.load(open(sys.argv[1])); print(len(d['repos']), d['repos'][0]['name'])" <tmp>/perf-smoke.json
```

- [ ] **Step 4: Full run to generate the committed report**

Run (may take several minutes; the 4 synthetic tiers + fastapi with communities dominate):
```bash
uv run python scripts/run_perf_benchmark.py --synthetic 500,1000,2000,4000
```
Expected: exit 0; `benchmark-results/perf-<date>.json` written (gitignored); `benchmarks/PERF.md` rendered.

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest`
Expected: PASS (all prior tests + test_perf.py). Note: with psutil now installed, `test_peak_rss_during_returns_none_without_psutil` is skipped — that is correct.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml scripts/run_perf_benchmark.py benchmarks/PERF.md
git commit -m "feat(perf): benchmark orchestration script + generated report"
```

---

## Self-Review Notes

- **Spec coverage:** synthetic generator (Task 2), query p99 + sampling (Task 3), peak RSS via optional psutil with DB-size fallback (Task 3), build-vs-scale curve from real+current+synthetic entries under one fixed config (Task 5), report template JSON+Markdown (Tasks 4–5), `perf` extra (Task 5), no CLI/MCP/benchmark.py changes (honored — nothing touches them).
- **Type consistency:** `measure_repo` returns `{"source_files","nodes","edges","flows","build_ms","query_ms","peak_rss_mb","database_bytes"}` (Task 3) and the script wraps with `{"name","kind",**...}` (Task 5); `render_markdown` reads exactly those keys plus `date/environment/config` (Task 4). `_peak_rss_during` returns `(result, peak_rss_mb)` and injected `rss_monitor` lambdas in tests return the same 2-tuple shape.
- **Spec divergence (intentional):** `SyntheticStats` carries only `files` and `nodes` (both deterministic); measured `edges`/`flows` come from `measure_repo`'s `RebuildStats`, keeping the generator free of brittle hand-counted edge math. The report still shows edges/flows.
