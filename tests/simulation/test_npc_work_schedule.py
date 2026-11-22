from dataclasses import FrozenInstanceError
from decimal import Decimal
from uuid import UUID

import pytest

from world_engine.domain.components import DailyRoutineComponent, DailyRoutineWindow, TreasuryComponent
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
from world_engine.domain.npc_attendance import plan_npc_attendance_start
from world_engine.domain.occupations import (
    OccupationSource,
    WorkAssignment,
    WorkPosting,
    occupation_assignment_id,
)
from world_engine.domain.work_shift import PaidWorkShiftPlan, WorkShiftTerms
from world_engine.simulation.npc_work_schedule import (
    NpcWorkTransition,
    next_npc_work_tick,
    npc_work_transition,
)

DAY_SECONDS = 86_400
DURATION = 3_600
WAGE = Decimal("12.50")
WORKER_ID = UUID("10000000-0000-4000-8000-000000000001")
EMPLOYER_ID = UUID("20000000-0000-4000-8000-000000000002")
PLACE_ID = UUID("30000000-0000-4000-8000-000000000003")
OTHER_PLACE_ID = UUID("31000000-0000-4000-8000-000000000013")
POSTING_ID = UUID("40000000-0000-4000-8000-000000000004")
ASSIGNMENT_SOURCE_ID = UUID("50000000-0000-4000-8000-000000000005")
WORKER_ACCOUNT_ID = UUID("60000000-0000-4000-8000-000000000006")
EMPLOYER_ACCOUNT_ID = UUID("70000000-0000-4000-8000-000000000007")
RESOURCE_ID = "core.silver"
WINDOW_ID = UUID("83000000-0000-4000-8000-000000000001")
OTHER_WORKER_ID = UUID("10000000-0000-4000-8000-000000000002")
OTHER_WORKER_ACCOUNT_ID = UUID("60000000-0000-4000-8000-000000000008")
OTHER_ASSIGNMENT_SOURCE_ID = UUID("50000000-0000-4000-8000-000000000008")


def make_state(*, tick=0, employer_balance="100"):
    source = OccupationSource(kind="event", reference_id=str(ASSIGNMENT_SOURCE_ID))
    assignment = WorkAssignment(
        assignment_id=occupation_assignment_id(source, WORKER_ID, POSTING_ID),
        actor_id=WORKER_ID,
        posting_id=POSTING_ID,
        employer_id=EMPLOYER_ID,
        location_id=PLACE_ID,
        title="Registry clerk",
        required_skill=None,
        started_tick=0,
        source=source,
    )
    terms = WorkShiftTerms(
        posting_id=POSTING_ID,
        duration_seconds=DURATION,
        resource_id=RESOURCE_ID,
        wage=WAGE,
    )
    return WorldState(
        current_tick=tick,
        entities={
            WORKER_ID: WorldEntity(id=WORKER_ID, kind="character", location_id=PLACE_ID),
            EMPLOYER_ID: WorldEntity(
                id=EMPLOYER_ID,
                kind="organization",
                location_id=PLACE_ID,
                components={"treasury": TreasuryComponent(accounts={RESOURCE_ID: EMPLOYER_ACCOUNT_ID})},
            ),
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", status="active"),
            OTHER_PLACE_ID: WorldEntity(id=OTHER_PLACE_ID, kind="place", status="active"),
        },
        resources={
            WORKER_ACCOUNT_ID: ResourceAccount(resource_id=RESOURCE_ID, balance="5", owner_id=WORKER_ID),
            EMPLOYER_ACCOUNT_ID: ResourceAccount(
                resource_id=RESOURCE_ID,
                balance=employer_balance,
                owner_id=EMPLOYER_ID,
            ),
        },
        work_postings=[WorkPosting(
            posting_id=POSTING_ID,
            employer_id=EMPLOYER_ID,
            location_id=PLACE_ID,
            title="Registry clerk",
            slots=1,
            valid_from_tick=0,
        )],
        work_assignments=[assignment],
        metadata={"work_shift_terms": [terms.model_dump(mode="json")]},
    )


def _state(*, tick=0, routine=True, employer_balance="100", player_id=None):
    state = make_state(tick=tick, employer_balance=employer_balance)
    payload = state.model_dump(mode="python")
    payload["player_id"] = player_id
    if routine:
        payload["entities"][WORKER_ID]["components"]["daily_routine"] = DailyRoutineComponent(
            windows=[DailyRoutineWindow(
                window_id=WINDOW_ID,
                start_second=0,
                duration_seconds=DURATION,
                activity="work",
                place_id=PLACE_ID,
            )],
        ).model_dump(mode="python")
    return WorldState.model_validate(payload)


def _assignment_id(state):
    return next(row.assignment_id for row in state.work_assignments if row.actor_id == WORKER_ID)


def _with_attendance(state):
    record = plan_npc_attendance_start(state, _assignment_id(state))
    assert record is not None
    payload = state.model_dump(mode="python")
    payload["npc_attendance"] = [record.model_dump(mode="python")]
    return WorldState.model_validate(payload), record


def _with_tick(state, tick):
    payload = state.model_dump(mode="python")
    payload["current_tick"] = tick
    return WorldState.model_validate(payload)


def test_transition_starts_only_at_the_exact_registered_work_time_and_is_frozen():
    transition = npc_work_transition(_state())

    assert isinstance(transition, NpcWorkTransition)
    assert transition.operation == "started"
    assert transition.attendance.starts_at_tick == 0
    assert transition.attendance.ends_at_tick == DURATION
    assert transition.payment is None
    with pytest.raises(FrozenInstanceError):
        transition.operation = "settled"
    assert npc_work_transition(_with_tick(_state(), 1)) is None


