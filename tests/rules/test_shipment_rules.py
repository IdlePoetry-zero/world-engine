from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4, uuid5

import pytest

from world_engine.domain.components import PlaceRoute, ScheduledEventRecord
from world_engine.domain.shipment import ShipmentComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import (
    ShipmentArrivalDeferredEvent,
    ShipmentArrivalDeliveredEvent,
    ShipmentDispatchedEvent,
    ShipmentReceiptAcceptedEvent,
    ShipmentSpoilageResolvedEvent,
)
from world_engine.domain.models import ResourceAccount, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, ledger_postings, state_json
from world_engine.domain.shipment_validation import validate_shipment_spoilage_batch
from world_engine.rules import RuleContext, RuleEngine


def _context(actor_id: UUID) -> RuleContext:
    return RuleContext(
        world_id=UUID(int=1),
        branch_id=UUID(int=2),
        command_id=uuid4(),
        actor_id=actor_id,
        ruleset_version="1.0.0",
    )


def _scenario(*, receiver_is_person: bool = False):
    origin_id, destination_id, sender_id = (uuid4() for _ in range(3))
    destination_owner_id = uuid4() if receiver_is_person else destination_id
    route_id, source_account_id, destination_account_id, source_lot_id, second_source_lot_id = (uuid4() for _ in range(5))
    route = PlaceRoute(
        route_id=route_id,
        from_place_id=origin_id,
        to_place_id=destination_id,
        travel_ticks=60,
        capacity=1,
    )
    origin = WorldEntity(
        id=origin_id,
        kind="place",
        name="Origin",
        components={"place": {"place_type": "settlement", "routes": [route.model_dump(mode="json")] }},
    )
    destination = WorldEntity(
        id=destination_id,
        kind="place",
        name="Destination",
        components={"place": {"place_type": "settlement"}, "inventory": {"lots": []}},
    )
    sender = WorldEntity(
        id=sender_id,
        kind="character",
        name="Sender",
        location_id=origin_id,
        components={
            "capabilities": {"grants": ["world.travel"], "restrictions": []},
            "inventory": {"lots": [
                {
                    "lot_id": source_lot_id,
                    "account_id": source_account_id,
                    "resource_id": "core.food",
                    "quantity": "1",
                    "expiry_tick": 120,
                },
                {
                    "lot_id": second_source_lot_id,
                    "account_id": source_account_id,
                    "resource_id": "core.food",
                    "quantity": "2",
                    "expiry_tick": 180,
                },
            ]},
        },
    )
    if receiver_is_person:
        owner = WorldEntity(
            id=destination_owner_id,
            kind="character",
            name="Receiver",
            location_id=destination_id,
            components={"inventory": {"lots": []}},
        )
    else:
        owner = destination
    state = WorldState(
        entities={
            origin_id: origin,
            destination_id: destination,
            sender_id: sender,
            destination_owner_id: owner,
        },
        resources={
            source_account_id: ResourceAccount(
                resource_id="core.food", balance="3", owner_id=sender_id,
            ),
            destination_account_id: ResourceAccount(
                resource_id="core.food", balance="0", owner_id=destination_owner_id,
            ),
        },
        player_id=sender_id,
    )
    proposal = {
            "kind": "dispatch_shipment",
            "route_id": str(route_id),
            "source_owner_id": str(sender_id),
            "destination_owner_id": str(destination_owner_id),
            "resource_id": "core.food",
            "quantity": "2",
            "source_account_id": str(source_account_id),
            "destination_account_id": str(destination_account_id),
        }
    context = _context(sender_id)
    result = RuleEngine().evaluate(state, proposal, context)
    event = next(item for item in result.events if isinstance(item, ShipmentDispatchedEvent))
    dispatched = apply_event(state, event)
    return state, dispatched, event, sender_id, destination_id, destination_owner_id, destination_account_id, context, proposal


