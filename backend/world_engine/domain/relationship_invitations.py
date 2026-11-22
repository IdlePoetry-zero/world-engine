"""Pure, consent-preserving transitions for romantic invitations.

An accepted invitation records mutual assent. Dialogue, familiarity, trust,
or an NPC's generated words never change this record by themselves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .components import LifeComponent
from .errors import InvalidEvent
from .npc_attendance import npc_attendance_is_valid

if TYPE_CHECKING:
    from .models import WorldState


_MAX_REVISION = 2**63 - 1
Tick = Annotated[int, Field(strict=True, ge=-(2**63), le=_MAX_REVISION)]
InvitationStatus = Literal["pending", "declined", "accepted", "withdrawn", "ended"]


class InvitationMeeting(BaseModel):
    """A reserved half-open world-tick interval for an invitation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    start_tick: Tick
    end_tick: Tick

    @model_validator(mode="after")
    def valid_interval(self) -> InvitationMeeting:
        if self.end_tick <= self.start_tick:
            raise ValueError("an invitation meeting must have a positive half-open interval")
        return self


class InvitationMeetingAttendance(BaseModel):
    """A participant's explicit attendance at one tick in the reserved meeting."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    actor_id: UUID
    attended_tick: Tick


class InvitationMeetingUnavailable(InvalidEvent):
    """A scheduled invitation can no longer be accepted as proposed."""

    def __init__(self) -> None:
        super().__init__("This invitation meeting is no longer available.")


class RelationshipInvitation(BaseModel):
    """A private invitation between two adult characters.

    The record intentionally contains no intimacy score, relationship label,
    or inferred consent. An accepted invitation means only that this exact
    proposal received an explicit response.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    invitation_id: UUID
    proposer_id: UUID
    invitee_id: UUID
    status: InvitationStatus = "pending"
    proposed_tick: Tick
    updated_tick: Tick
    revision: StrictInt = Field(default=1, ge=1, le=_MAX_REVISION)
    meeting: InvitationMeeting | None = Field(default=None, exclude_if=lambda value: value is None)
    meeting_attendance: list[InvitationMeetingAttendance] = Field(
        default_factory=list, exclude_if=lambda rows: not rows,
    )

    @model_validator(mode="after")
    def valid_identity_and_lifecycle(self) -> RelationshipInvitation:
        if self.proposer_id == self.invitee_id:
            raise ValueError("a romantic invitation needs two distinct participants")
        if self.updated_tick < self.proposed_tick:
            raise ValueError("an invitation cannot be updated before it was proposed")
        if self.status == "pending" and (self.revision != 1 or self.updated_tick != self.proposed_tick):
            raise ValueError("a pending invitation must retain its original proposal revision and tick")
        if self.status in {"declined", "withdrawn", "accepted"} and self.revision < 2:
            raise ValueError("a resolved invitation must have at least one transition revision")
        if self.status == "ended" and self.revision < 3:
            raise ValueError("an ended relationship invitation must have been accepted first")
        if self.meeting is not None:
            if self.meeting.start_tick <= self.proposed_tick:
                raise ValueError("an invitation meeting must start after it was proposed")
            if self.status == "accepted" and self.updated_tick >= self.meeting.start_tick:
                raise ValueError("a timed invitation must be accepted before its meeting starts")
        if self.meeting_attendance:
            if self.meeting is None or self.status not in {"accepted", "ended"}:
                raise ValueError("meeting attendance requires an accepted invitation with a scheduled meeting")
            participant_ids = {self.proposer_id, self.invitee_id}
            attendance_actor_ids = [attendance.actor_id for attendance in self.meeting_attendance]
            if len(attendance_actor_ids) != len(set(attendance_actor_ids)):
                raise ValueError("each participant may attend this meeting at most once")
            if any(actor_id not in participant_ids for actor_id in attendance_actor_ids):
                raise ValueError("only an invitation participant may be recorded as attending")
            if any(
                not self.meeting.start_tick <= attendance.attended_tick < self.meeting.end_tick
                for attendance in self.meeting_attendance
            ):
                raise ValueError("meeting attendance must fall within the half-open meeting interval")
        return self


