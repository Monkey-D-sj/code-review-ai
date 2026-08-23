"""Shipment domain model."""

from __future__ import annotations


class Shipment:
    def __init__(self, order_id: int, carrier: str, tracking_code: str) -> None:
        self.order_id = order_id
        self.carrier = carrier
        self.tracking_code = tracking_code

    def label(self) -> str:
        return f"{self.carrier}:{self.tracking_code}"
