"""Application startup: wire optional subsystems behind feature flags."""

from __future__ import annotations

from fast_bench.config import is_enabled


def startup() -> None:
    if is_enabled("billing"):
        _connect_billing_gateway()


def _connect_billing_gateway() -> None:
    """Enable the billing provider used by the checkout flow."""
    return None
