"""Cold storage and archive housekeeping.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config




DEFAULT_ARCHIVE_TIMEOUT = 30





def _runtime_config(payload: dict[str, Any]) -> AppConfig:

    """Load runtime settings for the archive path."""

    return parse_config(payload)





def _resolve_blob_timeout(cfg: AppConfig):

    """Effective timeout for archive work, defaulting when unset."""

    return cfg.timeout or DEFAULT_ARCHIVE_TIMEOUT





def _describe_blob(cfg: AppConfig) -> str:

    """Human description of the runtime config for logs."""

    return f"archive:timeout={_resolve_blob_timeout(cfg)}"





def archive_batch(items, raw_config):

    """Process blobs under the runtime configuration."""

    cfg = _runtime_config(raw_config)

    timeout = _resolve_blob_timeout(cfg)

    normalized = [_normalize_blob(item) for item in items]

    return len(normalized)



def _normalize_blob(value):
    """Canonicalize a archive blob value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)




def _active_blobs(items):
    """Keep only blob marked active in a archive stream."""
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




def _sample_blobs(items, rate):
    """Deterministic sampling of blobs at a given rate."""
    step = max(int(round(1.0 / rate)), 1) if rate > 0 else 1
    return items[::step]




def _aggregate_blobs(items):
    """Fold blobs into a single summary dict for archive reporting."""
    total = 0
    for item in items:
        total += int(item.get("amount", 0))
    return {"count": len(items), "total": total, "domain": "archive"}




def _format_blob(value, precision=2):
    """Render a archive blob value with fixed precision."""
    return f"{float(value):.{precision}f}"




def _valid_blob(value):
    """Check a archive blob value against the expected shape."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value.get("id"))
    return bool(value)




def _as_timestamp(record):
    """Coerce a archive record to an epoch-style timestamp."""
    raw = record.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return 0.0




def _remap_blobs(items, key="id"):
    """Build an index of blobs keyed by ``key`` for quick lookup."""
    return {str(item.get(key)): item for item in items if item.get(key) is not None}




def _above_threshold(value, threshold):
    """Whether a archive metric exceeds its alert threshold."""
    if value is None:
        return False
    return float(value) > float(threshold)




def _to_payload(record):
    """Serialize a archive record to a compact JSON string."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))




def _truncate_text(text, limit=120):
    """Trim long archive text to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."




def _throttle(budget, used):
    """Remaining calls before a archive rate budget is exhausted."""
    return max(budget - used, 0)




def _shard_key(record, shards):
    """Stable shard assignment for a archive record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards

