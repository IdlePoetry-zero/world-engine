from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4, uuid5

import pytest

from world_engine.domain.components import TreasuryComponent
from world_engine.domain.institutional_credit import (
    accept_institutional_credit,
    mark_institutional_credit_overdue,
    pay_institutional_credit,
    propose_institutional_credit,
)
from world_engine.domain.institutional_credit_state import credit_maturity_schedule
from world_engine.domain.institutional_finance import (
    InstitutionalAssetRow,
    InstitutionalFinanceAssessment,
    assess_institutional_finance,
)
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState


_NAMESPACE = UUID("28117d62-fd6a-4f2f-9a93-65c94b4d3e26")


def _id(name: str) -> UUID:
    return uuid5(_NAMESPACE, name)


def _funded_credit(
    *,
    credit_name: str,
    lender_id: UUID,
    borrower_id: UUID,
    currency: str,
    principal: int,
    total: int,
    maturity: int,
    repaid: int = 0,
    overdue: bool = False,
):
    offered = propose_institutional_credit(
        credit_id=_id(credit_name),
        lender_organization_id=lender_id,
        borrower_organization_id=borrower_id,
        currency_resource_id=currency,
        principal_minor=principal,
        total_repayable_minor=total,
        accepted_through_tick=3,
        maturity_tick=maturity,
        current_tick=0,
    )
    active = accept_institutional_credit(
        offered,
        actor_organization_id=borrower_id,
        current_tick=1,
        expected_revision=offered.revision,
    )
    if repaid:
        active = pay_institutional_credit(
            active,
            actor_organization_id=borrower_id,
            amount_minor=repaid,
            current_tick=2,
            expected_revision=active.revision,
        )
    if overdue:
        active = mark_institutional_credit_overdue(
            active,
            actor_organization_id=lender_id,
            current_tick=maturity + 1,
            expected_revision=active.revision,
        )
    return active