def test_exact_end_settlement_uses_valid_attendance_and_existing_payment_planner():
    started, record = _with_attendance(_state())
    ending = _with_tick(started, record.ends_at_tick)

    transition = npc_work_transition(ending)

    assert transition is not None
    assert transition.operation == "settled"
    assert transition.attendance == record
    assert isinstance(transition.payment, PaidWorkShiftPlan)
    assert transition.payment.shift_started_tick == record.starts_at_tick
    assert transition.payment.completed_tick == record.ends_at_tick


def test_elapsed_record_closes_without_late_payment_and_invalid_record_cancels():
    started, record = _with_attendance(_state())
    late = _with_tick(started, record.ends_at_tick + 1)

    settled = npc_work_transition(late)

    assert settled is not None
    assert settled.operation == "settled"
    assert settled.payment is None

    departed_payload = started.model_dump(mode="python")
    departed_payload["current_tick"] = 1
    departed_payload["entities"][WORKER_ID]["location_id"] = OTHER_PLACE_ID
    departed = WorldState.model_validate(departed_payload)
    cancelled = npc_work_transition(departed)
    assert cancelled is not None
    assert cancelled.operation == "cancelled"
    assert cancelled.attendance == record


def test_exact_end_without_affordable_payment_still_closes_attendance():
    started, record = _with_attendance(_state(employer_balance="0"))

    transition = npc_work_transition(_with_tick(started, record.ends_at_tick))

    assert transition is not None
    assert transition.operation == "settled"
    assert transition.payment is None


def test_elapsed_attendance_closes_before_a_simultaneous_new_start():
    state = _state()
    payload = state.model_dump(mode="python")
    payload["work_postings"][0]["slots"] = 2
    payload["entities"][OTHER_WORKER_ID] = WorldEntity(
        id=OTHER_WORKER_ID,
        kind="character",
        location_id=PLACE_ID,
        components={"daily_routine": DailyRoutineComponent(
            windows=[DailyRoutineWindow(
                window_id=UUID("83000000-0000-4000-8000-000000000002"),
                start_second=DURATION,
                duration_seconds=DURATION,
                activity="work",
                place_id=PLACE_ID,
            )],
        ).model_dump(mode="python")},
    ).model_dump(mode="python")
    payload["resources"][OTHER_WORKER_ACCOUNT_ID] = ResourceAccount(
        resource_id=RESOURCE_ID,
        balance="5",
        owner_id=OTHER_WORKER_ID,
    ).model_dump(mode="python")
    other_source = OccupationSource(kind="event", reference_id=str(OTHER_ASSIGNMENT_SOURCE_ID))
    posting_id = payload["work_postings"][0]["posting_id"]
    payload["work_assignments"].append(WorkAssignment(
        assignment_id=occupation_assignment_id(other_source, OTHER_WORKER_ID, posting_id),
        actor_id=OTHER_WORKER_ID,
        posting_id=posting_id,
        employer_id=EMPLOYER_ID,
        location_id=PLACE_ID,
        title="Registry clerk",
        required_skill=None,
        started_tick=0,
        source=other_source,
    ).model_dump(mode="python"))
    simultaneous = WorldState.model_validate(payload)
    started, record = _with_attendance(simultaneous)
    at_end = _with_tick(started, record.ends_at_tick)

    first = npc_work_transition(at_end)
    assert first is not None and first.operation == "settled"
    assert first.attendance.assignment_id == record.assignment_id

    after_close_payload = at_end.model_dump(mode="python")
    after_close_payload["npc_attendance"] = []
    after_close = WorldState.model_validate(after_close_payload)
    second = npc_work_transition(after_close)
    assert second is not None and second.operation == "started"
    assert second.attendance.actor_id == OTHER_WORKER_ID
    assert second.attendance.starts_at_tick == DURATION


def test_next_tick_returns_upcoming_start_and_inclusive_recorded_end():
    assert next_npc_work_tick(_state(tick=1), DAY_SECONDS) == DAY_SECONDS

    started, record = _with_attendance(_state())
    assert next_npc_work_tick(started, record.ends_at_tick) == record.ends_at_tick
    assert next_npc_work_tick(started, record.ends_at_tick - 1) is None
    assert next_npc_work_tick(_with_tick(started, record.ends_at_tick), DAY_SECONDS) is None


def test_next_tick_skips_missing_or_ineligible_routines_and_uses_far_future_arithmetic():
    assert next_npc_work_tick(_state(routine=False), DAY_SECONDS * 10_000) is None

    payload = _state().model_dump(mode="python")
    future_start = DAY_SECONDS * 1_000_000
    payload["work_postings"][0]["valid_from_tick"] = future_start
    far_future = WorldState.model_validate(payload)
    assert next_npc_work_tick(far_future, future_start + DAY_SECONDS * 600) == future_start

    away_payload = _state().model_dump(mode="python")
    away_payload["entities"][WORKER_ID]["location_id"] = OTHER_PLACE_ID
    away = WorldState.model_validate(away_payload)
    assert next_npc_work_tick(away, DAY_SECONDS * 10_000) is None


def test_excluded_controlled_actor_does_not_start_or_create_future_start_wakeups():
    state = _state()

    assert npc_work_transition(state, excluded_actor_ids={WORKER_ID}) is None
    assert next_npc_work_tick(state, DAY_SECONDS * 10, excluded_actor_ids={WORKER_ID}) is None

    started, record = _with_attendance(state)
    ending = _with_tick(started, record.ends_at_tick)
    transition = npc_work_transition(ending, excluded_actor_ids={WORKER_ID})
    assert transition is not None
    assert transition.operation == "settled"
    assert transition.payment is not None
