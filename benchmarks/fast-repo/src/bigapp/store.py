"""Persistence layer over the data store.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config
from bigapp.archive import archive_batch




DEFAULT_STORE_TIMEOUT = 30





def _runtime_config(payload: dict[str, Any]) -> AppConfig:

    """Load runtime settings for the store path."""

    return parse_config(payload)





def _resolve_record_timeout(cfg: AppConfig):

    """Effective timeout for store work, defaulting when unset."""

    return cfg.timeout if cfg.timeout is not None else DEFAULT_STORE_TIMEOUT





def _describe_record(cfg: AppConfig) -> str:

    """Human description of the runtime config for logs."""

    return f"store:timeout={_resolve_record_timeout(cfg)}"





def store_row(items, raw_config):

    """Process records under the runtime configuration."""

    cfg = _runtime_config(raw_config)

    timeout = _resolve_record_timeout(cfg)

    normalized = [_normalize_record(item) for item in items]

    return len(normalized)



def _normalize_record(value):
    """Canonicalize a store record value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)




def _throttle(budget, used):
    """Remaining calls before a store rate budget is exhausted."""
    return max(budget - used, 0)




def _shard_key(record, shards):
    """Stable shard assignment for a store record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards




def _batch_records(items, batch_size):
    """Yield records in fixed-size batches for store processing."""
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]




def _dedupe_records(items):
    """Drop duplicate record entries while preserving first-seen order."""
    seen = set()
    result = []
    for item in items:
        marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result




def _active_records(items):
    """Keep only record marked active in a store stream."""
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




def _sample_records(items, rate):
    """Deterministic sampling of records at a given rate."""
    step = max(int(round(1.0 / rate)), 1) if rate > 0 else 1
    return items[::step]




def _aggregate_records(items):
    """Fold records into a single summary dict for store reporting."""
    total = 0
    for item in items:
        total += int(item.get("amount", 0))
    return {"count": len(items), "total": total, "domain": "store"}




def _format_record(value, precision=2):
    """Render a store record value with fixed precision."""
    return f"{float(value):.{precision}f}"




def _valid_record(value):
    """Check a store record value against the expected shape."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value.get("id"))
    return bool(value)




def _as_timestamp(record):
    """Coerce a store record to an epoch-style timestamp."""
    raw = record.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return 0.0




def _remap_records(items, key="id"):
    """Build an index of records keyed by ``key`` for quick lookup."""
    return {str(item.get(key)): item for item in items if item.get(key) is not None}




def _above_threshold(value, threshold):
    """Whether a store metric exceeds its alert threshold."""
    if value is None:
        return False
    return float(value) > float(threshold)




def _handoff_archive(item, raw_config):

    """Hand one item to the archive path."""

    return archive_batch(item, raw_config)