def test_dispatch_and_due_arrival_move_exact_fifo_custody_and_complete_schedule():
    _initial, dispatched, dispatch, _sender, destination_id, owner_id, destination_account_id, _context_value, _proposal = _scenario()
    shipment = dispatched.entities[dispatch.shipment_id]
    assert shipment.location_id is None
    assert shipment.components["shipment"]["status"] == "in_transit"
    assert dispatched.resources[dispatch.source_account_id].balance == Decimal("1")
    assert dispatched.resources[dispatch.transit_account_id].balance == Decimal("2")
    assert dispatched.scheduled_events[0].status == "queued"

    arrived = apply_event(dispatched, TimeAdvancedEvent(to_tick=dispatch.arrival_tick))
    schedule = arrived.scheduled_events[0]
    resolution = RuleEngine().resolve_scheduled(arrived, schedule, _context(dispatch.sender_id))
    event = next(item for item in resolution.events if isinstance(item, ShipmentArrivalDeliveredEvent))
    assert resolution.events == [event]
    delivered = apply_event(arrived, event)

    shipment = delivered.entities[dispatch.shipment_id]
    assert shipment.location_id == destination_id
    assert shipment.components["shipment"]["status"] == "delivered"
    assert shipment.components["shipment"]["arrived_tick"] == dispatch.arrival_tick
    assert delivered.scheduled_events[0].status == "completed"
    assert delivered.resources[dispatch.transit_account_id].balance == Decimal("0")
    assert delivered.resources[destination_account_id].balance == Decimal("2")
    delivered_lots = delivered.entities[owner_id].components["inventory"]["lots"]
    assert [(lot["quantity"], lot["expiry_tick"]) for lot in delivered_lots] == [("1", 120), ("1", 180)]


def test_unavailable_receiver_defers_arrival_then_accepts_without_losing_transit_custody():
    _initial, state, dispatch, _sender, destination_id, receiver_id, destination_account_id, _context_value, _proposal = _scenario(receiver_is_person=True)
    receiver = state.entities[receiver_id]
    elsewhere = WorldEntity(id=uuid4(), kind="place", name="Elsewhere", components={"place": {"place_type": "settlement"}})
    moved_entities = dict(state.entities)
    moved_entities[elsewhere.id] = elsewhere
    moved_entities[receiver_id] = receiver.model_copy(update={"location_id": elsewhere.id})
    state = WorldState.model_validate({**state.model_dump(mode="json"), "entities": moved_entities})

    arrived = apply_event(state, TimeAdvancedEvent(to_tick=dispatch.arrival_tick))
    deferred_result = RuleEngine().resolve_scheduled(arrived, arrived.scheduled_events[0], _context(dispatch.sender_id))
    deferred_event = next(item for item in deferred_result.events if isinstance(item, ShipmentArrivalDeferredEvent))
    awaiting = apply_event(arrived, deferred_event)
    shipment = awaiting.entities[dispatch.shipment_id]
    assert shipment.location_id == destination_id
    assert shipment.components["shipment"]["status"] == "awaiting_receipt"
    assert awaiting.resources[dispatch.transit_account_id].balance == Decimal("2")
    assert awaiting.resources[destination_account_id].balance == Decimal("0")
    assert awaiting.scheduled_events[0].status == "completed"

    receiver = awaiting.entities[receiver_id]
    moved_entities = dict(awaiting.entities)
    moved_entities[receiver_id] = receiver.model_copy(update={"location_id": destination_id})
    awaiting = WorldState.model_validate({**awaiting.model_dump(mode="json"), "entities": moved_entities})
    receipt = RuleEngine().evaluate(
        awaiting,
        {"kind": "receive_shipment", "shipment_id": str(dispatch.shipment_id)},
        _context(receiver_id),
    )
    accepted = next(item for item in receipt.events if isinstance(item, ShipmentReceiptAcceptedEvent))
    delivered = apply_event(awaiting, accepted)
    assert delivered.entities[dispatch.shipment_id].components["shipment"]["status"] == "delivered"
    assert delivered.resources[dispatch.transit_account_id].balance == Decimal("0")
    assert delivered.resources[destination_account_id].balance == Decimal("2")


