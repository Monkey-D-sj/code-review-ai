"""Order-confirmation service."""

from __future__ import annotations

from fast_bench.notify import send


def send_order_confirmation(email: str) -> None:
    body = f"Thanks for your order, {email}"
    send(email, body)