def test_assessment_separates_exact_treasury_assets_reserves_and_credit_maturities():
    organization_id, other_org_id = _id("institution"), _id("other-institution")
    treasury_account_id = _id("treasury-account")
    missing_account_id = _id("missing-account")
    mismatched_account_id = _id("mismatched-account")
    cash_reserve_id = _id("cash-reserve")
    ore_account_id = _id("ore-account")
    medicine_account_id = _id("medicine-account")
    second_ore_account_id = _id("second-ore-account")
    zero_account_id = _id("zero-account")
    other_owner_account_id = _id("other-owner-account")
    source_account_id = _id("source-account")

    treasury = TreasuryComponent(accounts={
        "modern.credit": treasury_account_id,
        "feudal.silver_mark": missing_account_id,
        "cultivation.spirit_stone": mismatched_account_id,
    })
    organization = WorldEntity(
        id=organization_id,
        kind="organization",
        name="Borrowing House",
        components={"treasury": treasury.model_dump(mode="json")},
    )
    other_org = WorldEntity(id=other_org_id, kind="company", name="Other House")

    due_id = _funded_credit(
        credit_name="due-at-inclusive-maturity",
        lender_id=other_org_id,
        borrower_id=organization_id,
        currency="modern.credit",
        principal=100,
        total=140,
        maturity=6,
        repaid=20,
    )
    overdue_id = _funded_credit(
        credit_name="recorded-overdue",
        lender_id=other_org_id,
        borrower_id=organization_id,
        currency="modern.credit",
        principal=50,
        total=80,
        maturity=4,
        repaid=10,
        overdue=True,
    )
    future_id = _funded_credit(
        credit_name="future-credit-without-treasury",
        lender_id=other_org_id,
        borrower_id=organization_id,
        currency="foreign.credit",
        principal=25,
        total=35,
        maturity=10,
    )
    settled = _funded_credit(
        credit_name="settled-credit",
        lender_id=other_org_id,
        borrower_id=organization_id,
        currency="modern.credit",
        principal=5,
        total=5,
        maturity=10,
    )
    settled = pay_institutional_credit(
        settled,
        actor_organization_id=organization_id,
        amount_minor=5,
        current_tick=3,
        expected_revision=settled.revision,
    )
    lender_receivable = _funded_credit(
        credit_name="lender-receivable-not-obligation",
        lender_id=organization_id,
        borrower_id=other_org_id,
        currency="lender.receivable",
        principal=900,
        total=900,
        maturity=10,
    )
    offered = propose_institutional_credit(
        credit_id=_id("unaccepted-offer"),
        lender_organization_id=other_org_id,
        borrower_organization_id=organization_id,
        currency_resource_id="offered.only",
        principal_minor=999,
        accepted_through_tick=3,
        maturity_tick=7,
        current_tick=0,
    )

    state = WorldState(
        current_tick=6,
        entities={organization_id: organization, other_org_id: other_org},
        scheduled_events=[
            credit_maturity_schedule(due_id),
            credit_maturity_schedule(overdue_id).model_copy(update={
                "status": "completed", "completed_runs": 1, "revision": 2,
            }),
            credit_maturity_schedule(future_id),
            credit_maturity_schedule(settled).model_copy(update={"status": "cancelled", "revision": 2}),
            credit_maturity_schedule(lender_receivable),
        ],
        resources={
            treasury_account_id: ResourceAccount(
                resource_id="modern.credit", balance="0", owner_id=organization_id,
            ),
            missing_account_id: ResourceAccount(
                resource_id="feudal.silver_mark", balance="9", owner_id=organization_id,
            ),
            mismatched_account_id: ResourceAccount(
                resource_id="cultivation.spirit_stone", balance="-75", owner_id=organization_id,
                account_type="production_source",
            ),
            cash_reserve_id: ResourceAccount(
                resource_id="modern.credit", balance="13.25", owner_id=organization_id,
            ),
            ore_account_id: ResourceAccount(
                resource_id="resource.ore", balance="2.50", owner_id=organization_id,
            ),
            second_ore_account_id: ResourceAccount(
                resource_id="resource.ore", balance="4", owner_id=organization_id,
            ),
            medicine_account_id: ResourceAccount(
                resource_id="resource.medicine", balance="1.125", owner_id=organization_id,
            ),
            zero_account_id: ResourceAccount(
                resource_id="resource.zero", balance="0", owner_id=organization_id,
            ),
            other_owner_account_id: ResourceAccount(
                resource_id="resource.ore", balance="99", owner_id=other_org_id,
            ),
            source_account_id: ResourceAccount(
                resource_id="resource.ore", balance="-7", owner_id=organization_id,
                account_type="production_source",
            ),
        },
        institutional_credit_contracts=[due_id, overdue_id, future_id, settled, lender_receivable, offered],
    )
    # Simulate a stale/corrupt projected treasury reference after normal state
    # validation; assessment must report unknown rather than infer a balance.
    state.resources.pop(missing_account_id)
    before = state.model_dump(mode="json")

    assessment = assess_institutional_finance(state, organization_id)

    assert isinstance(assessment, InstitutionalFinanceAssessment)
    assert [row.currency_resource_id for row in assessment.currencies] == [
        "cultivation.spirit_stone", "feudal.silver_mark", "foreign.credit", "modern.credit",
    ]
    profiles = {row.currency_resource_id: row for row in assessment.currencies}
    assert profiles["modern.credit"].treasury_balance_minor == Decimal("0")
    assert profiles["feudal.silver_mark"].treasury_balance_minor is None
    assert profiles["cultivation.spirit_stone"].treasury_balance_minor is None
    assert profiles["modern.credit"].due_credit_minor == 190
    assert profiles["modern.credit"].overdue_credit_minor == 70
    assert profiles["modern.credit"].future_credit_minor == 0
    assert profiles["modern.credit"].due_credit_ids == tuple(sorted((due_id.credit_id, overdue_id.credit_id)))
    assert profiles["foreign.credit"].treasury_balance_minor is None
    assert profiles["foreign.credit"].due_credit_minor == 0
    assert profiles["foreign.credit"].future_credit_minor == future_id.remaining_total_minor == 35
    assert profiles["foreign.credit"].due_credit_ids == ()
    assert "lender.receivable" not in profiles
    assert "offered.only" not in profiles

    expected_assets = [
        InstitutionalAssetRow(medicine_account_id, "resource.medicine", Decimal("1.125")),
        InstitutionalAssetRow(ore_account_id, "resource.ore", Decimal("2.50")),
        InstitutionalAssetRow(second_ore_account_id, "resource.ore", Decimal("4")),
    ]
    expected_assets.sort(key=lambda row: (row.resource_id, row.account_id.hex))
    assert assessment.asset_rows == tuple(expected_assets)
    assert assessment.cash_reserve_rows == (
        InstitutionalAssetRow(
            account_id=cash_reserve_id,
            resource_id="modern.credit",
            quantity=Decimal("13.25"),
        ),
    )
    assert state.model_dump(mode="json") == before


def test_assessment_exposes_owned_assets_without_a_treasury_or_credit():
    organization_id = _id("asset-only-organization")
    account_id = _id("asset-only-account")
    state = WorldState(
        entities={organization_id: WorldEntity(id=organization_id, kind="faction")},
        resources={account_id: ResourceAccount(
            resource_id="resource.grain", balance="12.75", owner_id=organization_id,
        )},
    )

    assessment = assess_institutional_finance(state, organization_id)

    assert assessment.currencies == ()
    assert [(row.account_id, row.resource_id, row.quantity) for row in assessment.asset_rows] == [
        (account_id, "resource.grain", Decimal("12.75")),
    ]
    assert assessment.cash_reserve_rows == ()


def test_assessment_rejects_a_missing_or_non_organization_target():
    state = WorldState()
    missing_id = uuid4()
    with pytest.raises(ValueError, match="existing organization"):
        assess_institutional_finance(state, missing_id)

    person_id = uuid4()
    state.entities[person_id] = WorldEntity(id=person_id, kind="character")
    with pytest.raises(ValueError, match="existing organization"):
        assess_institutional_finance(state, person_id)

