from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import (
    LifeComponent, NeedsComponent, ProductionQuotaPolicy, ScheduledEventRecord, TreasuryComponent,
)
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
from world_engine.domain.occupations import OccupationSource, WorkAssignment, WorkPosting, occupation_assignment_id
from world_engine.domain.reducers import apply_event
from world_engine.domain.travel_itinerary import TravelItinerary, TravelItineraryLeg, travel_itinerary_leg_id
from world_engine.domain.work_shift import WorkShiftTerms
from world_engine.domain.work_shift_events import WorkShiftPaidEvent
from world_engine.simulation.contracts import LongActionPlan, RoutineWorkSchedule
from world_engine.simulation.engine import SimulationContext, SimulationEngine


DAY = 86_400
SHIFT = 3_600
WORKER_ID = UUID("10000000-0000-4000-8000-000000000001")
EMPLOYER_ID = UUID("20000000-0000-4000-8000-000000000002")
WORKPLACE_ID = UUID("30000000-0000-4000-8000-000000000003")
POSTING_ID = UUID("40000000-0000-4000-8000-000000000004")
SOURCE_ID = UUID("50000000-0000-4000-8000-000000000005")
WORKER_ACCOUNT_ID = UUID("60000000-0000-4000-8000-000000000006")
EMPLOYER_ACCOUNT_ID = UUID("70000000-0000-4000-8000-000000000007")
RESOURCE_ID = "core.silver"