def test_route_capacity_is_one_slot_per_manifest_and_dispatch_is_not_replayable_twice():
    _initial, state, dispatch, sender_id, _destination_id, receiver_id, _destination_account_id, _context_value, _proposal = _scenario()
    with pytest.raises(InvalidEvent, match="already exists"):
        apply_event(state, dispatch)

    blocked = RuleEngine().evaluate(
        state,
        {
            "kind": "dispatch_shipment",
            "route_id": str(dispatch.route_id),
            "source_owner_id": str(sender_id),
            "destination_owner_id": str(receiver_id),
            "resource_id": "core.food",
            "quantity": "1",
            "source_account_id": str(dispatch.source_account_id),
            "destination_account_id": str(dispatch.destination_account_id),
        },
        _context(sender_id),
    )
    assert blocked.outcome.status == "rejected"
    assert blocked.outcome.code == "shipment_route_capacity_full"
    assert blocked.events == []


def test_retry_event_identity_is_stable_and_fork_command_gets_independent_shipment():
    initial, _dispatched, original, sender_id, _destination_id, _receiver_id, _account_id, context, proposal = _scenario()
    retry = RuleEngine().evaluate(initial, proposal, context)
    retry_event = next(item for item in retry.events if isinstance(item, ShipmentDispatchedEvent))
    assert retry_event.event_id == original.event_id
    assert retry_event.shipment_id == original.shipment_id
    assert retry_event.arrival_schedule_id == original.arrival_schedule_id
    assert [item.transit_lot_id for item in retry_event.lot_transfers] == [
        item.transit_lot_id for item in original.lot_transfers
    ]

    fork_context = context.model_copy(update={"branch_id": uuid4(), "command_id": uuid4()})
    fork_result = RuleEngine().evaluate(initial, proposal, fork_context)
    fork_event = next(item for item in fork_result.events if isinstance(item, ShipmentDispatchedEvent))
    assert fork_event.event_id != original.event_id
    assert fork_event.shipment_id != original.shipment_id
    assert fork_event.arrival_schedule_id != original.arrival_schedule_id


def test_legacy_shipment_replay_omits_expiry_defaults_and_keeps_delivered_custody_empty():
    initial, _v2_state, dispatched_v2, sender_id, _destination_id, _receiver, _account_id, context, _proposal = _scenario()
    legacy_dispatch = dispatched_v2.model_copy(update={
        "lifecycle_version": "legacy_v1",
        "spoilage_schedule_id": None,
        "spoilage_due_tick": None,
    })
    legacy_state = apply_event(initial, legacy_dispatch)
    shipment = ShipmentComponent.model_validate(
        legacy_state.entities[legacy_dispatch.shipment_id].components["shipment"]
    )
    assert shipment.lifecycle_version == "legacy_v1"
    assert shipment.remaining_quantity == Decimal("2")
    assert len(legacy_state.scheduled_events) == 1

    at_arrival = apply_event(legacy_state, TimeAdvancedEvent(to_tick=legacy_dispatch.arrival_tick))
    arrival = RuleEngine().resolve_scheduled(
        at_arrival,
        at_arrival.scheduled_events[0],
        context.model_copy(update={"command_id": uuid4(), "actor_id": sender_id}),
    )
    delivered_event = next(item for item in arrival.events if isinstance(item, ShipmentArrivalDeliveredEvent))
    delivered = apply_event(at_arrival, delivered_event)
    delivered_shipment = ShipmentComponent.model_validate(
        delivered.entities[legacy_dispatch.shipment_id].components["shipment"]
    )
    assert delivered_shipment.status == "delivered"
    assert delivered_shipment.remaining_quantity == Decimal("0")
    assert delivered_shipment.delivered_lots == []
    serialized = state_json(delivered)
    component = serialized["entities"][str(legacy_dispatch.shipment_id)]["components"]["shipment"]
    assert "lifecycle_version" not in component
    assert "spoilage_schedule_id" not in component
    assert "spoiled_lots" not in component
    assert "delivered_lots" not in component


