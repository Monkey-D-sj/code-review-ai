"""Blob storage access."""

from __future__ import annotations


def fetch(key: str) -> bytes:
    """Read a blob's raw bytes from storage."""
    return _blob_for(key)


def _blob_for(key: str) -> bytes:
    return f"blob:{key}".encode("utf-8")
