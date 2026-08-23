"""Configuration loading."""

from __future__ import annotations

from fast_bench.settings import parse


def load_timeout(raw: str) -> int:
    """Read the request timeout from a raw settings string."""
    return parse(raw)
