"""Pricing tests: accumulated usage -> yuan cost at the DeepSeek per-million rates.

Prices (元 / 1M tokens): cache-hit input 0.05, cache-miss input 1.5, output 4.5.
These are pure arithmetic checks over ``compute_cost``; no DB, no model.
"""

from __future__ import annotations

import pytest

from code_review_ai.review_loop import compute_cost


def test_pure_miss_input_and_output_price_at_full_rates():
    # 1M miss input + 1M output: 1.5 + 4.5 = 6.0 元
    cost = compute_cost({"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert cost == pytest.approx(6.0)


def test_cache_hit_input_prices_at_the_cheap_rate():
    # everything reported is a cache hit: 1M x 0.05 = 0.05 元
    cost = compute_cost({"input_tokens": 1_000_000, "cache_read": 1_000_000,
                         "output_tokens": 0})
    assert cost == pytest.approx(0.05)


def test_mixed_run_splits_input_between_hit_and_miss():
    # 0.5M miss (0.75) + 1.5M hit (0.075) + 0.5M output (2.25) = 3.075 元
    cost = compute_cost({"input_tokens": 2_000_000, "cache_read": 1_500_000,
                         "output_tokens": 500_000})
    assert cost == pytest.approx(3.075)


def test_missing_cache_read_is_priced_entirely_as_miss():
    # cache_read absent -> 0, so all 1M input is a miss
    cost = compute_cost({"input_tokens": 1_000_000, "output_tokens": 0})
    assert cost == pytest.approx(1.5)


def test_cache_read_never_prices_more_input_than_reported():
    # cache_read > input_tokens (odd provider data): miss clamps to 0
    cost = compute_cost({"input_tokens": 100_000, "cache_read": 200_000,
                         "output_tokens": 0})
    assert cost == pytest.approx(200_000 / 1_000_000.0 * 0.05)


def test_empty_or_absent_usage_costs_zero():
    assert compute_cost({}) == 0.0
    assert compute_cost({"input_tokens": 0, "output_tokens": 0}) == 0.0


def test_reported_prices_override_the_deepseek_defaults():
    usage = {"input_tokens": 1_000_000, "cache_read": 1_000_000,
             "output_tokens": 1_000_000}
    cost = compute_cost(usage, cache_hit_per_million=1.0,
                        cache_miss_per_million=2.0, output_per_million=3.0)
    assert cost == pytest.approx(1.0 + 3.0)