def _state(*, tick=0, rest_value=None, scheduled_events=(), with_expiring_npc=False):
    source = OccupationSource(kind="event", reference_id=str(SOURCE_ID))
    assignment = WorkAssignment(
        assignment_id=occupation_assignment_id(source, WORKER_ID, POSTING_ID),
        actor_id=WORKER_ID,
        posting_id=POSTING_ID,
        employer_id=EMPLOYER_ID,
        location_id=WORKPLACE_ID,
        title="Registry clerk",
        started_tick=0,
        revision=1,
        source=source,
    )
    worker_components = {}
    if rest_value is not None:
        worker_components["needs"] = NeedsComponent(
            rest={"value": rest_value, "critical_below": 20},
        ).model_dump(mode="json")
    entities = {
        WORKER_ID: WorldEntity(
            id=WORKER_ID, kind="character", status="active", location_id=WORKPLACE_ID,
            components=worker_components,
        ),
        EMPLOYER_ID: WorldEntity(
            id=EMPLOYER_ID, kind="organization", status="active",
            components={"treasury": TreasuryComponent(accounts={RESOURCE_ID: EMPLOYER_ACCOUNT_ID}).model_dump(mode="json")},
        ),
        WORKPLACE_ID: WorldEntity(id=WORKPLACE_ID, kind="place", status="active"),
    }
    if with_expiring_npc:
        npc_id = UUID("80000000-0000-4000-8000-000000000008")
        entities[npc_id] = WorldEntity(
            id=npc_id, kind="person", status="active", location_id=WORKPLACE_ID,
            components={"life": LifeComponent(birth_tick=0, lifespan_ticks=DAY // 2).model_dump(mode="json")},
        )
    return WorldState(
        current_tick=tick,
        calendar={"day_seconds": DAY},
        entities=entities,
        resources={
            WORKER_ACCOUNT_ID: ResourceAccount(resource_id=RESOURCE_ID, balance="5", owner_id=WORKER_ID),
            EMPLOYER_ACCOUNT_ID: ResourceAccount(resource_id=RESOURCE_ID, balance="100", owner_id=EMPLOYER_ID),
        },
        work_postings=[WorkPosting(
            posting_id=POSTING_ID, employer_id=EMPLOYER_ID, location_id=WORKPLACE_ID,
            title="Registry clerk", slots=1, valid_from_tick=0,
        )],
        work_assignments=[assignment],
        metadata={"work_shift_terms": [WorkShiftTerms(
            posting_id=POSTING_ID, duration_seconds=SHIFT, resource_id=RESOURCE_ID, wage="12.50",
        ).model_dump(mode="json")]},
        scheduled_events=list(scheduled_events),
    )


def _plan(*, first_shift_tick, target_tick=3 * DAY, repeat_every_days=1, balance_target=None):
    return LongActionPlan(
        actor_id=WORKER_ID,
        started_tick=0,
        target_tick=target_tick,
        activity="work",
        work_schedule=RoutineWorkSchedule(
            assignment_id=occupation_assignment_id(
                OccupationSource(kind="event", reference_id=str(SOURCE_ID)), WORKER_ID, POSTING_ID,
            ),
            first_shift_tick=first_shift_tick,
            repeat_every_days=repeat_every_days,
            balance_target=balance_target,
        ),
    )


def _context(version):
    return SimulationContext(
        world_id=UUID("90000000-0000-4000-8000-000000000009"),
        branch_id=UUID("a0000000-0000-4000-8000-00000000000a"),
        command_id=uuid4(),
        world_seed="routine-work-test",
        state_version=version,
    )


def _itinerary(*, actor_id=WORKER_ID, start=0, deadline=100):
    itinerary_id = UUID("e0000000-0000-4000-8000-00000000000e")
    route_id = UUID("f0000000-0000-4000-8000-00000000000f")
    origin_id = UUID("11000000-0000-4000-8000-000000000011")
    destination_id = UUID("12000000-0000-4000-8000-000000000012")
    return TravelItinerary(
        itinerary_id=itinerary_id,
        actor_id=actor_id,
        origin_place_id=origin_id,
        destination_place_id=destination_id,
        created_tick=start,
        deadline_tick=deadline,
        legs=(TravelItineraryLeg(
            leg_id=travel_itinerary_leg_id(itinerary_id, 0, route_id),
            leg_index=0,
            route_id=route_id,
            from_place_id=origin_id,
            to_place_id=destination_id,
            travel_ticks=60,
            route_fingerprint="a" * 64,
        ),),
    )


def _apply(state, events):
    for event in events:
        state = apply_event(state, event)
    return state


def test_routine_work_contract_is_required_only_for_work_and_omitted_when_absent():
    actor = WORKER_ID
    with pytest.raises(ValidationError, match="explicit recurring work schedule"):
        LongActionPlan(actor_id=actor, target_tick=10, activity="work")
    with pytest.raises(ValidationError, match="only valid for work"):
        LongActionPlan(
            actor_id=actor, target_tick=10, activity="wait",
            work_schedule=RoutineWorkSchedule(
                assignment_id=uuid4(), first_shift_tick=0, repeat_every_days=1,
            ),
        )
    old_plan = LongActionPlan(actor_id=actor, target_tick=10, activity="wait")
    assert "work_schedule" not in old_plan.model_dump(mode="json")


def test_itinerary_contract_is_bound_to_actor_and_fixed_window_and_context_id_is_optional():
    itinerary = _itinerary()
    plan = LongActionPlan(
        actor_id=WORKER_ID, started_tick=0, target_tick=100, activity="travel",
        travel_itinerary=itinerary,
    )
    assert plan.travel_itinerary == itinerary
    assert "travel_itinerary" not in LongActionPlan(
        actor_id=WORKER_ID, started_tick=0, target_tick=100, activity="wait",
    ).model_dump(mode="json")
    assert "itinerary_journey_id" not in _context(0).model_dump(mode="json")
    with pytest.raises(ValidationError, match="exactly one of route_id or travel_itinerary"):
        LongActionPlan(
            actor_id=WORKER_ID, started_tick=0, target_tick=100, activity="travel",
            route_id=uuid4(), travel_itinerary=itinerary,
        )
    with pytest.raises(ValidationError, match="deadline must match"):
        LongActionPlan(
            actor_id=WORKER_ID, started_tick=0, target_tick=99, activity="travel",
            travel_itinerary=itinerary,
        )
    with pytest.raises(ValidationError, match="actor must match"):
        LongActionPlan(
            actor_id=uuid4(), started_tick=0, target_tick=100, activity="travel",
            travel_itinerary=itinerary,
        )


def test_future_first_shift_and_later_recurrence_settle_once_at_registered_starts():
    engine = SimulationEngine()
    state = _state(rest_value=100)
    plan = _plan(first_shift_tick=9 * 3_600, balance_target="30")
    version = 0
    paid = []
    first_wait_context = _context(version)
    resolution = engine.plan_window(state, plan, first_wait_context)
    assert resolution.outcome == "advanced"
    assert resolution.metrics.integration_boundary == "routine_work_shift_start"
    assert resolution.next_tick == 9 * 3_600
    state = _apply(state, resolution.events)
    version += 1

    first_shift_context = _context(version)
    resolution = engine.plan_window(state, plan, first_shift_context)
    first_payment = next(event for event in resolution.events if isinstance(event, WorkShiftPaidEvent))
    assert resolution.outcome == "advanced"
    assert first_payment.plan.shift_started_tick == 9 * 3_600
    assert first_payment.command_id == first_shift_context.command_id
    assert len([event for event in resolution.events if isinstance(event, WorkShiftPaidEvent)]) == 1
    paid.append(first_payment)
    state = _apply(state, resolution.events)
    version += 1

    second_wait_context = _context(version)
    resolution = engine.plan_window(state, plan, second_wait_context)
    assert resolution.outcome == "advanced"
    assert resolution.metrics.integration_boundary == "routine_work_shift_start"
    assert resolution.next_tick == 9 * 3_600 + DAY
    state = _apply(state, resolution.events)
    version += 1

    second_shift_context = _context(version)
    resolution = engine.plan_window(state, plan, second_shift_context)
    second_payment = next(event for event in resolution.events if isinstance(event, WorkShiftPaidEvent))
    assert resolution.outcome == "completed"
    assert resolution.metrics.integration_boundary == "work_balance_target_reached"
    assert second_payment.plan.shift_started_tick == 9 * 3_600 + DAY
    assert second_payment.command_id == second_shift_context.command_id
    assert len([event for event in resolution.events if isinstance(event, WorkShiftPaidEvent)]) == 1
    paid.append(second_payment)
    state = _apply(state, resolution.events)

    assert len(paid) == 2
    assert state.resources[WORKER_ACCOUNT_ID].balance == Decimal("30")
    assert state.resources[EMPLOYER_ACCOUNT_ID].balance == Decimal("75.00")


def test_critical_self_care_at_exact_shift_start_pauses_without_clock_or_wage():
    state = _state(tick=0, rest_value=10)
    plan = _plan(first_shift_tick=0)
    resolution = SimulationEngine().plan_window(state, plan, _context(0))

    assert resolution.outcome == "stopped"
    assert resolution.interruption.reason_code == "work_shift_self_care_required"
    assert resolution.events == ()
    assert resolution.next_tick == 0
    assert state.resources[WORKER_ACCOUNT_ID].balance == Decimal("5")


def test_rest_is_scheduled_before_next_shift_when_waiting_would_cross_policy_threshold():
    engine = SimulationEngine()
    state = _state(rest_value=44)
    plan = _plan(first_shift_tick=0, target_tick=2 * DAY)
    version = 0

    first = engine.plan_window(state, plan, _context(version))
    assert any(isinstance(event, WorkShiftPaidEvent) for event in first.events)
    state = _apply(state, first.events)
    version += 1

    # Rest is forecast from the exact passive decay remainder and is scheduled
    # immediately before the next shift instead of being discovered at the due
    # work start.
    wait_for_rest = engine.plan_window(state, plan, _context(version))
    assert wait_for_rest.outcome == "advanced"
    assert wait_for_rest.metrics.integration_boundary == "routine_work_rest_start"
    state = _apply(state, wait_for_rest.events)
    version += 1

    rest_to_shift = engine.plan_window(state, plan, _context(version))
    assert rest_to_shift.outcome == "advanced"
    assert rest_to_shift.metrics.integration_boundary == "routine_work_shift_start"
    assert rest_to_shift.rule_resolution.outcome.code == "rested"
    state = _apply(state, rest_to_shift.events)
    version += 1

    next_shift = engine.plan_window(state, plan, _context(version))
    assert next_shift.outcome == "advanced"
    payment = next(event for event in next_shift.events if isinstance(event, WorkShiftPaidEvent))
    assert payment.plan.shift_started_tick == DAY
    state = _apply(state, next_shift.events)
    assert state.current_tick == DAY + SHIFT


def test_boundary_inside_atomic_shift_stops_before_clock_or_wage():
    event_id = UUID("b0000000-0000-4000-8000-00000000000b")
    state = _state(scheduled_events=[ScheduledEventRecord(
        id=event_id, due_tick=SHIFT // 2, phase="resolution",
        handler_id="test.pending", handler_version="1.0.0",
    )])
    plan = _plan(first_shift_tick=0)
    resolution = SimulationEngine().plan_window(state, plan, _context(0))

    assert resolution.outcome == "stopped"
    assert resolution.interruption.reason_code == "work_shift_boundary_required"
    assert resolution.next_tick == 0
    assert resolution.events == ()
    assert state.resources[WORKER_ACCOUNT_ID].balance == Decimal("5")


def test_same_tick_scheduler_runs_before_balance_target_or_deadline_completes():
    schedule_id = UUID("c0000000-0000-4000-8000-00000000000c")
    quota_source_id = UUID("d0000000-0000-4000-8000-00000000000d")
    state = _state(rest_value=100)
    raw = state.model_dump(mode="json")
    quota = ProductionQuotaPolicy(
        quota_id="quota.daily",
        resource_id="core.grain",
        source_account_id=quota_source_id,
        period_ticks=SHIFT,
        per_period="1",
        maximum_renewal="1",
        base_credit_limit="1",
        issued_total="1",
        last_issued_period=0,
    )
    raw["entities"][str(WORKPLACE_ID)]["components"]["production"] = {
        "quotas": [quota.model_dump(mode="json")],
    }
    raw["resources"][str(quota_source_id)] = ResourceAccount(
        resource_id="core.grain", balance="0", owner_id=WORKPLACE_ID,
        account_type="production_source", credit_limit="2",
    ).model_dump(mode="json")
    raw["scheduled_events"] = [ScheduledEventRecord(
        id=schedule_id,
        due_tick=SHIFT,
        phase="environment",
        handler_id="production.quota_renewal",
        handler_version="1.0.0",
        payload={"producer_id": str(WORKPLACE_ID), "quota_id": "quota.daily"},
    ).model_dump(mode="json")]
    state = WorldState.model_validate(raw)
    plan = _plan(first_shift_tick=0, target_tick=SHIFT, balance_target="17.50")
    engine = SimulationEngine()

    work_context = _context(0)
    work = engine.plan_window(state, plan, work_context)

    assert work.outcome == "advanced"
    assert work.metrics.integration_boundary == "work_shift_complete_pending_boundary"
    payment, = [event for event in work.events if isinstance(event, WorkShiftPaidEvent)]
    assert payment.command_id == work_context.command_id
    state = _apply(state, work.events)
    assert state.resources[WORKER_ACCOUNT_ID].balance == Decimal("17.50")
    assert state.scheduled_events[0].status == "queued"

    boundary = engine.plan_window(state, plan, _context(1))
    assert boundary.outcome == "advanced"
    assert boundary.metrics.integration_boundary == "production_quota_exhausted"
    assert sum(event.type == "core.scheduled_event_completed.v1" for event in boundary.events) == 1
    state = _apply(state, boundary.events)

    completed = engine.plan_window(state, plan, _context(2))
    assert completed.outcome == "completed"
    assert completed.metrics.integration_boundary == "work_balance_target_reached"
    assert completed.events == ()
    assert state.resources[WORKER_ACCOUNT_ID].balance == Decimal("17.50")
