from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import GovernanceComponent, OfficeRecord, TreasuryComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.institutional_credit import InstitutionalCreditError
from world_engine.domain.institutional_treasury_reserve import (
    apply_reserve_mobilization,
    plan_reserve_mobilization,
    validate_reserve_mobilization_batch,
)
from world_engine.domain.institutional_treasury_reserve_events import (
    INSTITUTIONAL_TREASURY_RESERVE_COMMAND,
    InstitutionalTreasuryMobilizedEvent,
    InstitutionalTreasuryReserveAllocation,
    institutional_treasury_mobilized_command_payload,
    institutional_treasury_mobilized_event_id,
)
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState


CURRENCY = "modern.credit"
RULESET = "institutional-reserve.test.v1"
WORLD_ID = UUID("667ed9df-48dc-424e-b1f0-11a203cbfd77")
BRANCH_ID = UUID("3937d803-27da-4ca2-b8dc-9f22c42926a8")
ACTOR_ID = UUID("c3a08c79-8109-4fde-a8ee-5a14ec26fc8c")
ORG_ID = UUID("919d3057-a72e-49bd-8de7-4e8b49ba39a7")
OTHER_ORG_ID = UUID("e012b0bc-936f-4539-a0f4-9176dc628a3e")
DEST_ID = UUID("00000000-0000-0000-0000-000000000010")
RESERVE_IDS = (
    UUID("00000000-0000-0000-0000-000000000020"),
    UUID("00000000-0000-0000-0000-000000000030"),
    UUID("00000000-0000-0000-0000-000000000040"),
)


def _world(*, reserve_balances=("0.75", "0.75", "0.5"), destination_balance="10.25"):
    other_treasury_id = UUID("00000000-0000-0000-0000-000000000050")
    treasury = TreasuryComponent(accounts={
        CURRENCY: DEST_ID,
        "feudal.silver_mark": other_treasury_id,
    })
    governance = GovernanceComponent(
        members=[ACTOR_ID],
        leader_id=ACTOR_ID,
        offices=[OfficeRecord(
            office_id="office.treasurer",
            title="Treasury Keeper",
            holder_id=ACTOR_ID,
            authority=["authority.treasury", "authority.represent"],
        )],
        succession_policy="appointment",
    )
    organization = WorldEntity(
        id=ORG_ID,
        kind="organization",
        name="House",
        components={
            "governance": governance.model_dump(mode="json"),
            "treasury": treasury.model_dump(mode="json"),
        },
    )
    accounts = {
        DEST_ID: ResourceAccount(resource_id=CURRENCY, balance=destination_balance, owner_id=ORG_ID),
        other_treasury_id: ResourceAccount(
            resource_id="feudal.silver_mark", balance="7", owner_id=ORG_ID,
        ),
        RESERVE_IDS[0]: ResourceAccount(
            resource_id=CURRENCY, balance=reserve_balances[0], owner_id=ORG_ID,
        ),
        RESERVE_IDS[1]: ResourceAccount(
            resource_id=CURRENCY, balance=reserve_balances[1], owner_id=ORG_ID,
        ),
        RESERVE_IDS[2]: ResourceAccount(
            resource_id=CURRENCY, balance=reserve_balances[2], owner_id=ORG_ID,
        ),
        UUID("00000000-0000-0000-0000-000000000060"): ResourceAccount(
            resource_id="resource.ore", balance="500", owner_id=ORG_ID,
        ),
        UUID("00000000-0000-0000-0000-000000000070"): ResourceAccount(
            resource_id=CURRENCY, balance="100", owner_id=OTHER_ORG_ID,
        ),
        UUID("00000000-0000-0000-0000-000000000080"): ResourceAccount(
            resource_id=CURRENCY, balance="-10", owner_id=None, account_type="issuance_source",
        ),
    }
    state = WorldState(
        current_tick=11,
        player_id=ACTOR_ID,
        entities={
            ACTOR_ID: WorldEntity(id=ACTOR_ID, kind="character", name="Treasurer"),
            ORG_ID: organization,
            OTHER_ORG_ID: WorldEntity(id=OTHER_ORG_ID, kind="organization", name="Other owner"),
        },
        resources=accounts,
    )
    return state, other_treasury_id


