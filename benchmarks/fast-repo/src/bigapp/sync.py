"""Incremental sync cursors across replicas.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config
from bigapp.store import store_row
from bigapp.analytics import aggregate




DEFAULT_SYNC_TIMEOUT = 30





def _runtime_config(payload: dict[str, Any]) -> AppConfig:

    """Load runtime settings for the sync path."""

    return parse_config(payload)





def _resolve_cursor_timeout(cfg: AppConfig):

    """Effective timeout for sync work, defaulting when unset."""

    return cfg.timeout * 1000 if cfg.timeout is not None else DEFAULT_SYNC_TIMEOUT * 1000





def _describe_cursor(cfg: AppConfig) -> str:

    """Human description of the runtime config for logs."""

    return f"sync:timeout={_resolve_cursor_timeout(cfg)}"





def sync_cursor(items, raw_config):

    """Process cursors under the runtime configuration."""

    cfg = _runtime_config(raw_config)

    timeout = _resolve_cursor_timeout(cfg)

    normalized = [_normalize_cursor(item) for item in items]

    return len(normalized)



def _normalize_cursor(value):
    """Canonicalize a sync cursor value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)




def _throttle(budget, used):
    """Remaining calls before a sync rate budget is exhausted."""
    return max(budget - used, 0)




def _shard_key(record, shards):
    """Stable shard assignment for a sync record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards




def _batch_cursors(items, batch_size):
    """Yield cursors in fixed-size batches for sync processing."""
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]




def _dedupe_cursors(items):
    """Drop duplicate cursor entries while preserving first-seen order."""
    seen = set()
    result = []
    for item in items:
        marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result




def _active_cursors(items):
    """Keep only cursor marked active in a sync stream."""
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




def _sample_cursors(items, rate):
    """Deterministic sampling of cursors at a given rate."""
    step = max(int(round(1.0 / rate)), 1) if rate > 0 else 1
    return items[::step]




def _aggregate_cursors(items):
    """Fold cursors into a single summary dict for sync reporting."""
    total = 0
    for item in items:
        total += int(item.get("amount", 0))
    return {"count": len(items), "total": total, "domain": "sync"}




def _format_cursor(value, precision=2):
    """Render a sync cursor value with fixed precision."""
    return f"{float(value):.{precision}f}"




def _valid_cursor(value):
    """Check a sync cursor value against the expected shape."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value.get("id"))
    return bool(value)




def _as_timestamp(record):
    """Coerce a sync record to an epoch-style timestamp."""
    raw = record.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return 0.0




def _remap_cursors(items, key="id"):
    """Build an index of cursors keyed by ``key`` for quick lookup."""
    return {str(item.get(key)): item for item in items if item.get(key) is not None}




def _above_threshold(value, threshold):
    """Whether a sync metric exceeds its alert threshold."""
    if value is None:
        return False
    return float(value) > float(threshold)




def _handoff_store(item, raw_config):

    """Hand one item to the store path."""

    return store_row(item, raw_config)



def _drain_to_analytics(items, raw_config):

    """Bulk hand-off of items to the analytics path."""

    return [aggregate(item, raw_config) for item in items]
