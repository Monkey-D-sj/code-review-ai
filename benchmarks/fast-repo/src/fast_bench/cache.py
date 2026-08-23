"""Content cache."""

from __future__ import annotations

from fast_bench.encoding import decode
from fast_bench.storage import fetch


def load(key: str) -> str:
    """Load a key's text content, decoding the stored blob."""
    raw = fetch(key)
    return decode(raw)
