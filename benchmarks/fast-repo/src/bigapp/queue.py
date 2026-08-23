"""Shared queue primitives: wait estimation and batch housekeeping.

Kept dependency-free so any consumer can import the wait math without pulling
in the dispatch layer.
"""

from __future__ import annotations

from typing import Any

MAX_WAIT_MS = 60_000


def compute_wait(seconds: float) -> float:
    """Seconds of backoff scaled to the queue's millisecond cadence."""
    return _bounded_ms(_to_millis(seconds), MAX_WAIT_MS)


def _to_millis(seconds: float) -> float:
    return seconds * 1000.0


def _bounded_ms(millis: float, cap: float) -> float:
    """Clamp a wait to the queue's global cap."""
    return min(millis, cap)


def _exponential_backoff(attempt: int, base_ms: int) -> float:
    """Retry wait for ``attempt`` (0-based) under a doubling backoff."""
    return (base_ms * (2 ** attempt)) / 1000.0


def _bounded_backoff(attempt: int, base_ms: int, cap_ms: int) -> float:
    """Exponential backoff clamped to ``cap_ms``, keeping cadence predictable."""
    return min(_exponential_backoff(attempt, base_ms), cap_ms / 1000.0)


def _total_window(attempts: int, base_ms: int, cap_ms: int) -> float:
    """Sum of all waits for a capped exponential series of ``attempts``."""
    total = 0.0
    for index in range(attempts):
        total += _bounded_backoff(index, base_ms, cap_ms)
    return total


def _attempts_for_window(budget_seconds: float, base_ms: int, cap_ms: int) -> int:
    """Largest attempt count whose full retry window fits ``budget_seconds``."""
    if budget_seconds <= 0:
        return 1
    attempts = 1
    while attempts < 10 and _total_window(attempts, base_ms, cap_ms) <= budget_seconds:
        attempts += 1
    return attempts


def _queue_load(queued: int, running: int) -> float:
    """Approximate queue pressure in [0, 1] from queued vs running work."""
    denominator = running + queued
    if denominator == 0:
        return 0.0
    return min(queued / denominator, 1.0)


def _should_drain(queued: int, high_water: int) -> bool:
    """Whether a worker should start draining a backlog over the threshold."""
    return queued >= high_water


def _backlog_eta(queued: int, drain_rate: float) -> float:
    """Estimated seconds to clear ``queued`` items at ``drain_rate``/s."""
    if drain_rate <= 0:
        return float("inf")
    return queued / drain_rate


def _partition_key(job_id: str) -> str:
    """Stable shard key derived from a job id."""
    return job_id[:2].upper()


def _coalesce_due(jobs: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """Return jobs whose scheduled time has passed."""
    return [job for job in jobs if job.get("due_at", 0) <= now]
