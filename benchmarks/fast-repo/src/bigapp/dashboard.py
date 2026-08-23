"""Dashboard widget rendering.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config
from bigapp.analytics import aggregate
from bigapp.reporting import render_report




DEFAULT_DASHBOARD_TIMEOUT = 30





def _runtime_config(payload: dict[str, Any]) -> AppConfig:

    """Load runtime settings for the dashboard path."""

    return parse_config(payload)





def _resolve_widget_timeout(cfg: AppConfig):

    """Effective timeout for dashboard work, defaulting when unset."""

    return cfg.timeout or DEFAULT_DASHBOARD_TIMEOUT





def _describe_widget(cfg: AppConfig) -> str:

    """Human description of the runtime config for logs."""

    return f"dashboard:timeout={_resolve_widget_timeout(cfg)}"





def render_widget(items, raw_config):

    """Process widgets under the runtime configuration."""

    cfg = _runtime_config(raw_config)

    timeout = _resolve_widget_timeout(cfg)

    normalized = [_normalize_widget(item) for item in items]

    return len(normalized)



def _normalize_widget(value):
    """Canonicalize a dashboard widget value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)




def _as_timestamp(record):
    """Coerce a dashboard record to an epoch-style timestamp."""
    raw = record.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return 0.0




def _remap_widgets(items, key="id"):
    """Build an index of widgets keyed by ``key`` for quick lookup."""
    return {str(item.get(key)): item for item in items if item.get(key) is not None}




def _above_threshold(value, threshold):
    """Whether a dashboard metric exceeds its alert threshold."""
    if value is None:
        return False
    return float(value) > float(threshold)




def _to_payload(record):
    """Serialize a dashboard record to a compact JSON string."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))




def _truncate_text(text, limit=120):
    """Trim long dashboard text to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."




def _throttle(budget, used):
    """Remaining calls before a dashboard rate budget is exhausted."""
    return max(budget - used, 0)




def _shard_key(record, shards):
    """Stable shard assignment for a dashboard record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards




def _batch_widgets(items, batch_size):
    """Yield widgets in fixed-size batches for dashboard processing."""
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]




def _dedupe_widgets(items):
    """Drop duplicate widget entries while preserving first-seen order."""
    seen = set()
    result = []
    for item in items:
        marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result




def _active_widgets(items):
    """Keep only widget marked active in a dashboard stream."""
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




def _sample_widgets(items, rate):
    """Deterministic sampling of widgets at a given rate."""
    step = max(int(round(1.0 / rate)), 1) if rate > 0 else 1
    return items[::step]




def _aggregate_widgets(items):
    """Fold widgets into a single summary dict for dashboard reporting."""
    total = 0
    for item in items:
        total += int(item.get("amount", 0))
    return {"count": len(items), "total": total, "domain": "dashboard"}




def _handoff_analytics(item, raw_config):

    """Hand one item to the analytics path."""

    return aggregate(item, raw_config)



def _drain_to_reporting(items, raw_config):

    """Bulk hand-off of items to the reporting path."""

    return [render_report(item, raw_config) for item in items]
