from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError

from world_engine.domain.components import PlaceComponent, PlaceRoute, ScheduledEventRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import event_payload, parse_event
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.domain.travel_journeys import (
    TRAVEL_RESOLUTION_HANDLER_ID,
    TRAVEL_RESOLUTION_HANDLER_VERSION,
    TravelJourney,
    plan_travel_departure,
    plan_travel_resolution,
    travel_resolution_schedule_id,
)
from world_engine.domain.travel_journey_events import (
    TravelJourneyChangedEvent,
    travel_event_id,
    travel_journey_visibility,
)


ORIGIN_ID = UUID("10000000-0000-4000-8000-000000000001")
DESTINATION_ID = UUID("20000000-0000-4000-8000-000000000002")
ACTOR_ID = UUID("30000000-0000-4000-8000-000000000003")
ROUTE_ID = UUID("40000000-0000-4000-8000-000000000004")
JOURNEY_ID = UUID("50000000-0000-4000-8000-000000000005")


def _world(
    *,
    tick=100,
    actor_status="active",
    actor_location=ORIGIN_ID,
    routes=None,
    journeys=(),
    schedules=(),
):
    route = PlaceRoute(
        route_id=ROUTE_ID,
        from_place_id=ORIGIN_ID,
        to_place_id=DESTINATION_ID,
        travel_ticks=8,
    )
    origin = WorldEntity(
        id=ORIGIN_ID,
        kind="place",
        components={
            "place": PlaceComponent(routes=[route] if routes is None else routes).model_dump(mode="json")
        },
    )
    destination = WorldEntity(
        id=DESTINATION_ID,
        kind="place",
        components={"place": PlaceComponent().model_dump(mode="json")},
    )
    actor = WorldEntity(
        id=ACTOR_ID,
        kind="character",
        status=actor_status,
        location_id=actor_location,
    )
    schedule_rows = list(schedules)
    known_schedule_ids = {item.id for item in schedule_rows}
    for journey in journeys:
        schedule_id = travel_resolution_schedule_id(journey.journey_id)
        if schedule_id not in known_schedule_ids:
            schedule_rows.append(_scheduled(
                journey,
                status="queued" if journey.status == "in_transit" else "completed",
            ))
    return WorldState(
        current_tick=tick,
        entities={ORIGIN_ID: origin, DESTINATION_ID: destination, ACTOR_ID: actor},
        travel_journeys=list(journeys),
        scheduled_events=schedule_rows,
    )


def _scheduled(journey: TravelJourney, *, due_tick=None, status="queued"):
    due = journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick
    return ScheduledEventRecord(
        id=travel_resolution_schedule_id(journey.journey_id),
        due_tick=due if due_tick is None else due_tick,
        phase="resolution",
        handler_id=TRAVEL_RESOLUTION_HANDLER_ID,
        handler_version=TRAVEL_RESOLUTION_HANDLER_VERSION,
        status=status,
        completed_runs=1 if status == "completed" else 0,
        payload={"journey_id": str(journey.journey_id)},
    )


def _departure(state=None, *, journey_id=JOURNEY_ID, hazard=False, hazard_timing=None):
    return plan_travel_departure(
        state or _world(), journey_id=journey_id, actor_id=ACTOR_ID, route_id=ROUTE_ID,
        hazard=hazard, hazard_timing=hazard_timing,
    )


def _departure_event(journey, *, hazard_timing=None, command_id=UUID("70000000-0000-4000-8000-000000000007")):
    return TravelJourneyChangedEvent(
        event_id=travel_event_id(command_id, "travel-test-v1", "depart", journey.journey_id),
        world_id=UUID("80000000-0000-4000-8000-000000000008"),
        branch_id=UUID("90000000-0000-4000-8000-000000000009"),
        command_id=command_id,
        actor_id=ACTOR_ID,
        ruleset_version="travel-test-v1",
        operation="depart",
        journey=journey,
        visibility=travel_journey_visibility(ACTOR_ID),
        hazard_timing=hazard_timing,
    )


def _in_transit_state(journey, *, tick, actor_status="active", schedule=None):
    schedules = [_scheduled(journey) if schedule is None else schedule]
    return _world(
        tick=tick,
        actor_status=actor_status,
        actor_location=None,
        journeys=[journey],
        schedules=schedules,
    )


def test_travel_arrival_cannot_overfill_its_destination():
    trip = _departure()
    state = _in_transit_state(trip, tick=trip.arrival_tick)
    destination = state.entities[DESTINATION_ID]
    occupant_id = UUID("60000000-0000-4000-8000-000000000006")
    state = state.model_copy(update={"entities": {
        **state.entities,
        DESTINATION_ID: destination.model_copy(update={"components": {"place": {"capacity": 1}}}),
        occupant_id: WorldEntity(id=occupant_id, kind="character", location_id=DESTINATION_ID),
    }})
    with pytest.raises(InvalidEvent, match="registered capacity"):
        plan_travel_resolution(state, journey_id=trip.journey_id, actor_id=ACTOR_ID, operation="arrive")


