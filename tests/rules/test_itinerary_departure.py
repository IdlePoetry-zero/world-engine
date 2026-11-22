from decimal import Decimal
from uuid import UUID, uuid4, uuid5

import pytest

from world_engine.domain.components import PlaceComponent, PlaceRoute
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent, travel_event_id
from world_engine.rules import RuleContext, RuleEngine
from world_engine.simulation.contracts import LongActionPlan, SimulationContext
from world_engine.simulation.engine import SimulationEngine


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
ACTOR_ID = UUID("40000000-0000-4000-8000-000000000004")
ORIGIN_ID = UUID("50000000-0000-4000-8000-000000000005")
DESTINATION_ID = UUID("60000000-0000-4000-8000-000000000006")
ROUTE_ID = UUID("70000000-0000-4000-8000-000000000007")
ITINERARY_JOURNEY_ID = UUID("80000000-0000-4000-8000-000000000008")
RULESET = "travel-itinerary-test.v1"


def _state(*, risk: str = "0.5", travel_ticks: int = 120) -> WorldState:
    route = PlaceRoute(
        route_id=ROUTE_ID,
        from_place_id=ORIGIN_ID,
        to_place_id=DESTINATION_ID,
        travel_ticks=travel_ticks,
        risk=Decimal(risk),
    )
    return WorldState(
        current_tick=10,
        entities={
            ORIGIN_ID: WorldEntity(
                id=ORIGIN_ID,
                kind="place",
                components={"place": PlaceComponent(routes=[route]).model_dump(mode="json")},
            ),
            DESTINATION_ID: WorldEntity(
                id=DESTINATION_ID,
                kind="place",
                components={"place": PlaceComponent().model_dump(mode="json")},
            ),
            ACTOR_ID: WorldEntity(
                id=ACTOR_ID,
                kind="character",
                location_id=ORIGIN_ID,
                components={"capabilities": {"grants": ["world.travel"], "restrictions": []}},
            ),
        },
    )


def _context(
    *,
    command_id: UUID = COMMAND_ID,
    world_id: UUID = WORLD_ID,
    branch_id: UUID = BRANCH_ID,
    ruleset_version: str = RULESET,
) -> RuleContext:
    return RuleContext(
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=ACTOR_ID,
        ruleset_version=ruleset_version,
    )


def _travel_result(context: RuleContext, *, journey_id: UUID | None, state=None):
    return RuleEngine().evaluate_travel_departure(
        state or _state(risk="1"),
        ROUTE_ID,
        context,
        itinerary_journey_id=journey_id,
    )


def test_itinerary_departure_is_replay_stable_after_lost_command_and_worker_restart():
    first_context = _context(command_id=COMMAND_ID)
    retry_context = _context(command_id=uuid4())

    first = _travel_result(first_context, journey_id=ITINERARY_JOURNEY_ID)
    retry = _travel_result(retry_context, journey_id=ITINERARY_JOURNEY_ID)

    assert first.outcome.status == retry.outcome.status == "succeeded"
    assert first.rolls == retry.rolls
    expected_roll = RuleEngine._roll(
        first_context.model_copy(update={"command_id": ITINERARY_JOURNEY_ID}),
        "movement.route_risk",
        100,
    )
    assert first.rolls[0] == expected_roll.model_copy(update={"outcome": "hazard"})

    first_event, = first.events
    retry_event, = retry.events
    assert isinstance(first_event, TravelJourneyChangedEvent)
    assert isinstance(retry_event, TravelJourneyChangedEvent)
    assert first_event.journey.journey_id == retry_event.journey.journey_id == ITINERARY_JOURNEY_ID
    assert first_event.command_id == COMMAND_ID
    assert retry_event.command_id == retry_context.command_id
    assert first_event.event_id == travel_event_id(COMMAND_ID, RULESET, "depart", ITINERARY_JOURNEY_ID)
    assert retry_event.event_id == travel_event_id(retry_context.command_id, RULESET, "depart", ITINERARY_JOURNEY_ID)
    assert first_event.event_id != retry_event.event_id
    assert first_event.hazard_timing == retry_event.hazard_timing == "journey_seed_v1"
    assert first_event.journey.hazard_tick == retry_event.journey.hazard_tick


def test_itinerary_risk_roll_still_binds_world_branch_ruleset_and_leg_identity():
    base = _travel_result(_context(), journey_id=ITINERARY_JOURNEY_ID).rolls[0]
    other_leg = _travel_result(_context(), journey_id=uuid4()).rolls[0]
    other_branch = _travel_result(
        _context(branch_id=uuid4()), journey_id=ITINERARY_JOURNEY_ID,
    ).rolls[0]
    other_world = _travel_result(
        _context(world_id=uuid4()), journey_id=ITINERARY_JOURNEY_ID,
    ).rolls[0]
    other_ruleset = _travel_result(
        _context(ruleset_version="travel-itinerary-test.v2"),
        journey_id=ITINERARY_JOURNEY_ID,
    ).rolls[0]

    assert base.digest != other_leg.digest
    assert base.digest != other_branch.digest
    assert base.digest != other_world.digest
    assert base.digest != other_ruleset.digest


