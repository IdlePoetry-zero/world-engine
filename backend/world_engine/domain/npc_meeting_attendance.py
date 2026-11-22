"""Durable, private decisions for autonomous attendance at accepted meetings."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from .errors import InvalidEvent
from .json_types import JsonObject
from .relationship_invitation_events import (
    RelationInvitationChangedEvent,
    invitation_event_id,
    participant_visibility,
)
from .relationship_invitations import attend_relationship_meeting

if TYPE_CHECKING:
    from .models import WorldState


NPC_MEETING_ATTENDANCE_DECISION_RECORDED_EVENT_TYPE = "core.npc_meeting_attendance_decision_recorded.v1"
NPC_MEETING_ATTENDANCE_COMMAND = "simulation.npc_meeting_attendance"
NpcMeetingAttendanceChoice = Literal["attend", "defer"]
_MAX_TICK = 2**63 - 1
_MIN_TICK = -(2**63)
_MAX_REVISION = 2**63 - 1


class _NpcMeetingAttendanceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class NpcMeetingAttendanceDecision(_NpcMeetingAttendanceModel):
    """One actor's settlement of one autonomous attendance opportunity."""

    invitation_id: UUID
    actor_id: UUID
    choice: NpcMeetingAttendanceChoice
    source_invitation_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    as_of_tick: StrictInt = Field(ge=_MIN_TICK, le=_MAX_TICK)
    request_id: UUID
    linked_attendance_event_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def attendance_link_matches_choice(self) -> NpcMeetingAttendanceDecision:
        if (self.choice == "attend") != (self.linked_attendance_event_id is not None):
            raise ValueError("an attendance decision links an attendance event if and only if it chooses attend")
        return self


def npc_meeting_attendance_request_id(
    command_id: UUID,
    invitation_id: UUID,
    actor_id: UUID,
    source_invitation_revision: int,
    at_tick: int,
) -> UUID:
    """Derive the model request identity from its exact command and opportunity."""

    if not all(isinstance(value, UUID) for value in (command_id, invitation_id, actor_id)):
        raise ValueError("NPC meeting attendance request IDs require UUID command, invitation, and actor IDs")
    if (
        not isinstance(source_invitation_revision, int)
        or isinstance(source_invitation_revision, bool)
        or not 1 <= source_invitation_revision <= _MAX_REVISION
    ):
        raise ValueError("NPC meeting attendance request IDs require a positive invitation revision")
    if not isinstance(at_tick, int) or isinstance(at_tick, bool) or not _MIN_TICK <= at_tick <= _MAX_TICK:
        raise ValueError("NPC meeting attendance request IDs require a signed 64-bit tick")
    return uuid5(
        command_id,
        f"{NPC_MEETING_ATTENDANCE_DECISION_RECORDED_EVENT_TYPE}:request:"
        f"{invitation_id}:{actor_id}:{source_invitation_revision}:{at_tick}",
    )


