from __future__ import annotations

from uuid import UUID, uuid5

import pytest

from world_engine.domain.components import LifeComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.domain.relationship_invitation_events import (
    RelationInvitationChangedEvent,
    invitation_event_id,
    participant_visibility,
)
from world_engine.domain.relationship_invitation_state import (
    NPC_ATTENDANCE_COMMAND,
    apply_relationship_invitation,
    validate_relationship_invitation_batch,
)
from world_engine.domain.relationship_invitations import (
    InvitationMeeting,
    RelationshipInvitation,
    accept_relationship_invitation,
    attend_relationship_meeting,
    propose_relationship_invitation,
)


WORLD_ID = UUID("91000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("92000000-0000-4000-8000-000000000002")
PROPOSAL_COMMAND_ID = UUID("93000000-0000-4000-8000-000000000003")
ACCEPT_COMMAND_ID = UUID("94000000-0000-4000-8000-000000000004")
ATTEND_COMMAND_ID = UUID("98000000-0000-4000-8000-000000000008")
NPC_ATTENDANCE_COMMAND_ID = UUID("99000000-0000-4000-8000-000000000009")
RULESET = "relationship-meeting-replay.v1"
PLACE_ID = UUID("95000000-0000-4000-8000-000000000005")
PROPOSER_ID = UUID("96000000-0000-4000-8000-000000000006")
INVITEE_ID = UUID("97000000-0000-4000-8000-000000000007")


def _state() -> WorldState:
    year_seconds = 86_400 * 360
    return WorldState(
        current_tick=0,
        entities={
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Station"),
            PROPOSER_ID: WorldEntity(
                id=PROPOSER_ID,
                kind="character",
                name="Ari",
                location_id=PLACE_ID,
                components={
                    "life": LifeComponent(birth_tick=-20 * year_seconds, life_stage="adult").model_dump(mode="json")
                },
            ),
            INVITEE_ID: WorldEntity(
                id=INVITEE_ID,
                kind="character",
                name="Bea",
                location_id=PLACE_ID,
                components={
                    "life": LifeComponent(birth_tick=-20 * year_seconds, life_stage="adult").model_dump(mode="json")
                },
            ),
        },
        calendar={"day_seconds": 86_400, "year_days": 360},
    )


def _event(
    invitation: RelationshipInvitation,
    *,
    command_id: UUID,
    operation: str,
    actor_id: UUID,
    expected_revision: int,
    requested_by: UUID | None = None,
    work_calendar_checked: bool | None = None,
) -> RelationInvitationChangedEvent:
    return RelationInvitationChangedEvent(
        event_id=invitation_event_id(command_id, RULESET, operation, invitation.invitation_id),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        actor_id=actor_id,
        requested_by=requested_by,
        work_calendar_checked=work_calendar_checked,
        ruleset_version=RULESET,
        operation=operation,
        expected_revision=expected_revision,
        resulting_invitation=invitation,
        visibility=participant_visibility(PROPOSER_ID, INVITEE_ID),
    )


def test_replay_rederives_and_preserves_meeting_across_acceptance():
    state = _state()
    meeting = InvitationMeeting(start_tick=100, end_tick=200)
    proposed = propose_relationship_invitation(
        state,
        state.relationship_invitations,
        invitation_id=uuid5(PROPOSAL_COMMAND_ID, "relationship-invitation"),
        proposer_id=PROPOSER_ID,
        invitee_id=INVITEE_ID,
        meeting=meeting,
    )
    proposal_event = _event(
        proposed,
        command_id=PROPOSAL_COMMAND_ID,
        operation="propose",
        actor_id=PROPOSER_ID,
        expected_revision=0,
    )
    proposed_state = apply_event(state, proposal_event)
    assert proposed_state.relationship_invitations == [proposed]
    assert apply_relationship_invitation(state, proposal_event) == [proposed]

    accepted = accept_relationship_invitation(
        proposed_state,
        proposed_state.relationship_invitations,
        invitation_id=proposed.invitation_id,
        actor_id=INVITEE_ID,
        expected_revision=1,
    )
    acceptance_event = _event(
        accepted,
        command_id=ACCEPT_COMMAND_ID,
        operation="accept",
        actor_id=INVITEE_ID,
        expected_revision=1,
    )
    accepted_state = apply_event(proposed_state, acceptance_event)
    assert accepted_state.relationship_invitations == [accepted]
    assert accepted_state.relationship_invitations[0].meeting == meeting


def test_proposal_command_binding_includes_meeting_when_present():
    state = _state()
    proposed = propose_relationship_invitation(
        state,
        [],
        invitation_id=uuid5(PROPOSAL_COMMAND_ID, "relationship-invitation"),
        proposer_id=PROPOSER_ID,
        invitee_id=INVITEE_ID,
        meeting=InvitationMeeting(start_tick=100, end_tick=200),
    )
    event = _event(
        proposed,
        command_id=PROPOSAL_COMMAND_ID,
        operation="propose",
        actor_id=PROPOSER_ID,
        expected_revision=0,
    )
    payload = {
        "kind": "propose_relationship_invitation",
        "target_id": str(INVITEE_ID),
        "meeting": {"start_tick": 100, "end_tick": 200},
    }
    scope = {
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": PROPOSAL_COMMAND_ID,
        "actor_id": PROPOSER_ID,
        "ruleset_version": RULESET,
        "command_type": "action.propose_relationship_invitation",
    }
    validate_relationship_invitation_batch([event], command_payload=payload, **scope)
    for forged_payload in (
        {key: value for key, value in payload.items() if key != "meeting"},
        {**payload, "meeting": {"start_tick": 101, "end_tick": 200}},
        {**payload, "meeting": {"start_tick": 100, "end_tick": 200}, "extra": True},
    ):
        with pytest.raises(InvalidEvent, match="does not match its command proposal"):
            validate_relationship_invitation_batch([event], command_payload=forged_payload, **scope)


def test_legacy_proposal_command_binding_keeps_its_original_payload_shape():
    state = _state()
    proposed = propose_relationship_invitation(
        state,
        [],
        invitation_id=uuid5(PROPOSAL_COMMAND_ID, "relationship-invitation"),
        proposer_id=PROPOSER_ID,
        invitee_id=INVITEE_ID,
    )
    assert "meeting" not in proposed.model_dump(mode="json")
    event = _event(
        proposed,
        command_id=PROPOSAL_COMMAND_ID,
        operation="propose",
        actor_id=PROPOSER_ID,
        expected_revision=0,
    )
    validate_relationship_invitation_batch(
        [event],
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=PROPOSAL_COMMAND_ID,
        actor_id=PROPOSER_ID,
        ruleset_version=RULESET,
        command_type="action.propose_relationship_invitation",
        command_payload={
            "kind": "propose_relationship_invitation",
            "target_id": str(INVITEE_ID),
        },
    )


def test_attendance_replay_binds_actor_and_exact_command_payload():
    state = _state()
    proposed = propose_relationship_invitation(
        state,
        [],
        invitation_id=uuid5(PROPOSAL_COMMAND_ID, "relationship-invitation"),
        proposer_id=PROPOSER_ID,
        invitee_id=INVITEE_ID,
        meeting=InvitationMeeting(start_tick=100, end_tick=200),
    )
    proposed_state = apply_event(state, _event(
        proposed, command_id=PROPOSAL_COMMAND_ID, operation="propose",
        actor_id=PROPOSER_ID, expected_revision=0,
    ))
    accepted = accept_relationship_invitation(
        proposed_state, proposed_state.relationship_invitations,
        invitation_id=proposed.invitation_id, actor_id=INVITEE_ID, expected_revision=1,
    )
    accepted_state = apply_event(proposed_state, _event(
        accepted, command_id=ACCEPT_COMMAND_ID, operation="accept",
        actor_id=INVITEE_ID, expected_revision=1,
    ))
    in_meeting = WorldState.model_validate({
        **accepted_state.model_dump(mode="python"), "current_tick": 100,
    })
    attended = attend_relationship_meeting(
        in_meeting, in_meeting.relationship_invitations,
        invitation_id=proposed.invitation_id, actor_id=PROPOSER_ID, expected_revision=2,
        check_work_calendar=True,
    )
    event = _event(
        attended, command_id=ATTEND_COMMAND_ID, operation="attend",
        actor_id=PROPOSER_ID, expected_revision=2, work_calendar_checked=True,
    )
    scope = {
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": ATTEND_COMMAND_ID,
        "actor_id": PROPOSER_ID,
        "ruleset_version": RULESET,
        "command_type": "action.attend_relationship_meeting",
    }
    payload = {
        "kind": "attend_relationship_meeting",
        "invitation_id": str(proposed.invitation_id),
    }
    validate_relationship_invitation_batch([event], command_payload=payload, **scope)
    validate_relationship_invitation_batch(
        [event], command_payload=payload, require_work_calendar_checked=True, **scope,
    )
    replayed = apply_event(in_meeting, event)
    assert replayed.relationship_invitations == [attended]
    assert attended.revision == 3
    assert attended.updated_tick == 0
    assert [(row.actor_id, row.attended_tick) for row in attended.meeting_attendance] == [(PROPOSER_ID, 100)]

    for forged_payload in (
        {"kind": "attend_relationship_meeting", "invitation_id": str(uuid5(ATTEND_COMMAND_ID, "other"))},
        {**payload, "extra": True},
        {"kind": "attend_relationship_meeting"},
    ):
        with pytest.raises(InvalidEvent, match="does not match its command proposal"):
            validate_relationship_invitation_batch([event], command_payload=forged_payload, **scope)
    with pytest.raises(InvalidEvent, match="requires its typed event"):
        validate_relationship_invitation_batch([], command_payload=payload, **scope)

    forged_attendance = attended.meeting_attendance[0].model_copy(update={"attended_tick": 101})
    forged_invitation = attended.model_copy(update={"meeting_attendance": [forged_attendance]})
    with pytest.raises(ValueError, match="attendance cannot be recorded in the future"):
        WorldState.model_validate({
            **replayed.model_dump(mode="python"),
            "relationship_invitations": [forged_invitation.model_dump(mode="python")],
        })
    forged_event = RelationInvitationChangedEvent.model_validate({
        **event.model_dump(mode="python"),
        "resulting_invitation": forged_invitation.model_dump(mode="python"),
    })
    with pytest.raises(InvalidEvent, match="does not match its authoritative transition"):
        apply_event(in_meeting, forged_event)


def test_requested_npc_attendance_binds_requester_to_exact_typed_command_and_replays_ordinary_attend():
    state = _state()
    proposed = propose_relationship_invitation(
        state,
        [],
        invitation_id=uuid5(PROPOSAL_COMMAND_ID, "relationship-invitation"),
        proposer_id=PROPOSER_ID,
        invitee_id=INVITEE_ID,
        meeting=InvitationMeeting(start_tick=100, end_tick=200),
    )
    proposed_state = apply_event(state, _event(
        proposed, command_id=PROPOSAL_COMMAND_ID, operation="propose",
        actor_id=PROPOSER_ID, expected_revision=0,
    ))
    accepted = accept_relationship_invitation(
        proposed_state, proposed_state.relationship_invitations,
        invitation_id=proposed.invitation_id, actor_id=INVITEE_ID, expected_revision=1,
    )
    accepted_state = apply_event(proposed_state, _event(
        accepted, command_id=ACCEPT_COMMAND_ID, operation="accept",
        actor_id=INVITEE_ID, expected_revision=1,
    ))
    in_meeting = WorldState.model_validate({
        **accepted_state.model_dump(mode="python"), "current_tick": 100,
    })
    attended = attend_relationship_meeting(
        in_meeting, in_meeting.relationship_invitations,
        invitation_id=proposed.invitation_id, actor_id=INVITEE_ID, expected_revision=2,
        check_work_calendar=True,
    )
    event = _event(
        attended, command_id=NPC_ATTENDANCE_COMMAND_ID, operation="attend",
        actor_id=INVITEE_ID, expected_revision=2, requested_by=PROPOSER_ID,
        work_calendar_checked=True,
    )
    payload = {
        "kind": "request_relationship_meeting_attendance",
        "invitation_id": str(proposed.invitation_id),
    }
    scope = {
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": NPC_ATTENDANCE_COMMAND_ID,
        "actor_id": PROPOSER_ID,
        "ruleset_version": RULESET,
        "command_type": NPC_ATTENDANCE_COMMAND,
    }
    validate_relationship_invitation_batch([event], command_payload=payload, **scope)
    validate_relationship_invitation_batch(
        [event], command_payload=payload, require_work_calendar_checked=True, **scope,
    )
    assert apply_event(in_meeting, event).relationship_invitations == [attended]

    for forged_payload in (
        {"kind": "request_relationship_meeting_attendance", "invitation_id": str(uuid5(NPC_ATTENDANCE_COMMAND_ID, "other"))},
        {**payload, "extra": True},
        {"kind": "request_relationship_meeting_attendance"},
    ):
        with pytest.raises(InvalidEvent, match="does not match its command proposal"):
            validate_relationship_invitation_batch([event], command_payload=forged_payload, **scope)
    with pytest.raises(InvalidEvent, match="does not match its command proposal"):
        validate_relationship_invitation_batch(
            [event], command_type="action.attend_relationship_meeting", command_payload={
                "kind": "attend_relationship_meeting", "invitation_id": str(proposed.invitation_id),
            }, **{key: value for key, value in scope.items() if key != "command_type"},
        )
    with pytest.raises(InvalidEvent, match="requires its typed event"):
        validate_relationship_invitation_batch([], command_payload=payload, **scope)
