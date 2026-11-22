from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4, uuid5

import pytest

from world_engine.domain.components import CombatEncounterComponent, PlaceComponent, PlaceRoute
from world_engine.domain.gameplay_events import DamageAppliedEvent
from world_engine.domain.models import TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.domain.travel_journey_events import (
    TravelJourneyChangedEvent,
    travel_event_id,
    travel_journey_visibility,
)
from world_engine.rules import RuleContext, RuleEngine


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
ACTOR_ID = UUID("40000000-0000-4000-8000-000000000004")
ORIGIN_ID = UUID("50000000-0000-4000-8000-000000000005")
DESTINATION_ID = UUID("60000000-0000-4000-8000-000000000006")
ROUTE_ID = UUID("70000000-0000-4000-8000-000000000007")
OPPONENT_ID = UUID("80000000-0000-4000-8000-000000000008")
RULESET = "travel-rules.v1"


def _context(*, actor_id: UUID = ACTOR_ID, command_id: UUID = COMMAND_ID) -> RuleContext:
    return RuleContext(
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=RULESET,
    )


def _state(
    *, risk: str = "0", destination_hidden: bool = False, combat: bool = False, can_travel: bool = True,
) -> WorldState:
    route = PlaceRoute(
        route_id=ROUTE_ID,
        from_place_id=ORIGIN_ID,
        to_place_id=DESTINATION_ID,
        travel_ticks=120,
        risk=Decimal(risk),
    )
    actor_components: dict[str, object] = {"health": {"current": 80, "maximum": 100}}
    if can_travel:
        actor_components["capabilities"] = {"grants": ["world.travel"], "restrictions": []}
    entities = {
        ORIGIN_ID: WorldEntity(
            id=ORIGIN_ID,
            kind="place",
            name="Origin",
            components={"place": PlaceComponent(routes=[route]).model_dump(mode="json")},
        ),
        DESTINATION_ID: WorldEntity(
            id=DESTINATION_ID,
            kind="place",
            name="Destination",
            components={
                "place": {"access_policy": "open"},
                **({"hidden": True} if destination_hidden else {}),
            },
        ),
        ACTOR_ID: WorldEntity(
            id=ACTOR_ID,
            kind="character",
            name="Traveler",
            location_id=ORIGIN_ID,
            components=actor_components,
        ),
    }
    if combat:
        encounter_id, last_event_id = uuid4(), uuid4()
        encounter_fields = {
            "encounter_id": encounter_id,
            "location_id": ORIGIN_ID,
            "started_tick": 0,
            "status": "active",
            "exchange_count": 1,
            "last_event_id": last_event_id,
            "last_outcome": "miss",
        }
        actor_encounter = CombatEncounterComponent(opponent_id=OPPONENT_ID, **encounter_fields)
        opponent_encounter = CombatEncounterComponent(opponent_id=ACTOR_ID, **encounter_fields)
        entities[ACTOR_ID] = entities[ACTOR_ID].model_copy(update={
            "components": {**entities[ACTOR_ID].components,
                           "combat_encounter": actor_encounter.model_dump(mode="json")},
        })
        entities[OPPONENT_ID] = WorldEntity(
            id=OPPONENT_ID,
            kind="character",
            name="Opponent",
            location_id=ORIGIN_ID,
            components={"combat_encounter": opponent_encounter.model_dump(mode="json")},
        )
    return WorldState(current_tick=10, entities=entities, player_id=ACTOR_ID)


def test_departure_emits_one_scoped_private_event_without_advancing_time() -> None:
    state = _state()
    context = _context()

    result = RuleEngine().evaluate_travel_departure(state, ROUTE_ID, context)

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "travel_departed"
    assert result.elapsed_ticks == 0
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, TravelJourneyChangedEvent)
    journey_id = uuid5(context.command_id, "travel-journey")
    assert event.event_id == travel_event_id(context.command_id, RULESET, "depart", journey_id)
    assert (event.world_id, event.branch_id, event.command_id, event.actor_id, event.ruleset_version) == (
        WORLD_ID, BRANCH_ID, COMMAND_ID, ACTOR_ID, RULESET,
    )
    assert event.operation == "depart"
    assert event.journey.journey_id == journey_id
    assert (event.journey.from_place_id, event.journey.to_place_id) == (ORIGIN_ID, DESTINATION_ID)
    assert (event.journey.departed_tick, event.journey.arrival_tick, event.journey.hazard_tick) == (10, 130, None)
    assert event.visibility == travel_journey_visibility(ACTOR_ID)

    after = apply_event(state, event)
    assert after.current_tick == state.current_tick
    assert after.entities[ACTOR_ID].location_id is None
    assert after.travel_journeys == [event.journey]
    assert after.scheduled_events[0].due_tick == 130