def npc_meeting_attendance_event_id(
    command_id: UUID,
    ruleset_version: str,
    invitation_id: UUID,
    actor_id: UUID,
    source_invitation_revision: int,
    at_tick: int,
) -> UUID:
    """Derive a stable event identity from the command and exact opportunity."""

    if not isinstance(command_id, UUID) or not isinstance(invitation_id, UUID) or not isinstance(actor_id, UUID):
        raise ValueError("NPC meeting attendance event IDs require UUID command, invitation, and actor IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("NPC meeting attendance event IDs require a nonempty ruleset version")
    if (
        not isinstance(source_invitation_revision, int)
        or isinstance(source_invitation_revision, bool)
        or not 1 <= source_invitation_revision <= _MAX_REVISION
        or not isinstance(at_tick, int)
        or isinstance(at_tick, bool)
        or not _MIN_TICK <= at_tick <= _MAX_TICK
    ):
        raise ValueError("NPC meeting attendance event IDs require a valid source revision and tick")
    return uuid5(
        command_id,
        f"{ruleset_version}:npc_meeting_attendance.decision:"
        f"{invitation_id}:{actor_id}:{source_invitation_revision}:{at_tick}",
    )


def npc_meeting_attendance_visibility() -> JsonObject:
    """Return the private audience for an internal autonomous decision marker."""

    return {"policy": "author_only"}


class NpcMeetingAttendanceDecisionRecordedEvent(_NpcMeetingAttendanceModel):
    """A command-scoped event recording one private model attendance decision."""

    type: Literal["core.npc_meeting_attendance_decision_recorded.v1"] = (
        NPC_MEETING_ATTENDANCE_DECISION_RECORDED_EVENT_TYPE
    )
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    record: NpcMeetingAttendanceDecision
    visibility: JsonObject

    @model_validator(mode="after")
    def event_is_scoped_and_private(self) -> NpcMeetingAttendanceDecisionRecordedEvent:
        record = self.record
        if self.actor_id != record.actor_id:
            raise ValueError("NPC meeting attendance actor must match its decision record")
        if record.request_id != npc_meeting_attendance_request_id(
            self.command_id,
            record.invitation_id,
            record.actor_id,
            record.source_invitation_revision,
            record.as_of_tick,
        ):
            raise ValueError("NPC meeting attendance request ID must match its command and opportunity")
        if self.event_id != npc_meeting_attendance_event_id(
            self.command_id,
            self.ruleset_version,
            record.invitation_id,
            record.actor_id,
            record.source_invitation_revision,
            record.as_of_tick,
        ):
            raise ValueError("NPC meeting attendance event ID must match its command and opportunity")
        if self.visibility != npc_meeting_attendance_visibility():
            raise ValueError("NPC meeting attendance decision markers must be author-only")
        return self


NPC_MEETING_ATTENDANCE_EVENT_ADAPTER = TypeAdapter(NpcMeetingAttendanceDecisionRecordedEvent)


def parse_npc_meeting_attendance_event(
    value: NpcMeetingAttendanceDecisionRecordedEvent | dict[str, object],
) -> NpcMeetingAttendanceDecisionRecordedEvent:
    """Parse the versioned private decision event."""

    return NPC_MEETING_ATTENDANCE_EVENT_ADAPTER.validate_python(value)


def npc_meeting_attendance_command_payload(record: NpcMeetingAttendanceDecision) -> JsonObject:
    """Return the minimal command identity payload; model prose is never persisted."""

    if not isinstance(record, NpcMeetingAttendanceDecision):
        raise ValueError("NPC meeting attendance command payload requires a typed decision")
    return {"invitation_id": str(record.invitation_id), "request_id": str(record.request_id)}


def build_npc_meeting_attendance_decision_events(
    state: WorldState,
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
    invitation_id: UUID,
    source_invitation_revision: int,
    choice: NpcMeetingAttendanceChoice,
) -> list[RelationInvitationChangedEvent | NpcMeetingAttendanceDecisionRecordedEvent]:
    """Build the canonical attendance transition, when chosen, then its marker."""

    if choice not in {"attend", "defer"}:
        raise InvalidEvent("An autonomous meeting decision must choose attend or defer.")
    invitation = next(
        (item for item in state.relationship_invitations if item.invitation_id == invitation_id),
        None,
    )
    if invitation is None:
        raise InvalidEvent("An autonomous meeting decision requires a known invitation.")

    # The same canonical preflight governs both outcomes; ``defer`` discards
    # the planned attendance value and changes no invitation state.
    attended = attend_relationship_meeting(
        state,
        state.relationship_invitations,
        invitation_id=invitation_id,
        actor_id=actor_id,
        expected_revision=source_invitation_revision,
        check_work_calendar=True,
    )
    linked_attendance_event_id: UUID | None = None
    events: list[RelationInvitationChangedEvent | NpcMeetingAttendanceDecisionRecordedEvent] = []
    if choice == "attend":
        linked_attendance_event_id = invitation_event_id(
            command_id, ruleset_version, "attend", invitation_id,
        )
        events.append(RelationInvitationChangedEvent(
            event_id=linked_attendance_event_id,
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            actor_id=actor_id,
            work_calendar_checked=True,
            ruleset_version=ruleset_version,
            operation="attend",
            expected_revision=source_invitation_revision,
            resulting_invitation=attended,
            visibility=participant_visibility(invitation.proposer_id, invitation.invitee_id),
        ))

    record = NpcMeetingAttendanceDecision(
        invitation_id=invitation_id,
        actor_id=actor_id,
        choice=choice,
        source_invitation_revision=source_invitation_revision,
        as_of_tick=state.current_tick,
        request_id=npc_meeting_attendance_request_id(
            command_id, invitation_id, actor_id, source_invitation_revision, state.current_tick,
        ),
        linked_attendance_event_id=linked_attendance_event_id,
    )
    decision_event = NpcMeetingAttendanceDecisionRecordedEvent(
        event_id=npc_meeting_attendance_event_id(
            command_id,
            ruleset_version,
            invitation_id,
            actor_id,
            source_invitation_revision,
            state.current_tick,
        ),
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=ruleset_version,
        record=record,
        visibility=npc_meeting_attendance_visibility(),
    )
    events.append(decision_event)
    validate_npc_meeting_attendance_decision_batch(
        events,
        pre_state=state,
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=ruleset_version,
        command_type=NPC_MEETING_ATTENDANCE_COMMAND,
        command_payload=npc_meeting_attendance_command_payload(record),
    )
    return events


def validate_npc_meeting_attendance_decision_batch(
    events: Sequence[object],
    *,
    pre_state: WorldState,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
    command_type: str | None = None,
    command_payload: Mapping[str, object] | None = None,
) -> None:
    """Validate a decision against its exact command and optional attendance event."""

    normalized_events: list[object] = []
    for item in events:
        if isinstance(item, NpcMeetingAttendanceDecisionRecordedEvent):
            try:
                item = NpcMeetingAttendanceDecisionRecordedEvent.model_validate(item.model_dump(mode="json"))
            except (TypeError, ValueError) as exc:
                raise InvalidEvent("An autonomous attendance decision event failed schema validation.") from exc
        elif isinstance(item, RelationInvitationChangedEvent):
            try:
                item = RelationInvitationChangedEvent.model_validate(item.model_dump(mode="json"))
            except (TypeError, ValueError) as exc:
                raise InvalidEvent("A linked invitation attendance event failed schema validation.") from exc
        normalized_events.append(item)
    events = normalized_events

    decision_indexes = [
        index for index, event in enumerate(events)
        if isinstance(event, NpcMeetingAttendanceDecisionRecordedEvent)
    ]
    if not decision_indexes:
        if command_type == NPC_MEETING_ATTENDANCE_COMMAND:
            raise InvalidEvent("An autonomous meeting attendance command requires its typed decision event.")
        return
    if len(decision_indexes) != 1:
        raise InvalidEvent("An autonomous attendance command can settle only one actor and invitation.")

    index = decision_indexes[0]
    event = events[index]
    record = event.record
    command_actor_id = event.actor_id
    if (
        (event.world_id, event.branch_id, event.command_id, command_actor_id, event.ruleset_version)
        != (world_id, branch_id, command_id, actor_id, ruleset_version)
        or event.actor_id != record.actor_id
        or event.visibility != npc_meeting_attendance_visibility()
    ):
        raise InvalidEvent("An autonomous attendance decision must match its trusted command scope and privacy.")

    if (command_type is None) != (command_payload is None):
        raise InvalidEvent("NPC meeting attendance command type and payload must be supplied together.")
    if command_type is not None and (
        command_type != NPC_MEETING_ATTENDANCE_COMMAND
        or command_payload != npc_meeting_attendance_command_payload(record)
    ):
        raise InvalidEvent("The autonomous attendance decision does not match its command payload.")

    invitation = next(
        (item for item in pre_state.relationship_invitations if item.invitation_id == record.invitation_id),
        None,
    )
    if (
        invitation is None
        or invitation.revision != record.source_invitation_revision
        or invitation.status != "accepted"
        or invitation.meeting is None
        or record.actor_id not in {invitation.proposer_id, invitation.invitee_id}
        or (actor := pre_state.entities.get(record.actor_id)) is None
        or actor.kind not in {"character", "person"}
        or actor.status != "active"
        or pre_state.player_id == record.actor_id
        or record.as_of_tick != pre_state.current_tick
        or not invitation.meeting.start_tick <= record.as_of_tick < invitation.meeting.end_tick
    ):
        raise InvalidEvent("An autonomous attendance decision requires its exact current accepted meeting opportunity.")

    from .npc_plans import occupied_npc_ids
    from .travel_journeys import active_travel_journey

    participants = {invitation.proposer_id, invitation.invitee_id}
    if participants & occupied_npc_ids(pre_state, pre_state.current_tick) or any(
        active_travel_journey(pre_state, participant) is not None for participant in participants
    ):
        raise InvalidEvent("Meeting participants cannot attend during active travel or a timed plan.")
    try:
        expected_attendance = attend_relationship_meeting(
            pre_state,
            pre_state.relationship_invitations,
            invitation_id=record.invitation_id,
            actor_id=record.actor_id,
            expected_revision=record.source_invitation_revision,
            check_work_calendar=True,
        )
    except InvalidEvent as exc:
        raise InvalidEvent("An autonomous attendance opportunity must pass canonical meeting eligibility checks.") from exc

    relevant_transitions = [
        (position, item)
        for position, item in enumerate(events)
        if isinstance(item, RelationInvitationChangedEvent)
        and item.resulting_invitation.invitation_id == record.invitation_id
    ]
    if record.choice == "defer":
        if index != 0 or len(events) != 1 or record.linked_attendance_event_id is not None or relevant_transitions:
            raise InvalidEvent("A deferred autonomous opportunity must not change its invitation calendar.")
        return

    linked = [
        (position, item)
        for position, item in relevant_transitions
        if item.event_id == record.linked_attendance_event_id
    ]
    if index != 1 or len(events) != 2 or len(relevant_transitions) != 1 or len(linked) != 1:
        raise InvalidEvent("An attendance decision must link its canonical invitation attendance event.")
    transition_index, attendance_event = linked[0]
    if (
        transition_index >= index
        or attendance_event.operation != "attend"
        or (attendance_event.world_id, attendance_event.branch_id, attendance_event.command_id,
            attendance_event.ruleset_version)
        != (world_id, branch_id, command_id, ruleset_version)
        or attendance_event.actor_id != record.actor_id
        or attendance_event.requested_by is not None
        or attendance_event.expected_revision != record.source_invitation_revision
        or attendance_event.work_calendar_checked is not True
        or attendance_event.resulting_invitation != expected_attendance
    ):
        raise InvalidEvent("The linked invitation event is not the exact checked attendance for this decision.")


def validate_npc_meeting_attendance_decisions_state(state: WorldState) -> None:
    """Validate sparse decision records against current durable invitation state."""

    invitations = {item.invitation_id: item for item in state.relationship_invitations}
    seen: set[tuple[UUID, UUID]] = set()
    for record in state.npc_meeting_attendance_decisions:
        key = record.invitation_id, record.actor_id
        invitation = invitations.get(record.invitation_id)
        if (
            key in seen
            or invitation is None
            or record.actor_id not in {invitation.proposer_id, invitation.invitee_id}
            or invitation.meeting is None
            or record.source_invitation_revision > invitation.revision
            or record.as_of_tick > state.current_tick
            or not invitation.meeting.start_tick <= record.as_of_tick < invitation.meeting.end_tick
        ):
            raise ValueError("NPC meeting attendance decisions require a unique known participant and committed meeting tick")
        actor = state.entities.get(record.actor_id)
        if actor is None or actor.kind not in {"character", "person"}:
            raise ValueError("NPC meeting attendance decisions require a registered character participant")
        actor_attendance = [
            attendance for attendance in invitation.meeting_attendance
            if attendance.actor_id == record.actor_id
        ]
        if record.choice == "attend":
            if record.source_invitation_revision + 1 > invitation.revision:
                raise ValueError("An attended NPC meeting decision must precede the invitation's attendance revision")
            if not any(attendance.attended_tick == record.as_of_tick for attendance in actor_attendance):
                raise ValueError("An attended NPC meeting decision requires its recorded canonical attendance")
        seen.add(key)


def validate_npc_meeting_attendance_decision_transition(
    state: WorldState,
    record: NpcMeetingAttendanceDecision,
) -> None:
    """Check the exact current state required before a decision is reduced."""

    if record.as_of_tick != state.current_tick:
        raise InvalidEvent("An autonomous meeting decision must be recorded at the exact current world tick.")
    if any(
        item.invitation_id == record.invitation_id and item.actor_id == record.actor_id
        for item in state.npc_meeting_attendance_decisions
    ):
        raise InvalidEvent("A participant may settle an autonomous attendance opportunity only once per invitation.")
    invitation = next(
        (item for item in state.relationship_invitations if item.invitation_id == record.invitation_id),
        None,
    )
    actor = state.entities.get(record.actor_id)
    if (
        invitation is None
        or invitation.status != "accepted"
        or invitation.meeting is None
        or record.actor_id not in {invitation.proposer_id, invitation.invitee_id}
        or actor is None
        or actor.kind not in {"character", "person"}
        or actor.status != "active"
        or state.player_id == record.actor_id
        or not invitation.meeting.start_tick <= record.as_of_tick < invitation.meeting.end_tick
    ):
        raise InvalidEvent("An autonomous meeting decision requires its active participant and accepted meeting.")

    if record.choice == "defer":
        if invitation.revision != record.source_invitation_revision or any(
            item.actor_id == record.actor_id for item in invitation.meeting_attendance
        ):
            raise InvalidEvent("A deferred opportunity must match its unchanged invitation revision and attendance.")
        try:
            attend_relationship_meeting(
                state,
                state.relationship_invitations,
                invitation_id=record.invitation_id,
                actor_id=record.actor_id,
                expected_revision=record.source_invitation_revision,
                check_work_calendar=True,
            )
        except InvalidEvent as exc:
            raise InvalidEvent("A deferred opportunity must pass canonical meeting eligibility checks.") from exc
        return

    if (
        record.source_invitation_revision + 1 != invitation.revision
        or record.linked_attendance_event_id is None
        or not any(
            item.actor_id == record.actor_id and item.attended_tick == record.as_of_tick
            for item in invitation.meeting_attendance
        )
    ):
        raise InvalidEvent("An attended decision requires its prior canonical attendance transition.")

