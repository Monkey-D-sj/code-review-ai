"""Bulk data export to external sinks.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config
from bigapp.transform import transform_row
from bigapp.store import store_row




DEFAULT_EXPORT_TIMEOUT = 30





def _runtime_config(payload: dict[str, Any]) -> AppConfig:

    """Load runtime settings for the export path."""

    return parse_config(payload)





def _resolve_file_timeout(cfg: AppConfig):

    """Effective timeout for export work, defaulting when unset."""

    return _effective_timeout(cfg.timeout, DEFAULT_EXPORT_TIMEOUT)



def _effective_timeout(value, fallback):
    """Resolve a timeout, falling back when unset or falsy."""
    return value or fallback





def _describe_file(cfg: AppConfig) -> str:

    """Human description of the runtime config for logs."""

    return f"export:timeout={_resolve_file_timeout(cfg)}"





def export_rows(items, raw_config):

    """Process files under the runtime configuration."""

    cfg = _runtime_config(raw_config)

    timeout = _resolve_file_timeout(cfg)

    normalized = [_normalize_file(item) for item in items]

    return len(normalized)



def _normalize_file(value):
    """Canonicalize a export file value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)




def _active_files(items):
    """Keep only file marked active in a export stream."""
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




def _sample_files(items, rate):
    """Deterministic sampling of files at a given rate."""
    step = max(int(round(1.0 / rate)), 1) if rate > 0 else 1
    return items[::step]




def _aggregate_files(items):
    """Fold files into a single summary dict for export reporting."""
    total = 0
    for item in items:
        total += int(item.get("amount", 0))
    return {"count": len(items), "total": total, "domain": "export"}




def _format_file(value, precision=2):
    """Render a export file value with fixed precision."""
    return f"{float(value):.{precision}f}"




def _valid_file(value):
    """Check a export file value against the expected shape."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value.get("id"))
    return bool(value)




def _as_timestamp(record):
    """Coerce a export record to an epoch-style timestamp."""
    raw = record.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return 0.0




def _remap_files(items, key="id"):
    """Build an index of files keyed by ``key`` for quick lookup."""
    return {str(item.get(key)): item for item in items if item.get(key) is not None}




def _above_threshold(value, threshold):
    """Whether a export metric exceeds its alert threshold."""
    if value is None:
        return False
    return float(value) > float(threshold)




def _to_payload(record):
    """Serialize a export record to a compact JSON string."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))




def _truncate_text(text, limit=120):
    """Trim long export text to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."




def _throttle(budget, used):
    """Remaining calls before a export rate budget is exhausted."""
    return max(budget - used, 0)




def _shard_key(record, shards):
    """Stable shard assignment for a export record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards




def _handoff_transform(item, raw_config):

    """Hand one item to the transform path."""

    return transform_row(item, raw_config)



def _drain_to_store(items, raw_config):

    """Bulk hand-off of items to the store path."""

    return [store_row(item, raw_config) for item in items]
