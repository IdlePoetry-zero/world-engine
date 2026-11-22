"""Typed, private event contracts for NPC work attendance lifecycle records.

These DTOs bind an attendance snapshot to the persisted command that started
or cancelled it. State mutation and authoritative attendance checks remain in
the host repository/reducer integration.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from .json_types import JsonObject
from .npc_attendance import NpcAttendance
from .work_shift import PaidWorkShiftPlan, Tick


NPC_ATTENDANCE_STARTED_EVENT_TYPE = "core.npc_attendance_started.v1"
NPC_ATTENDANCE_CANCELLED_EVENT_TYPE = "core.npc_attendance_cancelled.v1"
NPC_ATTENDANCE_SETTLED_EVENT_TYPE = "core.npc_attendance_settled.v1"
_AUTHOR_ONLY: JsonObject = {"policy": "author_only"}
_MIN_TICK = -(2**63)
_MAX_TICK = 2**63 - 1


def npc_attendance_event_id(
    command_id: UUID,
    ruleset_version: str,
    operation: Literal["started", "cancelled", "settled"],
    assignment_id: UUID,
) -> UUID:
    """Return an operation-specific deterministic event ID for an assignment.

    The UUID5 name is ``{ruleset}:npc_attendance.{operation}:{assignment}``.
    Including the operation allows one command to carry independent start and
    cancellation and settlement records without reusing an event identity.
    """

    if not isinstance(command_id, UUID) or not isinstance(assignment_id, UUID):
        raise ValueError("NPC attendance event IDs require UUID command and assignment IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("NPC attendance event IDs require a nonempty ruleset version")
    if operation not in {"started", "cancelled", "settled"}:
        raise ValueError("NPC attendance operation must be started, cancelled, or settled")
    return uuid5(
        command_id,
        f"{ruleset_version}:npc_attendance.{operation}:{assignment_id}",
    )


class NpcAttendanceEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class NpcAttendanceStartedEvent(NpcAttendanceEventModel):
    """Record the start of one scheduled NPC attendance interval."""

    type: Literal["core.npc_attendance_started.v1"] = NPC_ATTENDANCE_STARTED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    attendance: NpcAttendance
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def attendance_start_is_command_bound_and_private(self) -> NpcAttendanceStartedEvent:
        if self.event_id != npc_attendance_event_id(
            self.command_id, self.ruleset_version, "started", self.attendance.assignment_id,
        ):
            raise ValueError("NPC attendance start event ID must be bound to its command and assignment")
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("NPC attendance audit visibility is fixed to author_only")
        return self


class NpcAttendanceCancelledEvent(NpcAttendanceEventModel):
    """Record cancellation of an NPC attendance within its scheduled interval."""

    type: Literal["core.npc_attendance_cancelled.v1"] = NPC_ATTENDANCE_CANCELLED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    attendance: NpcAttendance
    effective_tick: StrictInt = Field(ge=_MIN_TICK, le=_MAX_TICK)
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def attendance_cancellation_is_bound_and_private(self) -> NpcAttendanceCancelledEvent:
        if self.event_id != npc_attendance_event_id(
            self.command_id, self.ruleset_version, "cancelled", self.attendance.assignment_id,
        ):
            raise ValueError("NPC attendance cancellation event ID must be bound to its command and assignment")
        if not self.attendance.starts_at_tick <= self.effective_tick <= self.attendance.ends_at_tick:
            raise ValueError("NPC attendance cancellation must occur within its scheduled interval")
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("NPC attendance audit visibility is fixed to author_only")
        return self


class NpcAttendanceSettledEvent(NpcAttendanceEventModel):
    """Record completed NPC attendance, with optional exact-end wage settlement."""

    type: Literal["core.npc_attendance_settled.v1"] = NPC_ATTENDANCE_SETTLED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    attendance: NpcAttendance
    effective_tick: Tick
    payment: PaidWorkShiftPlan | None = Field(default=None, exclude_if=lambda value: value is None)
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def attendance_settlement_is_bound_and_private(self) -> NpcAttendanceSettledEvent:
        if self.event_id != npc_attendance_event_id(
            self.command_id, self.ruleset_version, "settled", self.attendance.assignment_id,
        ):
            raise ValueError("NPC attendance settlement event ID must be bound to its command and assignment")
        if self.effective_tick < self.attendance.ends_at_tick:
            raise ValueError("NPC attendance settlement cannot occur before its scheduled end")
        if self.payment is not None:
            if self.effective_tick != self.attendance.ends_at_tick:
                raise ValueError("NPC attendance payment is only valid at the exact scheduled end")
            plan = self.payment
            if (
                plan.assignment_id != self.attendance.assignment_id
                or plan.actor_id != self.attendance.actor_id
                or plan.workplace_id != self.attendance.workplace_id
                or plan.assignment_revision != self.attendance.expected_assignment_revision
                or plan.posting_revision != self.attendance.expected_posting_revision
                or plan.shift_started_tick != self.attendance.starts_at_tick
                or plan.completed_tick != self.attendance.ends_at_tick
            ):
                raise ValueError("NPC attendance payment must match its registered assignment and interval")
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("NPC attendance audit visibility is fixed to author_only")
        return self


NpcAttendanceEvent = Annotated[
    NpcAttendanceStartedEvent | NpcAttendanceCancelledEvent | NpcAttendanceSettledEvent,
    Field(discriminator="type"),
]
NPC_ATTENDANCE_EVENT_ADAPTER = TypeAdapter(NpcAttendanceEvent)
NPC_ATTENDANCE_EVENTS = (NpcAttendanceStartedEvent, NpcAttendanceCancelledEvent, NpcAttendanceSettledEvent)


def parse_npc_attendance_event(value: NpcAttendanceEvent | dict[str, object]) -> NpcAttendanceEvent:
    """Parse one registered-shape attendance event DTO."""

    return NPC_ATTENDANCE_EVENT_ADAPTER.validate_python(value)
