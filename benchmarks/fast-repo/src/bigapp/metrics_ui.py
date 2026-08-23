"""Metrics cards for the operations UI.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config
from bigapp.analytics import aggregate
from bigapp.dashboard import render_widget




DEFAULT_METRICS_UI_TIMEOUT = 30





def _runtime_config(payload: dict[str, Any]) -> AppConfig:

    """Load runtime settings for the metrics ui path."""

    return parse_config(payload)





def _resolve_card_timeout(cfg: AppConfig):

    """Effective timeout for metrics ui work, defaulting when unset."""

    return cfg.timeout if cfg.timeout is not None else DEFAULT_METRICS_UI_TIMEOUT





def _describe_card(cfg: AppConfig) -> str:

    """Human description of the runtime config for logs."""

    return f"metrics ui:timeout={_resolve_card_timeout(cfg)}"





def metrics_card(items, raw_config):

    """Process cards under the runtime configuration."""

    cfg = _runtime_config(raw_config)

    timeout = _resolve_card_timeout(cfg)

    normalized = [_normalize_card(item) for item in items]

    return len(normalized)



def _normalize_card(value):
    """Canonicalize a metrics ui card value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)




def _sample_cards(items, rate):
    """Deterministic sampling of cards at a given rate."""
    step = max(int(round(1.0 / rate)), 1) if rate > 0 else 1
    return items[::step]




def _aggregate_cards(items):
    """Fold cards into a single summary dict for metrics ui reporting."""
    total = 0
    for item in items:
        total += int(item.get("amount", 0))
    return {"count": len(items), "total": total, "domain": "metrics ui"}




def _format_card(value, precision=2):
    """Render a metrics ui card value with fixed precision."""
    return f"{float(value):.{precision}f}"




def _valid_card(value):
    """Check a metrics ui card value against the expected shape."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value.get("id"))
    return bool(value)




def _as_timestamp(record):
    """Coerce a metrics ui record to an epoch-style timestamp."""
    raw = record.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return 0.0




def _remap_cards(items, key="id"):
    """Build an index of cards keyed by ``key`` for quick lookup."""
    return {str(item.get(key)): item for item in items if item.get(key) is not None}




def _above_threshold(value, threshold):
    """Whether a metrics ui metric exceeds its alert threshold."""
    if value is None:
        return False
    return float(value) > float(threshold)




def _to_payload(record):
    """Serialize a metrics ui record to a compact JSON string."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))




def _truncate_text(text, limit=120):
    """Trim long metrics ui text to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."




def _throttle(budget, used):
    """Remaining calls before a metrics ui rate budget is exhausted."""
    return max(budget - used, 0)




def _shard_key(record, shards):
    """Stable shard assignment for a metrics ui record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards




def _batch_cards(items, batch_size):
    """Yield cards in fixed-size batches for metrics ui processing."""
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]




def _dedupe_cards(items):
    """Drop duplicate card entries while preserving first-seen order."""
    seen = set()
    result = []
    for item in items:
        marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result




def _handoff_analytics(item, raw_config):

    """Hand one item to the analytics path."""

    return aggregate(item, raw_config)



def _drain_to_dashboard(items, raw_config):

    """Bulk hand-off of items to the dashboard path."""

    return [render_widget(item, raw_config) for item in items]