def _event(state, destination_id, allocations, *, amount_minor=1, actor_id=ACTOR_ID, command_id=None):
    command_id = command_id or uuid4()
    return InstitutionalTreasuryMobilizedEvent(
        event_id=institutional_treasury_mobilized_event_id(command_id, RULESET, ORG_ID),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        actor_id=actor_id,
        organization_id=ORG_ID,
        ruleset_version=RULESET,
        currency_resource_id=CURRENCY,
        amount_minor=amount_minor,
        destination_account_id=destination_id,
        allocations=allocations,
        visibility={"policy": "author_only"},
    )


def test_plan_and_apply_use_stable_fractional_reserve_allocations_without_mutation():
    state, other_treasury_id = _world()
    before = state.model_dump(mode="json")

    destination_id, allocations = plan_reserve_mobilization(
        state,
        organization_id=ORG_ID,
        actor_id=ACTOR_ID,
        currency_resource_id=CURRENCY,
        amount_minor=1,
    )

    assert destination_id == DEST_ID
    assert allocations == (
        InstitutionalTreasuryReserveAllocation(account_id=RESERVE_IDS[0], quantity=Decimal("0.75")),
        InstitutionalTreasuryReserveAllocation(account_id=RESERVE_IDS[1], quantity=Decimal("0.25")),
    )
    event = _event(state, destination_id, allocations)
    updated = apply_reserve_mobilization(state, event)
    assert len(updated) == len(state.resources)
    assert updated[DEST_ID].balance == Decimal("11.25")
    assert updated[RESERVE_IDS[0]].balance == Decimal("0")
    assert updated[RESERVE_IDS[1]].balance == Decimal("0.5")
    assert updated[RESERVE_IDS[2]].balance == Decimal("0.5")
    assert updated[other_treasury_id] == state.resources[other_treasury_id]
    assert updated[UUID("00000000-0000-0000-0000-000000000060")] == state.resources[
        UUID("00000000-0000-0000-0000-000000000060")
    ]
    assert state.model_dump(mode="json") == before


def test_apply_rejects_a_valid_but_noncanonical_allocation_or_unauthorized_actor():
    state, _ = _world()
    destination_id, allocations = plan_reserve_mobilization(
        state, organization_id=ORG_ID, actor_id=ACTOR_ID,
        currency_resource_id=CURRENCY, amount_minor=1,
    )
    noncanonical = _event(
        state,
        destination_id,
        (
            InstitutionalTreasuryReserveAllocation(account_id=RESERVE_IDS[0], quantity=Decimal("0.5")),
            InstitutionalTreasuryReserveAllocation(account_id=RESERVE_IDS[1], quantity=Decimal("0.5")),
        ),
    )
    with pytest.raises(InvalidEvent, match="deterministic allocation"):
        apply_reserve_mobilization(state, noncanonical)

    unauthorized = _event(state, destination_id, allocations, actor_id=uuid4())
    with pytest.raises(InstitutionalCreditError) as error:
        apply_reserve_mobilization(state, unauthorized)
    assert error.value.code == "signer_unavailable"


def test_plan_never_spends_treasury_other_resources_other_owners_or_system_credit():
    state, _ = _world(reserve_balances=("0", "0", "0"), destination_balance="100")

    with pytest.raises(InstitutionalCreditError) as error:
        plan_reserve_mobilization(
            state, organization_id=ORG_ID, actor_id=ACTOR_ID,
            currency_resource_id=CURRENCY, amount_minor=1,
        )
    assert error.value.code == "insufficient_reserves"

    for amount in (True, 0, -1, 10**30):
        with pytest.raises(InstitutionalCreditError) as invalid:
            plan_reserve_mobilization(
                state, organization_id=ORG_ID, actor_id=ACTOR_ID,
                currency_resource_id=CURRENCY, amount_minor=amount,
            )
        assert invalid.value.code == "invalid_amount"


