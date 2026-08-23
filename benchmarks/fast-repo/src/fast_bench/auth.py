"""Authentication against the user store."""

from __future__ import annotations


class AuthError(Exception):
    """Raised when credentials are invalid."""


_KNOWN: dict[str, str] = {"alice": "s3cret"}


def authenticate(user: str, password: str) -> dict:
    if _KNOWN.get(user) != password:
        raise AuthError(f"bad credentials for {user}")
    return {"user": user}