def test_departure_derives_exact_registered_route_timing_and_resolution_schedule_identity():
    journey = _departure(hazard=True)

    assert journey == TravelJourney(
        journey_id=JOURNEY_ID,
        actor_id=ACTOR_ID,
        route_id=ROUTE_ID,
        from_place_id=ORIGIN_ID,
        to_place_id=DESTINATION_ID,
        departed_tick=100,
        arrival_tick=108,
        hazard_tick=104,
    )
    assert travel_resolution_schedule_id(JOURNEY_ID) == uuid5(JOURNEY_ID, "travel-journey-resolution")
    assert TRAVEL_RESOLUTION_HANDLER_ID == "travel.journey_resolve"
    assert TRAVEL_RESOLUTION_HANDLER_VERSION == "1.0.0"
    assert TravelJourney.model_validate(journey.model_dump(mode="json")) == journey


def test_legacy_hazard_departure_keeps_midpoint_and_omits_new_event_field():
    state = _world()
    journey = _departure(state, hazard=True)
    event = _departure_event(journey)

    assert journey.hazard_tick == 104
    assert "hazard_timing" not in event.model_dump(mode="json")
    assert "hazard_timing" not in event_payload(event)

    restored_event = parse_event(event.model_dump(mode="json"))
    assert isinstance(restored_event, TravelJourneyChangedEvent)
    stored = apply_event(state, restored_event)
    replayed = apply_event(state, event.model_dump(mode="json"))
    assert stored.travel_journeys == [journey]
    assert stored.scheduled_events[0].due_tick == journey.hazard_tick
    assert state_hash(stored) == state_hash(replayed)


def test_versioned_hazard_timing_is_stable_varied_and_inside_route_bounds():
    offsets = [
        _departure(
            journey_id=UUID(int=0x50000000000040008000000000000000 + index),
            hazard=True,
            hazard_timing="journey_seed_v1",
        ).hazard_tick - 100
        for index in range(1, 40)
    ]
    assert all(1 <= offset < 8 for offset in offsets)
    assert len(set(offsets)) > 1
    stable_id = UUID("60000000-0000-4000-8000-000000000006")
    first = _departure(journey_id=stable_id, hazard=True, hazard_timing="journey_seed_v1")
    repeated = _departure(journey_id=stable_id, hazard=True, hazard_timing="journey_seed_v1")
    assert first.hazard_tick == repeated.hazard_tick
    assert first.hazard_tick - 100 == uuid5(
        stable_id, "movement.route_hazard_timing.v1",
    ).int % 7 + 1


def test_versioned_departure_reducer_rejects_tampered_hazard_tick():
    state = _world()
    journey = _departure(state, hazard=True, hazard_timing="journey_seed_v1")
    event = _departure_event(journey, hazard_timing="journey_seed_v1")
    applied = apply_event(state, event)
    assert applied.travel_journeys == [journey]
    assert applied.scheduled_events[0].due_tick == journey.hazard_tick

    tampered = journey.model_copy(update={"hazard_tick": journey.arrival_tick})
    forged_event = _departure_event(tampered, hazard_timing="journey_seed_v1")
    with pytest.raises(InvalidEvent, match="authoritative transition"):
        apply_event(state, forged_event)


def test_versioned_hazard_timing_handles_one_and_two_tick_routes():
    for duration in (1, 2):
        route = PlaceRoute(
            route_id=ROUTE_ID,
            from_place_id=ORIGIN_ID,
            to_place_id=DESTINATION_ID,
            travel_ticks=duration,
        )
        journey = _departure(
            _world(routes=[route]),
            journey_id=UUID(int=0x70000000000040008000000000000000 + duration),
            hazard=True,
            hazard_timing="journey_seed_v1",
        )
        offset = journey.hazard_tick - journey.departed_tick
        assert 1 <= offset <= duration
        if duration > 1:
            assert journey.hazard_tick < journey.arrival_tick