def propose_relationship_invitation(
    state: WorldState,
    records: Sequence[RelationshipInvitation],
    *,
    invitation_id: UUID,
    proposer_id: UUID,
    invitee_id: UUID,
    meeting: InvitationMeeting | None = None,
) -> RelationshipInvitation:
    """Propose to one co-located adult, refusing a second live record for the pair."""

    _require_state(state)
    parsed = _validated_records(records, current_tick=state.current_tick)
    parsed_meeting = _validated_meeting(meeting)
    if parsed_meeting is not None and parsed_meeting.start_tick <= state.current_tick:
        raise InvitationMeetingUnavailable()
    if any(item.invitation_id == invitation_id for item in parsed):
        raise InvalidEvent("Invitation ID already exists.")
    if any(
        _same_pair(item, proposer_id, invitee_id)
        and relationship_invitation_blocks_pair_at_tick(item, state.current_tick)
        for item in parsed
    ):
        raise InvalidEvent("This pair already has a pending or accepted invitation.")
    _require_active_adult_pair(state, proposer_id, invitee_id)
    return RelationshipInvitation(
        invitation_id=invitation_id,
        proposer_id=proposer_id,
        invitee_id=invitee_id,
        proposed_tick=state.current_tick,
        updated_tick=state.current_tick,
        revision=1,
        meeting=parsed_meeting,
    )


def accept_relationship_invitation(
    state: WorldState,
    records: Sequence[RelationshipInvitation],
    *,
    invitation_id: UUID,
    actor_id: UUID,
    expected_revision: int,
) -> RelationshipInvitation:
    """Record the invitee's explicit acceptance of a still-pending proposal."""

    return _respond(state, records, invitation_id=invitation_id, actor_id=actor_id,
                    expected_revision=expected_revision,
                    new_status="accepted")


def decline_relationship_invitation(
    state: WorldState,
    records: Sequence[RelationshipInvitation],
    *,
    invitation_id: UUID,
    actor_id: UUID,
    expected_revision: int,
) -> RelationshipInvitation:
    """Record the invitee's explicit refusal of a still-pending proposal."""

    return _respond(state, records, invitation_id=invitation_id, actor_id=actor_id,
                    expected_revision=expected_revision,
                    new_status="declined")


def withdraw_relationship_invitation(
    state: WorldState,
    records: Sequence[RelationshipInvitation],
    *,
    invitation_id: UUID,
    actor_id: UUID,
    expected_revision: int,
) -> RelationshipInvitation:
    """Let the proposer withdraw a proposal before it has been answered."""

    return _respond(state, records, invitation_id=invitation_id, actor_id=actor_id,
                    expected_revision=expected_revision,
                    new_status="withdrawn")


def end_relationship_invitation(
    state: WorldState,
    records: Sequence[RelationshipInvitation],
    *,
    invitation_id: UUID,
    actor_id: UUID,
    expected_revision: int,
) -> RelationshipInvitation:
    """End a previously accepted pairing by either active participant.

    Ending is allowed when the participants are apart; a participant need not
    stay physically present to withdraw their assent from an existing pairing.
    """

    _require_state(state)
    parsed = _current_invitation(state, records, invitation_id)
    _require_transition_state(state, parsed, actor_id, expected_revision)
    if parsed.status != "accepted":
        raise InvalidEvent("Only an accepted invitation can be ended.")
    if actor_id not in {parsed.proposer_id, parsed.invitee_id}:
        raise InvalidEvent("Only a participant can end this relationship invitation.")
    _require_active_adult(state, actor_id)
    return _transition(parsed, "ended", state.current_tick)


def attend_relationship_meeting(
    state: WorldState,
    records: Sequence[RelationshipInvitation],
    *,
    invitation_id: UUID,
    actor_id: UUID,
    expected_revision: int,
    check_work_calendar: bool = False,
) -> RelationshipInvitation:
    """Record participant attendance during an accepted meeting.

    ``check_work_calendar`` opts new host commands into the current-work
    conflict rule. It defaults to false so replay of attendance records created
    before that rule preserves its original behavior.
    """

    _require_state(state)
    parsed = _current_invitation(state, records, invitation_id)
    _require_transition_state(state, parsed, actor_id, expected_revision)
    if parsed.status != "accepted":
        raise InvalidEvent("Only an accepted invitation can record meeting attendance.")
    if parsed.meeting is None:
        raise InvalidEvent("Only a scheduled invitation meeting can record attendance.")
    if actor_id not in {parsed.proposer_id, parsed.invitee_id}:
        raise InvalidEvent("Only a participant can attend this relationship meeting.")
    if not parsed.meeting.start_tick <= state.current_tick < parsed.meeting.end_tick:
        raise InvitationMeetingUnavailable()
    _require_active_adult_pair(state, parsed.proposer_id, parsed.invitee_id)
    if check_work_calendar is True and _has_ongoing_work_attendance(
        state, {parsed.proposer_id, parsed.invitee_id},
    ):
        raise InvitationMeetingUnavailable()
    if any(attendance.actor_id == actor_id for attendance in parsed.meeting_attendance):
        raise InvalidEvent("This participant's attendance has already been recorded.")
    if parsed.revision >= _MAX_REVISION:
        raise InvalidEvent("Invitation revision has reached its supported limit.")
    return RelationshipInvitation(
        invitation_id=parsed.invitation_id,
        proposer_id=parsed.proposer_id,
        invitee_id=parsed.invitee_id,
        status=parsed.status,
        proposed_tick=parsed.proposed_tick,
        updated_tick=parsed.updated_tick,
        revision=parsed.revision + 1,
        meeting=parsed.meeting,
        meeting_attendance=[
            *parsed.meeting_attendance,
            InvitationMeetingAttendance(actor_id=actor_id, attended_tick=state.current_tick),
        ],
    )


