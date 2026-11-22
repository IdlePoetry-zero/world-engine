"""Private, participant-owned impressions after a completed relationship meeting."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .errors import InvalidEvent
from .relationship_invitations import RelationshipInvitation

if TYPE_CHECKING:
    from .models import WorldState


_MIN_TICK = -(2**63)
_MAX_TICK = 2**63 - 1
Tick = Annotated[StrictInt, Field(ge=_MIN_TICK, le=_MAX_TICK)]
MeetingOutlook = Literal["closer", "unchanged", "uneasy"]


class MeetingReflection(BaseModel):
    """One participant's private impression of a completed shared meeting."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    invitation_id: UUID
    actor_id: UUID
    outlook: MeetingOutlook
    recorded_tick: Tick


def plan_meeting_reflection(
    state: WorldState,
    *,
    invitation_id: UUID,
    actor_id: UUID,
    outlook: MeetingOutlook,
) -> MeetingReflection:
    """Plan one participant's private reflection without changing world state."""

    _require_state(state)
    if not isinstance(invitation_id, UUID):
        raise InvalidEvent("Meeting reflection invitation ID is invalid.")
    if not isinstance(actor_id, UUID):
        raise InvalidEvent("Meeting reflection actor ID is invalid.")
    if not isinstance(outlook, str) or outlook not in {"closer", "unchanged", "uneasy"}:
        raise InvalidEvent("Meeting reflection outlook is invalid.")

    invitations = _validated_invitations(state.relationship_invitations)
    matches = [item for item in invitations if item.invitation_id == invitation_id]
    if len(matches) != 1:
        raise InvalidEvent("Meeting reflection invitation does not exist exactly once.")
    invitation = matches[0]
    if invitation.updated_tick > state.current_tick or any(
        row.attended_tick > state.current_tick for row in invitation.meeting_attendance
    ):
        raise InvalidEvent("Meeting reflection invitation records cannot be from the future.")

    if invitation.status != "accepted":
        raise InvalidEvent("Meeting reflections require a still-accepted invitation.")
    if invitation.meeting is None:
        raise InvalidEvent("Meeting reflections require a timed invitation meeting.")

    participant_ids = {invitation.proposer_id, invitation.invitee_id}
    attendance_actor_ids = [row.actor_id for row in invitation.meeting_attendance]
    if len(attendance_actor_ids) != 2 or set(attendance_actor_ids) != participant_ids:
        raise InvalidEvent("Both invitation participants must have attended the meeting.")
    if state.current_tick < invitation.meeting.end_tick:
        raise InvalidEvent("Meeting reflections can be recorded only at or after the meeting ends.")
    if actor_id not in participant_ids:
        raise InvalidEvent("Only an invitation participant can record a meeting reflection.")
    if not _participants_are_active(state, invitation):
        raise InvalidEvent("Both meeting participants must currently be active characters.")

    reflections = _validated_reflections(state.relationship_reflections)
    if any(
        item.invitation_id == invitation_id and item.actor_id == actor_id
        for item in reflections
    ):
        raise InvalidEvent("This participant has already recorded a reflection for the meeting.")

    return MeetingReflection(
        invitation_id=invitation_id,
        actor_id=actor_id,
        outlook=outlook,
        recorded_tick=state.current_tick,
    )


def _participants_are_active(state: WorldState, invitation: RelationshipInvitation) -> bool:
    """Use the invitation lifecycle's definition of currently living participants."""

    return all(
        (entity := state.entities.get(participant_id)) is not None
        and entity.kind in {"character", "person"}
        and entity.status == "active"
        for participant_id in (invitation.proposer_id, invitation.invitee_id)
    )


def _validated_invitations(values: object) -> list[RelationshipInvitation]:
    if not isinstance(values, list):
        raise InvalidEvent("World relationship invitations are invalid.")
    parsed: list[RelationshipInvitation] = []
    try:
        for value in values:
            raw = value.model_dump(mode="python") if isinstance(value, RelationshipInvitation) else value
            parsed.append(RelationshipInvitation.model_validate(raw))
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("World relationship invitations are invalid.") from exc
    invitation_ids = [item.invitation_id for item in parsed]
    if len(invitation_ids) != len(set(invitation_ids)):
        raise InvalidEvent("World relationship invitation IDs must be unique.")
    return parsed


def _validated_reflections(values: object) -> list[MeetingReflection]:
    if not isinstance(values, list):
        raise InvalidEvent("World relationship reflections are invalid.")
    parsed: list[MeetingReflection] = []
    try:
        for value in values:
            raw = value.model_dump(mode="python") if isinstance(value, MeetingReflection) else value
            parsed.append(MeetingReflection.model_validate(raw))
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("World relationship reflections are invalid.") from exc
    keys = [(item.invitation_id, item.actor_id) for item in parsed]
    if len(keys) != len(set(keys)):
        raise InvalidEvent("A participant may reflect only once per meeting invitation.")
    return parsed


def _require_state(state: WorldState) -> None:
    from .models import WorldState

    if not isinstance(state, WorldState):
        raise InvalidEvent("A validated WorldState is required.")
