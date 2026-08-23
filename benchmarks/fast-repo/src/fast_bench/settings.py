"""Runtime settings parsing."""

from __future__ import annotations


def parse(text: str) -> int:
    """Parse a settings value as an integer."""
    return int(text.strip())
