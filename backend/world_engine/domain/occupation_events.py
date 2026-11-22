"""Typed private events for occupation appointment and resignation.

The host registry/reducer owns persistence and replay against authoritative
postings and prior assignments. These DTOs bind the resulting assignment to a
single actor, effective tick, persisted command context, deterministic event
ID, and private audit visibility without doing world-state or ledger work.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field, StrictInt, TypeAdapter, model_validator

from .json_types import JsonObject
from .errors import InvalidEvent
from .occupations import OccupationModel, OccupationSource, WorkAssignment

OCCUPATION_APPOINTED_EVENT_TYPE = "core.occupation_appointed.v1"
OCCUPATION_RESIGNED_EVENT_TYPE = "core.occupation_resigned.v1"
_AUTHOR_ONLY: JsonObject = {"policy": "author_only"}
_MAX_TICK = 2**63 - 1
_MIN_TICK = -(2**63)
_MAX_REVISION = 2**63 - 1


def occupation_event_id(
    command_id: UUID,
    ruleset_version: str,
    operation: Literal["appointed", "resigned"],
    actor_id: UUID,
    subject_id: UUID,
) -> UUID:
    """Derive an event ID from the persisted command, actor, and target.

    The exact name payload is ``{ruleset}:occupation.{operation}:{actor}:{subject}``;
    callers must persist the command before constructing the event.
    """

    return uuid5(
        command_id,
        f"{ruleset_version}:occupation.{operation}:{actor_id}:{subject_id}",
    )


class OccupationEventModel(OccupationModel):
    """Shared immutable event contract configuration."""


class OccupationAppointedEvent(OccupationEventModel):
    """Record one actor's appointment into a specific work posting."""

    type: Literal["core.occupation_appointed.v1"] = OCCUPATION_APPOINTED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    effective_tick: StrictInt = Field(ge=_MIN_TICK, le=_MAX_TICK)
    posting_id: UUID
    expected_posting_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    resulting_assignment: WorkAssignment
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def appointment_is_command_bound_and_private(self) -> OccupationAppointedEvent:
        if self.event_id != occupation_event_id(
            self.command_id, self.ruleset_version, "appointed", self.actor_id, self.posting_id,
        ):
            raise ValueError("occupation appointment event ID must be bound to its command, actor, and posting")
        assignment = self.resulting_assignment
        if (
            assignment.actor_id != self.actor_id
            or assignment.posting_id != self.posting_id
            or assignment.started_tick != self.effective_tick
            or assignment.ended_tick is not None
            or assignment.end_source is not None
            or assignment.revision != 1
            or assignment.source != OccupationSource(kind="event", reference_id=str(self.event_id))
        ):
            raise ValueError("appointment must start one active revision-one assignment for its actor and posting")
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("occupation appointment audit visibility is fixed to author_only")
        return self


class OccupationResignedEvent(OccupationEventModel):
    """Record the assignment holder's resignation while preserving job history."""

    type: Literal["core.occupation_resigned.v1"] = OCCUPATION_RESIGNED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    effective_tick: StrictInt = Field(ge=_MIN_TICK, le=_MAX_TICK)
    assignment_id: UUID
    expected_assignment_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    resulting_assignment: WorkAssignment
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def resignation_is_command_bound_and_private(self) -> OccupationResignedEvent:
        if self.event_id != occupation_event_id(
            self.command_id, self.ruleset_version, "resigned", self.actor_id, self.assignment_id,
        ):
            raise ValueError("occupation resignation event ID must be bound to its command, actor, and assignment")
        assignment = self.resulting_assignment
        if (
            assignment.actor_id != self.actor_id
            or assignment.assignment_id != self.assignment_id
            or assignment.ended_tick != self.effective_tick
            or assignment.revision != self.expected_assignment_revision + 1
            or assignment.end_source != OccupationSource(kind="event", reference_id=str(self.event_id))
        ):
            raise ValueError("resignation must end the actor's assignment at the event tick with its next revision")
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("occupation resignation audit visibility is fixed to author_only")
        return self


OCCUPATION_EVENTS = (OccupationAppointedEvent, OccupationResignedEvent)


def validate_occupation_batch(
    events: Sequence[object], *, world_id: UUID, branch_id: UUID, command_id: UUID,
    actor_id: UUID, ruleset_version: str, command_type: str | None = None,
    command_payload: Mapping[str, object] | None = None,
) -> None:
    event = next((item for item in events if isinstance(item, OCCUPATION_EVENTS)), None)
    if event is None:
        if command_type in {"action.take_job", "action.resign_job"}:
            raise InvalidEvent("An employment command requires its typed event.")
        return
    if len(events) != 1 or (event.world_id, event.branch_id, event.command_id, event.actor_id, event.ruleset_version) != (
        world_id, branch_id, command_id, actor_id, ruleset_version,
    ):
        raise InvalidEvent("Employment event must match its persisted command context.")
    if command_type is None and command_payload is None:
        return
    if isinstance(event, OccupationAppointedEvent):
        kind, subject = "take_job", {"posting_id": str(event.posting_id)}
    else:
        kind, subject = "resign_job", {"assignment_id": str(event.assignment_id)}
    if command_type != f"action.{kind}" or command_payload != {"kind": kind, **subject}:
        raise InvalidEvent("Employment event must match its normalized action.")


OccupationEvent = Annotated[
    OccupationAppointedEvent | OccupationResignedEvent,
    Field(discriminator="type"),
]
OCCUPATION_EVENT_ADAPTER = TypeAdapter(OccupationEvent)


def parse_occupation_event(value: OccupationEvent | dict[str, object]) -> OccupationEvent:
    """Parse only the typed appointment and resignation contracts."""

    return OCCUPATION_EVENT_ADAPTER.validate_python(value)
