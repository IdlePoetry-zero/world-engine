from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from world_engine.domain.institutional_credit import (
    InstitutionalCreditContract,
    InstitutionalCreditError,
    accept_institutional_credit,
    decline_institutional_credit,
    mark_institutional_credit_overdue,
    pay_institutional_credit,
    propose_institutional_credit,
    withdraw_institutional_credit,
)


CREDIT_ID = UUID("10000000-0000-4000-8000-000000000001")
LENDER_ID = UUID("20000000-0000-4000-8000-000000000002")
BORROWER_ID = UUID("30000000-0000-4000-8000-000000000003")
CURRENCY_ID = "modern.credit"


def _offer(
    *,
    current_tick: int = 5,
    accepted_through_tick: int = 10,
    maturity_tick: int = 20,
    principal_minor: int = 100,
    total_repayable_minor: int | None = 120,
) -> InstitutionalCreditContract:
    return propose_institutional_credit(
        credit_id=CREDIT_ID,
        lender_organization_id=LENDER_ID,
        borrower_organization_id=BORROWER_ID,
        currency_resource_id=CURRENCY_ID,
        principal_minor=principal_minor,
        total_repayable_minor=total_repayable_minor,
        accepted_through_tick=accepted_through_tick,
        maturity_tick=maturity_tick,
        current_tick=current_tick,
    )


def _accept(contract: InstitutionalCreditContract, *, tick: int = 6) -> InstitutionalCreditContract:
    return accept_institutional_credit(
        contract,
        actor_organization_id=BORROWER_ID,
        current_tick=tick,
        expected_revision=contract.revision,
    )


def test_offer_acceptance_and_exact_payment_allow_inclusive_boundaries():
    offered = _offer()
    assert offered.status == "offered"
    assert offered.revision == 1
    assert offered.created_tick == offered.updated_tick == 5
    assert offered.total_due_minor == 120
    assert offered.remaining_total_minor == 120
    assert offered.remaining_principal_minor == 100

    accepted = _accept(offered, tick=offered.accepted_through_tick)
    assert accepted.status == "active"
    assert accepted.revision == 2
    assert accepted.accepted_tick == accepted.updated_tick == 10

    principal_paid = pay_institutional_credit(
        accepted,
        actor_organization_id=BORROWER_ID,
        amount_minor=100,
        current_tick=20,
        expected_revision=accepted.revision,
    )
    assert principal_paid.status == "active"
    assert principal_paid.repaid_minor == 100
    assert principal_paid.remaining_principal_minor == 0
    assert principal_paid.remaining_total_minor == 20

    settled = pay_institutional_credit(
        principal_paid,
        actor_organization_id=BORROWER_ID,
        amount_minor=20,
        current_tick=20,
        expected_revision=principal_paid.revision,
    )
    assert settled.status == "settled"
    assert settled.repaid_minor == settled.total_due_minor == 120
    assert settled.remaining_total_minor == settled.remaining_principal_minor == 0
    assert settled.overdue_at_tick is None
    assert settled.updated_tick == 20


def test_late_cure_preserves_overdue_breach_and_uses_principal_first_accounting():
    active = _accept(_offer(), tick=7)
    partial = pay_institutional_credit(
        active,
        actor_organization_id=BORROWER_ID,
        amount_minor=25,
        current_tick=8,
        expected_revision=active.revision,
    )
    assert partial.remaining_principal_minor == 75
    assert partial.remaining_total_minor == 95

    overdue = mark_institutional_credit_overdue(
        partial,
        actor_organization_id=LENDER_ID,
        current_tick=21,
        expected_revision=partial.revision,
    )
    assert overdue.status == "overdue"
    assert overdue.overdue_at_tick == overdue.updated_tick == 21

    late_partial = pay_institutional_credit(
        overdue,
        actor_organization_id=BORROWER_ID,
        amount_minor=50,
        current_tick=22,
        expected_revision=overdue.revision,
    )
    assert late_partial.status == "overdue"
    assert late_partial.overdue_at_tick == 21
    assert late_partial.remaining_total_minor == 45

    late_settled = pay_institutional_credit(
        late_partial,
        actor_organization_id=BORROWER_ID,
        amount_minor=45,
        current_tick=23,
        expected_revision=late_partial.revision,
    )
    assert late_settled.status == "settled"
    assert late_settled.overdue_at_tick == 21
    assert late_settled.updated_tick == 23
    assert late_settled.remaining_total_minor == 0


