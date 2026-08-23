"""Historical backfill workers.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config
from bigapp.ingest import ingest_batch
from bigapp.store import store_row




DEFAULT_BACKFILL_TIMEOUT = 30





def _runtime_config(payload: dict[str, Any]) -> AppConfig:

    """Load runtime settings for the backfill path."""

    return parse_config(payload)





def _resolve_chunk_timeout(cfg: AppConfig):

    """Effective timeout for backfill work, defaulting when unset."""

    return str(cfg.timeout if cfg.timeout is not None else DEFAULT_BACKFILL_TIMEOUT)





def _describe_chunk(cfg: AppConfig) -> str:

    """Human description of the runtime config for logs."""

    return f"backfill:timeout={_resolve_chunk_timeout(cfg)}"





def backfill_chunk(items, raw_config):

    """Process chunks under the runtime configuration."""

    cfg = _runtime_config(raw_config)

    timeout = _resolve_chunk_timeout(cfg)

    normalized = [_normalize_chunk(item) for item in items]

    return len(normalized)



def _normalize_chunk(value):
    """Canonicalize a backfill chunk value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)




def _as_timestamp(record):
    """Coerce a backfill record to an epoch-style timestamp."""
    raw = record.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return 0.0




def _remap_chunks(items, key="id"):
    """Build an index of chunks keyed by ``key`` for quick lookup."""
    return {str(item.get(key)): item for item in items if item.get(key) is not None}




def _above_threshold(value, threshold):
    """Whether a backfill metric exceeds its alert threshold."""
    if value is None:
        return False
    return float(value) > float(threshold)




def _to_payload(record):
    """Serialize a backfill record to a compact JSON string."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))




def _truncate_text(text, limit=120):
    """Trim long backfill text to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."




def _throttle(budget, used):
    """Remaining calls before a backfill rate budget is exhausted."""
    return max(budget - used, 0)




def _shard_key(record, shards):
    """Stable shard assignment for a backfill record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards




def _batch_chunks(items, batch_size):
    """Yield chunks in fixed-size batches for backfill processing."""
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]




def _dedupe_chunks(items):
    """Drop duplicate chunk entries while preserving first-seen order."""
    seen = set()
    result = []
    for item in items:
        marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result




def _active_chunks(items):
    """Keep only chunk marked active in a backfill stream."""
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




def _sample_chunks(items, rate):
    """Deterministic sampling of chunks at a given rate."""
    step = max(int(round(1.0 / rate)), 1) if rate > 0 else 1
    return items[::step]




def _aggregate_chunks(items):
    """Fold chunks into a single summary dict for backfill reporting."""
    total = 0
    for item in items:
        total += int(item.get("amount", 0))
    return {"count": len(items), "total": total, "domain": "backfill"}




def _handoff_ingest(item, raw_config):

    """Hand one item to the ingest path."""

    return ingest_batch(item, raw_config)



def _drain_to_store(items, raw_config):

    """Bulk hand-off of items to the store path."""

    return [store_row(item, raw_config) for item in items]
