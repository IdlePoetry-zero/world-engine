from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from world_engine.domain.components import ExactDecimal


_DECIMAL_ADAPTER = TypeAdapter(ExactDecimal)


def test_exact_decimal_scale_and_integer_boundaries_are_unchanged():
    assert _DECIMAL_ADAPTER.validate_python("0.000001") == Decimal("0.000001")
    assert _DECIMAL_ADAPTER.validate_python("999999999999999999999999999999") == Decimal(
        "999999999999999999999999999999"
    )
    with pytest.raises(ValidationError, match="6 fractional digits"):
        _DECIMAL_ADAPTER.validate_python("0.0000001")
    with pytest.raises(ValidationError, match="30 integer digits"):
        _DECIMAL_ADAPTER.validate_python("9999999999999999999999999999999")


def test_trailing_zero_normalization_keeps_decimal_value_and_signed_zero():
    assert _DECIMAL_ADAPTER.validate_python("1.0000000") == Decimal("1.0000000")
    negative_zero = _DECIMAL_ADAPTER.validate_python("-0.0000000")
    assert negative_zero == Decimal("-0.0000000")
    assert negative_zero.as_tuple().sign == 1
    assert negative_zero.as_tuple().exponent == -7
