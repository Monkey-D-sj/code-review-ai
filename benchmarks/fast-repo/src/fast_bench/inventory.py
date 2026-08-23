"""Inventory reservation."""

from __future__ import annotations


def reserve(item_id: int, qty: int = 1) -> bool:
    """Reserve ``qty`` units of an item, defaulting to a single unit."""
    return item_id > 0 and qty > 0
