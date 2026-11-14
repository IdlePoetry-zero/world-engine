"""Reported-versus-estimated usage costs; no provider charge is guessed."""

from __future__ import annotations

from decimal import Decimal

from .contracts import CostRecord, CostState, PriceVersion, ProviderUsage, UsageSource


_MILLION = Decimal("1000000")


def estimate_cost(usage: ProviderUsage, price: PriceVersion | None) -> CostRecord:
    """Calculate only when reported token categories and applicable rates exist.

    An unknown cache split or an unknown required rate leaves the amount unknown.
    The returned record is always marked `estimated`; provider-reported spend is
    a separate concern and is never synthesized from token counters.
    """
    if price is None or usage.source is not UsageSource.REPORTED:
        return CostRecord()
    if usage.input_tokens is None or usage.output_tokens is None or price.input_per_million is None or price.output_per_million is None:
        return CostRecord()

    if usage.cached_input_tokens is None:
        # The adapter cannot know whether the provider applied a cache price.
        return CostRecord()
    cached = usage.cached_input_tokens
    non_cached = usage.non_cached_input_tokens
    if non_cached is None:
        if usage.input_tokens < cached:
            return CostRecord()
        non_cached = usage.input_tokens - cached

    if cached and price.cached_input_per_million is None:
        return CostRecord()
    cached_rate = price.cached_input_per_million or Decimal("0")
    non_cached_cost = Decimal(non_cached) * price.input_per_million / _MILLION
    cached_cost = Decimal(cached) * cached_rate / _MILLION
    output_cost = Decimal(usage.output_tokens) * price.output_per_million / _MILLION
    components = {
        "non_cached_input": non_cached_cost,
        "cached_input": cached_cost,
        "output": output_cost,
    }
    return CostRecord(
        state=CostState.ESTIMATED,
        amount=non_cached_cost + cached_cost + output_cost,
        currency=price.currency,
        source_url=price.source_url,
        price_effective_from=price.effective_from,
        components=components,
    )

