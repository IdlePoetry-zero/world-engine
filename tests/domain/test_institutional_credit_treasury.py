from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from world_engine.domain.components import GovernanceComponent, OfficeRecord, TreasuryComponent
from world_engine.domain.institutional_credit import InstitutionalCreditError
from world_engine.domain.institutional_credit_treasury import (
    apply_institutional_credit_transfer,
    plan_institutional_credit_transfer,
    require_institutional_credit_signer,
)
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState


CURRENCY = "modern.credit"


def _state(*, current_tick: int = 10, borrower_balance: str = "0") -> dict[str, object]:
    lender_id, borrower_id = uuid4(), uuid4()
    lender_leader_id, lender_treasurer_id, lender_member_id = uuid4(), uuid4(), uuid4()
    borrower_leader_id = uuid4()
    lender_account_id, borrower_account_id = uuid4(), uuid4()
    lender_governance = GovernanceComponent(
        members=[lender_leader_id, lender_treasurer_id, lender_member_id],
        leader_id=lender_leader_id,
        offices=[
            OfficeRecord(
                office_id="office.leader",
                title="Leader",
                holder_id=lender_leader_id,
                authority=["authority.represent"],
                term_end_tick=10,
            ),
            OfficeRecord(
                office_id="office.treasurer",
                title="Treasurer",
                holder_id=lender_treasurer_id,
                authority=["authority.treasury"],
                term_end_tick=11,
            ),
        ],
    )
    borrower_governance = GovernanceComponent(
        members=[borrower_leader_id],
        leader_id=borrower_leader_id,
        offices=[OfficeRecord(
            office_id="office.leader",
            title="Leader",
            holder_id=borrower_leader_id,
            authority=["authority.represent"],
        )],
    )
    entities = {
        lender_id: WorldEntity(
            id=lender_id,
            kind="company",
            components={
                "governance": lender_governance.model_dump(mode="json"),
                "treasury": TreasuryComponent(accounts={CURRENCY: lender_account_id}).model_dump(mode="json"),
            },
        ),
        borrower_id: WorldEntity(
            id=borrower_id,
            kind="company",
            components={
                "governance": borrower_governance.model_dump(mode="json"),
                "treasury": TreasuryComponent(accounts={CURRENCY: borrower_account_id}).model_dump(mode="json"),
            },
        ),
        lender_leader_id: WorldEntity(id=lender_leader_id, kind="character"),
        lender_treasurer_id: WorldEntity(id=lender_treasurer_id, kind="character"),
        lender_member_id: WorldEntity(id=lender_member_id, kind="character"),
        borrower_leader_id: WorldEntity(id=borrower_leader_id, kind="character"),
    }
    state = WorldState(
        current_tick=current_tick,
        entities=entities,
        resources={
            lender_account_id: ResourceAccount(
                resource_id=CURRENCY, balance="100", owner_id=lender_id, account_type="holder",
            ),
            borrower_account_id: ResourceAccount(
                resource_id=CURRENCY, balance=borrower_balance, owner_id=borrower_id, account_type="holder",
            ),
        },
    )
    return {
        "state": state,
        "lender_id": lender_id,
        "borrower_id": borrower_id,
        "lender_leader_id": lender_leader_id,
        "lender_treasurer_id": lender_treasurer_id,
        "lender_member_id": lender_member_id,
        "borrower_leader_id": borrower_leader_id,
        "lender_account_id": lender_account_id,
        "borrower_account_id": borrower_account_id,
    }


def test_signer_requires_current_membership_and_half_open_office_authority():
    data = _state(current_tick=10)
    state = data["state"]

    # The leader's authority ends at tick 10; the term is half-open.
    with pytest.raises(InstitutionalCreditError) as expired:
        require_institutional_credit_signer(state, data["lender_id"], data["lender_leader_id"])
    assert expired.value.code == "signer_unauthorized"

    # The explicit treasury office remains live through tick 10, but an
    # ordinary member has no signing power.
    require_institutional_credit_signer(state, data["lender_id"], data["lender_treasurer_id"])
    with pytest.raises(InstitutionalCreditError) as member:
        require_institutional_credit_signer(state, data["lender_id"], data["lender_member_id"])
    assert member.value.code == "signer_unauthorized"

    earlier = state.model_copy(update={"current_tick": 9})
    require_institutional_credit_signer(earlier, data["lender_id"], data["lender_leader_id"])

    with pytest.raises(InstitutionalCreditError) as nonmember:
        require_institutional_credit_signer(state, data["lender_id"], data["borrower_leader_id"])
    assert nonmember.value.code == "signer_unauthorized"


