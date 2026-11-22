from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.errors import DomainError
from world_engine.domain.shipment_relief import ShipmentReliefAllocation, ShipmentReliefPlan
from world_engine.domain.shipment_relief_events import (
    ShipmentReliefDistributedEvent,
    shipment_relief_event_id,
    shipment_relief_visibility,
)
from world_engine.rules import RuleContext, RuleEngine, parse_action
from world_engine.rules import engine as rules_engine


ACTOR_ID = UUID("40000000-0000-4000-8000-000000000004")
ORGANIZATION_ID = UUID("50000000-0000-4000-8000-000000000005")
SHIPMENT_ID = UUID("70000000-0000-4000-8000-000000000007")
RECIPIENT_ID = UUID("80000000-0000-4000-8000-000000000008")
DEPOT_ID = UUID("90000000-0000-4000-8000-000000000009")
SOURCE_ACCOUNT_ID = UUID("a0000000-0000-4000-8000-00000000000a")
RECIPIENT_ACCOUNT_ID = UUID("b0000000-0000-4000-8000-00000000000b")
TRANSIT_LOT_ID = UUID("c0000000-0000-4000-8000-00000000000c")
DESTINATION_LOT_ID = UUID("d0000000-0000-4000-8000-00000000000d")


def _context() -> RuleContext:
    return RuleContext(
        world_id=UUID("10000000-0000-4000-8000-000000000001"),
        branch_id=UUID("20000000-0000-4000-8000-000000000002"),
        command_id=UUID("30000000-0000-4000-8000-000000000003"),
        actor_id=ACTOR_ID,
        ruleset_version="shipment-relief.v1",
    )


def _plan(*, actor_id: UUID = ACTOR_ID) -> ShipmentReliefPlan:
    quantity = Decimal("2.500000")
    return ShipmentReliefPlan(
        actor_id=actor_id,
        organization_id=ORGANIZATION_ID,
        region_id=DEPOT_ID,
        shipment_id=SHIPMENT_ID,
        recipient_id=RECIPIENT_ID,
        depot_id=DEPOT_ID,
        tick=100,
        resource_id="core.food",
        source_account_id=SOURCE_ACCOUNT_ID,
        recipient_account_id=RECIPIENT_ACCOUNT_ID,
        quantity=quantity,
        allocations=(ShipmentReliefAllocation(
            destination_lot_id=DESTINATION_LOT_ID,
            transit_lot_id=TRANSIT_LOT_ID,
            account_id=SOURCE_ACCOUNT_ID,
            quantity=quantity,
        ),),
        expected_source_balance=Decimal("10.000000"),
        expected_recipient_balance=Decimal("0"),
        expected_depot_inventory_revision=4,
        expected_recipient_inventory_revision=None,
        expected_shipment_revision=2,
    )


def _action(*, quantity: str = "2.500000") -> dict[str, str]:
    return {
        "kind": "distribute_shipment_relief",
        "organization_id": str(ORGANIZATION_ID),
        "shipment_id": str(SHIPMENT_ID),
        "recipient_id": str(RECIPIENT_ID),
        "quantity": quantity,
    }


def test_relief_intent_is_closed_and_normalizes_quantity_for_command_payload() -> None:
    parsed = parse_action(_action())

    assert parsed.kind == "distribute_shipment_relief"
    assert parsed.quantity == Decimal("2.500000")
    assert parsed.model_dump(mode="json") == _action(quantity="2.5")
    with pytest.raises(ValueError, match="registered schema"):
        parse_action({**_action(), "extra": "not accepted"})
    with pytest.raises(ValueError, match="registered schema"):
        parse_action({**_action(), "quantity": 0.5})
    with pytest.raises(ValueError, match="registered schema"):
        parse_action({**_action(), "quantity": "0"})


def test_relief_rule_emits_one_private_command_bound_event_without_advancing_time(monkeypatch) -> None:
    plan = _plan()
    state = WorldState(
        current_tick=100,
        entities={ACTOR_ID: WorldEntity(id=ACTOR_ID, kind="character", name="Player")},
        player_id=ACTOR_ID,
    )
    context = _context()
    received: dict[str, object] = {}

    def fake_plan(
        state_arg: WorldState,
        *,
        actor_id: UUID,
        organization_id: UUID,
        shipment_id: UUID,
        recipient_id: UUID,
        quantity: Decimal,
    ) -> ShipmentReliefPlan:
        received.update({
            "state": state_arg,
            "actor_id": actor_id,
            "organization_id": organization_id,
            "shipment_id": shipment_id,
            "recipient_id": recipient_id,
            "quantity": quantity,
        })
        return plan

    monkeypatch.setattr(rules_engine, "plan_shipment_relief", fake_plan)
    # This test isolates the rule's event envelope. Reducer replanning is
    # exercised by the domain tests against a fully registered shipment state.
    monkeypatch.setattr(rules_engine, "apply_event", lambda current, _event: current)
    result = RuleEngine().evaluate(state, _action(), context)

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "shipment_relief_distributed"
    assert result.outcome.details == {}
    assert result.elapsed_ticks == 0
    assert received == {
        "state": state,
        "actor_id": ACTOR_ID,
        "organization_id": ORGANIZATION_ID,
        "shipment_id": SHIPMENT_ID,
        "recipient_id": RECIPIENT_ID,
        "quantity": Decimal("2.500000"),
    }
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, ShipmentReliefDistributedEvent)
    assert event.event_id == shipment_relief_event_id(
        context.command_id, context.ruleset_version, SHIPMENT_ID, RECIPIENT_ID,
    )
    assert (event.world_id, event.branch_id, event.command_id, event.actor_id) == (
        context.world_id, context.branch_id, context.command_id, ACTOR_ID,
    )
    assert event.ruleset_version == context.ruleset_version
    assert event.plan == plan
    assert event.visibility == shipment_relief_visibility(ACTOR_ID, RECIPIENT_ID)
    assert event.visibility["subject_ids"] == sorted((str(ACTOR_ID), str(RECIPIENT_ID)))


def test_unavailable_relief_plan_rejects_without_event_or_details(monkeypatch) -> None:
    state = WorldState(
        entities={ACTOR_ID: WorldEntity(id=ACTOR_ID, kind="character", name="Player")},
        player_id=ACTOR_ID,
    )

    def unavailable(*_args: object, **_kwargs: object) -> ShipmentReliefPlan:
        raise DomainError("private planner detail")

    monkeypatch.setattr(rules_engine, "plan_shipment_relief", unavailable)
    result = RuleEngine().evaluate(state, _action(), _context())

    assert result.outcome.status == "rejected"
    assert result.outcome.code == "shipment_relief_unavailable"
    assert result.events == []
    assert "private planner detail" not in result.outcome.message
    assert str(ACTOR_ID) not in str(result.outcome.details)