def test_departure_preserves_route_risk_roll_and_declares_hazard_tick() -> None:
    state = _state(risk="1")
    result = RuleEngine().evaluate_travel_departure(state, ROUTE_ID, _context())

    assert result.outcome.status == "succeeded"
    assert len(result.rolls) == 1
    roll = result.rolls[0]
    assert (roll.label, roll.maximum, roll.outcome) == ("movement.route_risk", 100, "hazard")
    expected_roll = RuleEngine._roll(_context(), "movement.route_risk", 100)
    assert roll == expected_roll.model_copy(update={"outcome": "hazard"})
    event = result.events[0]
    assert isinstance(event, TravelJourneyChangedEvent)
    assert event.journey.hazard_tick == 70
    assert event.journey.arrival_tick == 130
    assert result.elapsed_ticks == 0


@pytest.mark.parametrize(
    ("state", "expected_code"),
    [
        (_state(destination_hidden=True), "destination_unseen"),
        (_state(combat=True), "combat_encounter_active"),
        (_state(can_travel=False), "travel_capability_required"),
    ],
)
def test_departure_reuses_destination_visibility_and_combat_gates(state, expected_code: str) -> None:
    result = RuleEngine().evaluate_travel_departure(state, ROUTE_ID, _context())

    assert result.outcome.status == "rejected"
    assert result.outcome.code == expected_code
    assert result.events == []
    assert result.elapsed_ticks == 0


def test_arrival_resolves_at_the_due_tick_and_restores_actor_location() -> None:
    state = _state()
    engine = RuleEngine()
    departure = engine.evaluate_travel_departure(state, ROUTE_ID, _context())
    after_departure = apply_event(state, departure.events[0])
    journey_id = after_departure.travel_journeys[0].journey_id
    at_arrival = apply_event(after_departure, TimeAdvancedEvent(to_tick=130))

    early = engine.evaluate_travel_resolution(at_arrival.model_copy(update={"current_tick": 129}), journey_id, _context())
    assert early.outcome.status == "rejected"
    assert early.outcome.code == "travel_journey_not_due"
    assert early.events == []

    context = _context(command_id=uuid4())
    result = engine.evaluate_travel_resolution(at_arrival, journey_id, context)

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "travel_arrived"
    assert result.elapsed_ticks == 0
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, TravelJourneyChangedEvent)
    assert event.operation == "arrive"
    assert event.event_id == travel_event_id(context.command_id, RULESET, "arrive", journey_id)
    after_arrival = apply_event(at_arrival, event)
    assert after_arrival.current_tick == 130
    assert after_arrival.entities[ACTOR_ID].location_id == DESTINATION_ID
    assert after_arrival.travel_journeys[0].status == "arrived"
    assert after_arrival.scheduled_events[0].status == "completed"


def test_hazard_resolution_interrupts_without_claiming_future_injury_effects() -> None:
    state = _state(risk="1")
    engine = RuleEngine()
    departure = engine.evaluate_travel_departure(state, ROUTE_ID, _context())
    after_departure = apply_event(state, departure.events[0])
    journey = after_departure.travel_journeys[0]
    assert journey.hazard_tick == 70
    at_hazard = apply_event(after_departure, TimeAdvancedEvent(to_tick=journey.hazard_tick))
    context = _context(command_id=uuid4())

    result = engine.evaluate_travel_resolution(at_hazard, journey.journey_id, context)

    assert result.outcome.status == "partial"
    assert result.outcome.code == "route_hazard"
    assert result.elapsed_ticks == 0
    assert result.interruptions[0].code == "route_hazard"
    assert len(result.events) == 2
    journey_event, damage_event = result.events
    assert isinstance(journey_event, TravelJourneyChangedEvent)
    assert journey_event.operation == "interrupt"
    assert isinstance(damage_event, DamageAppliedEvent)
    assert damage_event.damage == 5
    assert damage_event.injury_type == "travel.route_hazard"
    assert damage_event.description == "Injury sustained during a route hazard."
    assert damage_event.expected_health_revision == 1
    after_interrupt = apply_event(at_hazard, journey_event)
    after_interrupt = apply_event(after_interrupt, damage_event)
    assert after_interrupt.current_tick == journey.hazard_tick
    assert after_interrupt.entities[ACTOR_ID].location_id == ORIGIN_ID
    assert after_interrupt.travel_journeys[0].status == "interrupted"
    assert after_interrupt.entities[ACTOR_ID].components["health"]["current"] == 75
    assert after_interrupt.scheduled_events[0].status == "completed"