def test_transfer_plan_uses_both_registered_treasuries_and_is_non_mutating():
    data = _state()
    state = data["state"]
    transfer = plan_institutional_credit_transfer(
        state,
        debit_organization_id=data["lender_id"],
        credit_organization_id=data["borrower_id"],
        currency_resource_id=CURRENCY,
        amount_minor=35,
    )

    assert transfer.from_account_id == data["lender_account_id"]
    assert transfer.to_account_id == data["borrower_account_id"]
    assert transfer.currency_resource_id == CURRENCY
    assert transfer.amount_minor == 35

    updates = apply_institutional_credit_transfer(state, transfer)
    assert updates[data["lender_account_id"]].balance == Decimal("65")
    assert updates[data["borrower_account_id"]].balance == Decimal("35")
    assert state.resources[data["lender_account_id"]].balance == Decimal("100")
    assert state.resources[data["borrower_account_id"]].balance == Decimal("0")

    with pytest.raises(InstitutionalCreditError) as insufficient:
        plan_institutional_credit_transfer(
            state,
            debit_organization_id=data["lender_id"],
            credit_organization_id=data["borrower_id"],
            currency_resource_id=CURRENCY,
            amount_minor=101,
        )
    assert insufficient.value.code == "insufficient_funds"

    with pytest.raises(InstitutionalCreditError) as invalid_amount:
        plan_institutional_credit_transfer(
            state,
            debit_organization_id=data["lender_id"],
            credit_organization_id=data["borrower_id"],
            currency_resource_id=CURRENCY,
            amount_minor=True,
        )
    assert invalid_amount.value.code == "invalid_amount"

    non_holder_resources = dict(state.resources)
    non_holder_resources[data["lender_account_id"]] = ResourceAccount(
        resource_id=CURRENCY,
        balance="0",
        owner_id=data["lender_id"],
        account_type="production_source",
    )
    non_holder_state = state.model_copy(update={"resources": non_holder_resources})
    with pytest.raises(InstitutionalCreditError) as wrong_type:
        plan_institutional_credit_transfer(
            non_holder_state,
            debit_organization_id=data["lender_id"],
            credit_organization_id=data["borrower_id"],
            currency_resource_id=CURRENCY,
            amount_minor=1,
        )
    assert wrong_type.value.code == "treasury_unavailable"

    wrong_owner_resources = dict(state.resources)
    wrong_owner_resources[data["lender_account_id"]] = ResourceAccount(
        resource_id=CURRENCY,
        balance="100",
        owner_id=data["borrower_id"],
    )
    wrong_owner_state = state.model_copy(update={"resources": wrong_owner_resources})
    with pytest.raises(InstitutionalCreditError) as stale_owner:
        apply_institutional_credit_transfer(wrong_owner_state, transfer)
    assert stale_owner.value.code == "treasury_unavailable"

    wrong_resource_resources = dict(state.resources)
    wrong_resource_resources[data["lender_account_id"]] = ResourceAccount(
        resource_id="resource.other",
        balance="100",
        owner_id=data["lender_id"],
    )
    wrong_resource_state = state.model_copy(update={"resources": wrong_resource_resources})
    with pytest.raises(InstitutionalCreditError) as stale_resource:
        apply_institutional_credit_transfer(wrong_resource_state, transfer)
    assert stale_resource.value.code == "treasury_unavailable"


def test_transfer_rejects_unregistered_destination_currency_and_overflow():
    data = _state(borrower_balance="999999999999999999999999999999.999999")
    state = data["state"]

    with pytest.raises(InstitutionalCreditError) as wrong_currency:
        plan_institutional_credit_transfer(
            state,
            debit_organization_id=data["lender_id"],
            credit_organization_id=data["borrower_id"],
            currency_resource_id="modern.credit.other",
            amount_minor=1,
        )
    assert wrong_currency.value.code == "treasury_unavailable"

    with pytest.raises(InstitutionalCreditError) as overflow:
        plan_institutional_credit_transfer(
            state,
            debit_organization_id=data["lender_id"],
            credit_organization_id=data["borrower_id"],
            currency_resource_id=CURRENCY,
            amount_minor=1,
        )
    assert overflow.value.code == "destination_overflow"
