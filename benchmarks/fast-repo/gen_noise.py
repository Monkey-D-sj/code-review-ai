"""Deterministic generator for the ``bigapp/`` noise modules.

The ``large-noise`` eval case needs a genuinely large repo so that whole-file
reading is expensive: a ~220-line mutated module (``config.py``) with **14
callers** spread across ~5500 lines. These files are boilerplate-heavy by
design, so instead of committing them by hand they are generated here and
committed to the seed; the generator is kept so the noise is reproducible.

Every generated module imports ``parse_config`` and consumes ``cfg.timeout``
in a *None-safe and behavior-preserving* way. The defensive syntax differs per
module, and several contain a ``* 1000`` next to ``cfg.timeout``, but every
pattern normalizes an omitted timeout to the same 30-second value before doing
more work. ``alerts.py`` also calls ``queue.compute_wait`` through a guarded
decoy path. The single real bug lives in ``dispatch.py`` (hand written): it
forwards the raw optional field into ``queue.compute_wait`` with no fallback,
so the dropped ``default=30`` in ``config.py`` makes ``compute_wait(None)``
raise ``TypeError`` two hops deeper.

Usage::

    python benchmarks/fast-repo/gen_noise.py          # writes src/bigapp/*.py
"""

from __future__ import annotations

import re
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent / "src" / "bigapp"

# Each module: (name, domain noun, plural, docstring, sibling edges).
# Sibling edge = (callee module, callee export function). Modules are ordered
# dependency-first so every import points at an already-defined module: the
# generated code must import cleanly (no circular imports) for reviewers.
MODULES = [
    ("archive", "blob", "blobs", "Cold storage and archive housekeeping.",
     []),
    ("store", "record", "records", "Persistence layer over the data store.",
     [("archive", "archive_batch")]),
    ("analytics", "metric", "metrics", "Metric aggregation and rollups.",
     [("store", "store_row"), ("archive", "archive_batch")]),
    ("ingest", "batch", "batches", "Record ingestion from event streams.",
     [("store", "store_row"), ("archive", "archive_batch")]),
    ("transform", "row", "rows", "Row-level transformation between formats.",
     [("ingest", "ingest_batch"), ("analytics", "aggregate")]),
    ("sync", "cursor", "cursors", "Incremental sync cursors across replicas.",
     [("store", "store_row"), ("analytics", "aggregate")]),
    ("reporting", "report", "reports", "Report generation and scheduling.",
     [("analytics", "aggregate"), ("store", "store_row")]),
    ("export", "file", "files", "Bulk data export to external sinks.",
     [("transform", "transform_row"), ("store", "store_row")]),
    ("backfill", "chunk", "chunks", "Historical backfill workers.",
     [("ingest", "ingest_batch"), ("store", "store_row")]),
    ("dashboard", "widget", "widgets", "Dashboard widget rendering.",
     [("analytics", "aggregate"), ("reporting", "render_report")]),
    ("metrics_ui", "card", "cards", "Metrics cards for the operations UI.",
     [("analytics", "aggregate"), ("dashboard", "render_widget")]),
    ("alerts", "alert", "alerts", "Alert evaluation and routing.",
     [("analytics", "aggregate"), ("metrics_ui", "metrics_card")]),
    ("gateway", "request", "requests", "API gateway request routing.",
     [("sync", "sync_cursor"), ("store", "store_row")]),
    ("pipeline", "stage", "stages", "Pipeline orchestration stages.",
     [("ingest", "ingest_batch"), ("transform", "transform_row"), ("store", "store_row")]),
]

# Canonical export function per module (what siblings import and call).
EXPORTS = {
    "pipeline": "run_stage",
    "ingest": "ingest_batch",
    "transform": "transform_row",
    "store": "store_row",
    "archive": "archive_batch",
    "sync": "sync_cursor",
    "analytics": "aggregate",
    "reporting": "render_report",
    "dashboard": "render_widget",
    "alerts": "evaluate_alert",
    "metrics_ui": "metrics_card",
    "gateway": "route_request",
    "export": "export_rows",
    "backfill": "backfill_chunk",
}

