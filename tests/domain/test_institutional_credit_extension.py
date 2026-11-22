from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.institutional_credit import (
    InstitutionalCreditContract,
    InstitutionalCreditError,
    accept_institutional_credit,
    mark_institutional_credit_overdue,
    pay_institutional_credit,
    propose_institutional_credit,
)
from world_engine.domain.institutional_credit_extension import (
    InstitutionalCreditExtension,
    propose_credit_extension,
    resolve_credit_extension,
)


LENDER_ID = UUID("a7302515-0651-4f30-bca8-247ca46291f5")
BORROWER_ID = UUID("cfb5ce3a-f3a8-465b-88a7-697e2d90dfaf")
LENDER_ACTOR_ID = UUID("50c7d4d2-720d-42e0-bec1-3fb5a37fcba5")
BORROWER_ACTOR_ID = UUID("4f61d422-1797-4943-a26c-23cc5254b951")
OTHER_BORROWER_ACTOR_ID = UUID("d7e5d474-d0cf-4e25-9ab4-18140d99216d")


def _active(*, maturity_tick: int = 20, principal: int = 100, total: int = 120):
    offered = propose_institutional_credit(
        credit_id=uuid4(),
        lender_organization_id=LENDER_ID,
        borrower_organization_id=BORROWER_ID,
        currency_resource_id="modern.credit",
        principal_minor=principal,
        total_repayable_minor=total,
        accepted_through_tick=5,
        maturity_tick=maturity_tick,
        current_tick=0,
    )
    return accept_institutional_credit(
        offered,
        actor_organization_id=BORROWER_ID,
        current_tick=2,
        expected_revision=offered.revision,
    )


def _propose(
    contract: InstitutionalCreditContract,
    *,
    extension_id: UUID | None = None,
    actor_org: UUID = LENDER_ID,
    actor: UUID = LENDER_ACTOR_ID,
    current_tick: int = 3,
    new_maturity_tick: int = 30,
    accept_through_tick: int = 10,
) -> InstitutionalCreditContract:
    return propose_credit_extension(
        contract,
        extension_id=extension_id or uuid4(),
        actor_organization_id=actor_org,
        actor_id=actor,
        current_tick=current_tick,
        expected_revision=contract.revision,
        new_maturity_tick=new_maturity_tick,
        accept_through_tick=accept_through_tick,
    )


def _resolve(
    contract: InstitutionalCreditContract,
    *,
    extension_id: UUID,
    operation: str = "accept",
    actor_org: UUID = BORROWER_ID,
    actor: UUID = BORROWER_ACTOR_ID,
    current_tick: int = 4,
) -> InstitutionalCreditContract:
    return resolve_credit_extension(
        contract,
        extension_id=extension_id,
        actor_organization_id=actor_org,
        actor_id=actor,
        current_tick=current_tick,
        expected_revision=contract.revision,
        operation=operation,  # type: ignore[arg-type]
    )


def test_old_contract_serialization_omits_empty_extension_history():
    contract = _active()
    assert contract.extensions == ()
    assert "extensions" not in contract.model_dump(mode="json")
    assert "extensions" not in contract.model_dump(mode="python")

    parsed = InstitutionalCreditContract.model_validate(contract.model_dump(mode="json"))
    assert parsed == contract


def test_either_party_can_propose_and_only_one_live_proposal_is_allowed():
    contract = _active(maturity_tick=20)
    extension_id = uuid4()
    proposal = _propose(
        contract,
        extension_id=extension_id,
        actor_org=BORROWER_ID,
        actor=BORROWER_ACTOR_ID,
        current_tick=8,
        new_maturity_tick=35,
        accept_through_tick=15,
    )

    assert proposal.revision == contract.revision + 1
    assert proposal.updated_tick == 8
    assert proposal.maturity_tick == contract.maturity_tick
    assert proposal.status == "active"
    (row,) = proposal.extensions
    assert isinstance(row, InstitutionalCreditExtension)
    assert row.extension_id == extension_id
    assert row.proposer_organization_id == BORROWER_ID
    assert row.proposer_actor_id == BORROWER_ACTOR_ID
    assert row.previous_maturity_tick == 20
    assert row.proposed_tick == 8
    assert row.status == "proposed"
    assert proposal.principal_minor == contract.principal_minor
    assert proposal.total_due_minor == contract.total_due_minor
    assert proposal.repaid_minor == contract.repaid_minor

    with pytest.raises(InstitutionalCreditError) as error:
        _propose(proposal, current_tick=9, new_maturity_tick=40, accept_through_tick=16)
    assert error.value.code == "extension_already_pending"


