from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import (
    InventoryComponent,
    InventoryLot,
    LifeComponent,
    NeedsComponent,
    PlaceComponent,
    PlaceRoute,
    ScheduledEventRecord,
    TreasuryComponent,
)
from world_engine.domain.gameplay_events import ScheduledEventCompletedEvent
from world_engine.domain.models import ResourceAccount, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.occupations import OccupationSource, WorkAssignment, WorkPosting, occupation_assignment_id
from world_engine.domain.reducers import apply_event
from world_engine.domain.travel_itinerary import TravelItinerary, TravelItineraryLeg, travel_itinerary_leg_id
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent
from world_engine.domain.travel_journeys import active_travel_journey
from world_engine.domain.work_shift import WorkShiftTerms
from world_engine.domain.work_shift_events import WorkShiftPaidEvent
from world_engine.rules import RuleContext, RuleEngine
from world_engine.simulation.contracts import LongActionPlan, RoutineWorkSchedule, SimulationContext
from world_engine.simulation.engine import SimulationEngine


DAY = 86_400
SHIFT = 3_600
ACTOR_ID = UUID("10000000-0000-4000-8000-000000000001")
ORIGIN_ID = UUID("20000000-0000-4000-8000-000000000002")
DESTINATION_ID = UUID("30000000-0000-4000-8000-000000000003")
ROUTE_ID = UUID("40000000-0000-4000-8000-000000000004")
JOURNEY_ID = UUID("50000000-0000-4000-8000-000000000005")
FOOD_ACCOUNT_ID = UUID("60000000-0000-4000-8000-000000000006")
FOOD_SINK_ID = UUID("70000000-0000-4000-8000-000000000007")
WATER_ACCOUNT_ID = UUID("80000000-0000-4000-8000-000000000008")
WATER_SINK_ID = UUID("90000000-0000-4000-8000-000000000009")
FOOD_LOT_ID = UUID("a0000000-0000-4000-8000-00000000000a")
WATER_LOT_ID = UUID("b0000000-0000-4000-8000-00000000000b")


def _travel_state(
    *, food=25, water=90, rest=100, food_stock="2", water_stock="0",
    travel_ticks=SHIFT, risk="0",
):
    route = PlaceRoute(
        route_id=ROUTE_ID,
        from_place_id=ORIGIN_ID,
        to_place_id=DESTINATION_ID,
        travel_ticks=travel_ticks,
        risk=Decimal(risk),
        is_open=True,
    )
    inventory_lots = []
    resources = {}
    if Decimal(food_stock) > 0:
        resources[FOOD_ACCOUNT_ID] = ResourceAccount(
            resource_id="core.food", balance=food_stock, owner_id=ACTOR_ID,
        )
        inventory_lots.append(InventoryLot(
            lot_id=FOOD_LOT_ID, account_id=FOOD_ACCOUNT_ID,
            resource_id="core.food", quantity=food_stock,
        ))
    if Decimal(water_stock) > 0:
        resources[WATER_ACCOUNT_ID] = ResourceAccount(
            resource_id="core.water", balance=water_stock, owner_id=ACTOR_ID,
        )
        inventory_lots.append(InventoryLot(
            lot_id=WATER_LOT_ID, account_id=WATER_ACCOUNT_ID,
            resource_id="core.water", quantity=water_stock,
        ))
    if Decimal(food_stock) > 0:
        resources[FOOD_SINK_ID] = ResourceAccount(
            resource_id="core.food", balance="0", account_type="consumption_sink",
        )
    if Decimal(water_stock) > 0:
        resources[WATER_SINK_ID] = ResourceAccount(
            resource_id="core.water", balance="0", account_type="consumption_sink",
        )
    state = WorldState(
        current_tick=0,
        player_id=ACTOR_ID,
        calendar={"day_seconds": DAY},
        entities={
            ACTOR_ID: WorldEntity(
                id=ACTOR_ID,
                kind="character",
                status="active",
                location_id=ORIGIN_ID,
                components={
                    "needs": NeedsComponent(
                        food={"value": food, "critical_below": 20},
                        water={"value": water, "critical_below": 20},
                        rest={"value": rest, "critical_below": 20},
                    ).model_dump(mode="json"),
                    "inventory": InventoryComponent(lots=inventory_lots).model_dump(mode="json"),
                    "capabilities": {"grants": ["world.travel"], "restrictions": []},
                },
            ),
            ORIGIN_ID: WorldEntity(
                id=ORIGIN_ID,
                kind="place",
                status="active",
                components={"place": PlaceComponent(place_type="settlement", routes=[route]).model_dump(mode="json")},
            ),
            DESTINATION_ID: WorldEntity(
                id=DESTINATION_ID,
                kind="place",
                status="active",
                components={"place": PlaceComponent(place_type="settlement").model_dump(mode="json")},
            ),
        },
        resources=resources,
        metadata={"resource_roles": {
            "food_resource_id": "core.food",
            "water_resource_id": "core.water",
        }},
    )
    depart_context = RuleContext(
        world_id=UUID("c0000000-0000-4000-8000-00000000000c"),
        branch_id=UUID("d0000000-0000-4000-8000-00000000000d"),
        command_id=uuid4(),
        actor_id=ACTOR_ID,
        ruleset_version="1.0.0",
    )
    departure = RuleEngine().evaluate_travel_departure(
        state, ROUTE_ID, depart_context, itinerary_journey_id=JOURNEY_ID,
    )
    return _apply(state, departure.events)


