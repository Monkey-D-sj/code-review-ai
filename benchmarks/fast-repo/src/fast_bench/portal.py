"""Public portal pages."""

from __future__ import annotations

from fast_bench.cache import load


def render_page(key: str) -> str:
    """Render a portal page from cached content."""
    content = load(key)
    return f"<html><body>{content}</body></html>"
