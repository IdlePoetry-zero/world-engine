from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.institutional_credit import (
    InstitutionalCreditContract,
    accept_institutional_credit,
    decline_institutional_credit,
    mark_institutional_credit_overdue,
    pay_institutional_credit,
    propose_institutional_credit,
    withdraw_institutional_credit,
)
from world_engine.domain.institutional_credit_events import (
    INSTITUTIONAL_CREDIT_CHANGED_EVENT_ADAPTER,
    INSTITUTIONAL_CREDIT_CHANGED_EVENT_TYPE,
    InstitutionalCreditChangedEvent,
    InstitutionalCreditTransfer,
    institutional_credit_event_id,
    institutional_credit_offer_id,
    parse_institutional_credit_changed_event,
)


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
LENDER_ID = UUID("40000000-0000-4000-8000-000000000004")
BORROWER_ID = UUID("50000000-0000-4000-8000-000000000005")
LENDER_ACTOR_ID = UUID("60000000-0000-4000-8000-000000000006")
BORROWER_ACTOR_ID = UUID("70000000-0000-4000-8000-000000000007")
LENDER_ACCOUNT_ID = UUID("80000000-0000-4000-8000-000000000008")
BORROWER_ACCOUNT_ID = UUID("90000000-0000-4000-8000-000000000009")
OTHER_ACCOUNT_ID = UUID("a0000000-0000-4000-8000-00000000000a")
CURRENCY_ID = "modern.credit"
RULESET = "credit-test.v1"


def _offer() -> InstitutionalCreditContract:
    return propose_institutional_credit(
        credit_id=institutional_credit_offer_id(COMMAND_ID),
        lender_organization_id=LENDER_ID,
        borrower_organization_id=BORROWER_ID,
        currency_resource_id=CURRENCY_ID,
        principal_minor=100,
        total_repayable_minor=120,
        accepted_through_tick=10,
        maturity_tick=20,
        current_tick=5,
    )


def _transfer(amount: int, *, currency: str = CURRENCY_ID, from_id=None, to_id=None):
    return InstitutionalCreditTransfer(
        from_account_id=from_id or LENDER_ACCOUNT_ID,
        to_account_id=to_id or BORROWER_ACCOUNT_ID,
        currency_resource_id=currency,
        amount_minor=amount,
    )


def _event(
    operation,
    contract,
    *,
    expected_revision,
    actor_organization_id,
    actor_id=None,
    transfer=None,
    event_id=None,
    visibility=None,
):
    return InstitutionalCreditChangedEvent(
        event_id=event_id or institutional_credit_event_id(
            COMMAND_ID, RULESET, operation, contract.credit_id,
        ),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=actor_id or BORROWER_ACTOR_ID,
        actor_organization_id=actor_organization_id,
        ruleset_version=RULESET,
        operation=operation,
        expected_revision=expected_revision,
        resulting_contract=contract,
        transfer=transfer,
        **({"visibility": visibility} if visibility is not None else {}),
    )


def test_offer_event_binds_command_derived_credit_and_parses_closed_payload():
    contract = _offer()
    event = _event(
        "offer", contract, expected_revision=0,
        actor_organization_id=LENDER_ID, actor_id=LENDER_ACTOR_ID,
    )

    assert event.type == INSTITUTIONAL_CREDIT_CHANGED_EVENT_TYPE
    assert contract.credit_id == institutional_credit_offer_id(COMMAND_ID)
    assert event.event_id == institutional_credit_event_id(COMMAND_ID, RULESET, "offer", contract.credit_id)
    assert event.visibility == {"policy": "author_only"}
    assert "actor_organization_id" in event.model_dump(mode="json")
    assert parse_institutional_credit_changed_event(event.model_dump(mode="json")) == event
    assert INSTITUTIONAL_CREDIT_CHANGED_EVENT_ADAPTER.validate_python(event) == event

    missing_organization = event.model_dump(mode="python")
    missing_organization.pop("actor_organization_id")
    with pytest.raises(ValidationError):
        parse_institutional_credit_changed_event(missing_organization)


