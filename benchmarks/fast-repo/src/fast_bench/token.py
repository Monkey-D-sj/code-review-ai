"""Auth-token normalization."""

from __future__ import annotations


def parse(token: str) -> str:
    """Return the stripped token, or an empty string when the token is blank."""
    return token.strip()
