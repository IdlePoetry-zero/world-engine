"""Pure selection of hosted NPCs who may be asked about an accepted meeting.

An accepted appointment is only a possible attendance opportunity. This
module does not choose attendance, move participants, or mutate the world.
Candidates exclude settled decisions and must still be revalidated at commit.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from uuid import UUID

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import WorldState
from world_engine.domain.npc_plans import occupied_npc_ids
from world_engine.domain.relationship_invitations import (
    attend_relationship_meeting,
)
from world_engine.domain.travel_journeys import active_travel_journey

from .contracts import DecisionOption, DecisionRequest


MeetingAttendanceAttemptKey = tuple[UUID, UUID]
NPC_MEETING_ATTENDANCE_DECISION_KIND = "npc.meeting_attendance"


@dataclass(frozen=True, slots=True)
class NpcMeetingAttendanceCandidate:
    """Minimal snapshot binding one NPC decision to one participant and tick."""

    invitation_id: UUID
    actor_id: UUID
    counterpart_id: UUID
    expected_invitation_revision: int
    at_tick: int


def npc_meeting_attendance_request(
    candidate: NpcMeetingAttendanceCandidate, command_id: UUID, state_version: int,
) -> DecisionRequest:
    from world_engine.domain.npc_meeting_attendance import npc_meeting_attendance_request_id

    return DecisionRequest(
        request_id=npc_meeting_attendance_request_id(
            command_id, candidate.invitation_id, candidate.actor_id,
            candidate.expected_invitation_revision, candidate.at_tick,
        ),
        kind=NPC_MEETING_ATTENDANCE_DECISION_KIND,
        actor_id=candidate.actor_id,
        at_tick=candidate.at_tick,
        state_version=state_version,
        title="决定是否按已经接受的约定赴约",
        reason_code="npc_meeting_attendance_decision",
        options=(
            DecisionOption(option_id="attend", label="现在赴约", effect_summary="只记录本人物到场。"),
            DecisionOption(option_id="defer", label="暂不赴约", effect_summary="保持双方的约定和已有出席记录。"),
        ),
        evidence_ids=(candidate.invitation_id,),
    )


def next_npc_meeting_attendance_candidate(
    state: WorldState,
    *,
    externally_controlled_actor_ids: Collection[UUID] = (),
    attempted: Collection[MeetingAttendanceAttemptKey] = (),
) -> NpcMeetingAttendanceCandidate | None:
    """Return the first currently eligible NPC attendance decision.

    Candidates are ordered by meeting start tick, invitation ID, then actor
    ID. Eligibility is evaluated at ``state.current_tick`` rather than at the
    scheduled start, so a real travel arrival or work-shift end can make an
    NPC eligible later in the half-open meeting interval. A participant is
    considered busy if either party is in transit or has an active timed NPC
    execution. The canonical invitation attendance planner checks the pair's
    current location, adulthood, activity, interval, duplicate attendance,
    and registered work attendance.

    The result contains IDs and revision only; callers must not use this
    selector to expose either participant's unrelated private records.
    """

    if not isinstance(state, WorldState):
        return None
    open_invitations = [
        (invitation, meeting) for invitation in state.relationship_invitations
        if invitation.status == "accepted"
        and (meeting := invitation.meeting) is not None
        and meeting.start_tick <= state.current_tick < meeting.end_tick
        and invitation.updated_tick <= state.current_tick
    ]
    if not open_invitations:
        return None

    controlled = _uuid_set(externally_controlled_actor_ids)
    attempted_keys = _attempted_set(attempted) | {
        (record.invitation_id, record.actor_id) for record in state.npc_meeting_attendance_decisions
    }
    timed_npc_ids = occupied_npc_ids(state, state.current_tick)
    candidates: list[tuple[int, str, str, NpcMeetingAttendanceCandidate]] = []

    for invitation, meeting in open_invitations:
        participants = sorted(
            (invitation.proposer_id, invitation.invitee_id),
            key=str,
        )
        for actor_id in participants:
            attempt_key = (invitation.invitation_id, actor_id)
            if (
                actor_id == state.player_id
                or actor_id in controlled
                or attempt_key in attempted_keys
                or any(row.actor_id == actor_id for row in invitation.meeting_attendance)
            ):
                continue

            actor = state.entities.get(actor_id)
            if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
                continue
            counterpart_id = (
                invitation.invitee_id if invitation.proposer_id == actor_id else invitation.proposer_id
            )
            counterpart = state.entities.get(counterpart_id)
            if (
                counterpart is None
                or counterpart.kind not in {"character", "person"}
                or counterpart.status != "active"
                or actor_id in timed_npc_ids
                or counterpart_id in timed_npc_ids
                or active_travel_journey(state, actor_id) is not None
                or active_travel_journey(state, counterpart_id) is not None
            ):
                continue

            try:
                # This is the same domain transition used by the live action
                # path. It is a pure preflight; no result is applied here.
                updated = attend_relationship_meeting(
                    state,
                    state.relationship_invitations,
                    invitation_id=invitation.invitation_id,
                    actor_id=actor_id,
                    expected_revision=invitation.revision,
                    check_work_calendar=True,
                )
            except (InvalidEvent, TypeError, ValueError):
                continue
            if not any(row.actor_id == actor_id and row.attended_tick == state.current_tick
                       for row in updated.meeting_attendance):
                continue

            candidate = NpcMeetingAttendanceCandidate(
                invitation_id=invitation.invitation_id,
                actor_id=actor_id,
                counterpart_id=counterpart_id,
                expected_invitation_revision=invitation.revision,
                at_tick=state.current_tick,
            )
            candidates.append((meeting.start_tick, str(invitation.invitation_id), str(actor_id), candidate))

    return min(candidates, key=lambda item: item[:3])[3] if candidates else None


def next_npc_meeting_attendance_boundary(
    state: WorldState,
    horizon: int,
    *,
    externally_controlled_actor_ids: Collection[UUID] = (),
    attempted: Collection[MeetingAttendanceAttemptKey] = (),
) -> int | None:
    """Return the next accepted meeting start worth waking the simulation for.

    This boundary deliberately does not require the NPC to be available or
    colocated now: those facts can change before the meeting or during its
    open interval. Call ``next_npc_meeting_attendance_candidate`` after the
    start boundary and again after ordinary travel, work, or timed-action
    boundaries. Starts are selected only when strictly future and within the
    requested horizon.
    """

    if (
        not isinstance(state, WorldState)
        or type(horizon) is not int
        or horizon <= state.current_tick
    ):
        return None

    controlled = _uuid_set(externally_controlled_actor_ids)
    attempted_keys = _attempted_set(attempted)
    starts: list[int] = []
    for invitation in state.relationship_invitations:
        meeting = invitation.meeting
        if (
            invitation.status != "accepted"
            or meeting is None
            or meeting.start_tick <= state.current_tick
            or meeting.start_tick > horizon
            or invitation.updated_tick > state.current_tick
        ):
            continue
        if any(
            actor_id != state.player_id
            and actor_id not in controlled
            and (invitation.invitation_id, actor_id) not in attempted_keys
            and not any(row.actor_id == actor_id for row in invitation.meeting_attendance)
            and (actor := state.entities.get(actor_id)) is not None
            and actor.kind in {"character", "person"}
            and actor.status == "active"
            for actor_id in (invitation.proposer_id, invitation.invitee_id)
        ):
            starts.append(meeting.start_tick)

    return min(starts) if starts else None


def _uuid_set(values: Collection[UUID]) -> set[UUID]:
    try:
        return {value for value in values if isinstance(value, UUID)}
    except TypeError:
        return set()


def _attempted_set(values: Collection[MeetingAttendanceAttemptKey]) -> set[MeetingAttendanceAttemptKey]:
    try:
        return {
            value for value in values
            if isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[0], UUID)
            and isinstance(value[1], UUID)
        }
    except TypeError:
        return set()
