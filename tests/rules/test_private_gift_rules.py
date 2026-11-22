from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from world_engine.domain.components import InventoryComponent, InventoryLot
from world_engine.domain.models import ResourceAccount, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.private_gifts import PrivateGiftChangedEvent
from world_engine.domain.reducers import apply_event
from world_engine.rules import RuleContext, RuleEngine, parse_action


WORLD_ID = UUID(int=970)
BRANCH_ID = UUID(int=971)
RULESET = "private-gift-rules.v1"
PLACE_ID = UUID(int=972)
GIVER_ID = UUID(int=973)
RECIPIENT_ID = UUID(int=974)
SOURCE_ACCOUNT_ID = UUID(int=975)
RECIPIENT_ACCOUNT_ID = UUID(int=976)
SOURCE_LOT_ID = UUID(int=977)
RESOURCE_ID = "core.grain"


def _context(actor_id: UUID = GIVER_ID, command_id: int = 980) -> RuleContext:
    return RuleContext(
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=UUID(int=command_id),
        actor_id=actor_id,
        ruleset_version=RULESET,
    )


def _state(*, colocated: bool = True, tick: int = 100) -> WorldState:
    giver_lots = [
        InventoryLot(
            lot_id=SOURCE_LOT_ID,
            account_id=SOURCE_ACCOUNT_ID,
            resource_id=RESOURCE_ID,
            quantity="2",
            quality=83,
            expiry_tick=500,
        )
    ]
    entities = {
        PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Mill"),
        GIVER_ID: WorldEntity(
            id=GIVER_ID,
            kind="character",
            name="Ari",
            location_id=PLACE_ID,
            components={"inventory": InventoryComponent(lots=giver_lots).model_dump(mode="json")},
        ),
        RECIPIENT_ID: WorldEntity(
            id=RECIPIENT_ID,
            kind="character",
            name="Bea",
            location_id=PLACE_ID if colocated else UUID(int=978),
            components={"inventory": InventoryComponent().model_dump(mode="json")},
        ),
    }
    if not colocated:
        other_place_id = UUID(int=978)
        entities[other_place_id] = WorldEntity(id=other_place_id, kind="place", name="Other place")
    return WorldState(
        current_tick=tick,
        entities=entities,
        resources={
            SOURCE_ACCOUNT_ID: ResourceAccount(
                resource_id=RESOURCE_ID,
                balance="2",
                owner_id=GIVER_ID,
            ),
            RECIPIENT_ACCOUNT_ID: ResourceAccount(
                resource_id=RESOURCE_ID,
                balance="0",
                owner_id=RECIPIENT_ID,
            ),
        },
    )


def _replay(state: WorldState, events) -> WorldState:
    current = state
    for event in events:
        current = apply_event(current, event)
    return current


def _offer(state: WorldState) -> tuple[WorldState, PrivateGiftChangedEvent]:
    result = RuleEngine().evaluate(
        state,
        {
            "kind": "offer_private_gift",
            "recipient_id": str(RECIPIENT_ID),
            "resource_id": RESOURCE_ID,
            "quantity": "1.25",
            "expires_at_tick": 200,
        },
        _context(command_id=980),
    )
    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "private_gift_offered"
    assert result.elapsed_ticks == 0
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, PrivateGiftChangedEvent)
    assert event.operation == "offer"
    return _replay(state, result.events), event


def test_private_gift_action_contracts_are_closed_and_typed() -> None:
    offer = parse_action({
        "kind": "offer_private_gift",
        "recipient_id": str(RECIPIENT_ID),
        "resource_id": RESOURCE_ID,
        "quantity": "1.25",
    })
    assert offer.kind == "offer_private_gift"
    assert offer.quantity == Decimal("1.25")
    assert offer.expires_at_tick is None

    response = parse_action({
        "kind": "resolve_private_gift",
        "gift_id": str(UUID(int=979)),
        "expected_revision": 1,
        "operation": "accept",
    })
    assert response.kind == "resolve_private_gift"
    assert response.operation == "accept"

    for malformed in (
        {
            "kind": "offer_private_gift",
            "recipient_id": str(RECIPIENT_ID),
            "resource_id": RESOURCE_ID,
            "quantity": 1.25,
        },
        {
            "kind": "offer_private_gift",
            "recipient_id": str(RECIPIENT_ID),
            "resource_id": RESOURCE_ID,
            "quantity": "0",
        },
        {
            "kind": "resolve_private_gift",
            "gift_id": str(UUID(int=979)),
            "expected_revision": "1",
            "operation": "accept",
        },
        {
            "kind": "resolve_private_gift",
            "gift_id": str(UUID(int=979)),
            "expected_revision": 1,
            "operation": "counter",
        },
    ):
        with pytest.raises(ValueError, match="registered schema"):
            parse_action(malformed)


