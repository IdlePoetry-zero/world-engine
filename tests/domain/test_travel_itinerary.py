from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import KnowledgeRecord, PlaceComponent, PlaceRoute, ScheduledEventRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import author_value_hash
from world_engine.domain.travel_itinerary import (
    MAX_TRAVEL_ITINERARY_LEGS,
    TravelItineraryArrivalProof,
    plan_travel_itinerary,
    revalidate_travel_itinerary_leg,
    travel_itinerary_journey_id,
    travel_route_fingerprint,
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


def _route(route_id, from_place, to_place, *, travel_ticks=10, **kwargs):
    return PlaceRoute(
        route_id=route_id,
        from_place_id=from_place,
        to_place_id=to_place,
        travel_ticks=travel_ticks,
        **kwargs,
    )


def _state(*, actor_location=ORIGIN_ID, current_tick=100, routes=None, hidden_destination=False,
           knowledge=(), journeys=(), schedules=()):
    route_1 = _route(ROUTE_1_ID, ORIGIN_ID, MIDDLE_ID, travel_ticks=10)
    route_2 = _route(ROUTE_2_ID, MIDDLE_ID, DESTINATION_ID, travel_ticks=20)
    if routes is not None:
        route_1, route_2 = routes
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
            components={
                "place": PlaceComponent().model_dump(mode="json"),
                **({"hidden": True} if hidden_destination else {}),
            },
        ),
        ACTOR_ID: WorldEntity(
            id=ACTOR_ID,
            kind="character",
            location_id=actor_location,
            components={"capabilities": {"grants": ["world.travel"], "restrictions": []}},
        ),
    }
    return WorldState(
        current_tick=current_tick,
        entities=entities,
        knowledge=list(knowledge),
        travel_journeys=list(journeys),
        scheduled_events=list(schedules),
    )


def _itinerary(state=None, *, deadline=200, route_ids=(ROUTE_1_ID, ROUTE_2_ID)):
    return plan_travel_itinerary(
        state or _state(),
        itinerary_id=ITINERARY_ID,
        actor_id=ACTOR_ID,
        route_ids=route_ids,
        deadline_tick=deadline,
    )


def _arrival_proof(itinerary, *, leg_index=0, overrides=None):
    leg = itinerary.legs[leg_index]
    return TravelItineraryArrivalProof.model_validate({
        "itinerary_id": itinerary.itinerary_id,
        "leg_id": leg.leg_id,
        "leg_index": leg.leg_index,
        "journey_id": travel_itinerary_journey_id(itinerary.itinerary_id, leg.leg_id),
        "actor_id": itinerary.actor_id,
        "route_id": leg.route_id,
        "from_place_id": leg.from_place_id,
        "to_place_id": leg.to_place_id,
        "route_fingerprint": leg.route_fingerprint,
        "departed_tick": 100,
        "arrived_tick": 110,
    } | (overrides or {}))


def test_plan_snapshots_explicit_connected_route_order_and_stable_leg_ids():
    state = _state()
    itinerary = _itinerary(state, deadline=150)

    assert itinerary.contract_version == "travel_itinerary.v1"
    assert itinerary.actor_id == ACTOR_ID
    assert itinerary.origin_place_id == ORIGIN_ID
    assert itinerary.destination_place_id == DESTINATION_ID
    assert itinerary.created_tick == 100 and itinerary.deadline_tick == 150
    assert itinerary.total_travel_ticks == 30
    assert [(leg.route_id, leg.from_place_id, leg.to_place_id, leg.travel_ticks) for leg in itinerary.legs] == [
        (ROUTE_1_ID, ORIGIN_ID, MIDDLE_ID, 10),
        (ROUTE_2_ID, MIDDLE_ID, DESTINATION_ID, 20),
    ]
    assert [leg.leg_index for leg in itinerary.legs] == [0, 1]
    assert len({leg.leg_id for leg in itinerary.legs}) == 2
    assert _itinerary(state, deadline=150).legs == itinerary.legs
    first_route = PlaceRoute.model_validate(state.entities[ORIGIN_ID].components["place"]["routes"][0])
    assert itinerary.legs[0].route_fingerprint == author_value_hash(first_route.model_dump(mode="json"))


def test_route_fingerprint_covers_every_registered_route_field():
    original = _route(ROUTE_1_ID, ORIGIN_ID, MIDDLE_ID)
    changes = (
        {"travel_ticks": 11},
        {"risk": "0.2"},
        {"access_tags": ["travel.safe_conduct"]},
        {"travel_modes": ["travel.boat"]},
        {"is_open": False},
    )

    fingerprint = travel_route_fingerprint(original)
    assert fingerprint == author_value_hash(original.model_dump(mode="json"))
    assert len(fingerprint) == 64
    for change in changes:
        changed = PlaceRoute.model_validate(original.model_dump(mode="python") | change)
        assert travel_route_fingerprint(changed) != fingerprint