def _sim_context(*, version=0, auto_care=False, journey_id=JOURNEY_ID):
    return SimulationContext(
        world_id=UUID("c0000000-0000-4000-8000-00000000000c"),
        branch_id=UUID("d0000000-0000-4000-8000-00000000000d"),
        command_id=uuid4(),
        world_seed="itinerary-auto-care",
        state_version=version,
        itinerary_journey_id=journey_id,
        itinerary_auto_care=auto_care,
    )


def _travel_plan(*, target_tick=10 * DAY):
    return LongActionPlan(
        actor_id=ACTOR_ID,
        started_tick=0,
        target_tick=target_tick,
        activity="travel",
        route_id=ROUTE_ID,
    )


def _apply(state, events):
    for event in events:
        state = apply_event(state, event)
    return state


def _itinerary(*, target_tick=10_000):
    itinerary_id = UUID("e0000000-0000-4000-8000-00000000000e")
    return TravelItinerary(
        itinerary_id=itinerary_id,
        actor_id=ACTOR_ID,
        origin_place_id=ORIGIN_ID,
        destination_place_id=DESTINATION_ID,
        created_tick=0,
        deadline_tick=target_tick,
        legs=(TravelItineraryLeg(
            leg_id=travel_itinerary_leg_id(itinerary_id, 0, ROUTE_ID),
            leg_index=0,
            route_id=ROUTE_ID,
            from_place_id=ORIGIN_ID,
            to_place_id=DESTINATION_ID,
            travel_ticks=SHIFT,
            route_fingerprint="a" * 64,
        ),),
    )


def test_itinerary_auto_care_is_explicit_optional_and_bound_to_verified_journey():
    itinerary = _itinerary()
    plan = LongActionPlan(
        actor_id=ACTOR_ID, started_tick=0, target_tick=10_000,
        activity="travel", travel_itinerary=itinerary, itinerary_auto_care=True,
    )
    assert plan.itinerary_auto_care is True
    assert "itinerary_auto_care" not in LongActionPlan(
        actor_id=ACTOR_ID, started_tick=0, target_tick=10_000,
        activity="travel", route_id=ROUTE_ID,
    ).model_dump(mode="json")
    with pytest.raises(ValidationError, match="requires a persisted travel_itinerary"):
        LongActionPlan(
            actor_id=ACTOR_ID, started_tick=0, target_tick=10_000,
            activity="travel", route_id=ROUTE_ID, itinerary_auto_care=True,
        )
    with pytest.raises(ValidationError, match="verified itinerary_journey_id"):
        SimulationContext(
            world_id=UUID("c0000000-0000-4000-8000-00000000000c"),
            branch_id=UUID("d0000000-0000-4000-8000-00000000000d"),
            command_id=uuid4(), world_seed="test", state_version=0,
            itinerary_auto_care=True,
        )


