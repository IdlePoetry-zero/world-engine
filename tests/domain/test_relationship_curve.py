from decimal import Decimal

import pytest

from world_engine.domain.relationship_policy import (
    INITIAL_RELATIONSHIP_SCORE,
    settle_relationship_change,
)


def test_new_relationship_starts_at_zero_and_small_changes_apply_in_full() -> None:
    assert INITIAL_RELATIONSHIP_SCORE == 0
    result = settle_relationship_change(INITIAL_RELATIONSHIP_SCORE, 3)

    assert result.current_score == Decimal("0")
    assert result.proposed_delta == Decimal("3")
    assert result.adjusted_delta == Decimal("3.000000")
    assert result.applied_delta == Decimal("3.000000")
    assert result.resulting_score == Decimal("3.000000")
    assert result.outcome == "applied"


@pytest.mark.parametrize(
    ("current", "delta", "expected"),
    [
        (Decimal("59"), Decimal("2"), Decimal("61.000000")),
        (Decimal("-59"), Decimal("-2"), Decimal("-61.000000")),
        (Decimal("20"), Decimal("-4"), Decimal("16.000000")),
    ],
)
def test_changes_below_soft_band_apply_without_cross_dimension_state(
    current: Decimal, delta: Decimal, expected: Decimal
) -> None:
    assert settle_relationship_change(current, delta).resulting_score == expected


def test_soft_band_uses_quarter_point_decimal_effect() -> None:
    result = settle_relationship_change(60, 4)

    assert result.adjusted_delta == Decimal("1.000000")
    assert result.applied_delta == Decimal("1.000000")
    assert result.resulting_score == Decimal("61.000000")
    assert result.outcome == "attenuated"


def test_key_evidence_bypasses_soft_band_dampening() -> None:
    result = settle_relationship_change(60, 4, verified_key_evidence=True)

    assert result.adjusted_delta == Decimal("4.000000")
    assert result.resulting_score == Decimal("64.000000")


@pytest.mark.parametrize(("current", "delta", "cap"), [
    (Decimal("79"), Decimal("10"), Decimal("79.999999")),
    (Decimal("-79"), Decimal("-10"), Decimal("-79.999999")),
    (Decimal("20"), Decimal("-100"), Decimal("-79.999999")),
])
def test_unverified_proposals_cannot_enter_either_high_magnitude_band(
    current: Decimal, delta: Decimal, cap: Decimal
) -> None:
    result = settle_relationship_change(current, delta)

    assert result.resulting_score == cap
    assert result.outcome == "threshold_limited"
    assert result.resulting_score.copy_abs() < Decimal("80")


def test_verified_key_evidence_can_cross_the_high_threshold() -> None:
    result = settle_relationship_change(79, 10, verified_key_evidence=True)

    # Verified key effects are not softened by the ordinary-interaction curve.
    assert result.adjusted_delta == Decimal("10.000000")
    assert result.resulting_score == Decimal("89.000000")
    assert result.outcome == "applied"


@pytest.mark.parametrize("current", [Decimal("80"), Decimal("100"), Decimal("-80"), Decimal("-100")])
@pytest.mark.parametrize("delta", [Decimal("-5"), Decimal("5")])
def test_existing_high_scores_are_preserved_without_verified_key_evidence(
    current: Decimal, delta: Decimal
) -> None:
    result = settle_relationship_change(current, delta)

    assert result.resulting_score == current
    assert result.applied_delta == 0
    assert result.outcome == "key_evidence_required"


@pytest.mark.parametrize(("current", "delta", "expected"), [
    (Decimal("90"), Decimal("-5"), Decimal("85.000000")),
    (Decimal("-90"), Decimal("5"), Decimal("-85.000000")),
    (Decimal("80"), Decimal("-10"), Decimal("70.000000")),
])
def test_verified_key_evidence_can_move_an_existing_high_score_either_way(
    current: Decimal, delta: Decimal, expected: Decimal
) -> None:
    result = settle_relationship_change(current, delta, verified_key_evidence=True)

    assert result.resulting_score == expected
    assert result.applied_delta == delta


def test_routine_equivalent_occurrences_decay_geometrically_to_zero() -> None:
    first = settle_relationship_change(0, 4, routine_occurrence=1)
    second = settle_relationship_change(0, 4, routine_occurrence=2)
    third = settle_relationship_change(0, 4, routine_occurrence=3)
    hundredth = settle_relationship_change(0, 4, routine_occurrence=100)

    assert [first.adjusted_delta, second.adjusted_delta, third.adjusted_delta] == [
        Decimal("4.000000"), Decimal("2.000000"), Decimal("1.000000")
    ]
    assert hundredth.adjusted_delta == 0
    assert hundredth.applied_delta == 0
    assert hundredth.resulting_score == 0
    assert hundredth.outcome == "rounded_to_zero"


def test_routine_decay_and_soft_band_dampening_compose_exactly() -> None:
    first = settle_relationship_change(60, 4, routine_occurrence=1)
    second = settle_relationship_change(60, 4, routine_occurrence=2)

    assert first.adjusted_delta == Decimal("1.000000")
    assert second.adjusted_delta == Decimal("0.500000")


def test_fractional_precision_matches_relation_value_scale_and_rounds_half_even() -> None:
    result = settle_relationship_change(60, Decimal("0.000006"))

    # 0.000006 * 0.25 = 0.0000015, rounded half-even to two micro-points.
    assert result.adjusted_delta == Decimal("0.000002")
    assert result.resulting_score == Decimal("60.000002")


@pytest.mark.parametrize(("current", "delta", "expected"), [
    (Decimal("99"), Decimal("10"), Decimal("100.000000")),
    (Decimal("-99"), Decimal("-10"), Decimal("-100.000000")),
])
def test_verified_effects_stay_inside_supported_score_range(
    current: Decimal, delta: Decimal, expected: Decimal
) -> None:
    result = settle_relationship_change(current, delta, verified_key_evidence=True)

    assert result.resulting_score == expected
    assert result.resulting_score.copy_abs() <= Decimal("100")
    assert result.outcome == "score_limited"


def test_independent_calls_keep_dimensions_and_directions_separate() -> None:
    affection = settle_relationship_change(Decimal("12"), Decimal("3"))
    reverse_affection = settle_relationship_change(Decimal("-7"), Decimal("3"))
    trust = settle_relationship_change(Decimal("65"), Decimal("3"))

    assert affection.resulting_score == Decimal("15.000000")
    assert reverse_affection.resulting_score == Decimal("-4.000000")
    assert trust.resulting_score == Decimal("65.750000")


@pytest.mark.parametrize(
    ("current", "delta", "kwargs"),
    [
        (True, 1, {}),
        (0.5, 1, {}),
        (0, 1.5, {}),
        (0, Decimal("0.0000001"), {}),
        (Decimal("100.000001"), 1, {}),
        (0, Decimal("100.000001"), {}),
        (0, 1, {"verified_key_evidence": 1}),
        (0, 1, {"routine_occurrence": True}),
        (0, 1, {"routine_occurrence": 0}),
    ],
)
def test_invalid_exact_values_and_policy_flags_are_rejected(
    current: object, delta: object, kwargs: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        settle_relationship_change(current, delta, **kwargs)


def test_zero_proposal_is_a_pure_noop_even_at_the_high_threshold() -> None:
    result = settle_relationship_change(80, 0)

    assert result.resulting_score == Decimal("80")
    assert result.outcome == "no_change"