def test_plan_rejects_non_connected_or_wrong_direction_route_sequence_without_pathfinding():
    state = _state()

    with pytest.raises(InvalidEvent, match="registered at its origin"):
        _itinerary(state, route_ids=(ROUTE_2_ID, ROUTE_1_ID))


def test_plan_requires_active_actor_and_current_registered_origin():
    state = _state(actor_location=None)
    with pytest.raises(InvalidEvent, match="start at a registered place"):
        _itinerary(state)

    inactive_actor = state.entities[ACTOR_ID].model_copy(update={"status": "dead"})
    state = _state().model_copy(update={"entities": _state().entities | {ACTOR_ID: inactive_actor}})
    with pytest.raises(InvalidEvent, match="active character"):
        _itinerary(state)


def test_plan_enforces_deadline_preflight_and_signed_clock_bounds():
    with pytest.raises(InvalidEvent, match="cannot fit the itinerary contract"):
        _itinerary(_state(current_tick=100), deadline=129)

    near_limit = _state(current_tick=2**63 - 15)
    with pytest.raises(InvalidEvent, match="cannot fit the itinerary contract"):
        _itinerary(near_limit, deadline=2**63 - 1)


def test_plan_bounds_input_and_has_no_progress_cursor():
    with pytest.raises(InvalidEvent, match="explicitly ordered bounded list"):
        _itinerary(route_ids="route-id")  # type: ignore[arg-type]
    with pytest.raises(InvalidEvent, match="between 1 and 256"):
        _itinerary(route_ids=(ROUTE_1_ID,) * (MAX_TRAVEL_ITINERARY_LEGS + 1))

    itinerary = _itinerary()
    assert "current_leg_index" not in itinerary.model_dump(mode="python")
    with pytest.raises(ValidationError):
        itinerary.deadline_tick = 300


def test_revalidation_of_first_leg_checks_location_but_does_not_claim_permission():
    itinerary = _itinerary()
    check = revalidate_travel_itinerary_leg(_state(), itinerary, 0)

    assert check.outcome == "current"
    assert check.route_snapshot == "matched"
    assert check.destination_observation == "not_required"
    assert check.travel_permission == "not_evaluated"
    moved = _state(actor_location=MIDDLE_ID)
    moved_check = revalidate_travel_itinerary_leg(moved, itinerary, 0)
    assert moved_check.outcome == "actor_not_at_origin"
    assert moved_check.reason == "actor_location_changed"


def test_next_leg_requires_matching_committed_arrival_proof_even_at_correct_location():
    itinerary = _itinerary()
    at_middle = _state(actor_location=MIDDLE_ID, current_tick=110)

    missing = revalidate_travel_itinerary_leg(at_middle, itinerary, 1)
    assert missing.outcome == "arrival_proof_required"
    assert missing.reason == "arrival_proof_missing"

    proof = _arrival_proof(itinerary)
    journey = TravelJourney(
        journey_id=proof.journey_id,
        actor_id=proof.actor_id,
        route_id=proof.route_id,
        from_place_id=proof.from_place_id,
        to_place_id=proof.to_place_id,
        departed_tick=proof.departed_tick,
        arrival_tick=proof.arrived_tick,
        status="arrived",
        resolved_tick=proof.arrived_tick,
    )
    arrived = at_middle.model_copy(update={"travel_journeys": [journey]})
    check = revalidate_travel_itinerary_leg(arrived, itinerary, 1, previous_arrival=proof)
    assert check.outcome == "current"
    assert check.leg_id == itinerary.legs[1].leg_id


def test_next_leg_proof_must_be_immediate_plan_leg_and_match_saved_journey():
    itinerary = _itinerary()
    proof = _arrival_proof(itinerary, overrides={"route_id": ROUTE_2_ID})
    state = _state(actor_location=MIDDLE_ID, current_tick=110)

    check = revalidate_travel_itinerary_leg(state, itinerary, 1, previous_arrival=proof)
    assert check.outcome == "arrival_proof_invalid"
    assert check.reason == "arrival_proof_mismatch"

    correct = _arrival_proof(itinerary)
    forged_state = state.model_copy(update={
        "travel_journeys": [TravelJourney(
            journey_id=correct.journey_id,
            actor_id=correct.actor_id,
            route_id=correct.route_id,
            from_place_id=correct.from_place_id,
            to_place_id=correct.to_place_id,
            departed_tick=correct.departed_tick,
            arrival_tick=correct.arrived_tick,
            status="interrupted",
            resolved_tick=correct.arrived_tick,
        )],
    })
    check = revalidate_travel_itinerary_leg(forged_state, itinerary, 1, previous_arrival=correct)
    assert check.outcome == "arrival_proof_invalid"
    assert check.reason == "arrival_proof_mismatch"


def test_arrival_proof_rejects_a_journey_id_not_bound_to_fixed_leg_id():
    itinerary = _itinerary()
    leg = itinerary.legs[0]
    with pytest.raises(ValidationError, match="bound to its itinerary leg"):
        _arrival_proof(itinerary, overrides={"journey_id": uuid4()})