def test_counterparty_acceptance_changes_only_maturity_and_preserves_terms():
    contract = _active()
    proposed = _propose(contract, current_tick=3, new_maturity_tick=30, accept_through_tick=9)
    (proposal,) = proposed.extensions

    accepted = _resolve(
        proposed, extension_id=proposal.extension_id, operation="accept", current_tick=4,
    )
    (resolved,) = accepted.extensions
    assert accepted.status == "active"
    assert accepted.maturity_tick == 30
    assert accepted.overdue_at_tick is None
    assert accepted.updated_tick == 4
    assert accepted.revision == proposed.revision + 1
    assert resolved.status == "accepted"
    assert resolved.resolved_tick == 4
    assert resolved.resolved_by_actor_id == BORROWER_ACTOR_ID
    assert resolved.previous_overdue_tick is None
    for field in (
        "credit_id", "lender_organization_id", "borrower_organization_id", "currency_resource_id",
        "principal_minor", "total_repayable_minor", "accepted_through_tick", "accepted_tick", "repaid_minor",
    ):
        assert getattr(accepted, field) == getattr(contract, field)


def test_overdue_extension_acceptance_carries_the_original_breach_and_clears_current_marker():
    active = _active(maturity_tick=10, principal=100, total=125)
    overdue = mark_institutional_credit_overdue(
        active,
        actor_organization_id=LENDER_ID,
        current_tick=11,
        expected_revision=active.revision,
    )
    proposed = _propose(
        overdue,
        actor_org=LENDER_ID,
        actor=LENDER_ACTOR_ID,
        current_tick=11,
        new_maturity_tick=25,
        accept_through_tick=15,
    )
    (proposal,) = proposed.extensions

    accepted = _resolve(
        proposed,
        extension_id=proposal.extension_id,
        operation="accept",
        actor_org=BORROWER_ID,
        actor=OTHER_BORROWER_ACTOR_ID,
        current_tick=13,
    )
    (resolved,) = accepted.extensions
    assert accepted.status == "active"
    assert accepted.maturity_tick == 25
    assert accepted.overdue_at_tick is None
    assert resolved.previous_maturity_tick == 10
    assert resolved.previous_overdue_tick == 11
    assert resolved.resolved_tick == 13
    assert resolved.resolved_by_actor_id == OTHER_BORROWER_ACTOR_ID
    assert accepted.principal_minor == overdue.principal_minor
    assert accepted.total_due_minor == overdue.total_due_minor
    assert accepted.repaid_minor == overdue.repaid_minor
    assert accepted.accepted_tick == overdue.accepted_tick


@pytest.mark.parametrize("operation", ["decline", "withdraw"])
def test_decline_and_withdraw_preserve_terms_and_resolve_by_authorized_party(operation: str):
    contract = _active()
    proposed = _propose(contract, actor_org=LENDER_ID, actor=LENDER_ACTOR_ID)
    (proposal,) = proposed.extensions

    actor_org = BORROWER_ID if operation == "decline" else LENDER_ID
    actor = OTHER_BORROWER_ACTOR_ID if operation == "decline" else BORROWER_ACTOR_ID
    result = _resolve(
        proposed,
        extension_id=proposal.extension_id,
        operation=operation,
        actor_org=actor_org,
        actor=actor,
        current_tick=4,
    )
    (resolved,) = result.extensions
    assert resolved.status == ("declined" if operation == "decline" else "withdrawn")
    assert resolved.resolved_by_actor_id == actor
    assert result.maturity_tick == contract.maturity_tick
    assert result.status == contract.status
    assert result.overdue_at_tick == contract.overdue_at_tick
    assert result.principal_minor == contract.principal_minor
    assert result.total_due_minor == contract.total_due_minor


def test_accept_and_decline_need_counterparty_and_a_distinct_character():
    contract = _active()
    proposed = _propose(contract, actor_org=LENDER_ID, actor=LENDER_ACTOR_ID)
    (proposal,) = proposed.extensions

    for actor_org, actor in (
        (LENDER_ID, BORROWER_ACTOR_ID),
        (BORROWER_ID, LENDER_ACTOR_ID),
        (BORROWER_ID, LENDER_ACTOR_ID),
    ):
        with pytest.raises(InstitutionalCreditError):
            _resolve(
                proposed, extension_id=proposal.extension_id, actor_org=actor_org, actor=actor,
            )

    same_character_proposal = _propose(
        contract, actor_org=LENDER_ID, actor=LENDER_ACTOR_ID,
    )
    (same_row,) = same_character_proposal.extensions
    with pytest.raises(InstitutionalCreditError) as same_actor_error:
        _resolve(
            same_character_proposal,
            extension_id=same_row.extension_id,
            actor_org=BORROWER_ID,
            actor=LENDER_ACTOR_ID,
        )
    assert same_actor_error.value.code == "same_extension_actor"


def test_expired_proposal_is_not_actionable_and_a_new_nonoverlapping_proposal_can_follow():
    contract = _active(maturity_tick=50)
    proposed = _propose(
        contract, current_tick=10, new_maturity_tick=60, accept_through_tick=12,
    )
    (first,) = proposed.extensions
    with pytest.raises(InstitutionalCreditError) as expired:
        _resolve(
            proposed, extension_id=first.extension_id, current_tick=13,
        )
    assert expired.value.code == "extension_expired"

    next_proposal = _propose(
        proposed,
        extension_id=uuid4(),
        actor_org=BORROWER_ID,
        actor=BORROWER_ACTOR_ID,
        current_tick=13,
        new_maturity_tick=70,
        accept_through_tick=15,
    )
    assert [row.status for row in next_proposal.extensions] == ["proposed", "proposed"]
    assert next_proposal.maturity_tick == 50


