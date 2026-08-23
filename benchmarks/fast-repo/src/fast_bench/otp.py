"""One-time token validation."""

from __future__ import annotations

from fast_bench.token import parse


def validate_token(raw_token: str) -> bool:
    """Return whether a raw token is a valid session token."""
    normalized = parse(raw_token)
    return normalized.startswith("sk-")
