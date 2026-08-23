"""HTTP-ish order creation entry point."""

from __future__ import annotations

from fast_bench.inventory import reserve


def create_order(item_id: int, qty: int) -> dict:
    if not reserve(item_id, qty):
        raise ValueError(f"cannot reserve {qty} of item {item_id}")
    return {"status": "created", "item_id": item_id, "qty": qty}
