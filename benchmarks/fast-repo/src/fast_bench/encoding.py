"""Payload encoding helpers."""

from __future__ import annotations


def decode(blob: bytes) -> str:
    """Decode a raw blob to its text form."""
    return blob.decode("utf-8")
