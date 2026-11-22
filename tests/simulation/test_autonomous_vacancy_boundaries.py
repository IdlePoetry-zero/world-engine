from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from world_engine.domain.autonomous_vacancy_events import LeadershipVacancyReviewRecordedEvent
from world_engine.domain.autonomous_vacancy import AUTONOMOUS_VACANCY_BALLOT_AUTHORITY
from world_engine.domain.components import (
    GovernanceComponent,
    OfficeRecord,
    PlaceComponent,
    PlaceRoute,
    SkillsComponent,
    ScheduledEventRecord,
    SkillValue,
    TreasuryComponent,
)
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
from world_engine.domain.occupations import OccupationSource, WorkAssignment, WorkPosting, occupation_assignment_id
from world_engine.domain.reducers import apply_event
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent
from world_engine.domain.travel_journeys import active_travel_journey
from world_engine.domain.work_shift import WorkShiftTerms
from world_engine.domain.work_shift_events import WorkShiftPaidEvent
from world_engine.rules import RuleContext, RuleEngine
from world_engine.simulation.contracts import DecisionResponse, LongActionPlan, RoutineWorkSchedule
from world_engine.simulation.engine import SimulationContext, SimulationEngine


PLAYER_ID = UUID("10000000-0000-4000-8000-000000000001")
ORG_ID = UUID("20000000-0000-4000-8000-000000000002")
MEMBER_IDS = tuple(UUID(f"30000000-0000-4000-8000-{value:012d}") for value in (1, 2, 3))
ORIGIN_ID = UUID("c0000000-0000-4000-8000-00000000000c")
DESTINATION_ID = UUID("d0000000-0000-4000-8000-00000000000d")
ROUTE_ID = UUID("e0000000-0000-4000-8000-00000000000e")
JOURNEY_ID = UUID("f0000000-0000-4000-8000-00000000000f")
YEAR_TICKS = 600
WORK_YEAR_TICKS = 6_000


def _vacancy_state(*, day_seconds: int = 60, year_days: int = 10, tick: int = 0) -> WorldState:
    organization = WorldEntity(
        id=ORG_ID,
        kind="organization",
        status="active",
        name="Vacant Council",
        components={"governance": GovernanceComponent(
            revision=1,
            members=list(MEMBER_IDS),
            offices=[OfficeRecord(office_id="office.leader", title="Leader")],
            succession_policy="election",
            authority_tags=[AUTONOMOUS_VACANCY_BALLOT_AUTHORITY],
        ).model_dump(mode="json")},
    )
    entities = {
        PLAYER_ID: WorldEntity(id=PLAYER_ID, kind="character", name="Player", status="active"),
        **{
            member_id: WorldEntity(
                id=member_id, kind="character", name=f"Member {index}", status="active",
            )
            for index, member_id in enumerate(MEMBER_IDS)
        },
        ORG_ID: organization,
    }
    return WorldState(
        current_tick=tick,
        player_id=PLAYER_ID,
        calendar={"day_seconds": day_seconds, "year_days": year_days},
        entities=entities,
    )


