from __future__ import annotations

import pytest

from world_engine.application.market_price_ceiling import (
    PRICE_CEILING_AMBIGUOUS,
    PRICE_CEILING_FRACTIONAL_MINOR_UNIT,
    PRICE_CEILING_TOTAL_REQUIRED,
    PRICE_CEILING_UNPARSEABLE,
    PRICE_CEILING_WRONG_CURRENCY,
    PriceCeilingEvidenceError,
    bind_trade_price_ceiling,
)


def _bind(text: str, *, currency_id: str = "modern.credit", currency_name: str | None = None):
    return bind_trade_price_ceiling(
        text,
        local_currency_resource_id=currency_id,
        local_currency_name=currency_name,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("总价不超过5个信用点就买两份矿石", 5),
        ("合计≤十二个信用点", 12),
        ("The total cost is at most 5 credits", 5),
        ("价格最高为6 credits", 6),
        ("总价不超过6信用点", 6),
    ],
)
def test_explicit_total_upper_bounds_bind_to_the_local_currency(text, expected):
    ceiling = _bind(text)

    assert ceiling is not None
    assert ceiling.max_total_minor == expected
    assert ceiling.currency_resource_id == "modern.credit"


def test_omitted_denomination_uses_the_single_local_market_currency():
    ceiling = _bind("总价不能超过六就买", currency_id="cultivation.spirit_stone")

    assert ceiling is not None
    assert ceiling.max_total_minor == 6
    assert ceiling.currency_resource_id == "cultivation.spirit_stone"


def test_custom_registered_currency_name_is_accepted_but_unregistered_suffix_is_not():
    ceiling = _bind("总价不超过6枚紫钞才买", currency_id="market.purple_note", currency_name="紫钞")

    assert ceiling is not None
    assert ceiling.currency_resource_id == "market.purple_note"

    with pytest.raises(PriceCeilingEvidenceError) as error:
        _bind("总价不超过6枚紫钞才买")
    assert error.value.code == PRICE_CEILING_WRONG_CURRENCY


@pytest.mark.parametrize(
    ("text", "expected_code"),
    [
        ("总价不超过6美元", PRICE_CEILING_WRONG_CURRENCY),
        ("总价不超过6星票才买", PRICE_CEILING_WRONG_CURRENCY),
        ("总价不超过6个灵石", PRICE_CEILING_WRONG_CURRENCY),
        ("总价不超过6 crédito", PRICE_CEILING_WRONG_CURRENCY),
        ("总价不超过5至6个信用点", PRICE_CEILING_AMBIGUOUS),
        ("总价不超过5.5个信用点", PRICE_CEILING_FRACTIONAL_MINOR_UNIT),
        ("总价不超过五点五个信用点", PRICE_CEILING_FRACTIONAL_MINOR_UNIT),
        ("单价不超过2信用点", PRICE_CEILING_TOTAL_REQUIRED),
        ("单价不超过2信用点，总价最多6信用点", PRICE_CEILING_TOTAL_REQUIRED),
        ("总价超过五个信用点我就买", PRICE_CEILING_UNPARSEABLE),
    ],
)
def test_unsafe_or_non_total_price_conditions_fail_closed(text, expected_code):
    with pytest.raises(PriceCeilingEvidenceError) as error:
        _bind(text)

    assert error.value.code == expected_code


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("总价超过五个信用点就先别买", 5),
        ("总价低于六个信用点才买", 5),
        ("不要超过10 credits", 10),
    ],
)
def test_clear_purchase_refusal_or_strict_limit_yields_integer_ceiling(text, expected):
    ceiling = _bind(text)

    assert ceiling is not None
    assert ceiling.max_total_minor == expected
    assert ceiling.currency_resource_id == "modern.credit"


@pytest.mark.parametrize(
    "text",
    [
        "我有6个信用点，买两份矿石",
        "最多买3份矿石",
        "请帮我买矿石",
        "",
    ],
)
def test_non_ceiling_language_returns_none(text):
    assert _bind(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "总价不超过5信用点，总价不超过6信用点",
        "总价不超过5信用点或6信用点",
    ],
)
def test_repeated_or_competing_price_limits_are_ambiguous(text):
    with pytest.raises(PriceCeilingEvidenceError) as error:
        _bind(text)
    assert error.value.code == PRICE_CEILING_AMBIGUOUS

