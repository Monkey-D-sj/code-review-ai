"""Order finalization: compute the total and collect payment."""

from __future__ import annotations

from fast_bench.pricing import compute_total


def finalize_order(cart: dict, shipping_cents: int) -> dict:
    total = compute_total(cart["subtotal_cents"], cart["tax_cents"],
                          shipping_cents)
    return {"total_cents": total.total_cents}
