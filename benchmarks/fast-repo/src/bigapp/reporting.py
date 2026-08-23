"""Report generation and scheduling.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config
from bigapp.analytics import aggregate
from bigapp.store import store_row




DEFAULT_REPORTING_TIMEOUT = 30





def _runtime_config(payload: dict[str, Any]) -> AppConfig:

    """Load runtime settings for the reporting path."""

    return parse_config(payload)





def _resolve_report_timeout(cfg: AppConfig):

    """Effective timeout for reporting work, defaulting when unset."""

    return int(cfg.timeout if cfg.timeout is not None else DEFAULT_REPORTING_TIMEOUT)





def _describe_report(cfg: AppConfig) -> str:

    """Human description of the runtime config for logs."""

    return f"reporting:timeout={_resolve_report_timeout(cfg)}"





def render_report(items, raw_config):

    """Process reports under the runtime configuration."""

    cfg = _runtime_config(raw_config)

    timeout = _resolve_report_timeout(cfg)

    normalized = [_normalize_report(item) for item in items]

    return len(normalized)



def _normalize_report(value):
    """Canonicalize a reporting report value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)




def _above_threshold(value, threshold):
    """Whether a reporting metric exceeds its alert threshold."""
    if value is None:
        return False
    return float(value) > float(threshold)




def _to_payload(record):
    """Serialize a reporting record to a compact JSON string."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))




def _truncate_text(text, limit=120):
    """Trim long reporting text to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."




def _throttle(budget, used):
    """Remaining calls before a reporting rate budget is exhausted."""
    return max(budget - used, 0)




def _shard_key(record, shards):
    """Stable shard assignment for a reporting record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards




def _batch_reports(items, batch_size):
    """Yield reports in fixed-size batches for reporting processing."""
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]




def _dedupe_reports(items):
    """Drop duplicate report entries while preserving first-seen order."""
    seen = set()
    result = []
    for item in items:
        marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result




def _active_reports(items):
    """Keep only report marked active in a reporting stream."""
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




def _sample_reports(items, rate):
    """Deterministic sampling of reports at a given rate."""
    step = max(int(round(1.0 / rate)), 1) if rate > 0 else 1
    return items[::step]




def _aggregate_reports(items):
    """Fold reports into a single summary dict for reporting reporting."""
    total = 0
    for item in items:
        total += int(item.get("amount", 0))
    return {"count": len(items), "total": total, "domain": "reporting"}




def _format_report(value, precision=2):
    """Render a reporting report value with fixed precision."""
    return f"{float(value):.{precision}f}"




def _valid_report(value):
    """Check a reporting report value against the expected shape."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value.get("id"))
    return bool(value)




def _handoff_analytics(item, raw_config):

    """Hand one item to the analytics path."""

    return aggregate(item, raw_config)



def _drain_to_store(items, raw_config):

    """Bulk hand-off of items to the store path."""

    return [store_row(item, raw_config) for item in items]