def _work_state() -> tuple[WorldState, LongActionPlan, UUID, UUID]:
    year_ticks = WORK_YEAR_TICKS
    shift_ticks = 1_200
    shift_start = year_ticks - shift_ticks // 2
    posting_id = UUID("40000000-0000-4000-8000-000000000004")
    workplace_id = UUID("50000000-0000-4000-8000-000000000005")
    worker_account_id = UUID("60000000-0000-4000-8000-000000000006")
    employer_account_id = UUID("70000000-0000-4000-8000-000000000007")
    resource_id = "core.silver"
    source = OccupationSource(kind="event", reference_id="80000000-0000-4000-8000-000000000008")
    assignment = WorkAssignment(
        assignment_id=occupation_assignment_id(source, PLAYER_ID, posting_id),
        actor_id=PLAYER_ID,
        posting_id=posting_id,
        employer_id=ORG_ID,
        location_id=workplace_id,
        title="Registry clerk",
        started_tick=0,
        source=source,
    )
    state = _vacancy_state(day_seconds=600, year_days=10, tick=shift_start)
    raw = state.model_dump(mode="python")
    raw["entities"][PLAYER_ID]["location_id"] = workplace_id
    raw["entities"][ORG_ID]["components"]["treasury"] = TreasuryComponent(
        accounts={resource_id: employer_account_id},
    ).model_dump(mode="python")
    raw["entities"][workplace_id] = WorldEntity(
        id=workplace_id, kind="place", status="active", name="Registry",
    ).model_dump(mode="python")
    raw["resources"] = {
        worker_account_id: ResourceAccount(
            resource_id=resource_id, balance="5", owner_id=PLAYER_ID,
        ).model_dump(mode="python"),
        employer_account_id: ResourceAccount(
            resource_id=resource_id, balance="100", owner_id=ORG_ID,
        ).model_dump(mode="python"),
    }
    raw["work_postings"] = [WorkPosting(
        posting_id=posting_id,
        employer_id=ORG_ID,
        location_id=workplace_id,
        title="Registry clerk",
        slots=1,
        valid_from_tick=0,
    ).model_dump(mode="python")]
    raw["work_assignments"] = [assignment.model_dump(mode="python")]
    raw["metadata"] = {"work_shift_terms": [WorkShiftTerms(
        posting_id=posting_id, duration_seconds=shift_ticks, resource_id=resource_id, wage="12.50",
    ).model_dump(mode="json")]}
    plan = LongActionPlan(
        actor_id=PLAYER_ID,
        started_tick=0,
        target_tick=shift_start + shift_ticks,
        activity="work",
        work_schedule=RoutineWorkSchedule(
            assignment_id=assignment.assignment_id,
            first_shift_tick=shift_start,
            repeat_every_days=10,
        ),
    )
    return WorldState.model_validate(raw), plan, worker_account_id, employer_account_id


def _context(
    state: WorldState, *, version: int, command_id: UUID | None = None,
    responses=(), allow=True, itinerary_auto_care=False,
):
    return SimulationContext(
        world_id=UUID("90000000-0000-4000-8000-000000000009"),
        branch_id=UUID("a0000000-0000-4000-8000-00000000000a"),
        command_id=command_id or uuid4(),
        command_actor_id=UUID("b0000000-0000-4000-8000-00000000000b"),
        externally_controlled_actor_ids=frozenset({PLAYER_ID}),
        allow_autonomous_vacancy=allow,
        world_seed="autonomous-vacancy-boundary-test",
        state_version=version,
        max_step_ticks=31_622_400,
        decision_responses=tuple(responses),
        itinerary_journey_id=JOURNEY_ID if itinerary_auto_care else None,
        itinerary_auto_care=itinerary_auto_care,
    )


def _apply(state: WorldState, events) -> WorldState:
    for event in events:
        state = apply_event(state, event)
    return state


def _resolve_defer(engine, state, plan, version: int):
    gate_context = _context(state, version=version)
    gate = engine.plan_window(state, plan, gate_context)
    assert gate.outcome == "decision_required"
    request = gate.decision_requests[0]
    assert request.kind == "organization.vacancy_nomination"
    response = DecisionResponse(
        request_id=request.request_id,
        state_version=request.state_version,
        at_tick=request.at_tick,
        source="model",
        option_id="defer",
    )
    resolved = engine.plan_window(
        state,
        plan,
        _context(state, version=version, command_id=gate_context.command_id, responses=(response,)),
    )
    assert resolved.outcome == "advanced"
    assert resolved.next_tick == state.current_tick
    assert any(isinstance(event, LeadershipVacancyReviewRecordedEvent) for event in resolved.events)
    return _apply(state, resolved.events)