def _respond(
    state: WorldState,
    records: Sequence[RelationshipInvitation],
    *,
    invitation_id: UUID,
    actor_id: UUID,
    expected_revision: int,
    new_status: Literal["accepted", "declined", "withdrawn"],
) -> RelationshipInvitation:
    _require_state(state)
    parsed = _current_invitation(state, records, invitation_id)
    _require_transition_state(state, parsed, actor_id, expected_revision)
    if parsed.status != "pending":
        raise InvalidEvent("Only a pending invitation can receive a response.")
    expected_actor = parsed.proposer_id if new_status == "withdrawn" else parsed.invitee_id
    if actor_id != expected_actor:
        role = "proposer" if new_status == "withdrawn" else "invitee"
        raise InvalidEvent(f"Only the {role} can make this invitation transition.")
    _require_active_adult_pair(state, parsed.proposer_id, parsed.invitee_id)
    if new_status == "accepted" and not invitation_meeting_available(state, records, parsed):
        raise InvitationMeetingUnavailable()
    return _transition(parsed, new_status, state.current_tick)


def invitation_meeting_available(
    state: WorldState,
    records: Sequence[RelationshipInvitation],
    invitation: RelationshipInvitation,
) -> bool:
    """Return whether this invitation's timed meeting is still available.

    Intervals are half-open, so an existing accepted meeting that ends at the
    proposed start (or starts at the proposed end) is adjacent rather than
    overlapping. Pending, withdrawn, declined and ended records do not reserve
    time. An accepted record also stops reserving time after either participant
    becomes inactive.
    """

    _require_state(state)
    candidate = _validated_invitation(invitation)
    if candidate.meeting is None:
        return True
    if candidate.meeting.start_tick <= state.current_tick:
        return False

    parsed = _validated_records(records, current_tick=state.current_tick)
    candidate_people = {candidate.proposer_id, candidate.invitee_id}
    for existing in parsed:
        if (
            existing.invitation_id == candidate.invitation_id
            or existing.status != "accepted"
            or existing.meeting is None
            or not _participants_are_active(state, existing)
            or candidate_people.isdisjoint({existing.proposer_id, existing.invitee_id})
        ):
            continue
        if (
            candidate.meeting.start_tick < existing.meeting.end_tick
            and existing.meeting.start_tick < candidate.meeting.end_tick
        ):
            return False
    return True


def _transition(
    invitation: RelationshipInvitation,
    status: Literal["accepted", "declined", "withdrawn", "ended"],
    updated_tick: int,
) -> RelationshipInvitation:
    if invitation.revision >= _MAX_REVISION:
        raise InvalidEvent("Invitation revision has reached its supported limit.")
    return RelationshipInvitation(
        invitation_id=invitation.invitation_id,
        proposer_id=invitation.proposer_id,
        invitee_id=invitation.invitee_id,
        status=status,
        proposed_tick=invitation.proposed_tick,
        updated_tick=updated_tick,
        revision=invitation.revision + 1,
        meeting=invitation.meeting,
        meeting_attendance=invitation.meeting_attendance,
    )


def _require_transition_state(
    state: WorldState,
    invitation: RelationshipInvitation,
    actor_id: UUID,
    expected_revision: int,
) -> None:
    _require_state(state)
    if not isinstance(actor_id, UUID):
        raise InvalidEvent("Invitation actor ID is invalid.")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
        raise InvalidEvent("Invitation expected_revision is invalid.")
    if invitation.revision != expected_revision:
        raise InvalidEvent("Invitation expected_revision does not match the current record.")
    if state.current_tick < invitation.updated_tick:
        raise InvalidEvent("Invitation cannot transition before its latest recorded tick.")


def _current_invitation(
    state: WorldState,
    records: Sequence[RelationshipInvitation],
    invitation_id: UUID,
) -> RelationshipInvitation:
    if not isinstance(invitation_id, UUID):
        raise InvalidEvent("Invitation ID is invalid.")
    parsed = _validated_records(records, current_tick=state.current_tick)
    matches = [item for item in parsed if item.invitation_id == invitation_id]
    if len(matches) != 1:
        raise InvalidEvent("Invitation does not exist in the current record set.")
    invitation = matches[0]
    if invitation.updated_tick > state.current_tick or any(
        attendance.attended_tick > state.current_tick for attendance in invitation.meeting_attendance
    ):
        raise InvalidEvent("Invitation record is from the future relative to this world state.")
    return invitation


