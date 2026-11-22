from uuid import UUID, uuid4

import pytest

from world_engine.domain.components import PlaceComponent, PlaceRoute, ScheduledEventRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.travel_itinerary import (
    TravelItinerary,
    TravelItineraryArrivalProof,
    plan_travel_itinerary,
    travel_itinerary_journey_id,
)
from world_engine.domain.travel_itinerary_progress import (
    build_travel_itinerary_arrival_proof,
    inspect_travel_itinerary_job,
    itinerary_requires_replan,
)
from world_engine.domain.travel_journey_events import (
    TravelJourneyChangedEvent,
    travel_event_id,
    travel_journey_visibility,
)
from world_engine.domain.travel_journeys import (
    TRAVEL_RESOLUTION_HANDLER_ID,
    TRAVEL_RESOLUTION_HANDLER_VERSION,
    TravelJourney,
    travel_resolution_schedule_id,
)


ACTOR_ID = UUID("30000000-0000-4000-8000-000000000003")
ORIGIN_ID = UUID("10000000-0000-4000-8000-000000000001")
MIDDLE_ID = UUID("20000000-0000-4000-8000-000000000002")
DESTINATION_ID = UUID("40000000-0000-4000-8000-000000000004")
ROUTE_1_ID = UUID("50000000-0000-4000-8000-000000000005")
ROUTE_2_ID = UUID("60000000-0000-4000-8000-000000000006")
ITINERARY_ID = UUID("70000000-0000-4000-8000-000000000007")
WORLD_ID = UUID("80000000-0000-4000-8000-000000000008")
BRANCH_ID = UUID("90000000-0000-4000-8000-000000000009")
RULESET_VERSION = "test.rules.v1"


@pytest.mark.parametrize(
    "reason_code",
    [
        "route_hazard",
        "travel_journey_interrupted",
        "deadline_passed",
        "remaining_route_cannot_fit_deadline",
    ],
)
def test_recorded_terminal_interruption_requires_a_new_itinerary(reason_code):
    assert itinerary_requires_replan(reason_code) is True


@pytest.mark.parametrize(
    "reason_code",
    [
        None,
        7,
        [],
        {},
        "route_snapshot_changed",
        "route_not_registered",
        "destination_not_observed",
        "arrival_proof_required",
        "actor_location_changed",
    ],
)
def test_transient_route_changes_and_nonstring_reasons_do_not_require_replanning(reason_code):
    assert itinerary_requires_replan(reason_code) is False


def _route(route_id: UUID, origin: UUID, destination: UUID, duration: int) -> PlaceRoute:
    return PlaceRoute(
        route_id=route_id,
        from_place_id=origin,
        to_place_id=destination,
        travel_ticks=duration,
    )


def _state(
    *,
    current_tick: int = 100,
    actor_location: UUID | None = ORIGIN_ID,
    journeys: tuple[TravelJourney, ...] = (),
    schedules: tuple[ScheduledEventRecord, ...] = (),
    routes: tuple[PlaceRoute, PlaceRoute] | None = None,
    actor_status: str = "active",
) -> WorldState:
    route_1, route_2 = routes or (
        _route(ROUTE_1_ID, ORIGIN_ID, MIDDLE_ID, 10),
        _route(ROUTE_2_ID, MIDDLE_ID, DESTINATION_ID, 20),
    )
    entities = {
        ORIGIN_ID: WorldEntity(
            id=ORIGIN_ID,
            kind="place",
            components={"place": PlaceComponent(routes=[route_1]).model_dump(mode="json")},
        ),
        MIDDLE_ID: WorldEntity(
            id=MIDDLE_ID,
            kind="place",
            components={"place": PlaceComponent(routes=[route_2]).model_dump(mode="json")},
        ),
        DESTINATION_ID: WorldEntity(
            id=DESTINATION_ID,
            kind="place",
            components={"place": PlaceComponent().model_dump(mode="json")},
        ),
        ACTOR_ID: WorldEntity(
            id=ACTOR_ID,
            kind="character",
            location_id=actor_location,
            status=actor_status,
            components={"capabilities": {"grants": ["world.travel"], "restrictions": []}},
        ),
    }
    return WorldState(
        current_tick=current_tick,
        entities=entities,
        travel_journeys=list(journeys),
        scheduled_events=list(schedules),
    )


