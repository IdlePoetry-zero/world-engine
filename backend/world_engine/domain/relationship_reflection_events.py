"""Private, command-bound event contract for meeting reflections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .errors import InvalidEvent
from .json_types import JsonObject
from .relationship_reflections import MeetingReflection


MEETING_REFLECTION_RECORDED_EVENT_TYPE = "core.meeting_reflection_recorded.v1"
_RECORD_COMMAND = "action.record_meeting_reflection"
NPC_REFLECTION_COMMAND = "action.request_meeting_reflection"


def meeting_reflection_event_id(command_id: UUID, ruleset_version: str, invitation_id: UUID) -> UUID:
    """Derive the stable event identity from its command, ruleset, and invitation."""

    if not isinstance(command_id, UUID) or not isinstance(invitation_id, UUID):
        raise ValueError("meeting reflection event IDs require UUID command and invitation IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("meeting reflection event IDs require a nonempty ruleset version")
    return uuid5(command_id, f"{ruleset_version}:meeting_reflection.recorded:{invitation_id}")


def meeting_reflection_visibility(actor_id: UUID) -> JsonObject:
    """Return the exact actor-only audience for a private meeting reflection."""

    if not isinstance(actor_id, UUID):
        raise ValueError("meeting reflection visibility requires a UUID actor ID")
    return {"policy": "subject_ids", "subject_ids": [str(actor_id)]}


class MeetingReflectionEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class MeetingReflectionRecordedEvent(MeetingReflectionEventModel):
    """Record one participant's private reflection after a completed meeting."""

    type: Literal["core.meeting_reflection_recorded.v1"] = MEETING_REFLECTION_RECORDED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    requested_by: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    ruleset_version: str = Field(min_length=1, max_length=100)
    reflection: MeetingReflection
    visibility: JsonObject

    @model_validator(mode="after")
    def reflection_is_command_bound_and_private(self) -> MeetingReflectionRecordedEvent:
        if self.requested_by is not None and self.requested_by == self.actor_id:
            raise ValueError("a requested meeting reflection must be recorded by a distinct actor")
        if self.actor_id != self.reflection.actor_id:
            raise ValueError("meeting reflection actor must match the event actor")
        if self.event_id != meeting_reflection_event_id(
            self.command_id, self.ruleset_version, self.reflection.invitation_id,
        ):
            raise ValueError("meeting reflection event ID must be bound to its command and invitation")
        if self.visibility != meeting_reflection_visibility(self.actor_id):
            raise ValueError("meeting reflection must be visible only to its actor")
        return self


MEETING_REFLECTION_EVENT_ADAPTER = TypeAdapter(MeetingReflectionRecordedEvent)


def parse_meeting_reflection_event(
    value: MeetingReflectionRecordedEvent | dict[str, object],
) -> MeetingReflectionRecordedEvent:
    """Parse a typed meeting-reflection event."""

    return MEETING_REFLECTION_EVENT_ADAPTER.validate_python(value)


def validate_meeting_reflection_batch(
    events: Sequence[object],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
    command_type: str | None = None,
    command_payload: Mapping[str, object] | None = None,
) -> None:
    """Bind a standalone reflection event to its exact command and scope."""

    event = next((item for item in events if isinstance(item, MeetingReflectionRecordedEvent)), None)
    if event is None:
        if command_type in {_RECORD_COMMAND, NPC_REFLECTION_COMMAND}:
            raise InvalidEvent("A meeting reflection command requires its typed event.")
        return

    command_actor_id = event.requested_by or event.actor_id
    if len(events) != 1 or (
        event.world_id,
        event.branch_id,
        event.command_id,
        command_actor_id,
        event.ruleset_version,
    ) != (world_id, branch_id, command_id, actor_id, ruleset_version):
        raise InvalidEvent("A meeting reflection must be a standalone event scoped to one exact command.")

    if command_type is None:
        return

    if event.requested_by is not None:
        expected_command = NPC_REFLECTION_COMMAND
        expected_payload = {
            "kind": "request_meeting_reflection",
            "invitation_id": str(event.reflection.invitation_id),
        }
    else:
        expected_command = _RECORD_COMMAND
        expected_payload = {
            "kind": "record_meeting_reflection",
            "invitation_id": str(event.reflection.invitation_id),
            "outlook": event.reflection.outlook,
        }
    if command_type != expected_command or command_payload != expected_payload:
        raise InvalidEvent("The meeting reflection event does not match its command proposal.")
