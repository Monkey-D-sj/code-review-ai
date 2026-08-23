"""Alert evaluation and routing.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config
from bigapp.analytics import aggregate
from bigapp.metrics_ui import metrics_card
from bigapp.queue import compute_wait




DEFAULT_ALERTS_TIMEOUT = 30





def _runtime_config(payload: dict[str, Any]) -> AppConfig:

    """Load runtime settings for the alerts path."""

    return parse_config(payload)





def _resolve_alert_timeout(cfg: AppConfig):

    """Effective timeout for alerts work, defaulting when unset."""

    return (cfg.timeout or DEFAULT_ALERTS_TIMEOUT) * 1000





def _describe_alert(cfg: AppConfig) -> str:

    """Human description of the runtime config for logs."""

    return f"alerts:timeout={_resolve_alert_timeout(cfg)}"





def evaluate_alert(items, raw_config):

    """Process alerts under the runtime configuration."""

    cfg = _runtime_config(raw_config)

    timeout = _resolve_alert_timeout(cfg)

    normalized = [_normalize_alert(item) for item in items]

    return len(normalized)



def _normalize_alert(value):
    """Canonicalize a alerts alert value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)




def _to_payload(record):
    """Serialize a alerts record to a compact JSON string."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))




def _truncate_text(text, limit=120):
    """Trim long alerts text to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."




def _throttle(budget, used):
    """Remaining calls before a alerts rate budget is exhausted."""
    return max(budget - used, 0)




def _shard_key(record, shards):
    """Stable shard assignment for a alerts record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards




def _batch_alerts(items, batch_size):
    """Yield alerts in fixed-size batches for alerts processing."""
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]




def _dedupe_alerts(items):
    """Drop duplicate alert entries while preserving first-seen order."""
    seen = set()
    result = []
    for item in items:
        marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result




def _active_alerts(items):
    """Keep only alert marked active in a alerts stream."""
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




def _sample_alerts(items, rate):
    """Deterministic sampling of alerts at a given rate."""
    step = max(int(round(1.0 / rate)), 1) if rate > 0 else 1
    return items[::step]




def _aggregate_alerts(items):
    """Fold alerts into a single summary dict for alerts reporting."""
    total = 0
    for item in items:
        total += int(item.get("amount", 0))
    return {"count": len(items), "total": total, "domain": "alerts"}




def _format_alert(value, precision=2):
    """Render a alerts alert value with fixed precision."""
    return f"{float(value):.{precision}f}"




def _valid_alert(value):
    """Check a alerts alert value against the expected shape."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value.get("id"))
    return bool(value)




def _as_timestamp(record):
    """Coerce a alerts record to an epoch-style timestamp."""
    raw = record.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return 0.0




def _handoff_analytics(item, raw_config):

    """Hand one item to the analytics path."""

    return aggregate(item, raw_config)



def _drain_to_metrics_ui(items, raw_config):

    """Bulk hand-off of items to the metrics_ui path."""

    return [metrics_card(item, raw_config) for item in items]



def _wait_before_alert(cfg: AppConfig) -> float:
    """Seconds of quiet before alert evaluation starts."""
    return compute_wait(cfg.timeout or DEFAULT_ALERTS_TIMEOUT) / 1000.0
