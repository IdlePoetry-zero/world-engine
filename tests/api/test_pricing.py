from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from world_engine.adapters.models.contracts import (
    CostState,
    ProviderConfig,
    ProviderKind,
    ProviderUsage,
    UsageSource,
)
from world_engine.adapters.models.costs import estimate_cost
from world_engine.application.pricing import REVIEW_AFTER, SOURCE, VERIFIED_FROM, bundled_price


def _official_config() -> ProviderConfig:
    return ProviderConfig("official", ProviderKind.DEEPSEEK, "https://api.deepseek.com")


@pytest.mark.parametrize(("start", "peak"), [
    (datetime(2026, 11, 16, 0, 59, tzinfo=timezone.utc), False),
    (datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc), True),
    (datetime(2026, 11, 16, 3, 59, tzinfo=timezone.utc), True),
    (datetime(2026, 11, 16, 4, 0, tzinfo=timezone.utc), False),
    (datetime(2026, 11, 16, 5, 59, tzinfo=timezone.utc), False),
    (datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc), True),
    (datetime(2026, 11, 16, 9, 59, tzinfo=timezone.utc), True),
    (datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc), False),
    (datetime(2026, 11, 14, 1, 0, tzinfo=timezone.utc), False),  # Saturday
    (datetime(2026, 11, 15, 7, 0, tzinfo=timezone.utc), False),  # Sunday
])
def test_deepseek_flash_bundled_price_uses_utc_peak_slots(start, peak):
    price = bundled_price(_official_config(), "deepseek-flash", start)
    assert price is not None
    assert price.effective_from == VERIFIED_FROM
    assert price.source_url == SOURCE
    assert price.cached_input_per_million == Decimal("0.006" if peak else "0.003")
    assert price.input_per_million == Decimal("0.3" if peak else "0.15")
    assert price.output_per_million == Decimal("1.2" if peak else "0.6")


def test_peak_slot_is_selected_from_the_absolute_time_of_an_offset_timestamp():
    # 09:00 at UTC+08 is 01:00 UTC, the opening peak boundary.
    local_start = datetime(2026, 11, 16, 9, 0, tzinfo=timezone(timedelta(hours=8)))
    price = bundled_price(_official_config(), "deepseek-flash", local_start)
    assert price is not None
    assert price.input_per_million == Decimal("0.3")
    assert price.output_per_million == Decimal("1.2")


def test_peak_rates_are_used_by_estimated_cost_accounting():
    price = bundled_price(_official_config(), "deepseek-flash", datetime(2026, 11, 16, 1, tzinfo=timezone.utc))
    assert price is not None
    usage = ProviderUsage(
        source=UsageSource.REPORTED,
        input_tokens=1_000_000,
        cached_input_tokens=100_000,
        non_cached_input_tokens=900_000,
        output_tokens=1_000_000,
        total_tokens=2_000_000,
    )
    cost = estimate_cost(usage, price)
    assert cost.state is CostState.ESTIMATED
    assert cost.amount == Decimal("1.4706")
    assert cost.components == {
        "non_cached_input": Decimal("0.27"),
        "cached_input": Decimal("0.0006"),
        "output": Decimal("1.2"),
    }


@pytest.mark.parametrize("attempt_start", [
    datetime(2026, 11, 13, 23, 59, 59, tzinfo=timezone.utc),
    REVIEW_AFTER,
    datetime(2026, 11, 14, 1, 0),  # naive local times are not safe for price lookup
])
def test_out_of_window_or_timezone_ambiguous_attempts_remain_unknown(attempt_start):
    assert bundled_price(_official_config(), "deepseek-flash", attempt_start) is None


def test_validity_window_includes_its_start_and_excludes_its_end():
    before_start = VERIFIED_FROM - timedelta(microseconds=1)
    final_valid_instant = REVIEW_AFTER - timedelta(microseconds=1)
    assert bundled_price(_official_config(), "deepseek-flash", before_start) is None
    assert bundled_price(_official_config(), "deepseek-flash", VERIFIED_FROM) is not None
    assert bundled_price(_official_config(), "deepseek-flash", final_valid_instant) is not None
    assert bundled_price(_official_config(), "deepseek-flash", REVIEW_AFTER) is None


def test_nonofficial_endpoint_and_model_remain_unknown():
    proxy = ProviderConfig("proxy", ProviderKind.DEEPSEEK, "https://relay.example")
    official = _official_config()
    start = datetime(2026, 11, 16, 1, tzinfo=timezone.utc)
    assert bundled_price(proxy, "deepseek-flash", start) is None
    assert bundled_price(official, "deepseek-reasoner", start) is None
    assert bundled_price(official, "deepseek-flash", datetime(2027, 1, 1, tzinfo=timezone.utc)) is None