# {domain} / {noun} / {plural} placeholders are substituted by plain replace so
# the f-string braces inside template bodies survive untouched.
TEMPLATES = [
    '''def _normalize_{noun}(value):
    """Canonicalize a {domain} {noun} value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)
''',
    '''def _batch_{plural}(items, batch_size):
    """Yield {plural} in fixed-size batches for {domain} processing."""
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]
''',
    '''def _dedupe_{plural}(items):
    """Drop duplicate {noun} entries while preserving first-seen order."""
    seen = set()
    result = []
    for item in items:
        marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result
''',
    '''def _active_{plural}(items):
    """Keep only {noun} marked active in a {domain} stream."""
    return [item for item in items if item.get("active", True)]
''',
    '''def _retry_call(fn, attempts, base_delay):
    """Retry ``fn`` with a short delay, raising after ``attempts`` tries."""
    last_error = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as error:
            last_error = error
            time.sleep(base_delay * (attempt + 1))
    raise RuntimeError(f"retries exhausted: {last_error}")
''',
    '''def _sample_{plural}(items, rate):
    """Deterministic sampling of {plural} at a given rate."""
    step = max(int(round(1.0 / rate)), 1) if rate > 0 else 1
    return items[::step]
''',
    '''def _aggregate_{plural}(items):
    """Fold {plural} into a single summary dict for {domain} reporting."""
    total = 0
    for item in items:
        total += int(item.get("amount", 0))
    return {"count": len(items), "total": total, "domain": "{domain}"}
''',
    '''def _format_{noun}(value, precision=2):
    """Render a {domain} {noun} value with fixed precision."""
    return f"{float(value):.{precision}f}"
''',
    '''def _valid_{noun}(value):
    """Check a {domain} {noun} value against the expected shape."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value.get("id"))
    return bool(value)
''',
    '''def _as_timestamp(record):
    """Coerce a {domain} record to an epoch-style timestamp."""
    raw = record.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return 0.0
''',
    '''def _remap_{plural}(items, key="id"):
    """Build an index of {plural} keyed by ``key`` for quick lookup."""
    return {str(item.get(key)): item for item in items if item.get(key) is not None}
''',
    '''def _above_threshold(value, threshold):
    """Whether a {domain} metric exceeds its alert threshold."""
    if value is None:
        return False
    return float(value) > float(threshold)
''',
    '''def _to_payload(record):
    """Serialize a {domain} record to a compact JSON string."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))
''',
    '''def _truncate_text(text, limit=120):
    """Trim long {domain} text to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
''',
    '''def _throttle(budget, used):
    """Remaining calls before a {domain} rate budget is exhausted."""
    return max(budget - used, 0)
''',
    '''def _shard_key(record, shards):
    """Stable shard assignment for a {domain} record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards
''',
]


# Varied ways a module may normalize ``cfg.timeout``. Every pattern is
# intentionally equivalent for the fixed default (30) and the mutated None
# state. This is stronger than merely avoiding an exception: the noise paths
# must not introduce silent value, type, serialization, or logging changes.
TIMEOUT_PATTERNS = [
    '''    return cfg.timeout or DEFAULT_{token}_TIMEOUT
''',
    '''    return cfg.timeout if cfg.timeout is not None else DEFAULT_{token}_TIMEOUT
''',
    '''    return (cfg.timeout or DEFAULT_{token}_TIMEOUT) * 1000
''',
    '''    return max(cfg.timeout or DEFAULT_{token}_TIMEOUT, DEFAULT_{token}_TIMEOUT) * 1000
''',
    '''    return (cfg.timeout or DEFAULT_{token}_TIMEOUT) * 1000
''',
    '''    return cfg.timeout * 1000 if cfg.timeout is not None else DEFAULT_{token}_TIMEOUT * 1000
''',
    '''    return int(cfg.timeout if cfg.timeout is not None else DEFAULT_{token}_TIMEOUT)
''',
    '''    return _effective_timeout(cfg.timeout, DEFAULT_{token}_TIMEOUT)
''',
    '''    return str(cfg.timeout if cfg.timeout is not None else DEFAULT_{token}_TIMEOUT)
''',
]

EFFECTIVE_HELPER = '''def _effective_timeout(value, fallback):
    """Resolve a timeout, falling back when unset or falsy."""
    return value or fallback
'''

# Modules that ALSO call the real crash function ``queue.compute_wait`` but
# guard at the call site — so finding ``compute_wait`` is not enough to locate
# the bug; the reviewer must compare the callers and spot the one unguarded.
DECOY_IMPORT = "from bigapp.queue import compute_wait"