def test_opted_in_food_care_uses_owned_stock_and_delays_arrival_truthfully():
    state = _travel_state(food=20, food_stock="2", travel_ticks=SHIFT)
    engine = SimulationEngine()
    plan = _travel_plan()

    care = engine.plan_window(state, plan, _sim_context(auto_care=True))

    assert care.outcome == "advanced"
    assert care.metrics.integration_boundary == "itinerary_auto_care_completed"
    delay, = [event for event in care.events if isinstance(event, TravelJourneyChangedEvent)]
    assert delay.operation == "delay"
    assert delay.journey.journey_id == JOURNEY_ID
    assert (delay.stationary_from_tick, delay.stationary_until_tick) == (0, 300)
    assert delay.journey.arrival_tick == SHIFT + 300
    assert next(event for event in care.events if isinstance(event, TimeAdvancedEvent)).to_tick == 300
    state = _apply(state, care.events)
    assert state.resources[FOOD_ACCOUNT_ID].balance == Decimal("1")
    assert state.resources[FOOD_SINK_ID].balance == Decimal("1")
    assert state.entities[ACTOR_ID].components["inventory"]["lots"][0]["quantity"] == "1"
    assert state.entities[ACTOR_ID].components["needs"]["food"]["value"] == 30
    assert active_travel_journey(state, ACTOR_ID).arrival_tick == SHIFT + 300

    for version in range(1, 5):
        resolution = engine.plan_window(state, plan, _sim_context(version=version, auto_care=True))
        state = _apply(state, resolution.events)
        if resolution.outcome == "completed":
            break
    assert state.current_tick == SHIFT + 300
    assert state.entities[ACTOR_ID].location_id == DESTINATION_ID
    arrived = next(row for row in state.travel_journeys if row.journey_id == JOURNEY_ID)
    assert arrived.status == "arrived"
    assert arrived.resolved_tick == SHIFT + 300


def test_opted_in_rest_uses_stationary_rule_and_preserves_travel_distance():
    travel_ticks = 9 * DAY
    state = _travel_state(food=90, water=90, rest=30, travel_ticks=travel_ticks, risk="1")
    initial_journey = active_travel_journey(state, ACTOR_ID)
    assert initial_journey is not None and initial_journey.hazard_tick is not None
    initial_hazard_tick = initial_journey.hazard_tick
    resolution = SimulationEngine().plan_window(state, _travel_plan(), _sim_context(auto_care=True))

    assert resolution.outcome == "advanced"
    assert resolution.metrics.integration_boundary == "itinerary_auto_care_completed"
    delay, = [event for event in resolution.events if isinstance(event, TravelJourneyChangedEvent)]
    assert delay.operation == "delay"
    elapsed = resolution.metrics.ticks_advanced
    assert elapsed == DAY // 3
    assert delay.journey.arrival_tick == travel_ticks + elapsed
    assert delay.journey.hazard_tick == initial_hazard_tick + elapsed
    state = _apply(state, resolution.events)
    assert state.current_tick == elapsed
    assert state.entities[ACTOR_ID].components["needs"]["rest"]["value"] > 30
    assert active_travel_journey(state, ACTOR_ID).stationary_ticks == elapsed


def test_opted_in_rest_splits_at_first_other_need_threshold_then_resolves_that_care():
    state = _travel_state(
        food=90, water=26, rest=30, food_stock="2", water_stock="2",
        travel_ticks=2 * DAY,
    )
    engine = SimulationEngine()

    rest = engine.plan_window(state, _travel_plan(), _sim_context(auto_care=True))

    assert rest.outcome == "advanced"
    assert rest.metrics.integration_boundary == "itinerary_auto_care_completed"
    assert 60 <= rest.metrics.ticks_advanced < DAY // 3
    rest_delay, = [event for event in rest.events if isinstance(event, TravelJourneyChangedEvent)]
    assert rest_delay.stationary_until_tick == rest.next_tick
    assert rest_delay.journey.arrival_tick == 2 * DAY + rest.metrics.ticks_advanced
    state = _apply(state, rest.events)
    assert state.entities[ACTOR_ID].components["needs"]["rest"]["value"] > 30
    assert state.entities[ACTOR_ID].components["needs"]["water"]["value"] == 20

    drink = engine.plan_window(state, _travel_plan(), _sim_context(version=1, auto_care=True))

    assert drink.outcome == "advanced"
    assert drink.metrics.resources_consumed == (("core.water", "1"),)
    drink_delay, = [event for event in drink.events if isinstance(event, TravelJourneyChangedEvent)]
    assert drink_delay.stationary_from_tick == state.current_tick
    assert drink_delay.stationary_until_tick == drink.next_tick
    assert drink_delay.journey.arrival_tick == 2 * DAY + drink.next_tick