@pytest.mark.parametrize("activity", ["wait", "study", "rest"])
def test_character_adaptive_action_commits_at_terminal_year_then_resolves_review(activity: str):
    state = _vacancy_state()
    if activity == "study":
        raw = state.model_dump(mode="python")
        raw["entities"][PLAYER_ID]["components"]["skills"] = SkillsComponent(
            skills={"skill.meditation": SkillValue()},
        ).model_dump(mode="python")
        state = WorldState.model_validate(raw)
    plan = LongActionPlan(
        actor_id=PLAYER_ID,
        started_tick=0,
        target_tick=YEAR_TICKS,
        activity=activity,
        skill_id="skill.meditation" if activity == "study" else None,
    )
    engine = SimulationEngine()
    version = 0
    while state.current_tick < YEAR_TICKS:
        result = engine.plan_window(state, plan, _context(state, version=version))
        assert result.outcome == "advanced"
        state = _apply(state, result.events)
        version += 1

    assert state.current_tick == YEAR_TICKS
    assert result.next_tick == YEAR_TICKS
    assert result.metrics.integration_boundary == "autonomous_vacancy_year_boundary"
    if activity == "study":
        assert state.entities[PLAYER_ID].components["skills"]["skills"]["skill.meditation"]["trained_ticks"] == YEAR_TICKS

    # A terminal annual tick remains in the lease until the vacancy request is
    # handled. Only the following same-tick call can complete the action.
    state = _resolve_defer(engine, state, plan, version)
    completed = engine.plan_window(state, plan, _context(state, version=version + 1))
    assert completed.outcome == "completed"
    assert completed.next_tick == YEAR_TICKS


def test_intermediate_year_review_runs_before_character_action_continues():
    state = _vacancy_state()
    plan = LongActionPlan(
        actor_id=PLAYER_ID, started_tick=0, target_tick=2 * YEAR_TICKS, activity="wait",
    )
    engine = SimulationEngine()

    first = engine.plan_window(state, plan, _context(state, version=0))
    assert first.outcome == "advanced"
    assert first.next_tick == YEAR_TICKS < plan.target_tick
    assert first.metrics.integration_boundary == "autonomous_vacancy_year_boundary"
    state = _apply(state, first.events)
    state = _resolve_defer(engine, state, plan, version=1)

    second = engine.plan_window(state, plan, _context(state, version=2))
    assert second.outcome == "advanced"
    assert second.next_tick == plan.target_tick
    assert second.metrics.integration_boundary == "autonomous_vacancy_year_boundary"


def test_earlier_annual_tick_is_not_skipped_for_a_later_scheduled_event():
    state = _vacancy_state()
    raw = state.model_dump(mode="python")
    raw["scheduled_events"] = [ScheduledEventRecord(
        id=UUID("11111111-1111-4111-8111-111111111111"),
        due_tick=YEAR_TICKS + 50,
        phase="environment",
        handler_id="test.pending",
        handler_version="1.0.0",
    ).model_dump(mode="python")]
    state = WorldState.model_validate(raw)
    plan = LongActionPlan(
        actor_id=PLAYER_ID, started_tick=0, target_tick=YEAR_TICKS + 100, activity="wait",
    )

    result = SimulationEngine().plan_window(state, plan, _context(state, version=0))

    assert result.outcome == "advanced"
    assert result.next_tick == YEAR_TICKS
    assert result.metrics.integration_boundary == "autonomous_vacancy_year_boundary"


