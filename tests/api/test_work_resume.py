from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from world_engine.domain.models import CalendarConfig, ResourceAccount, WorldEntity, WorldState
from world_engine.domain.occupations import OccupationSource, WorkAssignment, WorkPosting, occupation_assignment_id
from world_engine.domain.work_shift import WorkShiftTerms
from world_engine.simulation.contracts import LongActionPlan, RoutineWorkSchedule
from world_engine.simulation.work_resume import WorkResumeCheckpoint, plan_work_resume


DAY = 1_000
ACTOR_ID = UUID("10000000-0000-4000-8000-000000000001")
EMPLOYER_ID = UUID("20000000-0000-4000-8000-000000000002")
WORKPLACE_ID = UUID("30000000-0000-4000-8000-000000000003")
POSTING_ID = UUID("40000000-0000-4000-8000-000000000004")
SOURCE_ID = UUID("50000000-0000-4000-8000-000000000005")
WORKER_ACCOUNT_ID = UUID("60000000-0000-4000-8000-000000000006")
EMPLOYER_ACCOUNT_ID = UUID("70000000-0000-4000-8000-000000000007")


def _world(*, tick=150, shift_seconds=100, posting_status="open", valid_until=None):
    source = OccupationSource(kind="event", reference_id=str(SOURCE_ID))
    assignment = WorkAssignment(
        assignment_id=occupation_assignment_id(source, ACTOR_ID, POSTING_ID),
        actor_id=ACTOR_ID,
        posting_id=POSTING_ID,
        employer_id=EMPLOYER_ID,
        location_id=WORKPLACE_ID,
        title="Registry clerk",
        started_tick=0,
        revision=1,
        source=source,
    )
    posting = WorkPosting(
        posting_id=POSTING_ID,
        employer_id=EMPLOYER_ID,
        location_id=WORKPLACE_ID,
        title="Registry clerk",
        slots=1,
        valid_from_tick=0,
        valid_until_tick=valid_until,
        status=posting_status,
    )
    terms = WorkShiftTerms(
        posting_id=POSTING_ID,
        duration_seconds=shift_seconds,
        resource_id="core.silver",
        wage="12.50",
    )
    state = WorldState(
        current_tick=tick,
        player_id=ACTOR_ID,
        calendar=CalendarConfig(day_seconds=DAY, year_days=360),
        entities={
            ACTOR_ID: WorldEntity(
                id=ACTOR_ID,
                kind="character",
                status="active",
                location_id=WORKPLACE_ID,
            ),
            EMPLOYER_ID: WorldEntity(
                id=EMPLOYER_ID,
                kind="organization",
                status="active",
                components={"treasury": {"accounts": {"core.silver": str(EMPLOYER_ACCOUNT_ID)}}},
            ),
            WORKPLACE_ID: WorldEntity(id=WORKPLACE_ID, kind="place", status="active"),
        },
        resources={
            WORKER_ACCOUNT_ID: ResourceAccount(
                resource_id="core.silver", balance="5", owner_id=ACTOR_ID,
            ),
            EMPLOYER_ACCOUNT_ID: ResourceAccount(
                resource_id="core.silver", balance="100", owner_id=EMPLOYER_ID,
            ),
        },
        work_postings=[posting],
        work_assignments=[assignment],
        metadata={"work_shift_terms": [terms.model_dump(mode="json")]},
    )
    plan = LongActionPlan(
        actor_id=ACTOR_ID,
        started_tick=0,
        target_tick=5_000,
        activity="work",
        work_schedule=RoutineWorkSchedule(
            assignment_id=assignment.assignment_id,
            first_shift_tick=100,
            repeat_every_days=1,
        ),
    )
    return state, plan, assignment


def test_resume_skips_shift_already_started_midshift_and_keeps_original_calendar():
    state, plan, _ = _world(tick=150)
    before_state = state.model_dump(mode="python")
    before_plan = plan.model_dump(mode="python")

    checkpoint = plan_work_resume(state, plan, previous_committed_tick=50)

    assert checkpoint == WorkResumeCheckpoint(next_shift_tick=1_100, skipped_shifts=1)
    assert state.model_dump(mode="python") == before_state
    assert plan.model_dump(mode="python") == before_plan