def test_acceptance_event_requires_exact_principal_transfer_and_borrower_identity():
    active = accept_institutional_credit(
        _offer(), actor_organization_id=BORROWER_ID, current_tick=6, expected_revision=1,
    )
    event = _event(
        "accept", active, expected_revision=1, actor_organization_id=BORROWER_ID,
        transfer=_transfer(100),
    )
    assert event.resulting_contract.status == "active"
    assert event.transfer.amount_minor == active.principal_minor

    with pytest.raises(ValidationError):
        _event("accept", active, expected_revision=1, actor_organization_id=BORROWER_ID)
    with pytest.raises(ValidationError):
        _event(
            "accept", active, expected_revision=1, actor_organization_id=LENDER_ID,
            transfer=_transfer(100),
        )
    with pytest.raises(ValidationError):
        _event(
            "accept", active, expected_revision=1, actor_organization_id=BORROWER_ID,
            transfer=_transfer(99),
        )
    with pytest.raises(ValidationError):
        _event(
            "accept", active, expected_revision=1, actor_organization_id=BORROWER_ID,
            transfer=_transfer(100, currency="core.coin"),
        )


@pytest.mark.parametrize(
    ("operation", "transition", "actor_id", "actor_org", "status"),
    [
        (
            "decline",
            lambda offer: decline_institutional_credit(
                offer, actor_organization_id=BORROWER_ID, current_tick=7, expected_revision=1,
            ),
            BORROWER_ACTOR_ID,
            BORROWER_ID,
            "declined",
        ),
        (
            "withdraw",
            lambda offer: withdraw_institutional_credit(
                offer, actor_organization_id=LENDER_ID, current_tick=7, expected_revision=1,
            ),
            LENDER_ACTOR_ID,
            LENDER_ID,
            "withdrawn",
        ),
    ],
)
def test_offer_closure_events_require_party_direction_and_no_transfer(
    operation, transition, actor_id, actor_org, status,
):
    closed = transition(_offer())
    event = _event(
        operation, closed, expected_revision=1,
        actor_organization_id=actor_org, actor_id=actor_id,
    )
    assert event.resulting_contract.status == status
    with pytest.raises(ValidationError):
        _event(
            operation, closed, expected_revision=1,
            actor_organization_id=actor_org, actor_id=actor_id,
            transfer=_transfer(1),
        )
    wrong_party = LENDER_ID if actor_org == BORROWER_ID else BORROWER_ID
    with pytest.raises(ValidationError):
        _event(
            operation, closed, expected_revision=1,
            actor_organization_id=wrong_party, actor_id=actor_id,
        )


def test_partial_payment_transfer_is_bounded_by_resulting_cumulative_repayment():
    active = accept_institutional_credit(
        _offer(), actor_organization_id=BORROWER_ID, current_tick=6, expected_revision=1,
    )
    partial = pay_institutional_credit(
        active, actor_organization_id=BORROWER_ID, amount_minor=25,
        current_tick=7, expected_revision=2,
    )
    event = _event(
        "pay", partial, expected_revision=2, actor_organization_id=BORROWER_ID,
        transfer=_transfer(20),
    )
    assert event.transfer.amount_minor <= partial.repaid_minor

    with pytest.raises(ValidationError):
        _event("pay", partial, expected_revision=2, actor_organization_id=BORROWER_ID)
    with pytest.raises(ValidationError):
        _event(
            "pay", partial, expected_revision=2, actor_organization_id=BORROWER_ID,
            transfer=_transfer(26),
        )
    with pytest.raises(ValidationError):
        _event(
            "pay", partial, expected_revision=2, actor_organization_id=LENDER_ID,
            transfer=_transfer(20),
        )


