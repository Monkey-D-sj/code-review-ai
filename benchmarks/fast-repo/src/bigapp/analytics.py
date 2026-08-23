"""Metric aggregation and rollups.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config
from bigapp.store import store_row
from bigapp.archive import archive_batch




DEFAULT_ANALYTICS_TIMEOUT = 30





def _runtime_config(payload: dict[str, Any]) -> AppConfig:

    """Load runtime settings for the analytics path."""

    return parse_config(payload)





def _resolve_metric_timeout(cfg: AppConfig):

    """Effective timeout for analytics work, defaulting when unset."""

    return (cfg.timeout or DEFAULT_ANALYTICS_TIMEOUT) * 1000





def _describe_metric(cfg: AppConfig) -> str:

    """Human description of the runtime config for logs."""

    return f"analytics:timeout={_resolve_metric_timeout(cfg)}"





def aggregate(items, raw_config):

    """Process metrics under the runtime configuration."""

    cfg = _runtime_config(raw_config)

    timeout = _resolve_metric_timeout(cfg)

    normalized = [_normalize_metric(item) for item in items]

    return len(normalized)



def _normalize_metric(value):
    """Canonicalize a analytics metric value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)




def _as_timestamp(record):
    """Coerce a analytics record to an epoch-style timestamp."""
    raw = record.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return 0.0




def _remap_metrics(items, key="id"):
    """Build an index of metrics keyed by ``key`` for quick lookup."""
    return {str(item.get(key)): item for item in items if item.get(key) is not None}




def _above_threshold(value, threshold):
    """Whether a analytics metric exceeds its alert threshold."""
    if value is None:
        return False
    return float(value) > float(threshold)




def _to_payload(record):
    """Serialize a analytics record to a compact JSON string."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))




def _truncate_text(text, limit=120):
    """Trim long analytics text to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."




def _throttle(budget, used):
    """Remaining calls before a analytics rate budget is exhausted."""
    return max(budget - used, 0)




def _shard_key(record, shards):
    """Stable shard assignment for a analytics record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards




def _batch_metrics(items, batch_size):
    """Yield metrics in fixed-size batches for analytics processing."""
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]




def _dedupe_metrics(items):
    """Drop duplicate metric entries while preserving first-seen order."""
    seen = set()
    result = []
    for item in items:
        marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result




def _active_metrics(items):
    """Keep only metric marked active in a analytics stream."""
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




def _sample_metrics(items, rate):
    """Deterministic sampling of metrics at a given rate."""
    step = max(int(round(1.0 / rate)), 1) if rate > 0 else 1
    return items[::step]




def _aggregate_metrics(items):
    """Fold metrics into a single summary dict for analytics reporting."""
    total = 0
    for item in items:
        total += int(item.get("amount", 0))
    return {"count": len(items), "total": total, "domain": "analytics"}




def _handoff_store(item, raw_config):

    """Hand one item to the store path."""

    return store_row(item, raw_config)



def _drain_to_archive(items, raw_config):

    """Bulk hand-off of items to the archive path."""

    return [archive_batch(item, raw_config) for item in items]
