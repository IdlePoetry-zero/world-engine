"""Author-private, command-bound event DTOs for paid work shifts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from .json_types import JsonObject
from .errors import InvalidEvent
from .work_shift import PaidWorkShiftPlan, WorkShiftModel


WORK_SHIFT_PAID_EVENT_TYPE = "core.work_shift_paid.v1"
_AUTHOR_ONLY: JsonObject = {"policy": "author_only"}


def work_shift_paid_event_id(
    command_id: UUID,
    ruleset_version: str,
    assignment_id: UUID,
) -> UUID:
    """Derive the stable paid-shift event ID from command, ruleset and job."""

    if not isinstance(command_id, UUID) or not isinstance(assignment_id, UUID):
        raise ValueError("paid work-shift event IDs require UUID command and assignment IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("paid work-shift event IDs require a nonempty ruleset version")
    return uuid5(command_id, f"{ruleset_version}:work_shift.paid:{assignment_id}")


class WorkShiftPaidEvent(WorkShiftModel):
    """Record a host-planned wage settlement for one completed work shift.

    The DTO binds attribution and deterministic identity. The reducer or
    repository must independently revalidate the plan against current posting,
    assignment, account, balance, and registered-term state before applying
    its ledger postings.
    """

    type: Literal["core.work_shift_paid.v1"] = WORK_SHIFT_PAID_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    plan: PaidWorkShiftPlan
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def binds_event_to_plan_and_private_audit(self) -> WorkShiftPaidEvent:
        if self.event_id != work_shift_paid_event_id(
            self.command_id,
            self.ruleset_version,
            self.plan.assignment_id,
        ):
            raise ValueError("paid work-shift event ID must be bound to its command, ruleset, and assignment")
        if self.actor_id != self.plan.actor_id:
            raise ValueError("paid work-shift event actor must match the plan worker")
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("paid work-shift audit visibility is fixed to author_only")
        return self


def validate_work_shift_batch(
    events: Sequence[object], *, world_id: UUID, branch_id: UUID, command_id: UUID,
    actor_id: UUID, ruleset_version: str, start_tick: int, command_type: str | None = None,
    command_payload: Mapping[str, object] | None = None,
) -> None:
    paid = [item for item in events if isinstance(item, WorkShiftPaidEvent)]
    if not paid:
        if command_type == "action.work_shift":
            raise InvalidEvent("A completed paid shift requires its wage settlement.")
        return
    event = paid[0]
    if len(paid) != 1 or (event.world_id, event.branch_id, event.command_id, event.actor_id, event.ruleset_version) != (
        world_id, branch_id, command_id, actor_id, ruleset_version,
    ):
        raise InvalidEvent("Paid shift must match its persisted command context.")
    position = events.index(event)
    from .models import TimeAdvancedEvent

    clock = events[0]
    if (position == 0 or not isinstance(clock, TimeAdvancedEvent)
            or sum(isinstance(item, TimeAdvancedEvent) for item in events) != 1
            or event.plan.shift_started_tick != start_tick or clock.to_tick != event.plan.completed_tick):
        raise InvalidEvent("Wages require the same command to advance from shift start to completion.")
    if command_type is None and command_payload is None:
        return
    expected = {"kind": "work_shift", "assignment_id": str(event.plan.assignment_id)}
    if command_type != "action.work_shift" or command_payload != expected:
        raise InvalidEvent("Paid shift must match its normalized player action.")
