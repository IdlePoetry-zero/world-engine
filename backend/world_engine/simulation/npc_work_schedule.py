"""Deterministic scheduling for autonomous work by hosted characters.

The scheduler proposes one lifecycle transition at the world's current tick.
It does not mutate state or emit events; the host remains responsible for
committing the returned attendance and optional payment plan.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from world_engine.domain.components import DailyRoutineComponent
from world_engine.domain.models import WorldState
from world_engine.domain.npc_plans import occupied_npc_ids
from world_engine.domain.npc_attendance import (
    NpcAttendance,
    npc_attendance_is_valid,
    plan_npc_attendance_start,
)
from world_engine.domain.npc_work_shift import plan_npc_work_shift
from world_engine.domain.work_shift import (
    PaidWorkShiftPlan,
    WorkShiftError,
    registered_work_shift_terms,
)

_MIN_TICK = -(2**63)
_MAX_TICK = 2**63 - 1


@dataclass(frozen=True, slots=True)
class NpcWorkTransition:
    """One proposed attendance lifecycle operation, without state mutation."""

    operation: Literal["started", "cancelled", "settled"]
    attendance: NpcAttendance
    payment: PaidWorkShiftPlan | None = None


def npc_work_transition(
    state: WorldState,
    *,
    excluded_actor_ids: Collection[UUID] = (),
) -> NpcWorkTransition | None:
    """Return one deterministic autonomous work transition for a hosted character.

    Ended attendance closes before cancellations, and all closures precede
    new starts. Ties within an operation are ordered by assignment UUID. Wages
    are proposed only at the recorded exact end, while attendance is still
    valid and the existing work-shift planner accepts it.
    """

    if not isinstance(state, WorldState):
        return None
    now = state.current_tick
    attendance_rows = sorted((row for row in state.npc_attendance if row.work_job_id is None),
                             key=lambda row: str(row.assignment_id))

    # Close all elapsed windows first. A delayed host still clears the record,
    # but payment remains bound to the exact scheduled end tick.
    for attendance in attendance_rows:
        if now < attendance.ends_at_tick:
            continue
        payment = None
        if now == attendance.ends_at_tick and npc_attendance_is_valid(state, attendance):
            payment = plan_npc_work_shift(state, attendance.assignment_id)
        return NpcWorkTransition("settled", attendance, payment)

    # An attendance record that became invalid before its end is cancelled.
    for attendance in attendance_rows:
        if not npc_attendance_is_valid(state, attendance):
            return NpcWorkTransition("cancelled", attendance)

    excluded = _uuid_set(excluded_actor_ids) | occupied_npc_ids(state, now)
    open_assignments = {row.assignment_id for row in attendance_rows}
    for assignment in sorted(state.work_assignments, key=lambda row: str(row.assignment_id)):
        if assignment.assignment_id in open_assignments or assignment.actor_id in excluded:
            continue
        attendance = plan_npc_attendance_start(state, assignment.assignment_id)
        if attendance is not None:
            return NpcWorkTransition("started", attendance)
    return None


def next_npc_work_tick(
    state: WorldState,
    horizon: int,
    *,
    excluded_actor_ids: Collection[UUID] = (),
) -> int | None:
    """Return the next relevant future work start or recorded attendance end.

    Candidate daily starts are computed directly from routine phases, so even
    a horizon spanning centuries does not enumerate calendar days. Each
    candidate is checked at its virtual tick by the existing attendance
    planner before it can wake the simulation.
    """

    if (
        not isinstance(state, WorldState)
        or type(horizon) is not int
        or not _MIN_TICK <= horizon <= _MAX_TICK
    ):
        return None
    now = state.current_tick
    if horizon <= now or now >= _MAX_TICK:
        return None

    candidates = [
        row.ends_at_tick
        for row in state.npc_attendance
        if row.work_job_id is None and now < row.ends_at_tick <= horizon
    ]

    excluded = _uuid_set(excluded_actor_ids) | occupied_npc_ids(state, now)
    open_assignments = {row.assignment_id for row in state.npc_attendance}
    day_seconds = state.calendar.day_seconds
    if day_seconds <= 0:
        return min(candidates) if candidates else None

    try:
        terms_by_posting = registered_work_shift_terms(state)
    except (WorkShiftError, ValueError, TypeError):
        terms_by_posting = {}

    for assignment in sorted(state.work_assignments, key=lambda row: str(row.assignment_id)):
        if (
            assignment.assignment_id in open_assignments
            or assignment.actor_id in excluded
            or assignment.ended_tick is not None
        ):
            continue
        posting_rows = [row for row in state.work_postings if row.posting_id == assignment.posting_id]
        if len(posting_rows) != 1:
            continue
        posting = posting_rows[0]
        terms = terms_by_posting.get(posting.posting_id)
        actor = state.entities.get(assignment.actor_id)
        employer = state.entities.get(posting.employer_id)
        workplace = state.entities.get(posting.location_id)
        if (
            terms is None
            or actor is None
            or employer is None
            or workplace is None
            or assignment.actor_id == state.player_id
            or assignment.employer_id != posting.employer_id
            or assignment.location_id != posting.location_id
            or actor.kind not in {"character", "person"}
            or actor.status != "active"
            or actor.location_id != posting.location_id
            or posting.status != "open"
            or posting.employer_id == assignment.actor_id
            or employer.kind not in {"organization", "polity", "faction", "company", "government"}
            or employer.status != "active"
            or workplace.kind not in {"place", "location"}
            or workplace.status != "active"
        ):
            continue

        routine_raw = actor.components.get("daily_routine")
        if routine_raw is None:
            continue
        try:
            routine = DailyRoutineComponent.model_validate(routine_raw)
        except (ValueError, TypeError):
            continue

        earliest = max(now + 1, assignment.started_tick, posting.valid_from_tick)
        for window in sorted(routine.windows, key=lambda row: str(row.window_id)):
            window_end = window.start_second + window.duration_seconds
            shift_phase = window_end - terms.duration_seconds
            if (
                window.activity != "work"
                or window.place_id != posting.location_id
                or window.duration_seconds < terms.duration_seconds
                or window_end > day_seconds
                or shift_phase < window.start_second
                or shift_phase + terms.duration_seconds > window_end
            ):
                continue

            candidate = earliest + (shift_phase - earliest) % day_seconds
            if (
                candidate > horizon
                or candidate > _MAX_TICK - terms.duration_seconds
                or posting.valid_until_tick is not None
                and (candidate >= posting.valid_until_tick
                     or candidate + terms.duration_seconds > posting.valid_until_tick)
            ):
                continue
            virtual_state = state.model_copy(update={"current_tick": candidate})
            attendance = plan_npc_attendance_start(virtual_state, assignment.assignment_id)
            if attendance is not None and attendance.starts_at_tick == candidate:
                candidates.append(candidate)

    return min(candidates) if candidates else None


def _uuid_set(values: Collection[UUID]) -> set[UUID]:
    """Keep the authority exclusion input fail-closed to valid UUID values."""

    try:
        return {value for value in values if isinstance(value, UUID)}
    except TypeError:
        return set()