def test_overdue_event_may_omit_org_identity_but_preserves_breach_and_forbids_transfer():
    active = accept_institutional_credit(
        _offer(), actor_organization_id=BORROWER_ID, current_tick=6, expected_revision=1,
    )
    overdue = mark_institutional_credit_overdue(
        active, actor_organization_id=LENDER_ID, current_tick=21, expected_revision=2,
    )
    event = _event(
        "overdue", overdue, expected_revision=2, actor_organization_id=None,
        actor_id=LENDER_ACTOR_ID,
    )
    assert "actor_organization_id" not in event.model_dump(mode="json")
    assert event.resulting_contract.overdue_at_tick == 21

    with pytest.raises(ValidationError):
        _event(
            "overdue", overdue, expected_revision=2, actor_organization_id=BORROWER_ID,
            actor_id=BORROWER_ACTOR_ID,
        )
    with pytest.raises(ValidationError):
        _event(
            "overdue", overdue, expected_revision=2, actor_organization_id=None,
            actor_id=LENDER_ACTOR_ID, transfer=_transfer(1),
        )


def test_late_settlement_event_retains_breach_and_payment_transfer():
    active = accept_institutional_credit(
        _offer(), actor_organization_id=BORROWER_ID, current_tick=6, expected_revision=1,
    )
    overdue = mark_institutional_credit_overdue(
        active, actor_organization_id=LENDER_ID, current_tick=21, expected_revision=2,
    )
    settled = pay_institutional_credit(
        overdue, actor_organization_id=BORROWER_ID, amount_minor=120,
        current_tick=22, expected_revision=3,
    )
    event = _event(
        "pay", settled, expected_revision=3, actor_organization_id=BORROWER_ID,
        transfer=_transfer(120, from_id=BORROWER_ACCOUNT_ID, to_id=LENDER_ACCOUNT_ID),
    )
    assert event.resulting_contract.status == "settled"
    assert event.resulting_contract.overdue_at_tick == 21


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_revision": 1},
        {"resulting_contract": _offer().model_copy(update={"revision": 2})},
        {"event_id": uuid4()},
        {"visibility": {"policy": "participants"}},
        {"unexpected": True},
    ],
)
def test_offer_event_rejects_bad_revision_id_visibility_and_extra_fields(changes):
    contract = _offer()
    raw = _event(
        "offer", contract, expected_revision=0, actor_organization_id=LENDER_ID,
    ).model_dump(mode="python") | changes
    with pytest.raises(ValidationError):
        parse_institutional_credit_changed_event(raw)


def test_event_rejects_pay_revision_skips_wrong_status_and_invalid_transfer():
    active = accept_institutional_credit(
        _offer(), actor_organization_id=BORROWER_ID, current_tick=6, expected_revision=1,
    )
    partial = pay_institutional_credit(
        active, actor_organization_id=BORROWER_ID, amount_minor=10,
        current_tick=7, expected_revision=2,
    )
    with pytest.raises(ValidationError):
        _event(
            "pay", partial, expected_revision=1, actor_organization_id=BORROWER_ID,
            transfer=_transfer(10),
        )
    with pytest.raises(ValidationError):
        _event(
            "pay", partial, expected_revision=2, actor_organization_id=BORROWER_ID,
            transfer=_transfer(10, currency="core.coin"),
        )
    with pytest.raises(ValidationError):
        InstitutionalCreditTransfer(
            from_account_id=LENDER_ACCOUNT_ID,
            to_account_id=LENDER_ACCOUNT_ID,
            currency_resource_id=CURRENCY_ID,
            amount_minor=10,
        )
    with pytest.raises(ValidationError):
        InstitutionalCreditTransfer(
            from_account_id=LENDER_ACCOUNT_ID,
            to_account_id=OTHER_ACCOUNT_ID,
            currency_resource_id=CURRENCY_ID,
            amount_minor=True,
        )


def test_parse_adapter_rejects_non_event_payload():
    with pytest.raises(ValidationError):
        parse_institutional_credit_changed_event({"type": "core.other.v1"})