def test_hazard_timing_marker_is_only_valid_on_hazardous_departure():
    non_hazard = _departure(hazard=False)
    with pytest.raises(ValidationError, match="only on a hazardous departure"):
        _departure_event(non_hazard, hazard_timing="journey_seed_v1")

    hazardous = _departure(hazard=True)
    command_id = UUID("70000000-0000-4000-8000-000000000007")
    common = {
        "world_id": UUID("80000000-0000-4000-8000-000000000008"),
        "branch_id": UUID("90000000-0000-4000-8000-000000000009"),
        "command_id": command_id,
        "actor_id": ACTOR_ID,
        "ruleset_version": "travel-test-v1",
        "visibility": travel_journey_visibility(ACTOR_ID),
    }
    with pytest.raises(ValidationError, match="only on a hazardous departure"):
        TravelJourneyChangedEvent(
            event_id=travel_event_id(command_id, "travel-test-v1", "delay", hazardous.journey_id),
            operation="delay",
            journey=hazardous,
            stationary_from_tick=100,
            stationary_until_tick=101,
            hazard_timing="journey_seed_v1",
            **common,
        )

    delay_event = TravelJourneyChangedEvent(
        event_id=travel_event_id(command_id, "travel-test-v1", "delay", hazardous.journey_id),
        operation="delay",
        journey=hazardous,
        stationary_from_tick=100,
        stationary_until_tick=101,
        **common,
    )
    assert "hazard_timing" not in delay_event.model_dump(mode="json")
    assert "hazard_timing" not in event_payload(delay_event)

    arrived = TravelJourney.model_validate(non_hazard.model_dump(mode="python") | {
        "status": "arrived", "resolved_tick": non_hazard.arrival_tick,
    })
    arrival_event = TravelJourneyChangedEvent(
        event_id=travel_event_id(command_id, "travel-test-v1", "arrive", arrived.journey_id),
        operation="arrive",
        journey=arrived,
        **common,
    )
    assert "hazard_timing" not in arrival_event.model_dump(mode="json")
    assert "hazard_timing" not in event_payload(arrival_event)


def test_departure_requires_active_local_actor_and_exact_open_route():
    closed_route = PlaceRoute(
        route_id=ROUTE_ID,
        from_place_id=ORIGIN_ID,
        to_place_id=DESTINATION_ID,
        travel_ticks=8,
        is_open=False,
    )
    mismatched_route = PlaceRoute(
        route_id=ROUTE_ID,
        from_place_id=DESTINATION_ID,
        to_place_id=ORIGIN_ID,
        travel_ticks=8,
    )
    with pytest.raises(InvalidEvent, match="active character"):
        _departure(_world(actor_status="dead"))
    with pytest.raises(InvalidEvent, match="registered origin place"):
        _departure(_world(actor_location=None))
    with pytest.raises(InvalidEvent, match="not registered exactly once"):
        _departure(_world(routes=[]))
    with pytest.raises(InvalidEvent, match="exact open route"):
        _departure(_world(routes=[closed_route]))
    valid = _world()
    bad_origin = valid.entities[ORIGIN_ID].model_copy(update={
        "components": {"place": PlaceComponent(routes=[mismatched_route]).model_dump(mode="json")}
    })
    bad_routes = valid.model_copy(update={
        "entities": {**valid.entities, ORIGIN_ID: bad_origin}
    })
    with pytest.raises(InvalidEvent, match="exact open route"):
        _departure(bad_routes)


def test_departure_rejects_existing_active_journey_duplicate_id_and_invalid_hazard_flag():
    existing = TravelJourney(
        journey_id=UUID("60000000-0000-4000-8000-000000000006"),
        actor_id=ACTOR_ID,
        route_id=ROUTE_ID,
        from_place_id=ORIGIN_ID,
        to_place_id=DESTINATION_ID,
        departed_tick=90,
        arrival_tick=98,
    )
    state = _world(tick=95, actor_location=None, journeys=[existing])
    with pytest.raises(InvalidEvent, match="only one active travel journey"):
        _departure(state)
    with pytest.raises(InvalidEvent, match="ID already exists"):
        _departure(journey_id=JOURNEY_ID, hazard=False, state=_world(
            journeys=[TravelJourney(
                journey_id=JOURNEY_ID,
                actor_id=ACTOR_ID,
                route_id=ROUTE_ID,
                from_place_id=ORIGIN_ID,
                to_place_id=DESTINATION_ID,
                departed_tick=1,
                arrival_tick=9,
                status="arrived",
                resolved_tick=9,
            )]
        ))
    with pytest.raises(InvalidEvent, match="boolean"):
        plan_travel_departure(_world(), journey_id=JOURNEY_ID, actor_id=ACTOR_ID, route_id=ROUTE_ID, hazard=1)


def test_departure_rejects_arrival_clock_overflow():
    with pytest.raises(InvalidEvent, match="supported world clock"):
        _departure(_world(tick=2**63 - 4))


