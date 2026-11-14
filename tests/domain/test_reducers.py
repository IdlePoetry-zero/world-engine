from decimal import Decimal
from uuid import uuid4

import pytest

from world_engine.domain.errors import InsufficientResources, InvalidEvent
from world_engine.domain.events import parse_event
from world_engine.domain.models import (
    EntityCreatedEvent,
    EntityFieldChangedEvent,
    ResourceAccount,
    ResourceTransferredEvent,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.domain.reducers import apply_event, ledger_postings, state_hash, validate_ledger_postings


def test_entity_create_and_allowlisted_field_change_are_replayable() -> None:
    place_id = uuid4()
    character_id = uuid4()
    state = WorldState(
        entities={place_id: WorldEntity(id=place_id, kind="place", name="Harbor")},
    )

    state = apply_event(
        state,
        EntityCreatedEvent(
            entity=WorldEntity(id=character_id, kind="character", name="Mira", location_id=place_id)
        ),
    )
    changed = EntityFieldChangedEvent(
        entity_id=character_id,
        field_name="name",
        value="Mira Chen",
    )
    state = apply_event(state, changed)

    assert state.entities[character_id].name == "Mira Chen"
    assert state.entities[character_id].location_id == place_id
    assert state_hash(state) == state_hash(WorldState.model_validate(state.model_dump(mode="json")))


def test_unregistered_or_generic_patch_events_are_rejected() -> None:
    with pytest.raises(InvalidEvent, match="Unregistered event type"):
        parse_event({"type": "core.state_patch.v1", "patch": {"entities": {}}})

    place_id = uuid4()
    state = WorldState(entities={place_id: WorldEntity(id=place_id, kind="place")})
    with pytest.raises(InvalidEvent):
        apply_event(state, {"type": "core.entity_created.v1", "patch": {"current_tick": 100}})


def test_entity_location_must_resolve_and_dead_entities_are_terminal() -> None:
    character_id = uuid4()
    missing_place_id = uuid4()
    state = WorldState()

    with pytest.raises(InvalidEvent, match="does not exist or is not a location"):
        apply_event(
            state,
            EntityCreatedEvent(
                entity=WorldEntity(id=character_id, kind="character", location_id=missing_place_id)
            ),
        )

    place_id = uuid4()
    state = WorldState(entities={place_id: WorldEntity(id=place_id, kind="place")})
    state = apply_event(
        state,
        EntityCreatedEvent(
            entity=WorldEntity(id=character_id, kind="character", location_id=place_id)
        ),
    )
    state = apply_event(
        state,
        EntityFieldChangedEvent(entity_id=character_id, field_name="status", value="dead"),
    )
    with pytest.raises(InvalidEvent, match="state is terminal"):
        apply_event(
            state,
            EntityFieldChangedEvent(entity_id=character_id, field_name="name", value="Returned"),
        )


def test_resource_transfer_updates_balances_and_emits_balanced_ledger() -> None:
    source_id, destination_id = uuid4(), uuid4()
    state = WorldState(
        resources={
            source_id: ResourceAccount(resource_id="core.silver", balance="5.25"),
            destination_id: ResourceAccount(resource_id="core.silver", balance="1"),
        }
    )
    event = ResourceTransferredEvent(
        from_account_id=source_id,
        to_account_id=destination_id,
        resource_id="core.silver",
        quantity="2.25",
    )

    changed = apply_event(state, event)
    postings = ledger_postings(event)
    validate_ledger_postings(postings)

    assert changed.resources[source_id].balance == Decimal("3.00")
    assert changed.resources[destination_id].balance == Decimal("3.25")
    assert sum((posting.delta for posting in postings), Decimal("0")) == 0


def test_resource_transfer_rejects_overdraft_and_float_amounts() -> None:
    source_id, destination_id = uuid4(), uuid4()
    state = WorldState(
        resources={
            source_id: ResourceAccount(resource_id="core.silver", balance="1"),
            destination_id: ResourceAccount(resource_id="core.silver", balance="0"),
        }
    )
    event = ResourceTransferredEvent(
        from_account_id=source_id,
        to_account_id=destination_id,
        resource_id="core.silver",
        quantity="2",
    )
    with pytest.raises(InsufficientResources):
        apply_event(state, event)

    with pytest.raises(ValueError, match="binary floats"):
        ResourceTransferredEvent(
            from_account_id=source_id,
            to_account_id=destination_id,
            resource_id="core.silver",
            quantity=0.1,
        )


def test_decimal_bounds_preserve_small_transfers_without_context_rounding() -> None:
    source_id, destination_id = uuid4(), uuid4()
    state = WorldState(
        resources={
            source_id: ResourceAccount(
                resource_id="core.silver",
                balance="999999999999999999999999999999.999999",
            ),
            destination_id: ResourceAccount(resource_id="core.silver", balance="0"),
        }
    )
    event = ResourceTransferredEvent(
        from_account_id=source_id,
        to_account_id=destination_id,
        resource_id="core.silver",
        quantity="0.000001",
    )

    changed = apply_event(state, event)

    assert changed.resources[source_id].balance == Decimal("999999999999999999999999999999.999998")
    assert changed.resources[destination_id].balance == Decimal("0.000001")
    with pytest.raises(ValueError, match="30 integer digits"):
        ResourceAccount(resource_id="core.silver", balance="1000000000000000000000000000000")


def test_large_ledger_posting_keeps_full_precision_under_default_decimal_context() -> None:
    source_id, destination_id = uuid4(), uuid4()
    event = ResourceTransferredEvent(
        from_account_id=source_id,
        to_account_id=destination_id,
        resource_id="core.silver",
        quantity="999999999999999999999999999999.999999",
    )

    postings = ledger_postings(event)
    validate_ledger_postings(postings)

    assert postings[0].delta == Decimal("-999999999999999999999999999999.999999")
    assert postings[1].delta == Decimal("999999999999999999999999999999.999999")


def test_world_state_rejects_player_owner_and_location_containment_cycles() -> None:
    player_id, place_id, account_id = uuid4(), uuid4(), uuid4()
    with pytest.raises(ValueError, match="player_id"):
        WorldState(player_id=player_id)

    with pytest.raises(ValueError, match="location or containment graph contains a cycle"):
        WorldState(
            entities={
                place_id: WorldEntity(id=place_id, kind="place", components={"parent_id": str(player_id)}),
                player_id: WorldEntity(id=player_id, kind="character", location_id=place_id),
            }
        )

    with pytest.raises(ValueError, match="missing owner entity"):
        WorldState(resources={account_id: ResourceAccount(resource_id="core.silver", balance="1", owner_id=player_id)})

    with pytest.raises(ValueError, match="finite number"):
        WorldEntity(id=uuid4(), kind="character", components={"score": float("nan")})


def test_time_cannot_move_backwards() -> None:
    state = WorldState(current_tick=42)
    advanced = apply_event(state, TimeAdvancedEvent(to_tick=43))
    assert advanced.current_tick == 43

    with pytest.raises(InvalidEvent, match="monotonically"):
        apply_event(advanced, TimeAdvancedEvent(to_tick=43))
