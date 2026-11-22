"""Pure start and validity checks for an NPC's registered work attendance.

Attendance records only establish that an NPC is present for a scheduled,
registered shift. They neither inspect wage balances nor settle pay, advance
the clock, or emit events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .components import DailyRoutineComponent
from .occupations import WorkAssignment, WorkPosting
from .work_shift import WorkShiftError, WorkShiftTerms, registered_work_shift_terms

if TYPE_CHECKING:
    from .models import WorldState


_MIN_TICK = -(2**63)
_MAX_TICK = 2**63 - 1
_MAX_REVISION = 2**63 - 1
Tick = Annotated[int, Field(strict=True, ge=_MIN_TICK, le=_MAX_TICK)]


class NpcAttendance(BaseModel):
    """Immutable provenance-free attendance window, suitable for host review."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    assignment_id: UUID
    actor_id: UUID
    workplace_id: UUID
    window_id: UUID
    expected_assignment_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    expected_posting_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    starts_at_tick: Tick
    ends_at_tick: Tick

    @model_validator(mode="after")
    def positive_bounded_interval(self) -> NpcAttendance:
        if self.ends_at_tick <= self.starts_at_tick:
            raise ValueError("NPC attendance must have a positive time interval")
        return self


def plan_npc_attendance_start(state: WorldState, assignment_id: UUID) -> NpcAttendance | None:
    """Plan attendance at the exact start of an eligible NPC's daily work shift."""
    from .models import WorldState

    resolved = _resolve_assignment(state, assignment_id)
    if resolved is None:
        return None
    assignment, actor, posting, terms, routine = resolved
    if actor.id == state.player_id or not _active_assignment_at_workplace(assignment, actor, posting, state):
        return None

    day_seconds = state.calendar.day_seconds
    start_tick = state.current_tick
    if start_tick > _MAX_TICK - terms.duration_seconds:
        return None
    end_tick = start_tick + terms.duration_seconds
    if not _posting_covers_shift(posting, start_tick, end_tick):
        return None

    window = next(
        (
            item for item in sorted(routine.windows, key=lambda value: str(value.window_id))
            if _window_starts_shift(item, posting.location_id, terms, day_seconds, start_tick)
        ),
        None,
    )
    if window is None:
        return None
    return NpcAttendance(
        assignment_id=assignment.assignment_id,
        actor_id=assignment.actor_id,
        workplace_id=posting.location_id,
        window_id=window.window_id,
        expected_assignment_revision=assignment.revision,
        expected_posting_revision=posting.revision,
        starts_at_tick=start_tick,
        ends_at_tick=end_tick,
    )


def npc_attendance_is_valid(state: WorldState, attendance: NpcAttendance) -> bool:
    """Check that an attendance window still matches employment and presence.

    The end tick is inclusive so a host can validate attendance at the exact
    scheduled completion boundary before its independent wage-settlement step.
    """
    from .models import WorldState

    if not isinstance(state, WorldState) or not isinstance(attendance, NpcAttendance):
        return False
    try:
        attendance = NpcAttendance.model_validate(attendance.model_dump(mode="python"))
    except Exception:
        return False
    if state.current_tick < attendance.starts_at_tick or state.current_tick > attendance.ends_at_tick:
        return False

    resolved = _resolve_assignment(state, attendance.assignment_id)
    if resolved is None:
        return False
    assignment, actor, posting, terms, routine = resolved
    if (
        actor.id == state.player_id
        or assignment.actor_id != attendance.actor_id
        or assignment.revision != attendance.expected_assignment_revision
        or assignment.started_tick > attendance.starts_at_tick
        or posting.revision != attendance.expected_posting_revision
        or posting.location_id != attendance.workplace_id
        or terms.duration_seconds != attendance.ends_at_tick - attendance.starts_at_tick
        or not _active_assignment_at_workplace(assignment, actor, posting, state)
        or not _posting_covers_shift(posting, attendance.starts_at_tick, attendance.ends_at_tick)
    ):
        return False

    day_seconds = state.calendar.day_seconds
    return any(
        window.window_id == attendance.window_id
        and _window_starts_shift(
            window, attendance.workplace_id, terms, day_seconds, attendance.starts_at_tick,
        )
        for window in routine.windows
    )


def _resolve_assignment(state: WorldState, assignment_id: UUID):
    from .models import WorldState

    if not isinstance(state, WorldState) or not isinstance(assignment_id, UUID):
        return None
    assignments = [item for item in state.work_assignments if item.assignment_id == assignment_id]
    if len(assignments) != 1:
        return None
    assignment: WorkAssignment = assignments[0]
    actors = state.entities.get(assignment.actor_id)
    if actors is None:
        return None
    postings = [item for item in state.work_postings if item.posting_id == assignment.posting_id]
    if len(postings) != 1:
        return None
    posting: WorkPosting = postings[0]
    try:
        terms: WorkShiftTerms | None = registered_work_shift_terms(state).get(posting.posting_id)
        routine_raw = actors.components.get("daily_routine")
        if terms is None or routine_raw is None:
            return None
        routine = DailyRoutineComponent.model_validate(routine_raw)
    except (WorkShiftError, ValueError, TypeError):
        return None
    return assignment, actors, posting, terms, routine


def _active_assignment_at_workplace(assignment, actor, posting, state: WorldState) -> bool:
    if (
        assignment.ended_tick is not None
        or assignment.employer_id != posting.employer_id
        or assignment.location_id != posting.location_id
        or actor.id == state.player_id
        or actor.kind not in {"character", "person"}
        or actor.status != "active"
        or actor.location_id != posting.location_id
        or posting.status != "open"
        or posting.employer_id == assignment.actor_id
    ):
        return False
    employer = state.entities.get(posting.employer_id)
    workplace = state.entities.get(posting.location_id)
    return bool(
        employer is not None
        and employer.status == "active"
        and employer.kind in {"organization", "polity", "faction", "company", "government"}
        and workplace is not None
        and workplace.status == "active"
        and workplace.kind in {"place", "location"}
    )


def _posting_covers_shift(posting: WorkPosting, start_tick: int, end_tick: int) -> bool:
    return not (
        start_tick < posting.valid_from_tick
        or posting.valid_until_tick is not None
        and (start_tick >= posting.valid_until_tick or end_tick > posting.valid_until_tick)
    )


def _window_starts_shift(window, workplace_id: UUID, terms: WorkShiftTerms, day_seconds: int, start_tick: int) -> bool:
    window_end = window.start_second + window.duration_seconds
    shift_phase = window_end - terms.duration_seconds
    return bool(
        window.activity == "work"
        and window.place_id == workplace_id
        and window.duration_seconds >= terms.duration_seconds
        and window_end <= day_seconds
        and start_tick % day_seconds == shift_phase % day_seconds
        and window.start_second <= shift_phase
        and shift_phase + terms.duration_seconds == window_end
    )
