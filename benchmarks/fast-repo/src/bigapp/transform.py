"""Row-level transformation between formats.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config
from bigapp.ingest import ingest_batch
from bigapp.analytics import aggregate




DEFAULT_TRANSFORM_TIMEOUT = 30





def _runtime_config(payload: dict[str, Any]) -> AppConfig:

    """Load runtime settings for the transform path."""

    return parse_config(payload)





def _resolve_row_timeout(cfg: AppConfig):

    """Effective timeout for transform work, defaulting when unset."""

    return (cfg.timeout or DEFAULT_TRANSFORM_TIMEOUT) * 1000





def _describe_row(cfg: AppConfig) -> str:

    """Human description of the runtime config for logs."""

    return f"transform:timeout={_resolve_row_timeout(cfg)}"





def transform_row(items, raw_config):

    """Process rows under the runtime configuration."""

    cfg = _runtime_config(raw_config)

    timeout = _resolve_row_timeout(cfg)

    normalized = [_normalize_row(item) for item in items]

    return len(normalized)



def _normalize_row(value):
    """Canonicalize a transform row value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)




def _truncate_text(text, limit=120):
    """Trim long transform text to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."




def _throttle(budget, used):
    """Remaining calls before a transform rate budget is exhausted."""
    return max(budget - used, 0)




def _shard_key(record, shards):
    """Stable shard assignment for a transform record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards




def _batch_rows(items, batch_size):
    """Yield rows in fixed-size batches for transform processing."""
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]




def _dedupe_rows(items):
    """Drop duplicate row entries while preserving first-seen order."""
    seen = set()
    result = []
    for item in items:
        marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result




def _active_rows(items):
    """Keep only row marked active in a transform stream."""
    return [item for item in items if item.get("active", True)]




def _retry_call(fn, attempts, base_delay):
    """Retry ``fn`` with a short delay, raising after ``attempts`` tries."""
    last_error = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as error:
            last_error = error
            time.sleep(base_delay * (attempt + 1))
    raise RuntimeError(f"retries exhausted: {last_error}")




def _sample_rows(items, rate):
    """Deterministic sampling of rows at a given rate."""
    step = max(int(round(1.0 / rate)), 1) if rate > 0 else 1
    return items[::step]




def _aggregate_rows(items):
    """Fold rows into a single summary dict for transform reporting."""
    total = 0
    for item in items:
        total += int(item.get("amount", 0))
    return {"count": len(items), "total": total, "domain": "transform"}




def _format_row(value, precision=2):
    """Render a transform row value with fixed precision."""
    return f"{float(value):.{precision}f}"




def _valid_row(value):
    """Check a transform row value against the expected shape."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value.get("id"))
    return bool(value)




def _as_timestamp(record):
    """Coerce a transform record to an epoch-style timestamp."""
    raw = record.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return 0.0




def _remap_rows(items, key="id"):
    """Build an index of rows keyed by ``key`` for quick lookup."""
    return {str(item.get(key)): item for item in items if item.get(key) is not None}




def _handoff_ingest(item, raw_config):

    """Hand one item to the ingest path."""

    return ingest_batch(item, raw_config)



def _drain_to_analytics(items, raw_config):

    """Bulk hand-off of items to the analytics path."""

    return [aggregate(item, raw_config) for item in items]