def test_paid_shift_crossing_review_year_settles_once_before_review_gate():
    state, plan, worker_account_id, employer_account_id = _work_state()
    engine = SimulationEngine()
    context = _context(state, version=0)

    paid = engine.plan_window(state, plan, context)

    wage_events = [event for event in paid.events if isinstance(event, WorkShiftPaidEvent)]
    assert len(wage_events) == 1
    assert wage_events[0].plan.shift_started_tick == WORK_YEAR_TICKS - 600
    assert wage_events[0].plan.completed_tick == plan.target_tick
    assert paid.outcome == "advanced"
    assert paid.next_tick == plan.target_tick
    assert paid.metrics.integration_boundary == "work_shift_complete_pending_boundary"
    state = _apply(state, paid.events)
    assert state.resources[worker_account_id].balance == Decimal("17.50")
    assert state.resources[employer_account_id].balance == Decimal("87.50")

    # The shift is fully paid at its registered end. The same lease resolves
    # the annual review at that committed tick before reporting completion.
    state = _resolve_defer(engine, state, plan, version=1)
    completed = engine.plan_window(state, plan, _context(state, version=2))
    assert completed.outcome == "completed"
    assert completed.next_tick == plan.target_tick


def test_without_autonomous_authority_context_terminal_window_keeps_legacy_completion():
    state = _vacancy_state()
    plan = LongActionPlan(actor_id=PLAYER_ID, started_tick=0, target_tick=YEAR_TICKS, activity="wait")

    result = SimulationEngine().plan_window(state, plan, _context(state, version=0, allow=False))

    assert result.outcome == "completed"
    assert result.next_tick == YEAR_TICKS
    assert result.metrics.integration_boundary == "target_reached"


def test_subminute_rest_slice_uses_bounded_wait_at_annual_boundary():
    state = _vacancy_state(tick=YEAR_TICKS - 30)
    plan = LongActionPlan(
        actor_id=PLAYER_ID, started_tick=0, target_tick=YEAR_TICKS, activity="rest",
    )

    result = SimulationEngine().plan_window(state, plan, _context(state, version=0))

    assert result.outcome == "advanced"
    assert result.next_tick == YEAR_TICKS
    assert result.metrics.ticks_advanced == 30
    assert result.metrics.integration_boundary == "autonomous_vacancy_year_boundary"
    assert result.rule_resolution.outcome.code == "waited"


@pytest.mark.parametrize("itinerary_auto_care", [False, True])
def test_in_transit_progress_stops_at_year_without_changing_journey_receipt(itinerary_auto_care: bool):
    route = PlaceRoute(
        route_id=ROUTE_ID,
        from_place_id=ORIGIN_ID,
        to_place_id=DESTINATION_ID,
        travel_ticks=3 * YEAR_TICKS,
        risk=Decimal("0"),
        is_open=True,
    )
    state = _vacancy_state()
    raw = state.model_dump(mode="python")
    raw["entities"][PLAYER_ID]["location_id"] = ORIGIN_ID
    raw["entities"][PLAYER_ID]["components"]["capabilities"] = {
        "grants": ["world.travel"], "restrictions": [],
    }
    raw["entities"][ORIGIN_ID] = WorldEntity(
        id=ORIGIN_ID,
        kind="place",
        status="active",
        components={"place": PlaceComponent(place_type="settlement", routes=[route]).model_dump(mode="json")},
    ).model_dump(mode="python")
    raw["entities"][DESTINATION_ID] = WorldEntity(
        id=DESTINATION_ID,
        kind="place",
        status="active",
        components={"place": PlaceComponent(place_type="settlement").model_dump(mode="json")},
    ).model_dump(mode="python")
    state = WorldState.model_validate(raw)
    departure_context = RuleContext(
        world_id=UUID("90000000-0000-4000-8000-000000000009"),
        branch_id=UUID("a0000000-0000-4000-8000-00000000000a"),
        command_id=uuid4(),
        actor_id=PLAYER_ID,
        ruleset_version="1.0.0",
    )
    departure = RuleEngine().evaluate_travel_departure(
        state, ROUTE_ID, departure_context, itinerary_journey_id=JOURNEY_ID,
    )
    state = _apply(state, departure.events)
    before = active_travel_journey(state, PLAYER_ID)
    assert before is not None
    plan = LongActionPlan(
        actor_id=PLAYER_ID,
        started_tick=0,
        target_tick=3 * YEAR_TICKS,
        activity="travel",
        route_id=ROUTE_ID,
    )
    engine = SimulationEngine()

    progress = engine.plan_window(
        state, plan,
        _context(state, version=1, itinerary_auto_care=itinerary_auto_care),
    )

    assert progress.outcome == "advanced"
    assert progress.next_tick == YEAR_TICKS
    assert progress.metrics.integration_boundary == "travel_in_progress"
    assert not any(isinstance(event, TravelJourneyChangedEvent) for event in progress.events)
    state = _apply(state, progress.events)
    after = active_travel_journey(state, PLAYER_ID)
    assert after == before
    assert state.current_tick == YEAR_TICKS

    # The review gate runs before another travel window, leaving route and
    # journey identity available for the durable itinerary cursor.
    gate = engine.plan_window(state, plan, _context(state, version=2))
    assert gate.outcome == "decision_required"
    assert gate.decision_requests[0].at_tick == YEAR_TICKS