def test_rest_window_splits_at_external_boundary_without_resolving_it_in_care_batch():
    due_tick = DAY // 8
    state = _travel_state(
        food=90, water=90, rest=30, travel_ticks=8 * DAY, risk="1",
    )
    initial_journey = active_travel_journey(state, ACTOR_ID)
    assert initial_journey is not None and initial_journey.hazard_tick is not None
    initial_hazard_tick = initial_journey.hazard_tick
    external = ScheduledEventRecord(
        id=UUID("f1000000-0000-4000-8000-00000000000f"),
        due_tick=due_tick,
        phase="environment",
        handler_id="test.rest_window_boundary",
        handler_version="1.0.0",
    )
    state = state.model_copy(update={"scheduled_events": [*state.scheduled_events, external]})
    engine = SimulationEngine()

    rest = engine.plan_window(state, _travel_plan(), _sim_context(auto_care=True))

    assert rest.outcome == "advanced"
    assert rest.next_tick == due_tick
    assert rest.metrics.ticks_advanced == due_tick
    delay, = [event for event in rest.events if isinstance(event, TravelJourneyChangedEvent)]
    assert delay.stationary_until_tick == due_tick
    assert delay.journey.arrival_tick == 8 * DAY + due_tick
    assert delay.journey.hazard_tick == initial_hazard_tick + due_tick
    assert not any(isinstance(event, ScheduledEventCompletedEvent) for event in rest.events)
    state = _apply(state, rest.events)
    assert next(item for item in state.scheduled_events if item.id == external.id).status == "queued"

    due = engine.plan_window(state, _travel_plan(), _sim_context(version=1, auto_care=True))

    assert due.outcome == "rule_required"
    assert due.metrics.integration_boundary == "scheduled_handler_unregistered"
    assert due.events == ()
    assert due.next_tick == due_tick


