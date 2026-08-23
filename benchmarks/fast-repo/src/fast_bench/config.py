"""Feature-flag configuration."""

from __future__ import annotations

_FEATURES: set[str] = {"billing", "invoices", "notifications"}


def is_enabled(feature: str) -> bool:
    """Whether a feature flag is enabled for this deployment."""
    return feature in _FEATURES
