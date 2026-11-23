"""Pure eligibility planning for an NPC's already-ended daily work window.

This planner does not advance time, create events, or settle wages. A trusted
host may call it at the exact end of a registered NPC work window and use the
returned CAS plan in its own authorized settlement flow.
"""

from __future__ import annotations

from uuid import UUID

from .components import DailyRoutineComponent
from .models import WorldState
from .npc_attendance import NpcAttendance, npc_attendance_is_valid
from .work_shift import (
    PaidWorkShiftPlan,
    WorkShiftError,
    WorkShiftTerms,
    plan_paid_work_shift,
    registered_work_shift_terms,
    select_wage_holder_account,
)


_MIN_TICK = -(2**63)


def plan_attended_work_shift(state: WorldState, attendance: NpcAttendance) -> PaidWorkShiftPlan | None:
    """Plan payment only after the registered attendance reached its exact end."""
    if state.current_tick != attendance.ends_at_tick or not npc_attendance_is_valid(state, attendance):
        return None
    if attendance.work_job_id is None:
        return plan_npc_work_shift(state, attendance.assignment_id)
    assignment = next(row for row in state.work_assignments if row.assignment_id == attendance.assignment_id)
    terms = registered_work_shift_terms(state)[assignment.posting_id]
    account_id = select_wage_holder_account(state, attendance.actor_id, terms.resource_id)
    if account_id is None:
        return None
    try:
        return plan_paid_work_shift(
            state, terms=terms, actor_id=attendance.actor_id, assignment_id=attendance.assignment_id,
            worker_account_id=account_id, shift_started_tick=attendance.starts_at_tick,
            expected_assignment_revision=attendance.expected_assignment_revision,
            expected_posting_revision=attendance.expected_posting_revision,
        )
    except WorkShiftError:
        return None


def plan_npc_work_shift(state: WorldState, assignment_id: UUID) -> PaidWorkShiftPlan | None:
    """Plan a registered NPC shift only at its exact recurring routine end.

    Ineligible and unaffordable assignments return ``None``. Eligible plans
    choose the NPC's matching currency holder by lexical UUID order and reuse
    the existing paid-shift planner for balance, account, posting, revision,
    and time checks.
    """
    if not isinstance(state, WorldState) or not isinstance(assignment_id, UUID):
        return None

    assignments = [row for row in state.work_assignments if row.assignment_id == assignment_id]
    if len(assignments) != 1:
        return None
    assignment = assignments[0]
    if assignment.actor_id == state.player_id:
        return None

    actor = state.entities.get(assignment.actor_id)
    if actor is None:
        return None

    postings = [row for row in state.work_postings if row.posting_id == assignment.posting_id]
    if len(postings) != 1:
        return None
    posting = postings[0]

    try:
        terms: WorkShiftTerms | None = registered_work_shift_terms(state).get(posting.posting_id)
        routine_raw = actor.components.get("daily_routine")
        if terms is None or routine_raw is None:
            return None
        routine = DailyRoutineComponent.model_validate(routine_raw)
    except (WorkShiftError, ValueError, TypeError):
        return None

    day_seconds = state.calendar.day_seconds
    shift_started_tick = state.current_tick - terms.duration_seconds
    if shift_started_tick < _MIN_TICK or shift_started_tick < assignment.started_tick:
        return None
    ending_phase = state.current_tick % day_seconds
    start_phase = shift_started_tick % day_seconds
    has_matching_window = any(
        window.activity == "work"
        and window.place_id == posting.location_id
        and window.duration_seconds >= terms.duration_seconds
        and window.start_second + window.duration_seconds <= day_seconds
        and ending_phase == (window.start_second + window.duration_seconds) % day_seconds
        and window.start_second <= start_phase
        and start_phase + terms.duration_seconds <= window.start_second + window.duration_seconds
        for window in routine.windows
    )
    if not has_matching_window:
        return None

    worker_account_id = select_wage_holder_account(state, assignment.actor_id, terms.resource_id)
    if worker_account_id is None:
        return None

    try:
        return plan_paid_work_shift(
            state,
            terms=terms,
            actor_id=assignment.actor_id,
            assignment_id=assignment.assignment_id,
            worker_account_id=worker_account_id,
            shift_started_tick=shift_started_tick,
            expected_posting_revision=posting.revision,
            expected_assignment_revision=assignment.revision,
        )
    except WorkShiftError:
        return None
