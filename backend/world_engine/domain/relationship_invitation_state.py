"""Replay and command binding for private relationship invitations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from .errors import InvalidEvent
from .relationship_invitation_events import RelationInvitationChangedEvent
from .relationship_invitations import (
    RelationshipInvitation,
    accept_relationship_invitation,
    attend_relationship_meeting,
    decline_relationship_invitation,
    end_relationship_invitation,
    propose_relationship_invitation,
    withdraw_relationship_invitation,
)

if TYPE_CHECKING:
    from .models import WorldState

_PROPOSE_COMMAND = "action.propose_relationship_invitation"
_RESOLVE_COMMAND = "action.resolve_relationship_invitation"
_ATTEND_COMMAND = "action.attend_relationship_meeting"
NPC_REPLY_COMMAND = "action.request_relationship_reply"
NPC_ATTENDANCE_COMMAND = "action.request_relationship_meeting_attendance"
_TRANSITIONS = {
    "accept": accept_relationship_invitation,
    "decline": decline_relationship_invitation,
    "withdraw": withdraw_relationship_invitation,
    "end": end_relationship_invitation,
    "attend": attend_relationship_meeting,
}


def apply_relationship_invitation(state: WorldState, event: RelationInvitationChangedEvent) -> list[RelationshipInvitation]:
    invitation = event.resulting_invitation
    records = state.relationship_invitations
    if event.operation == "propose":
        if invitation.invitation_id != uuid5(event.command_id, "relationship-invitation"):
            raise InvalidEvent("Relationship invitation identity must derive from its proposal command.")
        expected = propose_relationship_invitation(
            state, records, invitation_id=invitation.invitation_id,
            proposer_id=event.actor_id, invitee_id=invitation.invitee_id,
            meeting=invitation.meeting,
        )
        updated = [*records, expected]
    elif event.operation == "attend":
        expected = attend_relationship_meeting(
            state, records,
            invitation_id=invitation.invitation_id,
            actor_id=event.actor_id,
            expected_revision=event.expected_revision,
            check_work_calendar=event.work_calendar_checked is True,
        )
        updated = [expected if record.invitation_id == invitation.invitation_id else record for record in records]
    else:
        expected = _TRANSITIONS[event.operation](
            state, records, invitation_id=invitation.invitation_id,
            actor_id=event.actor_id, expected_revision=event.expected_revision,
        )
        updated = [expected if record.invitation_id == invitation.invitation_id else record for record in records]
    if invitation != expected:
        raise InvalidEvent("Stored invitation does not match its authoritative transition.")
    return updated


def validate_relationship_invitation_batch(
    events: Sequence[object], *, world_id: UUID, branch_id: UUID, command_id: UUID, actor_id: UUID,
    ruleset_version: str, command_type: str | None = None, command_payload: Mapping[str, object] | None = None,
    require_work_calendar_checked: bool = False,
) -> None:
    event = next((item for item in events if isinstance(item, RelationInvitationChangedEvent)), None)
    if event is None:
        if command_type in {
            _PROPOSE_COMMAND, _RESOLVE_COMMAND, _ATTEND_COMMAND, NPC_REPLY_COMMAND, NPC_ATTENDANCE_COMMAND,
        }:
            raise InvalidEvent("A relationship invitation command requires its typed event.")
        return
    command_actor_id = event.requested_by or event.actor_id
    if len(events) != 1 or (event.world_id, event.branch_id, event.command_id, command_actor_id, event.ruleset_version) != (
        world_id, branch_id, command_id, actor_id, ruleset_version,
    ):
        raise InvalidEvent("A relationship invitation must be scoped to one exact command.")
    if command_type is None:
        return
    invitation = event.resulting_invitation
    if require_work_calendar_checked and event.operation == "attend" and event.work_calendar_checked is not True:
        raise InvalidEvent("A live meeting attendance command must record its work-calendar check.")
    if event.requested_by is not None:
        if event.operation == "attend":
            expected_type = NPC_ATTENDANCE_COMMAND
            expected_payload = {
                "kind": "request_relationship_meeting_attendance",
                "invitation_id": str(invitation.invitation_id),
            }
        else:
            expected_type = NPC_REPLY_COMMAND
            expected_payload = {"kind": "request_relationship_reply", "invitation_id": str(invitation.invitation_id)}
    elif event.operation == "propose":
        expected_type = _PROPOSE_COMMAND
        expected_payload = {"kind": "propose_relationship_invitation", "target_id": str(invitation.invitee_id)}
        if invitation.meeting is not None:
            expected_payload["meeting"] = invitation.meeting.model_dump(mode="json")
    elif event.operation == "attend":
        expected_type = _ATTEND_COMMAND
        expected_payload = {
            "kind": "attend_relationship_meeting",
            "invitation_id": str(invitation.invitation_id),
        }
    else:
        expected_type = _RESOLVE_COMMAND
        expected_payload = {"kind": "resolve_relationship_invitation", "invitation_id": str(invitation.invitation_id),
                            "operation": event.operation}
    if command_type != expected_type or command_payload != expected_payload:
        raise InvalidEvent("The invitation event does not match its command proposal.")