def test_resolution_arrives_only_at_arrival_with_the_original_queued_schedule():
    journey = _departure(hazard=False)
    due_state = _in_transit_state(journey, tick=journey.arrival_tick)
    arrived = plan_travel_resolution(
        due_state, journey_id=journey.journey_id, actor_id=ACTOR_ID, operation="arrive",
    )
    assert arrived.status == "arrived" and arrived.resolved_tick == journey.arrival_tick

    with pytest.raises(InvalidEvent, match="exact scheduled due tick"):
        plan_travel_resolution(
            _in_transit_state(journey, tick=journey.arrival_tick - 1),
            journey_id=journey.journey_id,
            actor_id=ACTOR_ID,
            operation="arrive",
        )
    wrong_schedule_state = _in_transit_state(journey, tick=journey.arrival_tick).model_copy(update={
        "scheduled_events": [_scheduled(journey, due_tick=journey.arrival_tick - 1)]
    })
    with pytest.raises(InvalidEvent, match="original queued resolution schedule"):
        plan_travel_resolution(
            wrong_schedule_state,
            journey_id=journey.journey_id,
            actor_id=ACTOR_ID,
            operation="arrive",
        )


def test_hazard_interruption_requires_declared_hazard_and_exact_hazard_tick():
    journey = _departure(hazard=True)
    due_state = _in_transit_state(journey, tick=journey.hazard_tick)
    interrupted = plan_travel_resolution(
        due_state, journey_id=journey.journey_id, actor_id=ACTOR_ID, operation="interrupt",
    )
    assert interrupted.status == "interrupted" and interrupted.resolved_tick == journey.hazard_tick

    with pytest.raises(InvalidEvent, match="exact scheduled due tick"):
        plan_travel_resolution(
            _in_transit_state(journey, tick=journey.hazard_tick - 1),
            journey_id=journey.journey_id,
            actor_id=ACTOR_ID,
            operation="interrupt",
        )
    with pytest.raises(InvalidEvent, match="declared hazard tick"):
        plan_travel_resolution(
            _in_transit_state(_departure(hazard=False), tick=108),
            journey_id=JOURNEY_ID,
            actor_id=ACTOR_ID,
            operation="interrupt",
        )
    with pytest.raises(InvalidEvent, match="arrival tick and no declared hazard"):
        plan_travel_resolution(
            due_state, journey_id=journey.journey_id, actor_id=ACTOR_ID, operation="arrive",
        )


def test_terminal_traveler_can_be_interrupted_only_at_scheduled_due_tick():
    journey = _departure(hazard=False)
    state = _in_transit_state(journey, tick=journey.arrival_tick, actor_status="dead")

    interrupted = plan_travel_resolution(
        state, journey_id=journey.journey_id, actor_id=ACTOR_ID, operation="interrupt",
    )

    assert interrupted.status == "interrupted"
    assert interrupted.resolved_tick == journey.arrival_tick
    with pytest.raises(InvalidEvent, match="scheduled due tick"):
        plan_travel_resolution(
            _in_transit_state(journey, tick=journey.arrival_tick - 1, actor_status="dead"),
            journey_id=journey.journey_id,
            actor_id=ACTOR_ID,
            operation="interrupt",
        )


def test_resolution_rejects_wrong_actor_missing_or_already_resolved_journey():
    journey = _departure(hazard=False)
    due_state = _in_transit_state(journey, tick=journey.arrival_tick)
    with pytest.raises(InvalidEvent, match="does not match"):
        plan_travel_resolution(
            due_state,
            journey_id=journey.journey_id,
            actor_id=UUID("70000000-0000-4000-8000-000000000007"),
            operation="arrive",
        )
    with pytest.raises(InvalidEvent, match="does not exist"):
        plan_travel_resolution(
            due_state,
            journey_id=UUID("80000000-0000-4000-8000-000000000008"),
            actor_id=ACTOR_ID,
            operation="arrive",
        )
    completed = journey.model_copy(update={"status": "arrived", "resolved_tick": journey.arrival_tick})
    completed_state = _world(
        tick=journey.arrival_tick,
        actor_location=DESTINATION_ID,
        journeys=[completed],
        schedules=[_scheduled(journey, status="completed")],
    )
    with pytest.raises(InvalidEvent, match="already resolved"):
        plan_travel_resolution(
            completed_state,
            journey_id=journey.journey_id,
            actor_id=ACTOR_ID,
            operation="arrive",
        )


def test_journey_contract_rejects_inconsistent_resolution_ticks():
    with pytest.raises(ValidationError, match="hazard tick"):
        TravelJourney(
            journey_id=JOURNEY_ID,
            actor_id=ACTOR_ID,
            route_id=ROUTE_ID,
            from_place_id=ORIGIN_ID,
            to_place_id=DESTINATION_ID,
            departed_tick=100,
            arrival_tick=108,
            hazard_tick=109,
        )
    with pytest.raises(ValidationError, match="arrival without a declared hazard"):
        TravelJourney(
            journey_id=JOURNEY_ID,
            actor_id=ACTOR_ID,
            route_id=ROUTE_ID,
            from_place_id=ORIGIN_ID,
            to_place_id=DESTINATION_ID,
            departed_tick=100,
            arrival_tick=108,
            hazard_tick=104,
            status="arrived",
            resolved_tick=108,
        )