def test_settlement_makes_pending_extension_unactionable():
    contract = _active(maturity_tick=50, principal=100, total=100)
    proposed = _propose(
        contract, current_tick=3, new_maturity_tick=60, accept_through_tick=10,
    )
    (proposal,) = proposed.extensions
    settled = pay_institutional_credit(
        proposed,
        actor_organization_id=BORROWER_ID,
        amount_minor=100,
        current_tick=4,
        expected_revision=proposed.revision,
    )
    assert settled.status == "settled"
    with pytest.raises(InstitutionalCreditError) as error:
        _resolve(settled, extension_id=proposal.extension_id, current_tick=5)
    assert error.value.code == "invalid_status"


def test_proposal_terms_revision_and_party_errors_fail_closed():
    contract = _active(maturity_tick=20)
    invalid_terms = [
        {"new_maturity_tick": 20, "accept_through_tick": 10},
        {"new_maturity_tick": 22, "accept_through_tick": 22},
        {"new_maturity_tick": 21, "accept_through_tick": 2},
        {"new_maturity_tick": 2**63 - 1, "accept_through_tick": 10},
        {"new_maturity_tick": 30, "accept_through_tick": True},
    ]
    for terms in invalid_terms:
        with pytest.raises(InstitutionalCreditError):
            propose_credit_extension(
                contract,
                extension_id=uuid4(),
                actor_organization_id=LENDER_ID,
                actor_id=LENDER_ACTOR_ID,
                current_tick=3,
                expected_revision=contract.revision,
                new_maturity_tick=terms["new_maturity_tick"],
                accept_through_tick=terms["accept_through_tick"],
            )

    with pytest.raises(InstitutionalCreditError) as stale:
        propose_credit_extension(
            contract,
            extension_id=uuid4(),
            actor_organization_id=LENDER_ID,
            actor_id=LENDER_ACTOR_ID,
            current_tick=3,
            expected_revision=contract.revision + 1,
            new_maturity_tick=30,
            accept_through_tick=10,
        )
    assert stale.value.code == "stale_revision"

    with pytest.raises(InstitutionalCreditError) as wrong_party:
        propose_credit_extension(
            contract,
            extension_id=uuid4(),
            actor_organization_id=uuid4(),
            actor_id=LENDER_ACTOR_ID,
            current_tick=3,
            expected_revision=contract.revision,
            new_maturity_tick=30,
            accept_through_tick=10,
        )
    assert wrong_party.value.code == "wrong_organization"


def test_contract_validation_rejects_broken_maturity_chain_and_lost_overdue_history():
    overdue = mark_institutional_credit_overdue(
        _active(maturity_tick=10),
        actor_organization_id=LENDER_ID,
        current_tick=11,
        expected_revision=2,
    )
    proposed = _propose(
        overdue, current_tick=11, new_maturity_tick=25, accept_through_tick=15,
    )
    (proposal,) = proposed.extensions
    accepted = _resolve(
        proposed,
        extension_id=proposal.extension_id,
        actor_org=BORROWER_ID,
        actor=OTHER_BORROWER_ACTOR_ID,
        current_tick=13,
    )

    raw = accepted.model_dump(mode="python")
    raw["maturity_tick"] = 26
    with pytest.raises(ValidationError, match="accepted extension history"):
        InstitutionalCreditContract.model_validate(raw)

    raw = accepted.model_dump(mode="python")
    extension_row = dict(raw["extensions"][0])
    extension_row["previous_overdue_tick"] = None
    raw["extensions"] = (extension_row,)
    with pytest.raises(ValidationError, match="retain any prior overdue breach"):
        InstitutionalCreditContract.model_validate(raw)


def test_contract_validation_rejects_malformed_checkpoint_extension_scope_and_time():
    proposed = _propose(_active(), current_tick=3, new_maturity_tick=30, accept_through_tick=9)
    (proposal,) = proposed.extensions
    accepted = _resolve(proposed, extension_id=proposal.extension_id, current_tick=4)

    malformed_rows = [
        {"proposer_organization_id": uuid4()},
        {"proposed_tick": 1},
        {"resolved_tick": 5},
        {"previous_maturity_tick": 5},
    ]
    expected_messages = [
        "proposer must be a contract party",
        "proposal cannot precede the original acceptance",
        "history cannot be later than the contract update",
        "initial extension history must follow the original offer cutoff",
    ]
    for changes, expected_message in zip(malformed_rows, expected_messages, strict=True):
        raw = accepted.model_dump(mode="python")
        row = dict(raw["extensions"][0]) | changes
        raw["extensions"] = (row,)
        with pytest.raises(ValidationError, match=expected_message):
            InstitutionalCreditContract.model_validate(raw)