def test_legacy_departure_keeps_command_scoped_roll_and_journey_ids():
    context = _context()
    other_context = _context(command_id=uuid4())

    result = _travel_result(context, journey_id=None)
    other_result = _travel_result(other_context, journey_id=None)

    expected_roll = RuleEngine._roll(context, "movement.route_risk", 100)
    assert result.rolls[0] == expected_roll.model_copy(update={"outcome": "hazard"})
    assert result.rolls[0].digest != other_result.rolls[0].digest
    event, = result.events
    assert isinstance(event, TravelJourneyChangedEvent)
    legacy_journey_id = uuid5(context.command_id, "travel-journey")
    assert event.journey.journey_id == legacy_journey_id
    assert event.hazard_timing is None
    assert event.journey.hazard_tick == event.journey.departed_tick + max(
        1, (event.journey.arrival_tick - event.journey.departed_tick) // 2,
    )
    assert event.command_id == context.command_id
    assert event.event_id == travel_event_id(context.command_id, RULESET, "depart", legacy_journey_id)


def test_short_legacy_hazard_keeps_the_historical_one_tick_boundary():
    state = _state(risk="1", travel_ticks=1)

    result = RuleEngine().evaluate_travel_departure(state, ROUTE_ID, _context())

    event, = result.events
    assert isinstance(event, TravelJourneyChangedEvent)
    assert event.hazard_timing is None
    assert event.journey.hazard_tick == event.journey.departed_tick + 1


def test_itinerary_departure_rejects_non_uuid_identity_without_events():
    result = RuleEngine().evaluate_travel_departure(
        _state(risk="1"),
        ROUTE_ID,
        _context(),
        itinerary_journey_id="not-a-uuid",  # type: ignore[arg-type]
    )

    assert result.outcome.status == "rejected"
    assert result.outcome.code == "travel_itinerary_identity_invalid"
    assert result.events == []
    assert result.rolls == []


def test_distinct_stable_itinerary_ids_select_distinct_offsets_on_the_same_long_route():
    travel_ticks = 175 * 86_400
    route_state = _state(risk="1", travel_ticks=travel_ticks)
    journey_ids = (
        ITINERARY_JOURNEY_ID,
        UUID("81000000-0000-4000-8000-000000000009"),
        UUID("82000000-0000-4000-8000-00000000000a"),
        UUID("83000000-0000-4000-8000-00000000000b"),
    )

    departed = [
        _travel_result(_context(command_id=uuid4()), journey_id=journey_id, state=route_state).events[0]
        for journey_id in journey_ids
    ]
    offsets = {
        event.journey.hazard_tick - event.journey.departed_tick
        for event in departed
    }

    assert len(offsets) > 1
    assert all(0 < offset <= travel_ticks for offset in offsets)
    assert all(event.hazard_timing == "journey_seed_v1" for event in departed)


@pytest.mark.parametrize(
    ("risk", "outcome", "operation"),
    [("0", "completed", "arrive"), ("1", "stopped", "interrupt")],
)
def test_months_long_route_advances_to_the_first_actual_travel_boundary(risk, outcome, operation):
    day = 86_400
    travel_ticks = 175 * day
    state = _state(risk=risk, travel_ticks=travel_ticks)
    start = state.current_tick
    departure = _travel_result(_context(), journey_id=ITINERARY_JOURNEY_ID, state=state)
    departure_event, = departure.events
    assert isinstance(departure_event, TravelJourneyChangedEvent)
    expected_tick = (
        departure_event.journey.hazard_tick
        if departure_event.journey.hazard_tick is not None
        else departure_event.journey.arrival_tick
    )
    assert departure_event.hazard_timing == ("journey_seed_v1" if risk == "1" else None)
    for event in departure.events:
        state = apply_event(state, event)
    plan = LongActionPlan(
        actor_id=ACTOR_ID,
        started_tick=start,
        target_tick=start + 360 * day,
        activity="travel",
        route_id=ROUTE_ID,
    )
    context = SimulationContext(
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=uuid4(),
        state_version=1,
        world_seed="sparse-travel-test",
        itinerary_journey_id=ITINERARY_JOURNEY_ID,
        itinerary_auto_care=True,
    )
    engine = SimulationEngine()
    advance = engine.plan_window(state, plan, context)
    assert expected_tick > start
    assert advance.outcome == "advanced"
    assert advance.next_tick == expected_tick
    assert advance.interruption is None
    for event in advance.events:
        state = apply_event(state, event)

    next_context = context.model_copy(update={"command_id": uuid4(), "state_version": 2})
    resolved = engine.plan_window(state, plan, next_context)
    assert resolved.outcome == outcome
    assert resolved.next_tick == expected_tick
    journey_event, = [event for event in resolved.events if isinstance(event, TravelJourneyChangedEvent)]
    assert journey_event.operation == operation
    if risk == "1":
        assert resolved.interruption is not None
        assert resolved.interruption.reason_code == "route_hazard"
        assert resolved.interruption.tick == expected_tick
    for event in resolved.events:
        state = apply_event(state, event)
    assert state.entities[ACTOR_ID].location_id == (DESTINATION_ID if risk == "0" else ORIGIN_ID)
