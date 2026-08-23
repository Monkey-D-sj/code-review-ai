"""API gateway request routing.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config
from bigapp.sync import sync_cursor
from bigapp.store import store_row




DEFAULT_GATEWAY_TIMEOUT = 30





def _runtime_config(payload: dict[str, Any]) -> AppConfig:

    """Load runtime settings for the gateway path."""

    return parse_config(payload)





def _resolve_request_timeout(cfg: AppConfig):

    """Effective timeout for gateway work, defaulting when unset."""

    return max(cfg.timeout or DEFAULT_GATEWAY_TIMEOUT, DEFAULT_GATEWAY_TIMEOUT) * 1000





def _describe_request(cfg: AppConfig) -> str:

    """Human description of the runtime config for logs."""

    return f"gateway:timeout={_resolve_request_timeout(cfg)}"





def route_request(items, raw_config):

    """Process requests under the runtime configuration."""

    cfg = _runtime_config(raw_config)

    timeout = _resolve_request_timeout(cfg)

    normalized = [_normalize_request(item) for item in items]

    return len(normalized)



def _normalize_request(value):
    """Canonicalize a gateway request value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)




def _active_requests(items):
    """Keep only request marked active in a gateway stream."""
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




def _sample_requests(items, rate):
    """Deterministic sampling of requests at a given rate."""
    step = max(int(round(1.0 / rate)), 1) if rate > 0 else 1
    return items[::step]




def _aggregate_requests(items):
    """Fold requests into a single summary dict for gateway reporting."""
    total = 0
    for item in items:
        total += int(item.get("amount", 0))
    return {"count": len(items), "total": total, "domain": "gateway"}




def _format_request(value, precision=2):
    """Render a gateway request value with fixed precision."""
    return f"{float(value):.{precision}f}"




def _valid_request(value):
    """Check a gateway request value against the expected shape."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value.get("id"))
    return bool(value)




def _as_timestamp(record):
    """Coerce a gateway record to an epoch-style timestamp."""
    raw = record.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return 0.0




def _remap_requests(items, key="id"):
    """Build an index of requests keyed by ``key`` for quick lookup."""
    return {str(item.get(key)): item for item in items if item.get(key) is not None}




def _above_threshold(value, threshold):
    """Whether a gateway metric exceeds its alert threshold."""
    if value is None:
        return False
    return float(value) > float(threshold)




def _to_payload(record):
    """Serialize a gateway record to a compact JSON string."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))




def _truncate_text(text, limit=120):
    """Trim long gateway text to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."




def _throttle(budget, used):
    """Remaining calls before a gateway rate budget is exhausted."""
    return max(budget - used, 0)




def _shard_key(record, shards):
    """Stable shard assignment for a gateway record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards




def _handoff_sync(item, raw_config):

    """Hand one item to the sync path."""

    return sync_cursor(item, raw_config)



def _drain_to_store(items, raw_config):

    """Bulk hand-off of items to the store path."""

    return [store_row(item, raw_config) for item in items]