def test_destination_overflow_is_rejected_before_any_result_is_returned():
    maximum = "9" * 30
    state, _ = _world(destination_balance=maximum)
    with pytest.raises(InstitutionalCreditError) as error:
        plan_reserve_mobilization(
            state, organization_id=ORG_ID, actor_id=ACTOR_ID,
            currency_resource_id=CURRENCY, amount_minor=1,
        )
    assert error.value.code == "reserve_overflow"


def test_event_preserves_high_precision_sum_and_rejects_invalid_allocations():
    command_id = uuid4()
    high_amount = 123456789012345678901234567891
    first = InstitutionalTreasuryReserveAllocation(
        account_id=uuid4(), quantity=Decimal("123456789012345678901234567890.1"),
    )
    second = InstitutionalTreasuryReserveAllocation(account_id=uuid4(), quantity=Decimal("0.9"))
    event = InstitutionalTreasuryMobilizedEvent(
        event_id=institutional_treasury_mobilized_event_id(command_id, RULESET, ORG_ID),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        actor_id=ACTOR_ID,
        organization_id=ORG_ID,
        ruleset_version=RULESET,
        currency_resource_id=CURRENCY,
        amount_minor=high_amount,
        destination_account_id=uuid4(),
        allocations=(first, second),
        visibility={"policy": "author_only"},
    )
    assert event.amount_minor == high_amount

    with pytest.raises(ValidationError):
        InstitutionalTreasuryReserveAllocation(account_id=uuid4(), quantity=True)
    with pytest.raises(ValidationError):
        InstitutionalTreasuryReserveAllocation(account_id=uuid4(), quantity=0)
    with pytest.raises(ValidationError):
        InstitutionalTreasuryMobilizedEvent(
            event_id=event.event_id,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=command_id,
            actor_id=ACTOR_ID,
            organization_id=ORG_ID,
            ruleset_version=RULESET,
            currency_resource_id=CURRENCY,
            amount_minor=True,
            destination_account_id=uuid4(),
            allocations=(first, second),
        )


def test_batch_validator_binds_command_scope_and_payload_but_allows_scoped_restore():
    state, _ = _world()
    destination_id, allocations = plan_reserve_mobilization(
        state, organization_id=ORG_ID, actor_id=ACTOR_ID,
        currency_resource_id=CURRENCY, amount_minor=1,
    )
    event = _event(state, destination_id, allocations)
    payload = institutional_treasury_mobilized_command_payload(event)
    scope = {
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": event.command_id,
        "actor_id": ACTOR_ID,
        "ruleset_version": RULESET,
    }

    validate_reserve_mobilization_batch(
        [event], **scope, command_type=INSTITUTIONAL_TREASURY_RESERVE_COMMAND, command_payload=payload,
    )
    validate_reserve_mobilization_batch([event], **scope, command_type=None, command_payload=None)

    with pytest.raises(InvalidEvent, match="command proposal"):
        validate_reserve_mobilization_batch(
            [event], **scope, command_type=INSTITUTIONAL_TREASURY_RESERVE_COMMAND,
            command_payload=payload | {"amount_minor": 2},
        )
    with pytest.raises(InvalidEvent, match="scoped command"):
        validate_reserve_mobilization_batch(
            [event], **(scope | {"actor_id": uuid4()}), command_type=None,
        )
    with pytest.raises(InvalidEvent, match="single scoped command"):
        validate_reserve_mobilization_batch(
            [event, event], **scope, command_type=None,
        )
    with pytest.raises(InvalidEvent, match="typed mobilization event"):
        validate_reserve_mobilization_batch(
            [], **scope, command_type=INSTITUTIONAL_TREASURY_RESERVE_COMMAND,
        )

