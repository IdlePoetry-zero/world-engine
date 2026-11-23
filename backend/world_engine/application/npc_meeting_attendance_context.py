"""Private, bounded context for one NPC's meeting-attendance choice.

The projection is a pure read. It revalidates the selector's invitation,
participant pair, tick, work-calendar preflight and current co-location before
returning only the attending NPC's small persona allowlist, the counterpart's
display name, the NPC's own directed relation, and the exact meeting interval.
It deliberately excludes invitation notes, memories, goals, unrelated records,
and all other relationship data.
"""

from __future__ import annotations

from uuid import UUID

from world_engine.application.relationship_decisions import _own_directed_relation
from world_engine.domain.components import PersonalityComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import WorldState
from world_engine.domain.npc_plans import occupied_npc_ids
from world_engine.domain.relationship_invitations import (
    RelationshipInvitation,
    attend_relationship_meeting,
)
from world_engine.domain.travel_journeys import active_travel_journey
from world_engine.simulation.npc_meeting_attendance import NpcMeetingAttendanceCandidate


def project_npc_meeting_attendance_context(
    state: WorldState,
    candidate: NpcMeetingAttendanceCandidate,
) -> dict[str, object]:
    """Return only actor-safe facts for a current, eligible attendance choice.

    Invalid, stale, tampered, nonparticipant, busy, or non-colocated candidates
    return an empty mapping. The candidate's IDs and revision are used only to
    validate the state and are never copied into the model-facing projection.
    """

    if not isinstance(state, WorldState) or not isinstance(candidate, NpcMeetingAttendanceCandidate):
        return {}
    if (
        type(state.current_tick) is not int
        or not isinstance(candidate.invitation_id, UUID)
        or not isinstance(candidate.actor_id, UUID)
        or not isinstance(candidate.counterpart_id, UUID)
        or type(candidate.expected_invitation_revision) is not int
        or candidate.expected_invitation_revision < 1
        or type(candidate.at_tick) is not int
        or candidate.at_tick != state.current_tick
        or candidate.actor_id == candidate.counterpart_id
        or candidate.actor_id == state.player_id
    ):
        return {}

    if any(record.invitation_id == candidate.invitation_id and record.actor_id == candidate.actor_id
           for record in state.npc_meeting_attendance_decisions):
        return {}

    invitations = [
        invitation
        for invitation in state.relationship_invitations
        if isinstance(invitation, RelationshipInvitation)
        and invitation.invitation_id == candidate.invitation_id
    ]
    if len(invitations) != 1:
        return {}
    invitation = invitations[0]
    meeting = invitation.meeting
    if (
        invitation.status != "accepted"
        or meeting is None
        or invitation.revision != candidate.expected_invitation_revision
        or invitation.updated_tick > state.current_tick
        or not meeting.start_tick <= state.current_tick < meeting.end_tick
        or candidate.actor_id not in {invitation.proposer_id, invitation.invitee_id}
    ):
        return {}

    participant_ids = {invitation.proposer_id, invitation.invitee_id}
    other_ids = participant_ids - {candidate.actor_id}
    if len(other_ids) != 1 or candidate.counterpart_id not in other_ids:
        return {}

    actor = state.entities.get(candidate.actor_id)
    counterpart = state.entities.get(candidate.counterpart_id)
    if (
        actor is None
        or counterpart is None
        or actor.kind not in {"character", "person"}
        or counterpart.kind not in {"character", "person"}
        or actor.status != "active"
        or counterpart.status != "active"
        or not isinstance(actor.name, str)
        or not actor.name.strip()
        or not isinstance(counterpart.name, str)
        or not counterpart.name.strip()
        or actor.location_id is None
        or actor.location_id != counterpart.location_id
    ):
        return {}

    location = state.entities.get(actor.location_id)
    if (
        location is None
        or location.kind not in {"place", "location"}
        or location.status != "active"
    ):
        return {}

    try:
        occupied = occupied_npc_ids(state, state.current_tick)
        if (
            active_travel_journey(state, candidate.actor_id) is not None
            or active_travel_journey(state, candidate.counterpart_id) is not None
            or candidate.actor_id in occupied
            or candidate.counterpart_id in occupied
        ):
            return {}

        # Use the same pure domain transition as the selector and commit path
        # to revalidate participant adulthood, the half-open interval,
        # co-location, work-calendar conflicts, and duplicate attendance.
        validated_attendance = attend_relationship_meeting(
            state,
            state.relationship_invitations,
            invitation_id=candidate.invitation_id,
            actor_id=candidate.actor_id,
            expected_revision=candidate.expected_invitation_revision,
            check_work_calendar=True,
        )
    except (InvalidEvent, TypeError, ValueError):
        return {}

    if not any(
        row.actor_id == candidate.actor_id and row.attended_tick == state.current_tick
        for row in validated_attendance.meeting_attendance
    ):
        return {}

    try:
        personality = PersonalityComponent.model_validate(actor.components.get("personality", {}))
        own_relation = _own_directed_relation(state, candidate.actor_id, candidate.counterpart_id)
    except (TypeError, ValueError, AttributeError):
        return {}

    return {
        "actor": {
            "name": actor.name,
            "traits": list(personality.traits),
            "values": list(personality.values),
        },
        "counterpart": {"display_name": counterpart.name},
        "own_relationship_impression": own_relation,
        "meeting_interval": {
            "start_tick": meeting.start_tick,
            "end_tick": meeting.end_tick,
            "start_inclusive": True,
            "end_exclusive": True,
            **({"venue_name": location.name} if meeting.venue_id is not None and location.name else {}),
        },
        "current_tick": state.current_tick,
        "co_location_fact": {
            "both_invitation_participants_are_currently_co_located": True,
            "validated_at_tick": state.current_tick,
        },
    }