def test_deferred_mixed_expiry_spoils_early_lot_then_receives_remainder_and_cancels_stale_schedule():
    _initial, shipment_state, dispatch, _sender, destination_id, receiver_id, destination_account_id, base_context, _proposal = _scenario(receiver_is_person=True)
    receiver = shipment_state.entities[receiver_id]
    elsewhere = WorldEntity(id=uuid4(), kind="place", name="Elsewhere", components={"place": {"place_type": "settlement"}})
    moved_entities = dict(shipment_state.entities)
    moved_entities[elsewhere.id] = elsewhere
    moved_entities[receiver_id] = receiver.model_copy(update={"location_id": elsewhere.id})
    shipment_state = WorldState.model_validate({**shipment_state.model_dump(mode="json"), "entities": moved_entities})
    context = base_context.model_copy(update={"command_id": uuid4()})

    at_arrival = apply_event(shipment_state, TimeAdvancedEvent(to_tick=dispatch.arrival_tick))
    arrival_schedule = next(item for item in at_arrival.scheduled_events if item.id == dispatch.arrival_schedule_id)
    deferred = RuleEngine().resolve_scheduled(at_arrival, arrival_schedule, context)
    deferred_event = next(item for item in deferred.events if isinstance(item, ShipmentArrivalDeferredEvent))
    awaiting = apply_event(at_arrival, deferred_event)

    early_schedule = next(item for item in awaiting.scheduled_events if item.handler_id == "shipment.spoilage")
    at_first_expiry = apply_event(awaiting, TimeAdvancedEvent(to_tick=early_schedule.due_tick))
    expiry_context = context.model_copy(update={"command_id": uuid4()})
    spoilage = RuleEngine().resolve_scheduled(
        at_first_expiry,
        next(item for item in at_first_expiry.scheduled_events if item.id == early_schedule.id),
        expiry_context,
    )
    spoilage_event = next(item for item in spoilage.events if isinstance(item, ShipmentSpoilageResolvedEvent))
    assert sum((posting.delta for posting in ledger_postings(spoilage_event)), Decimal("0")) == 0
    validate_shipment_spoilage_batch(
        at_first_expiry,
        dispatch.sender_id,
        [spoilage_event],
        world_id=base_context.world_id,
        branch_id=base_context.branch_id,
        command_id=expiry_context.command_id,
        ruleset_version=expiry_context.ruleset_version,
        command_type="simulation.window",
        command_payload={"job_id": str(uuid4()), "next_tick": str(early_schedule.due_tick)},
    )
    spoiled = apply_event(at_first_expiry, spoilage_event)
    shipment_raw = spoiled.entities[dispatch.shipment_id].components["shipment"]
    shipment = shipment_raw
    assert shipment["status"] == "awaiting_receipt"
    assert len(shipment["cargo"][0]["lots"]) == 2
    assert [lot["quantity"] for lot in shipment["cargo"][0]["lots"]] == ["1", "1"]
    assert len(shipment["spoiled_lots"]) == 1
    assert shipment["spoiled_lots"][0]["quantity"] == "1"
    assert ShipmentComponent.model_validate(shipment).remaining_quantity == Decimal("1")
    transit_inventory = spoiled.entities[dispatch.shipment_id].components["inventory"]["lots"]
    assert [(lot["quantity"], lot["expiry_tick"]) for lot in transit_inventory] == [("1", 180)]
    assert spoiled.resources[dispatch.transit_account_id].balance == Decimal("1")
    destruction_sink_id = uuid5(
        base_context.world_id,
        "resource-destruction-sink:core.food",
    )
    destruction_sink = spoiled.resources[destruction_sink_id]
    assert destruction_sink.account_type == "destruction_sink"
    assert destruction_sink.owner_id is None
    assert destruction_sink.balance == Decimal("1")

    receiver = spoiled.entities[receiver_id]
    moved_entities = dict(spoiled.entities)
    moved_entities[receiver_id] = receiver.model_copy(update={"location_id": destination_id})
    spoiled = WorldState.model_validate({**spoiled.model_dump(mode="json"), "entities": moved_entities})
    assert spoiled.entities[receiver_id].location_id == destination_id
    assert spoiled.entities[receiver_id].kind == "character"
    assert spoiled.entities[receiver_id].status == "active"
    received = RuleEngine().evaluate(
        spoiled,
        {"kind": "receive_shipment", "shipment_id": str(dispatch.shipment_id)},
        context.model_copy(update={"actor_id": receiver_id, "command_id": uuid4()}),
    )
    assert received.outcome.status == "succeeded", received.outcome.model_dump(mode="json")
    receive_event = next(item for item in received.events if isinstance(item, ShipmentReceiptAcceptedEvent))
    assert receive_event.flows[0].quantity == Decimal("1")
    delivered = apply_event(spoiled, receive_event)
    final_shipment = delivered.entities[dispatch.shipment_id].components["shipment"]
    assert final_shipment["status"] == "delivered"
    assert len(final_shipment["spoiled_lots"]) == 1
    assert len(final_shipment["delivered_lots"]) == 1
    assert delivered.resources[dispatch.transit_account_id].balance == Decimal("0")
    assert delivered.resources[destination_account_id].balance == Decimal("1")
    pending_spoilage_id = next(
        item.id for item in spoiled.scheduled_events
        if item.handler_id == "shipment.spoilage" and item.status == "queued"
    )
    cancelled = next(item for item in delivered.scheduled_events if item.id == pending_spoilage_id)
    assert cancelled.status == "cancelled"
    stale = RuleEngine().resolve_scheduled(delivered, cancelled, expiry_context.model_copy(update={"command_id": uuid4()}))
    assert stale.outcome.status == "rejected"

    with pytest.raises(InvalidEvent):
        apply_event(delivered, spoilage_event)