def _plan(state: WorldState | None = None) -> TravelItinerary:
    return plan_travel_itinerary(
        state or _state(),
        itinerary_id=ITINERARY_ID,
        actor_id=ACTOR_ID,
        route_ids=(ROUTE_1_ID, ROUTE_2_ID),
        deadline_tick=200,
    )


def _journey(
    itinerary: TravelItinerary,
    leg_index: int,
    *,
    departed_tick: int,
    status: str = "in_transit",
    resolved_tick: int | None = None,
    journey_id: UUID | None = None,
    route_id: UUID | None = None,
) -> TravelJourney:
    leg = itinerary.legs[leg_index]
    return TravelJourney(
        journey_id=journey_id or travel_itinerary_journey_id(itinerary.itinerary_id, leg.leg_id),
        actor_id=itinerary.actor_id,
        route_id=route_id or leg.route_id,
        from_place_id=leg.from_place_id,
        to_place_id=leg.to_place_id,
        departed_tick=departed_tick,
        arrival_tick=departed_tick + leg.travel_ticks,
        status=status,  # type: ignore[arg-type]
        resolved_tick=resolved_tick,
    )


def _schedule(journey: TravelJourney, *, completed: bool = False) -> ScheduledEventRecord:
    return ScheduledEventRecord(
        id=travel_resolution_schedule_id(journey.journey_id),
        due_tick=journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick,
        phase="resolution",
        handler_id=TRAVEL_RESOLUTION_HANDLER_ID,
        handler_version=TRAVEL_RESOLUTION_HANDLER_VERSION,
        status="completed" if completed else "queued",
        completed_runs=1 if completed else 0,
        payload={"journey_id": str(journey.journey_id)},
    )


def _arrived(journey: TravelJourney) -> TravelJourney:
    return TravelJourney.model_validate(journey.model_dump(mode="python") | {
        "status": "arrived",
        "resolved_tick": journey.arrival_tick,
    })


def _arrival_event(
    journey: TravelJourney,
    *,
    world_id: UUID = WORLD_ID,
    branch_id: UUID = BRANCH_ID,
    command_id: UUID | None = None,
) -> TravelJourneyChangedEvent:
    command_id = command_id or uuid4()
    return TravelJourneyChangedEvent(
        event_id=travel_event_id(command_id, RULESET_VERSION, "arrive", journey.journey_id),
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=journey.actor_id,
        ruleset_version=RULESET_VERSION,
        operation="arrive",
        journey=journey,
        visibility=travel_journey_visibility(journey.actor_id),
    )


def _proof_for(
    itinerary: TravelItinerary,
    leg_index: int,
    *,
    departed_tick: int,
) -> TravelItineraryArrivalProof:
    leg = itinerary.legs[leg_index]
    return TravelItineraryArrivalProof(
        itinerary_id=itinerary.itinerary_id,
        leg_id=leg.leg_id,
        leg_index=leg_index,
        journey_id=travel_itinerary_journey_id(itinerary.itinerary_id, leg.leg_id),
        actor_id=itinerary.actor_id,
        route_id=leg.route_id,
        from_place_id=leg.from_place_id,
        to_place_id=leg.to_place_id,
        route_fingerprint=leg.route_fingerprint,
        departed_tick=departed_tick,
        arrived_tick=departed_tick + leg.travel_ticks,
    )


def _committed_arrival_state(
    itinerary: TravelItinerary,
    leg_index: int,
    *,
    departed_tick: int,
    earlier: tuple[TravelJourney, ...] = (),
) -> WorldState:
    journey = _arrived(_journey(itinerary, leg_index, departed_tick=departed_tick))
    return _state(
        current_tick=journey.arrival_tick,
        actor_location=journey.to_place_id,
        journeys=(*earlier, journey),
        schedules=(*(_schedule(item, completed=True) for item in earlier), _schedule(journey, completed=True)),
    )


