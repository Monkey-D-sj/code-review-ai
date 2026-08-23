"""Dispatch plans for background job execution.

Turns a job request into a concrete plan: how many attempts to allow, which
policy to apply, and how long to wait between attempts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from bigapp.config import DEFAULT_MAX_RETRIES, parse_config
from bigapp.queue import compute_wait


@dataclass
class DispatchPlan:
    """Concrete schedule for retrying one failed job."""

    attempts: int
    wait_seconds: float
    policy: str
    created_at: float = field(default_factory=time.time)


def _pick_policy(job_kind: str) -> str:
    """Choose a dispatch policy from the job kind (crash-fast vs best-effort)."""
    if job_kind in {"webhook", "callback"}:
        return "short"
    if job_kind in {"backfill", "reindex"}:
        return "long"
    return "default"


def _attempt_cap_for(job_kind: str) -> int:
    """Cap attempts for a job kind, never above the global maximum."""
    if job_kind in {"backfill", "reindex"}:
        return 6
    return DEFAULT_MAX_RETRIES


def build_plan(job: dict[str, Any]) -> DispatchPlan:
    """Build the concrete dispatch plan for one background job."""
    cfg = parse_config(job["config"])
    job_kind = str(job.get("kind", "default"))
    policy = _pick_policy(job_kind)
    attempts = _attempt_cap_for(job_kind)
    wait_ms = compute_wait(cfg.timeout)
    wait_seconds = wait_ms / 1000.0
    return DispatchPlan(attempts=attempts, wait_seconds=wait_seconds, policy=policy)


def _next_run_window(cron_spec: str) -> int:
    """Minutes until the next run for a daily ``HH:MM`` cron spec."""
    try:
        hour, minute = (int(part) for part in cron_spec.split(":"))
    except (ValueError, AttributeError):
        return 1440
    now = time.localtime()
    current_minutes = now.tm_hour * 60 + now.tm_min
    target_minutes = hour * 60 + minute
    delta = target_minutes - current_minutes
    return delta % (24 * 60)


def _cooldown_seconds(load: float) -> float:
    """Backoff imposed by cluster load: flat until load exceeds 0.8."""
    if load < 0.8:
        return 0.0
    return 15.0 * (load - 0.8) * 10.0


def _rate_limit_after(failures: int, step: float) -> float:
    """Seconds to wait after ``failures`` consecutive failures."""
    return step * failures


def _stale_job_after(max_seconds: int) -> int:
    """Reap jobs idle longer than ``max_seconds``, clamped to a day."""
    return min(max(max_seconds, 60), 86400)


def _pause_decision(error_rate: float) -> tuple[bool, str]:
    """Pause dispatch when the error rate spikes; return a reason string."""
    if error_rate > 0.15:
        return True, f"error_rate {error_rate:.2f} exceeds 0.15"
    return False, ""