@pytest.mark.parametrize(
    ("route_change", "expected_reason"),
    [
        ({"travel_ticks": 11}, "route_snapshot_changed"),
        ({"risk": "0.25"}, "route_snapshot_changed"),
        ({"access_tags": ["travel.safe_conduct"]}, "route_snapshot_changed"),
        ({"is_open": False}, "route_closed"),
    ],
)
def test_revalidation_reports_route_snapshot_changes(route_change, expected_reason):
    itinerary = _itinerary()
    changed_route = PlaceRoute.model_validate(
        _route(ROUTE_1_ID, ORIGIN_ID, MIDDLE_ID).model_dump(mode="python") | route_change,
    )
    state = _state(routes=(changed_route, _route(ROUTE_2_ID, MIDDLE_ID, DESTINATION_ID, travel_ticks=20)))

    check = revalidate_travel_itinerary_leg(state, itinerary, 0)
    assert check.outcome == "route_invalidated"
    assert check.reason == expected_reason
    assert check.route_snapshot == "mismatched"
    assert check.travel_permission == "not_evaluated"


def test_revalidation_distinguishes_hidden_destination_observation_from_route_authorization():
    itinerary = _itinerary(_state(hidden_destination=True))
    proof = _arrival_proof(itinerary)
    prior_journey = TravelJourney(
        journey_id=proof.journey_id,
        actor_id=proof.actor_id,
        route_id=proof.route_id,
        from_place_id=proof.from_place_id,
        to_place_id=proof.to_place_id,
        departed_tick=proof.departed_tick,
        arrival_tick=proof.arrived_tick,
        status="arrived",
        resolved_tick=proof.arrived_tick,
    )
    prior_schedule = ScheduledEventRecord(
        id=travel_resolution_schedule_id(prior_journey.journey_id),
        due_tick=prior_journey.arrival_tick,
        phase="resolution",
        handler_id=TRAVEL_RESOLUTION_HANDLER_ID,
        handler_version=TRAVEL_RESOLUTION_HANDLER_VERSION,
        status="completed",
        completed_runs=1,
        payload={"journey_id": str(prior_journey.journey_id)},
    )
    at_middle = _state(actor_location=MIDDLE_ID, current_tick=110, hidden_destination=True,
                       journeys=[prior_journey], schedules=[prior_schedule])
    unobserved = revalidate_travel_itinerary_leg(at_middle, itinerary, 1, previous_arrival=proof)
    assert unobserved.outcome == "route_unobserved"
    assert unobserved.reason == "destination_not_observed"
    assert unobserved.route_snapshot == "matched"
    assert unobserved.destination_observation == "unobserved"
    assert unobserved.travel_permission == "not_evaluated"

    observation = KnowledgeRecord(
        id=uuid4(),
        holder_id=ACTOR_ID,
        subject_id=DESTINATION_ID,
        claim="I observed this place.",
        kind="observation",
        received_tick=105,
        valid_from_tick=105,
    )
    state = _state(actor_location=MIDDLE_ID, current_tick=110, hidden_destination=True,
                   knowledge=[observation], journeys=[prior_journey], schedules=[prior_schedule])
    check = revalidate_travel_itinerary_leg(state, itinerary, 1, previous_arrival=proof)
    assert check.outcome == "current"
    assert check.destination_observation == "observed"
    assert check.travel_permission == "not_evaluated"


def test_revalidation_checks_remaining_route_against_absolute_deadline():
    itinerary = _itinerary(deadline=140)
    proof = _arrival_proof(itinerary)
    prior_journey = TravelJourney(
        journey_id=proof.journey_id,
        actor_id=proof.actor_id,
        route_id=proof.route_id,
        from_place_id=proof.from_place_id,
        to_place_id=proof.to_place_id,
        departed_tick=proof.departed_tick,
        arrival_tick=proof.arrived_tick,
        status="arrived",
        resolved_tick=proof.arrived_tick,
    )
    prior_schedule = ScheduledEventRecord(
        id=travel_resolution_schedule_id(prior_journey.journey_id),
        due_tick=prior_journey.arrival_tick,
        phase="resolution",
        handler_id=TRAVEL_RESOLUTION_HANDLER_ID,
        handler_version=TRAVEL_RESOLUTION_HANDLER_VERSION,
        status="completed",
        completed_runs=1,
        payload={"journey_id": str(prior_journey.journey_id)},
    )
    after_deadline = _state(actor_location=MIDDLE_ID, current_tick=141, journeys=[prior_journey],
                            schedules=[prior_schedule])
    check = revalidate_travel_itinerary_leg(after_deadline, itinerary, 1, previous_arrival=proof)
    assert check.outcome == "deadline_elapsed"
    assert check.reason == "deadline_passed"

    too_late = _state(actor_location=MIDDLE_ID, current_tick=125, journeys=[prior_journey],
                      schedules=[prior_schedule])
    check = revalidate_travel_itinerary_leg(too_late, itinerary, 1, previous_arrival=proof)
    assert check.outcome == "deadline_unreachable"
    assert check.reason == "remaining_route_cannot_fit_deadline"