def test_all_spoiled_shipment_is_terminal_and_malformed_spoilage_replay_is_rejected():
    _initial, state, dispatch, _sender, _destination, receiver_id, _destination_account_id, base_context, _proposal = _scenario(receiver_is_person=True)
    receiver = state.entities[receiver_id]
    elsewhere = WorldEntity(id=uuid4(), kind="place", name="Elsewhere", components={"place": {"place_type": "settlement"}})
    moved_entities = dict(state.entities)
    moved_entities[elsewhere.id] = elsewhere
    moved_entities[receiver_id] = receiver.model_copy(update={"location_id": elsewhere.id})
    state = WorldState.model_validate({**state.model_dump(mode="json"), "entities": moved_entities})
    context = base_context.model_copy(update={"command_id": uuid4()})
    state = apply_event(state, TimeAdvancedEvent(to_tick=dispatch.arrival_tick))
    arrival = RuleEngine().resolve_scheduled(state, state.scheduled_events[0], context)
    deferred = next(item for item in arrival.events if isinstance(item, ShipmentArrivalDeferredEvent))
    state = apply_event(state, deferred)
    schedule = next(item for item in state.scheduled_events if item.handler_id == "shipment.spoilage")
    state = apply_event(state, TimeAdvancedEvent(to_tick=schedule.due_tick))
    spoilage = RuleEngine().resolve_scheduled(state, schedule, context.model_copy(update={"command_id": uuid4()}))
    event = next(item for item in spoilage.events if isinstance(item, ShipmentSpoilageResolvedEvent))
    malformed_lot = event.spoiled_lots[0].model_copy(update={"quantity": Decimal("0.5")})
    malformed = event.model_copy(update={"spoiled_lots": [malformed_lot]})
    with pytest.raises(InvalidEvent):
        apply_event(state, malformed)
    with pytest.raises(InvalidEvent):
        validate_shipment_spoilage_batch(
            state,
            dispatch.sender_id,
            [malformed],
            world_id=malformed.world_id,
            branch_id=malformed.branch_id,
            command_id=malformed.command_id,
            ruleset_version=malformed.ruleset_version,
            command_type="simulation.window",
            command_payload={"job_id": str(uuid4()), "next_tick": str(schedule.due_tick)},
        )

    state = apply_event(state, event)
    next_schedule = next(item for item in state.scheduled_events if item.handler_id == "shipment.spoilage" and item.status == "queued")
    state = apply_event(state, TimeAdvancedEvent(to_tick=next_schedule.due_tick))
    final_expiry = RuleEngine().resolve_scheduled(
        state,
        next(item for item in state.scheduled_events if item.id == next_schedule.id),
        context.model_copy(update={"command_id": uuid4()}),
    )
    final_event = next(item for item in final_expiry.events if isinstance(item, ShipmentSpoilageResolvedEvent))
    state = apply_event(state, final_event)
    shipment = state.entities[dispatch.shipment_id].components["shipment"]
    assert shipment["status"] == "spoiled"
    assert len(shipment["cargo"][0]["lots"]) == 2
    assert len(shipment["spoiled_lots"]) == 2
    assert state.resources[dispatch.transit_account_id].balance == Decimal("0")
    assert not state.entities[dispatch.shipment_id].components["inventory"]["lots"]
