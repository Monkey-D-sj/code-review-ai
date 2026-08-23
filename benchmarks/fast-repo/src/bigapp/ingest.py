"""Record ingestion from event streams.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config
from bigapp.store import store_row
from bigapp.archive import archive_batch




DEFAULT_INGEST_TIMEOUT = 30





def _runtime_config(payload: dict[str, Any]) -> AppConfig:

    """Load runtime settings for the ingest path."""

    return parse_config(payload)





def _resolve_batch_timeout(cfg: AppConfig):

    """Effective timeout for ingest work, defaulting when unset."""

    return max(cfg.timeout or DEFAULT_INGEST_TIMEOUT, DEFAULT_INGEST_TIMEOUT) * 1000





def _describe_batch(cfg: AppConfig) -> str:

    """Human description of the runtime config for logs."""

    return f"ingest:timeout={_resolve_batch_timeout(cfg)}"





def ingest_batch(items, raw_config):

    """Process batches under the runtime configuration."""

    cfg = _runtime_config(raw_config)

    timeout = _resolve_batch_timeout(cfg)

    normalized = [_normalize_batch(item) for item in items]

    return len(normalized)



def _normalize_batch(value):
    """Canonicalize a ingest batch value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)




def _above_threshold(value, threshold):
    """Whether a ingest metric exceeds its alert threshold."""
    if value is None:
        return False
    return float(value) > float(threshold)




def _to_payload(record):
    """Serialize a ingest record to a compact JSON string."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))




def _truncate_text(text, limit=120):
    """Trim long ingest text to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."




def _throttle(budget, used):
    """Remaining calls before a ingest rate budget is exhausted."""
    return max(budget - used, 0)




def _shard_key(record, shards):
    """Stable shard assignment for a ingest record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards




def _batch_batches(items, batch_size):
    """Yield batches in fixed-size batches for ingest processing."""
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]




def _dedupe_batches(items):
    """Drop duplicate batch entries while preserving first-seen order."""
    seen = set()
    result = []
    for item in items:
        marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result




def _active_batches(items):
    """Keep only batch marked active in a ingest stream."""
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




def _sample_batches(items, rate):
    """Deterministic sampling of batches at a given rate."""
    step = max(int(round(1.0 / rate)), 1) if rate > 0 else 1
    return items[::step]




def _aggregate_batches(items):
    """Fold batches into a single summary dict for ingest reporting."""
    total = 0
    for item in items:
        total += int(item.get("amount", 0))
    return {"count": len(items), "total": total, "domain": "ingest"}




def _format_batch(value, precision=2):
    """Render a ingest batch value with fixed precision."""
    return f"{float(value):.{precision}f}"




def _valid_batch(value):
    """Check a ingest batch value against the expected shape."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value.get("id"))
    return bool(value)




def _handoff_store(item, raw_config):

    """Hand one item to the store path."""

    return store_row(item, raw_config)



def _drain_to_archive(items, raw_config):

    """Bulk hand-off of items to the archive path."""

    return [archive_batch(item, raw_config) for item in items]
