"""Order dispatch and shipment creation."""

from __future__ import annotations

from fast_bench.shipping import Shipment


def ship(order_id: int, carrier: str, tracking_code: str) -> str:
    shipment = Shipment(order_id, carrier, tracking_code)
    return shipment.label()