def _build_proof(
    itinerary: TravelItinerary,
    leg_index: int,
    *,
    departed_tick: int,
    cursor: TravelItineraryArrivalProof | None = None,
    earlier: tuple[TravelJourney, ...] = (),
) -> tuple[TravelItineraryArrivalProof, WorldState, TravelJourneyChangedEvent]:
    departed = _journey(itinerary, leg_index, departed_tick=departed_tick)
    prior_state = _state(
        current_tick=departed.arrival_tick,
        actor_location=None,
        journeys=(*earlier, departed),
        schedules=(*(_schedule(item, completed=True) for item in earlier), _schedule(departed)),
    )
    event = _arrival_event(_arrived(departed))
    proof = build_travel_itinerary_arrival_proof(
        itinerary,
        leg_index=leg_index,
        prior_state=prior_state,
        arrival_event=event,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        cursor=cursor,
    )
    return proof, prior_state, event


def test_inspection_starts_at_first_leg_with_fixed_journey_identity():
    itinerary = _plan()

    step = inspect_travel_itinerary_job(_state(), itinerary, cursor=None)

    assert step.outcome == "ready_to_run"
    assert step.leg_index == 0
    assert step.leg == itinerary.legs[0]
    assert step.journey_id == travel_itinerary_journey_id(itinerary.itinerary_id, itinerary.legs[0].leg_id)
    assert step.revalidation is not None and step.revalidation.outcome == "current"


def test_location_and_saved_arrival_cannot_advance_without_durable_cursor():
    itinerary = _plan()
    arrived = _arrived(_journey(itinerary, 0, departed_tick=100))
    state = _state(
        current_tick=110,
        actor_location=MIDDLE_ID,
        journeys=(arrived,),
        schedules=(_schedule(arrived, completed=True),),
    )

    with pytest.raises(InvalidEvent, match="missing its durable cursor proof"):
        inspect_travel_itinerary_job(state, itinerary, cursor=None)


def test_typed_arrival_from_prior_in_transit_state_builds_exact_cursor():
    itinerary = _plan()
    proof, prior_state, event = _build_proof(itinerary, 0, departed_tick=100)

    assert proof.leg_index == 0
    assert proof.journey_id == event.journey.journey_id
    assert proof.departed_tick == 100
    assert proof.arrived_tick == 110
    assert prior_state.entities[ACTOR_ID].location_id is None

    after_arrival = _committed_arrival_state(itinerary, 0, departed_tick=100)
    next_step = inspect_travel_itinerary_job(after_arrival, itinerary, cursor=proof)
    assert next_step.outcome == "ready_to_run"
    assert next_step.leg_index == 1
    assert next_step.journey_id == travel_itinerary_journey_id(itinerary.itinerary_id, itinerary.legs[1].leg_id)


def test_in_transit_leg_resumes_and_second_arrival_advances_only_that_cursor():
    itinerary = _plan()
    first_proof, _, _ = _build_proof(itinerary, 0, departed_tick=100)
    first_arrived = _arrived(_journey(itinerary, 0, departed_tick=100))
    second_proof, second_prior_state, second_event = _build_proof(
        itinerary,
        1,
        departed_tick=110,
        cursor=first_proof,
        earlier=(first_arrived,),
    )

    step = inspect_travel_itinerary_job(second_prior_state, itinerary, cursor=first_proof)
    assert step.outcome == "resume_in_transit"
    assert step.leg_index == 1
    assert step.journey == second_prior_state.travel_journeys[-1]
    assert second_proof.leg_index == 1
    assert second_proof.journey_id == second_event.journey.journey_id
    assert second_proof.departed_tick == 110
    assert second_proof.arrived_tick == 130

    completed = _state(
        current_tick=130,
        actor_location=DESTINATION_ID,
        journeys=(first_arrived, _arrived(second_event.journey)),
        schedules=(_schedule(first_arrived, completed=True), _schedule(second_event.journey, completed=True)),
    )
    final_step = inspect_travel_itinerary_job(completed, itinerary, cursor=second_proof)
    assert final_step.outcome == "completed"
    assert final_step.leg is None and final_step.journey_id is None


def test_route_invalidation_is_a_block_reason_with_revalidation_details():
    itinerary = _plan()
    changed_first_route = _route(ROUTE_1_ID, ORIGIN_ID, MIDDLE_ID, 11)
    changed_state = _state(routes=(changed_first_route, _route(ROUTE_2_ID, MIDDLE_ID, DESTINATION_ID, 20)))

    step = inspect_travel_itinerary_job(changed_state, itinerary, cursor=None)

    assert step.outcome == "blocked_interrupted"
    assert step.reason_code == "route_snapshot_changed"
    assert step.revalidation is not None
    assert step.revalidation.outcome == "route_invalidated"


