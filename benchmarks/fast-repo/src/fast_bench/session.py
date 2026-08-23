"""User session management."""

from __future__ import annotations

from fast_bench.auth import authenticate


def open_session(user: str, password: str) -> dict:
    identity = authenticate(user, password)
    return {"session": True, "user": identity["user"]}