def test_offer_is_one_zero_time_event_with_only_safe_receipt_details() -> None:
    state = _state()
    result = RuleEngine().evaluate(
        state,
        {
            "kind": "offer_private_gift",
            "recipient_id": str(RECIPIENT_ID),
            "resource_id": RESOURCE_ID,
            "quantity": "1.25",
            "expires_at_tick": 200,
        },
        _context(command_id=981),
    )

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "private_gift_offered"
    assert result.elapsed_ticks == 0
    assert len(result.events) == 1
    assert not any(isinstance(event, TimeAdvancedEvent) for event in result.events)
    event = result.events[0]
    assert isinstance(event, PrivateGiftChangedEvent)
    assert event.operation == "offer"
    assert event.offer.status == "pending"
    assert event.offer.giver_id == GIVER_ID
    assert event.offer.recipient_id == RECIPIENT_ID
    assert event.offer.resource_id == RESOURCE_ID
    assert event.offer.quantity == Decimal("1.25")
    assert result.outcome.details == {
        "gift_id": str(event.offer.gift_id),
        "status": "pending",
        "resource_id": RESOURCE_ID,
        "quantity": "1.25",
    }
    after = _replay(state, result.events)
    assert after.current_tick == state.current_tick
    assert after.resources[SOURCE_ACCOUNT_ID].balance == Decimal("2")
    assert after.private_gifts == [event.offer]


@pytest.mark.parametrize(
    ("operation", "actor_id", "expected_status", "expected_code"),
    [
        ("accept", RECIPIENT_ID, "accepted", "private_gift_accepted"),
        ("decline", RECIPIENT_ID, "declined", "private_gift_declined"),
        ("withdraw", GIVER_ID, "withdrawn", "private_gift_withdrawn"),
    ],
)
def test_private_gift_resolution_requires_an_explicit_correct_actor_and_time_does_not_advance(
    operation: str,
    actor_id: UUID,
    expected_status: str,
    expected_code: str,
) -> None:
    offered_state, offer_event = _offer(_state())
    gift = offer_event.offer
    result = RuleEngine().evaluate(
        offered_state,
        {
            "kind": "resolve_private_gift",
            "gift_id": str(gift.gift_id),
            "expected_revision": gift.revision,
            "operation": operation,
        },
        _context(actor_id=actor_id, command_id=982),
    )

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == expected_code
    assert result.elapsed_ticks == 0
    assert len(result.events) == 1
    assert not any(isinstance(event, TimeAdvancedEvent) for event in result.events)
    event = result.events[0]
    assert isinstance(event, PrivateGiftChangedEvent)
    assert event.operation == operation
    assert event.offer.status == expected_status
    assert set(result.outcome.details) == {"gift_id", "status", "resource_id", "quantity"}
    assert "source_account_id" not in result.outcome.details
    assert "destination_account_id" not in result.outcome.details
    assert _replay(offered_state, result.events).private_gifts[0].status == expected_status


def test_private_gift_rule_rejection_is_stable_and_does_not_return_domain_details() -> None:
    state = _state(colocated=False)
    result = RuleEngine().evaluate(
        state,
        {
            "kind": "offer_private_gift",
            "recipient_id": str(RECIPIENT_ID),
            "resource_id": RESOURCE_ID,
            "quantity": "1",
        },
        _context(command_id=983),
    )

    assert result.outcome.status == "rejected"
    assert result.outcome.code == "private_gift_unavailable"
    assert result.outcome.details == {}
    assert result.events == []
    assert result.elapsed_ticks == 0
