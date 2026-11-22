"""Private, command-bound event contract for invitation lifecycle changes."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from .json_types import JsonObject
from .relationship_invitations import RelationshipInvitation


RELATIONSHIP_INVITATION_CHANGED_EVENT_TYPE = "core.relationship_invitation_changed.v1"
_MAX_REVISION = 2**63 - 1
InvitationOperation = Literal["propose", "accept", "decline", "withdraw", "end", "attend"]


def invitation_event_id(
    command_id: UUID,
    ruleset_version: str,
    operation: InvitationOperation,
    invitation_id: UUID,
) -> UUID:
    """Derive a stable event identity from command, invitation and transition.

    UUID5's name is ``{ruleset}:relationship_invitation.{operation}:{id}``.
    """

    if not isinstance(command_id, UUID) or not isinstance(invitation_id, UUID):
        raise ValueError("invitation event IDs require UUID command and invitation IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("invitation event IDs require a nonempty ruleset version")
    if operation not in {"propose", "accept", "decline", "withdraw", "end", "attend"}:
        raise ValueError("invitation operation is unsupported")
    return uuid5(
        command_id,
        f"{ruleset_version}:relationship_invitation.{operation}:{invitation_id}",
    )


def participant_visibility(proposer_id: UUID, invitee_id: UUID) -> JsonObject:
    """Return the canonical, two-participant-only audience for an invitation."""

    if not isinstance(proposer_id, UUID) or not isinstance(invitee_id, UUID):
        raise ValueError("invitation visibility requires UUID participant IDs")
    if proposer_id == invitee_id:
        raise ValueError("invitation visibility requires distinct participants")
    return {"policy": "subject_ids", "subject_ids": sorted((str(proposer_id), str(invitee_id)))}


class InvitationEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class RelationInvitationChangedEvent(InvitationEventModel):
    """Commit one consent-specific invitation transition and its new snapshot."""

    type: Literal["core.relationship_invitation_changed.v1"] = RELATIONSHIP_INVITATION_CHANGED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    requested_by: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    work_calendar_checked: Literal[True] | None = Field(default=None, exclude_if=lambda value: value is None)
    ruleset_version: str = Field(min_length=1, max_length=100)
    operation: InvitationOperation
    expected_revision: StrictInt = Field(ge=0, le=_MAX_REVISION)
    resulting_invitation: RelationshipInvitation
    visibility: JsonObject

    @model_validator(mode="after")
    def transition_is_exact_and_participant_private(self) -> RelationInvitationChangedEvent:
        invitation = self.resulting_invitation
        if self.event_id != invitation_event_id(
            self.command_id, self.ruleset_version, self.operation, invitation.invitation_id,
        ):
            raise ValueError("invitation event ID must be bound to its command, operation, and invitation")

        if self.operation == "propose":
            if self.expected_revision != 0:
                raise ValueError("a new invitation proposal must expect revision zero")
            expected_status: str = "pending"
            expected_actor = invitation.proposer_id
            expected_result_revision = 1
        else:
            if self.expected_revision < 1:
                raise ValueError("an invitation transition must expect an existing positive revision")
            expected_actor = (
                invitation.proposer_id
                if self.operation == "withdraw"
                else invitation.invitee_id
                if self.operation in {"accept", "decline"}
                else self.actor_id
            )
            expected_status = {
                "accept": "accepted",
                "decline": "declined",
                "withdraw": "withdrawn",
                "end": "ended",
                "attend": "accepted",
            }[self.operation]
            expected_result_revision = self.expected_revision + 1

        if invitation.status != expected_status or invitation.revision != expected_result_revision:
            raise ValueError("invitation result status and revision must match the requested operation")
        if self.requested_by is not None:
            if self.operation in {"accept", "decline"}:
                if self.requested_by != invitation.proposer_id:
                    raise ValueError("a requested NPC answer must be requested by the proposer")
            elif self.operation == "attend":
                participants = {invitation.proposer_id, invitation.invitee_id}
                if self.requested_by not in participants or self.requested_by == self.actor_id:
                    raise ValueError("requested NPC attendance must be requested by the other invitation participant")
            else:
                raise ValueError("only an NPC answer or attendance may be requested")
        if self.work_calendar_checked is True and self.operation != "attend":
            raise ValueError("only meeting attendance may be marked as checked against the work calendar")
        if self.operation == "end" and self.actor_id not in {invitation.proposer_id, invitation.invitee_id}:
            raise ValueError("only an invitation participant may end the accepted relationship")
        if self.operation == "attend":
            if self.actor_id not in {invitation.proposer_id, invitation.invitee_id}:
                raise ValueError("only an invitation participant may attend the scheduled meeting")
            if not any(attendance.actor_id == self.actor_id for attendance in invitation.meeting_attendance):
                raise ValueError("the attending participant must appear in the resulting attendance records")
        if self.actor_id != expected_actor:
            raise ValueError("invitation operation actor does not have authority for this transition")
        if self.visibility != participant_visibility(invitation.proposer_id, invitation.invitee_id):
            raise ValueError("invitation details must be visible only to its two participants")
        return self


RELATIONSHIP_INVITATION_EVENT_ADAPTER = TypeAdapter(RelationInvitationChangedEvent)


def parse_relationship_invitation_event(
    value: RelationInvitationChangedEvent | dict[str, object],
) -> RelationInvitationChangedEvent:
    """Parse the typed invitation lifecycle event."""

    return RELATIONSHIP_INVITATION_EVENT_ADAPTER.validate_python(value)
