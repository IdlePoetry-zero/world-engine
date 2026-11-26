"""Pure, bounded curve for one directed relationship dimension.

The caller supplies a proposed signed change after deciding what the actual
event means. This policy applies the score bands, routine repetition decay,
the verified-key-evidence gate, and the existing ``SignedRelationValue``
range. It does not inspect prose or decide whether evidence is trustworthy.

Routine ordinals are one-based: the first equivalent routine event is 1, the
second is 2, and so on. The caller must derive the ordinal from a stable,
canonical event/evidence class and must handle event idempotency separately.
No text keyword or model-generated label is used to group events.

Each invocation changes exactly one directed dimension. Callers that settle
affection and trust, or both directions between two people, call the policy
separately with each dimension's own current value and evidence history.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from .components import SignedRelationValue


_SCORE_ADAPTER = TypeAdapter(SignedRelationValue)
_SCORE_LIMIT = Decimal("100")
_SOFT_THRESHOLD = Decimal("60")
_KEY_THRESHOLD = Decimal("80")
_SOFT_MULTIPLIER = Decimal("0.25")
_ROUTINE_DECAY_FACTOR = Decimal("0.5")
_SCORE_QUANTUM = Decimal("0.000001")
_SOFT_CAP = _KEY_THRESHOLD - _SCORE_QUANTUM
_MAX_ROUTINE_OCCURRENCE = 2**63 - 1

INITIAL_RELATIONSHIP_SCORE = Decimal("0")


RelationshipOutcome = Literal[
    "applied",
    "attenuated",
    "no_change",
    "rounded_to_zero",
    "key_evidence_required",
    "threshold_limited",
    "score_limited",
]


@dataclass(frozen=True, slots=True)
class RelationshipCurveResult:
    """The exact before/after values and decision from one curve application."""

    current_score: Decimal
    proposed_delta: Decimal
    adjusted_delta: Decimal
    applied_delta: Decimal
    resulting_score: Decimal
    outcome: RelationshipOutcome


def settle_relationship_change(
    current_score: Decimal | int | str,
    proposed_delta: Decimal | int | str,
    *,
    verified_key_evidence: bool = False,
    routine_occurrence: int | None = None,
) -> RelationshipCurveResult:
    """Apply the default relationship curve to one directed score.

    Scores and deltas use the engine's exact ``SignedRelationValue`` contract:
    finite decimal strings, integers, or ``Decimal`` values; no binary floats;
    at most six fractional digits; and values between -100 and 100. Integer
    inputs remain exact. Fractional effects are rounded half-even to six places,
    matching the existing relation-value precision.

    Ordinary changes at magnitudes 60 through just below 80 receive one quarter
    of their proposed effect. A verified key event uses its full proposal.
    Entering magnitude 80 from below without verified key evidence is capped at
    79.999999. Once a score is already at magnitude 80 or above, no movement in
    either direction is accepted without verified key evidence. Existing
    values in the supported [-100, 100] range are retained as supplied.

    Pass ``routine_occurrence`` only for an action already classified as a
    routine action equivalent to prior events. Each occurrence halves the
    proposal again (1, 1/2, 1/4, ...); quantization eventually makes its effect
    exactly zero. The stable grouping and duplicate-event check belong to the
    caller, which has access to the committed evidence ledger.
    """

    current = _validate_score(current_score, "current_score")
    proposed = _validate_score(proposed_delta, "proposed_delta")
    if type(verified_key_evidence) is not bool:
        raise ValueError("verified_key_evidence must be a bool")
    occurrence = _validate_occurrence(routine_occurrence)

    if proposed == 0:
        return _result(current, proposed, Decimal("0"), current, "no_change")

    if current.copy_abs() >= _KEY_THRESHOLD and not verified_key_evidence:
        return _result(
            current, proposed, Decimal("0"), current, "key_evidence_required"
        )

    adjusted = _adjusted_delta(
        current,
        proposed,
        verified_key_evidence=verified_key_evidence,
        routine_occurrence=occurrence,
    )
    if adjusted == 0:
        return _result(current, proposed, adjusted, current, "rounded_to_zero")

    candidate = current + adjusted
    if not verified_key_evidence and candidate.copy_abs() >= _KEY_THRESHOLD:
        candidate = _SOFT_CAP.copy_sign(candidate)
        applied = candidate - current
        return _result(
            current, proposed, adjusted, candidate, "threshold_limited", applied
        )

    bounded = min(_SCORE_LIMIT, max(-_SCORE_LIMIT, candidate))
    applied = bounded - current
    outcome: RelationshipOutcome
    if bounded != candidate:
        outcome = "score_limited"
    elif adjusted != proposed:
        outcome = "attenuated"
    else:
        outcome = "applied"
    return _result(current, proposed, adjusted, bounded, outcome, applied)


def _adjusted_delta(
    current: Decimal,
    proposed: Decimal,
    *,
    verified_key_evidence: bool,
    routine_occurrence: int | None,
) -> Decimal:
    # Given the bounded proposal, all routine effects from occurrence 29 on
    # are below half a micro-point, even before the soft-band multiplier.
    if routine_occurrence is not None and routine_occurrence >= 29:
        return Decimal("0.000000")

    with localcontext() as context:
        context.prec = 50
        adjusted = proposed
        if routine_occurrence is not None:
            adjusted *= _ROUTINE_DECAY_FACTOR ** (routine_occurrence - 1)
        if (
            not verified_key_evidence
            and _SOFT_THRESHOLD <= current.copy_abs() < _KEY_THRESHOLD
        ):
            adjusted *= _SOFT_MULTIPLIER
        return adjusted.quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN)


def _validate_score(value: object, field: str) -> Decimal:
    # The shared exact-decimal pre-validator accepts Python bool via its int
    # superclass check. Relationship policy must not interpret True as 1.
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite exact decimal from -100 to 100")
    try:
        return _SCORE_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"{field} must be a finite exact decimal from -100 to 100") from exc


def _validate_occurrence(value: int | None) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= _MAX_ROUTINE_OCCURRENCE:
        raise ValueError("routine_occurrence must be a positive signed 64-bit integer")
    return value


def _result(
    current: Decimal,
    proposed: Decimal,
    adjusted: Decimal,
    resulting: Decimal,
    outcome: RelationshipOutcome,
    applied: Decimal | None = None,
) -> RelationshipCurveResult:
    return RelationshipCurveResult(
        current_score=current,
        proposed_delta=proposed,
        adjusted_delta=adjusted,
        applied_delta=resulting - current if applied is None else applied,
        resulting_score=resulting,
        outcome=outcome,
    )