def test_resume_at_exact_shift_start_keeps_that_shift_without_counting_it():
    state, plan, _ = _world(tick=100)

    checkpoint = plan_work_resume(state, plan, previous_committed_tick=100)

    assert checkpoint.next_shift_tick == 100
    assert checkpoint.skipped_shifts == 0


def test_resume_returns_no_shift_when_no_complete_slot_fits_fixed_deadline():
    state, plan, _ = _world(tick=200)
    plan = plan.model_copy(update={"target_tick": 200})

    checkpoint = plan_work_resume(state, plan, previous_committed_tick=200)

    assert checkpoint.next_shift_tick is None
    assert checkpoint.skipped_shifts == 0


def test_resume_after_deadline_counts_only_complete_shifts_before_original_deadline():
    state, plan, _ = _world(tick=2_200)
    plan = plan.model_copy(update={"target_tick": 2_100})

    checkpoint = plan_work_resume(state, plan, previous_committed_tick=50)

    assert checkpoint.next_shift_tick is None
    assert checkpoint.skipped_shifts == 2  # starts at 100 and 1,100; 2,100 is incomplete past the deadline


def test_repeated_resume_from_same_tick_does_not_count_a_missed_shift_twice():
    state, plan, _ = _world(tick=150)
    first = plan_work_resume(state, plan, previous_committed_tick=50)
    second = plan_work_resume(state, plan, previous_committed_tick=150)

    assert first == WorkResumeCheckpoint(next_shift_tick=1_100, skipped_shifts=1)
    assert second == WorkResumeCheckpoint(next_shift_tick=1_100, skipped_shifts=0)


@pytest.mark.parametrize("previous_tick", [True, -1, 151])
def test_resume_rejects_nonmonotonic_or_invalid_committed_clock(previous_tick):
    state, plan, _ = _world(tick=150)

    with pytest.raises(ValueError):
        plan_work_resume(state, plan, previous_committed_tick=previous_tick)


def test_resume_revalidates_active_actor_assignment_and_posting():
    state, plan, _ = _world(tick=150)
    inactive_actor = state.entities[ACTOR_ID].model_copy(update={"status": "retired"})
    inactive = state.model_copy(update={"entities": state.entities | {ACTOR_ID: inactive_actor}})
    with pytest.raises(ValueError, match="actor is no longer active"):
        plan_work_resume(inactive, plan, previous_committed_tick=50)

    state, plan, _ = _world(tick=150)
    ended_assignment = state.work_assignments[0].model_copy(update={
        "ended_tick": 120,
        "end_source": OccupationSource(
            kind="event", reference_id="51000000-0000-4000-8000-000000000005",
        ),
    })
    ended = state.model_copy(update={"work_assignments": [ended_assignment]})
    with pytest.raises(ValueError, match="assignment is no longer active"):
        plan_work_resume(ended, plan, previous_committed_tick=50)

    closed, plan, _ = _world(tick=150, posting_status="closed")
    with pytest.raises(ValueError, match="open posting"):
        plan_work_resume(closed, plan, previous_committed_tick=50)


def test_resume_rejects_actor_moved_away_from_the_assigned_workplace():
    state, plan, _ = _world(tick=150)
    actor = state.entities[ACTOR_ID].model_copy(update={"location_id": None})
    state = state.model_copy(update={"entities": state.entities | {ACTOR_ID: actor}})

    with pytest.raises(ValueError, match="no longer at the assigned workplace"):
        plan_work_resume(state, plan, previous_committed_tick=50)


def test_checkpoint_contract_is_immutable_and_strict():
    checkpoint = WorkResumeCheckpoint(next_shift_tick=None, skipped_shifts=0)
    with pytest.raises(ValidationError):
        WorkResumeCheckpoint(next_shift_tick=True, skipped_shifts=0)
    with pytest.raises(ValidationError):
        WorkResumeCheckpoint(next_shift_tick=None, skipped_shifts=True)
    with pytest.raises(ValidationError):
        checkpoint.skipped_shifts = 1