def test_partial_rest_at_exact_lifespan_boundary_uses_normal_boundary_resolution():
    state = _travel_state(food=90, water=90, rest=30, travel_ticks=DAY)
    actor = state.entities[ACTOR_ID]
    components = {
        **actor.components,
        "life": LifeComponent(birth_tick=0, lifespan_ticks=DAY // 3).model_dump(mode="json"),
    }
    state = state.model_copy(update={
        "entities": state.entities | {ACTOR_ID: actor.model_copy(update={"components": components})},
    })

    resolution = SimulationEngine().plan_window(
        state, _travel_plan(), _sim_context(auto_care=True),
    )

    assert resolution.outcome == "stopped"
    assert resolution.next_tick == DAY // 3
    assert resolution.interruption.reason_code == "actor_lifespan_expired"
    assert not any(
        isinstance(event, TravelJourneyChangedEvent) and event.operation == "delay"
        for event in resolution.events
    )
    assert any(event.type == "core.life_ended.v1" for event in resolution.events)


def test_opted_in_care_pauses_when_stock_is_unavailable_without_consuming_time():
    state = _travel_state(food=20, food_stock="0", travel_ticks=SHIFT)
    resolution = SimulationEngine().plan_window(state, _travel_plan(), _sim_context(auto_care=True))

    assert resolution.outcome == "stopped"
    assert resolution.interruption.reason_code == "itinerary_auto_care_resource_unavailable"
    assert resolution.events == ()
    assert resolution.next_tick == state.current_tick
    assert state.current_tick == 0


def test_manual_itinerary_travel_does_not_auto_care_when_flag_is_false():
    state = _travel_state(food=20, food_stock="2", travel_ticks=SHIFT)
    resolution = SimulationEngine().plan_window(state, _travel_plan(target_tick=SHIFT), _sim_context())

    assert resolution.outcome == "advanced"
    assert not any(
        isinstance(event, TravelJourneyChangedEvent) and event.operation == "delay"
        for event in resolution.events
    )
    assert state.resources[FOOD_ACCOUNT_ID].balance == Decimal("2")
    assert not any(event.type == "core.resource_consumed.v1" for event in resolution.events)


def test_due_world_event_wins_same_tick_auto_care_boundary():
    state = _travel_state(food=20, food_stock="2", travel_ticks=SHIFT)
    external = ScheduledEventRecord(
        id=UUID("f0000000-0000-4000-8000-00000000000f"),
        due_tick=0,
        phase="environment",
        handler_id="test.auto_care_gate",
        handler_version="1.0.0",
    )
    state = state.model_copy(update={"scheduled_events": [*state.scheduled_events, external]})
    resolution = SimulationEngine().plan_window(state, _travel_plan(), _sim_context(auto_care=True))

    assert resolution.outcome == "rule_required"
    assert resolution.metrics.integration_boundary == "scheduled_handler_unregistered"
    assert resolution.events == ()
    assert state.current_tick == 0
    assert state.resources[FOOD_ACCOUNT_ID].balance == Decimal("2")


def test_work_rest_forecast_keeps_buffer_after_a_scheduled_meal_before_shift():
    actor_id = UUID("11000000-0000-4000-8000-000000000011")
    employer_id = UUID("12000000-0000-4000-8000-000000000012")
    workplace_id = UUID("13000000-0000-4000-8000-000000000013")
    posting_id = UUID("14000000-0000-4000-8000-000000000014")
    source_id = UUID("15000000-0000-4000-8000-000000000015")
    worker_account_id = UUID("16000000-0000-4000-8000-000000000016")
    employer_account_id = UUID("17000000-0000-4000-8000-000000000017")
    food_account_id = UUID("18000000-0000-4000-8000-000000000018")
    food_sink_id = UUID("19000000-0000-4000-8000-000000000019")
    food_lot_id = UUID("1a000000-0000-4000-8000-00000000001a")
    source = OccupationSource(kind="event", reference_id=str(source_id))
    assignment = WorkAssignment(
        assignment_id=occupation_assignment_id(source, actor_id, posting_id),
        actor_id=actor_id, posting_id=posting_id,
        employer_id=employer_id, location_id=workplace_id,
        title="Clerk", started_tick=0, revision=1, source=source,
    )
    needs = NeedsComponent(
        food={"value": 37, "critical_below": 20},
        water={"value": 90, "critical_below": 20},
        rest={"value": 19, "critical_below": 20},
    )
    work_state = WorldState(
        current_tick=0,
        player_id=actor_id,
        calendar={"day_seconds": DAY},
        entities={
            actor_id: WorldEntity(
                id=actor_id, kind="character", status="active", location_id=workplace_id,
                components={
                    "needs": needs.model_dump(mode="json"),
                    "inventory": InventoryComponent(lots=[InventoryLot(
                        lot_id=food_lot_id, account_id=food_account_id,
                        resource_id="core.food", quantity="1",
                    )]).model_dump(mode="json"),
                },
            ),
            employer_id: WorldEntity(
                id=employer_id, kind="organization", status="active",
                components={"treasury": TreasuryComponent(
                    accounts={"core.silver": employer_account_id},
                ).model_dump(mode="json")},
            ),
            workplace_id: WorldEntity(id=workplace_id, kind="place", status="active"),
        },
        resources={
            worker_account_id: ResourceAccount(resource_id="core.silver", balance="5", owner_id=actor_id),
            employer_account_id: ResourceAccount(resource_id="core.silver", balance="100", owner_id=employer_id),
            food_account_id: ResourceAccount(resource_id="core.food", balance="1", owner_id=actor_id),
            food_sink_id: ResourceAccount(resource_id="core.food", balance="0", account_type="consumption_sink"),
        },
        work_postings=[WorkPosting(
            posting_id=posting_id, employer_id=employer_id, location_id=workplace_id,
            title="Clerk", slots=1, valid_from_tick=0,
        )],
        work_assignments=[assignment],
        metadata={
            "resource_roles": {"food_resource_id": "core.food", "water_resource_id": "core.water"},
            "work_shift_terms": [WorkShiftTerms(
                posting_id=posting_id, duration_seconds=SHIFT,
                resource_id="core.silver", wage="12.50",
            ).model_dump(mode="json")],
        },
    )
    schedule = RoutineWorkSchedule(
        assignment_id=assignment.assignment_id,
        first_shift_tick=DAY,
        repeat_every_days=1,
    )
    plan = LongActionPlan(
        actor_id=actor_id, started_tick=0, target_tick=DAY + SHIFT,
        activity="work", work_schedule=schedule,
    )
    engine = SimulationEngine()
    version = 0
    paid_shift = None
    state = work_state
    for _ in range(80):
        resolution = engine.plan_window(
            state,
            plan,
            SimulationContext(
                world_id=UUID("1b000000-0000-4000-8000-00000000001b"),
                branch_id=UUID("1c000000-0000-4000-8000-00000000001c"),
                command_id=uuid4(), world_seed="work-care-buffer",
                state_version=version,
            ),
        )
        assert resolution.outcome != "stopped", resolution.interruption
        paid_shift = next((event for event in resolution.events if isinstance(event, WorkShiftPaidEvent)), paid_shift)
        state = _apply(state, resolution.events)
        version += int(bool(resolution.events))
        if resolution.outcome == "completed":
            break

    assert paid_shift is not None
    assert paid_shift.plan.shift_started_tick == DAY
    assert state.current_tick == DAY + SHIFT
    assert state.resources[worker_account_id].balance == Decimal("17.50")