def test_in_transit_rest_auto_care_ends_at_exact_review_tick():
    state = _vacancy_state(tick=YEAR_TICKS - 60)
    raw = state.model_dump(mode="python")
    raw["entities"][PLAYER_ID]["location_id"] = ORIGIN_ID
    raw["entities"][PLAYER_ID]["components"].update({
        "capabilities": {"grants": ["world.travel"], "restrictions": []},
        "needs": {"rest": {"value": 30, "critical_below": 20}},
    })
    route = PlaceRoute(
        route_id=ROUTE_ID, from_place_id=ORIGIN_ID, to_place_id=DESTINATION_ID,
        travel_ticks=3 * YEAR_TICKS, risk=Decimal("0"), is_open=True,
    )
    raw["entities"][ORIGIN_ID] = WorldEntity(
        id=ORIGIN_ID, kind="place", status="active",
        components={"place": PlaceComponent(place_type="settlement", routes=[route]).model_dump(mode="json")},
    ).model_dump(mode="python")
    raw["entities"][DESTINATION_ID] = WorldEntity(
        id=DESTINATION_ID, kind="place", status="active",
        components={"place": PlaceComponent(place_type="settlement").model_dump(mode="json")},
    ).model_dump(mode="python")
    state = WorldState.model_validate(raw)
    departure_context = RuleContext(
        world_id=UUID("90000000-0000-4000-8000-000000000009"),
        branch_id=UUID("a0000000-0000-4000-8000-00000000000a"),
        command_id=uuid4(), actor_id=PLAYER_ID, ruleset_version="1.0.0",
    )
    state = _apply(state, RuleEngine().evaluate_travel_departure(
        state, ROUTE_ID, departure_context, itinerary_journey_id=JOURNEY_ID,
    ).events)
    journey_before = active_travel_journey(state, PLAYER_ID)
    assert journey_before is not None
    plan = LongActionPlan(
        actor_id=PLAYER_ID,
        started_tick=YEAR_TICKS - 60,
        target_tick=journey_before.arrival_tick + YEAR_TICKS,
        activity="travel",
        route_id=ROUTE_ID,
    )

    care = SimulationEngine().plan_window(
        state, plan, _context(state, version=1, itinerary_auto_care=True),
    )

    assert care.outcome == "advanced"
    assert care.next_tick == YEAR_TICKS
    assert care.metrics.integration_boundary == "itinerary_auto_care_completed"
    delay, = [event for event in care.events if isinstance(event, TravelJourneyChangedEvent)]
    assert delay.journey.journey_id == journey_before.journey_id
    assert (delay.stationary_from_tick, delay.stationary_until_tick) == (YEAR_TICKS - 60, YEAR_TICKS)
    state = _apply(state, care.events)
    gate = SimulationEngine().plan_window(state, plan, _context(state, version=2))
    assert gate.outcome == "decision_required"
    assert gate.decision_requests[0].at_tick == YEAR_TICKS
