"""Pure checkpoint calculation for resuming an interrupted recurring work job.

The calculation preserves the authored calendar and fixed deadline. It only
identifies complete scheduled shifts that became unavailable while the job was
paused; it never creates work, time, or wage events.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StrictInt

from world_engine.domain.models import ContractModel, WorldState
from world_engine.domain.occupations import WorkAssignment, WorkPosting
from world_engine.domain.work_shift import WorkShiftError, registered_work_shift_terms
from world_engine.simulation.contracts import LongActionPlan


_MIN_TICK = -(2**63)
_MAX_TICK = 2**63 - 1
Tick = Annotated[StrictInt, Field(ge=_MIN_TICK, le=_MAX_TICK)]


class WorkResumeCheckpoint(ContractModel):
    """The next original-calendar shift and newly skipped shift count."""

    next_shift_tick: Tick | None
    skipped_shifts: StrictInt = Field(ge=0)


def plan_work_resume(
    state: WorldState,
    plan: LongActionPlan,
    *,
    previous_committed_tick: int,
) -> WorkResumeCheckpoint:
    """Calculate a safe recurring-work resume point from current trusted state.

    Shifts are counted only when their scheduled start is in the newly elapsed
    half-open interval and their full registered duration fits the original
    long-action deadline and posting window. A shift at ``state.current_tick``
    remains eligible to run now. Repeating this calculation from the same
    checkpoint cannot count that shift twice.
    """

    state = _checked_state(state)
    plan = _checked_plan(plan)
    previous_committed_tick = _checked_tick(previous_committed_tick, "previous committed")

    current_tick = state.current_tick
    if previous_committed_tick > current_tick:
        raise ValueError("The previous committed work tick cannot be after the current world clock.")
    if plan.started_tick is None or plan.activity != "work" or plan.work_schedule is None:
        raise ValueError("A persisted recurring work plan is required to calculate its resume checkpoint.")
    if plan.target_tick <= plan.started_tick:
        raise ValueError("The original work deadline must be later than the job start.")
    if previous_committed_tick < plan.started_tick:
        raise ValueError("The previous committed work tick cannot precede the original job start.")

    schedule = plan.work_schedule
    assignment = _current_assignment(state, plan, schedule.assignment_id)
    posting = _current_posting(state, assignment)
    actor = state.entities.get(plan.actor_id)
    employer = state.entities.get(posting.employer_id)
    workplace = state.entities.get(posting.location_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
        raise ValueError("The original work actor is no longer active.")
    if employer is None or employer.kind not in {
        "organization", "polity", "faction", "company", "government",
    } or employer.status != "active":
        raise ValueError("The original work employer is no longer active.")
    if workplace is None or workplace.kind not in {"place", "location"} or workplace.status != "active":
        raise ValueError("The original work location is no longer active.")
    if actor.location_id != assignment.location_id:
        raise ValueError("The original work actor is no longer at the assigned workplace.")

    try:
        terms = registered_work_shift_terms(state).get(posting.posting_id)
    except WorkShiftError as error:
        raise ValueError("The original posting's registered work terms are no longer valid.") from error
    if terms is None:
        raise ValueError("The original posting no longer has registered work-shift terms.")

    first_tick = schedule.first_shift_tick
    duration = terms.duration_seconds
    cadence = schedule.repeat_every_days * state.calendar.day_seconds
    if cadence <= 0 or cadence > _MAX_TICK or cadence < duration:
        raise ValueError("The original work cadence no longer safely fits a registered shift.")
    if first_tick < plan.started_tick or first_tick < assignment.started_tick:
        raise ValueError("The original first shift precedes the job or active assignment.")
    if first_tick < posting.valid_from_tick:
        raise ValueError("The original first shift precedes the registered posting window.")
    if first_tick > _MAX_TICK - duration:
        raise ValueError("The original first shift exceeds the supported world clock.")
    if first_tick + duration > plan.target_tick:
        raise ValueError("The original first complete shift does not fit the fixed work deadline.")
    if posting.valid_until_tick is not None and first_tick + duration > posting.valid_until_tick:
        raise ValueError("The original first complete shift does not fit the posting window.")

    last_allowed_end = plan.target_tick
    if posting.valid_until_tick is not None:
        last_allowed_end = min(last_allowed_end, posting.valid_until_tick)
    last_allowed_start = last_allowed_end - duration

    lower_index = max(
        0,
        _ceil_div(previous_committed_tick - first_tick, cadence),
        _ceil_div(assignment.started_tick - first_tick, cadence),
        _ceil_div(posting.valid_from_tick - first_tick, cadence),
    )
    starts_before_current = _ceil_div(current_tick - first_tick, cadence)
    upper_index = min(
        starts_before_current - 1,
        (last_allowed_start - first_tick) // cadence,
    )
    skipped_shifts = max(0, upper_index - lower_index + 1)

    next_index = max(
        0,
        _ceil_div(current_tick - first_tick, cadence),
        _ceil_div(assignment.started_tick - first_tick, cadence),
        _ceil_div(posting.valid_from_tick - first_tick, cadence),
    )
    next_tick = first_tick + next_index * cadence
    if next_tick > _MAX_TICK or next_tick > last_allowed_start:
        next_tick = None

    return WorkResumeCheckpoint(
        next_shift_tick=next_tick,
        skipped_shifts=skipped_shifts,
    )


def _checked_state(state: WorldState) -> WorldState:
    if not isinstance(state, WorldState) or type(state.current_tick) is not int:
        raise ValueError("A validated world state with an integer clock is required.")
    try:
        return WorldState.model_validate(state.model_dump(mode="python"))
    except Exception as error:
        raise ValueError("The current world state is invalid for a work resume.") from error


def _checked_plan(plan: LongActionPlan) -> LongActionPlan:
    if not isinstance(plan, LongActionPlan):
        raise ValueError("The original work plan must be a validated long-action plan.")
    if type(plan.target_tick) is not int or (
        plan.started_tick is not None and type(plan.started_tick) is not int
    ):
        raise ValueError("The original work plan has an invalid clock value.")
    try:
        checked = LongActionPlan.model_validate(plan.model_dump(mode="python"))
    except Exception as error:
        raise ValueError("The original work plan is invalid.") from error
    if (
        checked.activity != "work"
        or checked.started_tick is None
        or checked.work_schedule is None
    ):
        raise ValueError("A persisted recurring work plan is required to calculate its resume checkpoint.")
    return checked


def _checked_tick(value: int, name: str) -> int:
    if type(value) is not int or not _MIN_TICK <= value <= _MAX_TICK:
        raise ValueError(f"The {name} work tick is invalid.")
    return value


def _current_assignment(state: WorldState, plan: LongActionPlan, assignment_id) -> WorkAssignment:
    rows = [row for row in state.work_assignments if row.assignment_id == assignment_id]
    if len(rows) != 1:
        raise ValueError("The original work assignment is unavailable or ambiguous.")
    assignment = WorkAssignment.model_validate(rows[0].model_dump(mode="python"))
    if (
        assignment.actor_id != plan.actor_id
        or assignment.ended_tick is not None
        or assignment.started_tick > plan.work_schedule.first_shift_tick
    ):
        raise ValueError("The original work assignment is no longer active for this actor.")
    return assignment


def _current_posting(state: WorldState, assignment: WorkAssignment) -> WorkPosting:
    rows = [row for row in state.work_postings if row.posting_id == assignment.posting_id]
    if len(rows) != 1:
        raise ValueError("The original work posting is unavailable or ambiguous.")
    posting = WorkPosting.model_validate(rows[0].model_dump(mode="python"))
    if (
        posting.status != "open"
        or posting.employer_id != assignment.employer_id
        or posting.location_id != assignment.location_id
    ):
        raise ValueError("The original work assignment no longer matches an open posting.")
    return posting


def _ceil_div(value: int, divisor: int) -> int:
    """Return mathematical ceil(value / divisor) for positive divisor."""

    return -((-value) // divisor)