DECOY_SNIPPETS = {
    "alerts": '''def _wait_before_alert(cfg: AppConfig) -> float:
    """Seconds of quiet before alert evaluation starts."""
    return compute_wait(cfg.timeout or DEFAULT_ALERTS_TIMEOUT) / 1000.0
''',
}


def substitute(template: str, noun: str, plural: str, domain: str,
               token: str = "") -> str:
    return (template.replace("{noun}", noun)
                    .replace("{plural}", plural)
                    .replace("{domain}", domain)
                    .replace("{token}", token))


def module_source(name: str, domain: str, noun: str, plural: str,
                  docstring: str, siblings: list[tuple[str, str]],
                  module_index: int) -> str:
    const_token = name.upper()
    module_header = '"""' + docstring + '\n"""\n\nfrom __future__ import annotations\n\n'
    imports = [
        "import json",
        "import re",
        "import time",
        "from typing import Any",
        "",
        "from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config",
    ]
    for callee, export in siblings:
        imports.append(f"from bigapp.{callee} import {export}")
    if name in DECOY_SNIPPETS:
        imports.append(DECOY_IMPORT)
    timeout_pattern = TIMEOUT_PATTERNS[module_index % len(TIMEOUT_PATTERNS)]
    body = [
        "",
        f"DEFAULT_{const_token}_TIMEOUT = 30",
        "",
        "",
        'def _runtime_config(payload: dict[str, Any]) -> AppConfig:',
        f'    """Load runtime settings for the {domain} path."""',
        "    return parse_config(payload)",
        "",
        "",
        f'def _resolve_{noun}_timeout(cfg: AppConfig):',
        f'    """Effective timeout for {domain} work, defaulting when unset."""',
        substitute(timeout_pattern, noun, plural, domain, const_token).rstrip(),
    ]
    if module_index % len(TIMEOUT_PATTERNS) == 7:
        body += ["", EFFECTIVE_HELPER.rstrip()]
    body += [
        "",
        "",
        f'def _describe_{noun}(cfg: AppConfig) -> str:',
        f'    """Human description of the runtime config for logs."""',
        f'    return f"{domain}:timeout={{_resolve_{noun}_timeout(cfg)}}"',
        "",
        "",
        f'def {EXPORTS[name]}(items, raw_config):',
        f'    """Process {plural} under the runtime configuration."""',
        "    cfg = _runtime_config(raw_config)",
        f"    timeout = _resolve_{noun}_timeout(cfg)",
        f"    normalized = [_normalize_{noun}(item) for item in items]",
        "    return len(normalized)",
    ]
    # deterministic template pick: always emit _normalize_{noun} (the export
    # function calls it), then 13 more without duplicates.
    picked = [0]
    cursor = sum(ord(ch) for ch in name) % len(TEMPLATES)
    while len(picked) < 14:
        cursor = (cursor + 1) % len(TEMPLATES)
        if cursor not in picked:
            picked.append(cursor)
    for template_index in picked:
        body.append("")
        body.append(substitute(TEMPLATES[template_index], noun, plural, domain))
    # cross-module calls (resolved graph edges); pass the config through so the
    # callee's (items, raw_config) signature is satisfied.
    for index, (callee, export) in enumerate(siblings):
        body.append("")
        if index % 2 == 0:
            body += [
                f'def _handoff_{callee}(item, raw_config):',
                f'    """Hand one item to the {callee} path."""',
                f"    return {export}(item, raw_config)",
            ]
        else:
            body += [
                f'def _drain_to_{callee}(items, raw_config):',
                f'    """Bulk hand-off of items to the {callee} path."""',
                f"    return [{export}(item, raw_config) for item in items]",
            ]
    if name in DECOY_SNIPPETS:
        body += ["", DECOY_SNIPPETS[name].rstrip()]
    return module_header + "\n".join(imports) + "\n\n\n" + "\n\n".join(body) + "\n"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "__init__.py").write_text('"""bigapp: the large service under review."""\n',
                                         encoding="utf-8")
    for index, (name, noun, plural, docstring, siblings) in enumerate(MODULES):
        # keep order + determinism explicit
        domain = name.replace("_", " ")
        source = module_source(name, domain, noun, plural, docstring, siblings, index)
        (OUT_DIR / f"{name}.py").write_text(source, encoding="utf-8")
        lines = source.count("\n")
        print(f"wrote bigapp/{name}.py ({lines} lines)")
    print(f"total: {len(MODULES)} modules in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