def _require_active_adult_pair(state: WorldState, first_id: UUID, second_id: UUID) -> None:
    first = _require_active_adult(state, first_id)
    second = _require_active_adult(state, second_id)
    if first.location_id is None or first.location_id != second.location_id:
        raise InvalidEvent("Both adult characters must be co-located for this invitation action.")
    location = state.entities.get(first.location_id)
    if location is None or location.status != "active" or location.kind not in {"place", "location"}:
        raise InvalidEvent("Both adult characters must be at the same active place.")


def _require_active_adult(state: WorldState, actor_id: UUID):
    entity = state.entities.get(actor_id)
    if entity is None or entity.kind not in {"character", "person"} or entity.status != "active":
        raise InvalidEvent("Invitation participants must be active characters.")
    try:
        life = LifeComponent.model_validate(entity.components.get("life"))
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Invitation participant has no valid life component.") from exc
    age_year_ticks = state.calendar.day_seconds * state.calendar.year_days
    age_years = (state.current_tick - life.birth_tick) // age_year_ticks
    if life.life_stage not in {"adult", "elder"} or age_years < 18:
        raise InvalidEvent("Romantic invitations require characters at least 18 world years old.")
    return entity


def _same_pair(invitation: RelationshipInvitation, first_id: UUID, second_id: UUID) -> bool:
    return {invitation.proposer_id, invitation.invitee_id} == {first_id, second_id}


def _participants_are_active(state: WorldState, invitation: RelationshipInvitation) -> bool:
    return all(
        (entity := state.entities.get(actor_id)) is not None
        and entity.kind in {"character", "person"}
        and entity.status == "active"
        for actor_id in (invitation.proposer_id, invitation.invitee_id)
    )


def _has_ongoing_work_attendance(state: WorldState, participant_ids: set[UUID]) -> bool:
    """Check only registered, still-valid NPC attendance at the current tick."""
    return any(
        attendance.actor_id in participant_ids
        and attendance.starts_at_tick <= state.current_tick < attendance.ends_at_tick
        and npc_attendance_is_valid(state, attendance)
        for attendance in state.npc_attendance
    )


def _validated_invitation(value: RelationshipInvitation) -> RelationshipInvitation:
    if not isinstance(value, RelationshipInvitation):
        raise InvalidEvent("Invitation record is invalid.")
    try:
        return RelationshipInvitation.model_validate(value.model_dump(mode="python"))
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Invitation record is invalid.") from exc


def _validated_meeting(value: InvitationMeeting | None) -> InvitationMeeting | None:
    if value is None:
        return None
    raw = value.model_dump(mode="python") if isinstance(value, InvitationMeeting) else value
    try:
        return InvitationMeeting.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Invitation meeting is invalid.") from exc


def relationship_invitation_blocks_pair_at_tick(
    invitation: RelationshipInvitation,
    current_tick: int,
) -> bool:
    """Return whether an invitation still blocks another proposal for its pair.

    Timed proposals stop reserving the pair when their response cutoff arrives;
    timed acceptances stop reserving it when their half-open meeting ends.
    Untimed pending and accepted records remain open until an explicit response
    or end transition, preserving the legacy lifecycle.
    """

    if invitation.status not in {"pending", "accepted"}:
        return False
    if invitation.meeting is None:
        return True
    if invitation.status == "pending":
        return current_tick < invitation.meeting.start_tick
    return current_tick < invitation.meeting.end_tick


def _validated_records(
    records: Sequence[RelationshipInvitation],
    *,
    current_tick: int,
) -> list[RelationshipInvitation]:
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise InvalidEvent("Invitation records must be a sequence.")
    parsed = [_validated_invitation(item) for item in records]
    ids = [item.invitation_id for item in parsed]
    if len(ids) != len(set(ids)):
        raise InvalidEvent("Invitation record IDs must be unique.")
    live_pairs = [
        frozenset((item.proposer_id, item.invitee_id))
        for item in parsed
        if relationship_invitation_blocks_pair_at_tick(item, current_tick)
    ]
    if len(live_pairs) != len(set(live_pairs)):
        raise InvalidEvent("Each pair may have only one pending or accepted invitation.")
    return parsed


def _require_state(state: WorldState) -> None:
    from .models import WorldState

    if not isinstance(state, WorldState):
        raise InvalidEvent("A validated WorldState is required.")