def test_committed_interruption_blocks_the_itinerary():
    itinerary = _plan()
    interrupted = TravelJourney(
        journey_id=travel_itinerary_journey_id(itinerary.itinerary_id, itinerary.legs[0].leg_id),
        actor_id=ACTOR_ID,
        route_id=ROUTE_1_ID,
        from_place_id=ORIGIN_ID,
        to_place_id=MIDDLE_ID,
        departed_tick=100,
        arrival_tick=110,
        status="interrupted",
        resolved_tick=110,
    )
    state = _state(
        current_tick=110,
        journeys=(interrupted,),
        schedules=(_schedule(interrupted, completed=True),),
    )

    step = inspect_travel_itinerary_job(state, itinerary, cursor=None)

    assert step.outcome == "blocked_interrupted"
    assert step.reason_code == "travel_journey_interrupted"
    assert step.journey == interrupted


@pytest.mark.parametrize(
    ("event_scope", "message"),
    [
        ({"world_id": uuid4()}, "another world or branch"),
        ({"branch_id": uuid4()}, "another world or branch"),
    ],
)
def test_arrival_builder_checks_world_and_branch_scope(event_scope, message):
    itinerary = _plan()
    departed = _journey(itinerary, 0, departed_tick=100)
    event = _arrival_event(_arrived(departed), **event_scope)
    prior_state = _state(
        current_tick=110,
        actor_location=None,
        journeys=(departed,),
        schedules=(_schedule(departed),),
    )

    with pytest.raises(InvalidEvent, match=message):
        build_travel_itinerary_arrival_proof(
            itinerary,
            leg_index=0,
            prior_state=prior_state,
            arrival_event=event,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            cursor=None,
        )


def test_arrival_builder_rejects_dict_out_of_order_and_mismatching_event_journey():
    itinerary = _plan()
    departed = _journey(itinerary, 0, departed_tick=100)
    prior_state = _state(
        current_tick=110,
        actor_location=None,
        journeys=(departed,),
        schedules=(_schedule(departed),),
    )
    event = _arrival_event(_arrived(departed))
    common = {
        "itinerary": itinerary,
        "prior_state": prior_state,
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "cursor": None,
    }

    with pytest.raises(InvalidEvent, match="typed travel arrival event"):
        build_travel_itinerary_arrival_proof(
            **common, leg_index=0, arrival_event=event.model_dump(mode="python"),  # type: ignore[arg-type]
        )
    with pytest.raises(InvalidEvent, match="out of order"):
        build_travel_itinerary_arrival_proof(**common, leg_index=1, arrival_event=event)

    malformed_journey = TravelJourney.model_validate(event.journey.model_dump(mode="python") | {
        "route_id": uuid4(),
    })
    mismatched_event = _arrival_event(malformed_journey)
    with pytest.raises(InvalidEvent, match="exact in-transit itinerary leg"):
        build_travel_itinerary_arrival_proof(**common, leg_index=0, arrival_event=mismatched_event)


def test_inspection_rejects_unexpected_active_and_future_itinerary_journeys():
    itinerary = _plan()
    unexpected = _journey(itinerary, 0, departed_tick=100, journey_id=uuid4())
    state = _state(
        actor_location=None,
        journeys=(unexpected,),
        schedules=(_schedule(unexpected),),
    )
    with pytest.raises(InvalidEvent, match="unexpected active travel journey"):
        inspect_travel_itinerary_job(state, itinerary, cursor=None)

    future = _arrived(_journey(itinerary, 1, departed_tick=110))
    future_state = _state(
        current_tick=150,
        journeys=(future,),
        schedules=(_schedule(future, completed=True),),
    )
    with pytest.raises(InvalidEvent, match="later itinerary leg has a journey"):
        inspect_travel_itinerary_job(future_state, itinerary, cursor=None)


def test_inspection_rejects_a_terminal_actor_instead_of_silently_advancing():
    itinerary = _plan()
    terminal_state = _state(actor_status="retired")

    with pytest.raises(InvalidEvent, match="missing or terminal actor"):
        inspect_travel_itinerary_job(terminal_state, itinerary, cursor=None)
