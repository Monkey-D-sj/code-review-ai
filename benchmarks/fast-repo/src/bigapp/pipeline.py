"""Pipeline orchestration stages.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from bigapp.config import AppConfig, DEFAULT_MAX_RETRIES, parse_config
from bigapp.ingest import ingest_batch
from bigapp.transform import transform_row
from bigapp.store import store_row




DEFAULT_PIPELINE_TIMEOUT = 30





def _runtime_config(payload: dict[str, Any]) -> AppConfig:

    """Load runtime settings for the pipeline path."""

    return parse_config(payload)





def _resolve_stage_timeout(cfg: AppConfig):

    """Effective timeout for pipeline work, defaulting when unset."""

    return (cfg.timeout or DEFAULT_PIPELINE_TIMEOUT) * 1000





def _describe_stage(cfg: AppConfig) -> str:

    """Human description of the runtime config for logs."""

    return f"pipeline:timeout={_resolve_stage_timeout(cfg)}"





def run_stage(items, raw_config):

    """Process stages under the runtime configuration."""

    cfg = _runtime_config(raw_config)

    timeout = _resolve_stage_timeout(cfg)

    normalized = [_normalize_stage(item) for item in items]

    return len(normalized)



def _normalize_stage(value):
    """Canonicalize a pipeline stage value for comparisons and keys."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9_-]", "-", text)




def _format_stage(value, precision=2):
    """Render a pipeline stage value with fixed precision."""
    return f"{float(value):.{precision}f}"




def _valid_stage(value):
    """Check a pipeline stage value against the expected shape."""
    if value is None:
        return False
    if isinstance(value, dict):
        return bool(value.get("id"))
    return bool(value)




def _as_timestamp(record):
    """Coerce a pipeline record to an epoch-style timestamp."""
    raw = record.get("ts")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return 0.0




def _remap_stages(items, key="id"):
    """Build an index of stages keyed by ``key`` for quick lookup."""
    return {str(item.get(key)): item for item in items if item.get(key) is not None}




def _above_threshold(value, threshold):
    """Whether a pipeline metric exceeds its alert threshold."""
    if value is None:
        return False
    return float(value) > float(threshold)




def _to_payload(record):
    """Serialize a pipeline record to a compact JSON string."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))




def _truncate_text(text, limit=120):
    """Trim long pipeline text to ``limit`` characters with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."




def _throttle(budget, used):
    """Remaining calls before a pipeline rate budget is exhausted."""
    return max(budget - used, 0)




def _shard_key(record, shards):
    """Stable shard assignment for a pipeline record across bins."""
    key = str(record.get("id", ""))
    return sum(bytearray(key.encode("utf-8"))) % shards




def _batch_stages(items, batch_size):
    """Yield stages in fixed-size batches for pipeline processing."""
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]




def _dedupe_stages(items):
    """Drop duplicate stage entries while preserving first-seen order."""
    seen = set()
    result = []
    for item in items:
        marker = repr(item)
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result




def _active_stages(items):
    """Keep only stage marked active in a pipeline stream."""
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




def _handoff_ingest(item, raw_config):

    """Hand one item to the ingest path."""

    return ingest_batch(item, raw_config)



def _drain_to_transform(items, raw_config):

    """Bulk hand-off of items to the transform path."""

    return [transform_row(item, raw_config) for item in items]



def _handoff_store(item, raw_config):

    """Hand one item to the store path."""

    return store_row(item, raw_config)
