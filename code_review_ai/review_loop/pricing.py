"""Turn accumulated model-side usage into a yuan cost estimate.

The prices are DeepSeek's per-million-token list prices (yuan), stated by the
project for review-cost accounting:

    cache-hit input    0.05 元 / 1M tokens
    cache-miss input   1.5  元 / 1M tokens
    output             4.5  元 / 1M tokens

Cost is summed per token class, never ``input_tokens x one price``: a cache hit
is billed ~30x cheaper than a miss, and in multi-turn reviews cache hits can
dominate the input side, so pricing raw ``input_tokens`` would massively
overstate cost. Cache-miss input is derived as ``input_tokens - cache_read``.

Pure and provider-agnostic: it only reads the keys ``_accumulate_usage`` writes
into ``Usage`` (``input_tokens`` / ``output_tokens`` / ``cache_read``) and never
touches the loop or a model. All prices are per million tokens in yuan and are
overridable so a report can re-price without touching usage.
"""

from __future__ import annotations

CACHE_HIT_INPUT_PER_MILLION = 0.05  # 元/百万 tokens：命中缓存的前缀输入
CACHE_MISS_INPUT_PER_MILLION = 1.5  # 元/百万 tokens：未命中缓存的输入
OUTPUT_PER_MILLION = 4.5            # 元/百万 tokens：模型输出


def compute_cost(
    usage: dict[str, int],
    *,
    cache_hit_per_million: float = CACHE_HIT_INPUT_PER_MILLION,
    cache_miss_per_million: float = CACHE_MISS_INPUT_PER_MILLION,
    output_per_million: float = OUTPUT_PER_MILLION,
) -> float:
    """Yuan cost of one run's ``usage`` (see module docstring for the tiers).

    ``usage`` is the accumulated ``LoopResult.usage``: cache-miss input is
    ``input_tokens - cache_read`` (clamped at 0), a provider that reported no
    ``cache_read`` is priced entirely as cache miss, and a run with no reported
    usage costs 0.
    """
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read", 0)
    cache_miss = max(input_tokens - cache_read, 0)

    def yuan(tokens: int, per_million: float) -> float:
        return tokens / 1_000_000.0 * per_million

    return (yuan(cache_miss, cache_miss_per_million)
            + yuan(cache_read, cache_hit_per_million)
            + yuan(output_tokens, output_per_million))
