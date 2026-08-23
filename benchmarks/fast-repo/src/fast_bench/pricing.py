"""Price calculation for a cart."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OrderTotal:
    subtotal_cents: int
    tax_cents: int
    shipping_cents: int

    @property
    def total_cents(self) -> int:
        return self.subtotal_cents + self.tax_cents + self.shipping_cents


def compute_total(subtotal_cents: int, tax_cents: int,
                  shipping_cents: int) -> OrderTotal:
    """Compute the order total as a structured result."""
    return OrderTotal(subtotal_cents, tax_cents, shipping_cents)