def test_overdue_starts_only_after_inclusive_maturity_and_must_be_recorded_before_late_payment():
    active = _accept(_offer(), tick=6)
    due_day_payment = pay_institutional_credit(
        active,
        actor_organization_id=BORROWER_ID,
        amount_minor=10,
        current_tick=20,
        expected_revision=active.revision,
    )
    with pytest.raises(InstitutionalCreditError) as before_maturity_end:
        mark_institutional_credit_overdue(
            due_day_payment,
            actor_organization_id=LENDER_ID,
            current_tick=20,
            expected_revision=due_day_payment.revision,
        )
    assert before_maturity_end.value.code == "not_overdue"

    with pytest.raises(InstitutionalCreditError) as unmarked_late_payment:
        pay_institutional_credit(
            due_day_payment,
            actor_organization_id=BORROWER_ID,
            amount_minor=1,
            current_tick=21,
            expected_revision=due_day_payment.revision,
        )
    assert unmarked_late_payment.value.code == "overdue_not_marked"

    overdue = mark_institutional_credit_overdue(
        due_day_payment,
        actor_organization_id=LENDER_ID,
        current_tick=21,
        expected_revision=due_day_payment.revision,
    )
    assert overdue.overdue_at_tick == 21
    with pytest.raises(InstitutionalCreditError) as duplicate_breach:
        mark_institutional_credit_overdue(
            overdue,
            actor_organization_id=LENDER_ID,
            current_tick=22,
            expected_revision=overdue.revision,
        )
    assert duplicate_breach.value.code == "invalid_status"


@pytest.mark.parametrize(
    ("operation", "actor_id", "expected_status"),
    [
        (decline_institutional_credit, BORROWER_ID, "declined"),
        (withdraw_institutional_credit, LENDER_ID, "withdrawn"),
    ],
)
def test_offer_can_be_closed_only_by_the_correct_party(operation, actor_id, expected_status):
    offered = _offer()
    closed = operation(
        offered,
        actor_organization_id=actor_id,
        current_tick=8,
        expected_revision=offered.revision,
    )
    assert closed.status == expected_status
    assert closed.revision == offered.revision + 1
    assert closed.updated_tick == 8
    with pytest.raises(InstitutionalCreditError) as cannot_accept_closed_offer:
        accept_institutional_credit(
            closed,
            actor_organization_id=BORROWER_ID,
            current_tick=8,
            expected_revision=closed.revision,
        )
    assert cannot_accept_closed_offer.value.code == "invalid_status"


def test_party_revision_status_and_overpayment_checks_fail_closed():
    offered = _offer()
    with pytest.raises(InstitutionalCreditError) as wrong_acceptor:
        accept_institutional_credit(
            offered, actor_organization_id=LENDER_ID, current_tick=6, expected_revision=1,
        )
    assert wrong_acceptor.value.code == "wrong_organization"

    with pytest.raises(InstitutionalCreditError) as stale:
        accept_institutional_credit(
            offered, actor_organization_id=BORROWER_ID, current_tick=6, expected_revision=2,
        )
    assert stale.value.code == "stale_revision"

    active = _accept(offered)
    with pytest.raises(InstitutionalCreditError) as wrong_payer:
        pay_institutional_credit(
            active, actor_organization_id=LENDER_ID, amount_minor=1, current_tick=7,
            expected_revision=active.revision,
        )
    assert wrong_payer.value.code == "wrong_organization"

    with pytest.raises(InstitutionalCreditError) as overpay:
        pay_institutional_credit(
            active, actor_organization_id=BORROWER_ID, amount_minor=121, current_tick=7,
            expected_revision=active.revision,
        )
    assert overpay.value.code == "overpayment"

    with pytest.raises(InstitutionalCreditError) as zero_payment:
        pay_institutional_credit(
            active, actor_organization_id=BORROWER_ID, amount_minor=0, current_tick=7,
            expected_revision=active.revision,
        )
    assert zero_payment.value.code == "invalid_amount"


@pytest.mark.parametrize(
    "changes",
    [
        {"lender_organization_id": BORROWER_ID},
        {"principal_minor": 0},
        {"principal_minor": True},
        {"principal_minor": 1.5},
        {"principal_minor": "100"},
        {"principal_minor": 10**30},
        {"total_repayable_minor": 99},
        {"accepted_through_tick": 4},
        {"accepted_through_tick": 20},
        {"maturity_tick": 2**63 - 1},
        {"extra": "not allowed"},
    ],
)
def test_contract_schema_rejects_invalid_or_non_strict_terms(changes):
    raw = _offer().model_dump(mode="python") | changes
    with pytest.raises(ValidationError):
        InstitutionalCreditContract.model_validate(raw)


def test_acceptance_after_window_and_nonmonotonic_ticks_are_rejected():
    offered = _offer()
    with pytest.raises(InstitutionalCreditError) as late_acceptance:
        accept_institutional_credit(
            offered,
            actor_organization_id=BORROWER_ID,
            current_tick=11,
            expected_revision=offered.revision,
        )
    assert late_acceptance.value.code == "acceptance_window_closed"

    active = _accept(offered, tick=8)
    with pytest.raises(InstitutionalCreditError) as backwards:
        pay_institutional_credit(
            active,
            actor_organization_id=BORROWER_ID,
            amount_minor=1,
            current_tick=7,
            expected_revision=active.revision,
        )
    assert backwards.value.code == "time_reversed"

    with pytest.raises(InstitutionalCreditError) as offer_after_window:
        _offer(current_tick=11, accepted_through_tick=10)
    assert offer_after_window.value.code == "invalid_terms"


def test_contract_is_immutable_and_omits_unspecified_total_repayment():
    offered = _offer(total_repayable_minor=None)
    serialized = offered.model_dump(mode="json")
    assert "total_repayable_minor" not in serialized
    assert offered.total_due_minor == offered.principal_minor == 100
    with pytest.raises(ValidationError):
        offered.status = "active"

