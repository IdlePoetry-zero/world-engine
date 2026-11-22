from __future__ import annotations

from uuid import UUID

import pytest

from world_engine.domain.components import LifeComponent
from world_engine.domain.models import TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.domain.relationship_invitation_events import (
    RelationInvitationChangedEvent,
    invitation_event_id,
    participant_visibility,
)
from world_engine.domain.relationship_invitations import (
    InvitationMeeting,
    InvitationMeetingAttendance,
    RelationshipInvitation,
)
from world_engine.rules import RuleContext, RuleEngine, parse_action


PLACE_ID = UUID("a1000000-0000-4000-8000-000000000001")
OTHER_PLACE_ID = UUID("a1000000-0000-4000-8000-000000000002")
PROPOSER_ID = UUID("a2000000-0000-4000-8000-000000000001")
INVITEE_ID = UUID("a2000000-0000-4000-8000-000000000002")
OTHER_ID = UUID("a2000000-0000-4000-8000-000000000003")
INVITATION_ID = UUID("a3000000-0000-4000-8000-000000000001")
YEAR_TICKS = 86_400 * 360


def _state(
    *,
    tick: int = 150,
    status: str = "accepted",
    meeting: InvitationMeeting | None = InvitationMeeting(start_tick=100, end_tick=200),
    proposer_location: UUID = PLACE_ID,
    invitee_location: UUID = PLACE_ID,
    attendance: list[InvitationMeetingAttendance] | None = None,
    include_invitation: bool = True,
) -> WorldState:
    def character(entity_id: UUID, name: str, location_id: UUID) -> WorldEntity:
        return WorldEntity(
            id=entity_id,
            kind="character",
            name=name,
            location_id=location_id,
            components={
                "life": LifeComponent(birth_tick=-20 * YEAR_TICKS, life_stage="adult").model_dump(mode="json"),
            },
        )

    invitation = RelationshipInvitation(
        invitation_id=INVITATION_ID,
        proposer_id=PROPOSER_ID,
        invitee_id=INVITEE_ID,
        status=status,
        proposed_tick=0,
        updated_tick=50 if status == "accepted" else 0,
        revision=2 if status == "accepted" else 1,
        meeting=meeting,
        meeting_attendance=attendance or [],
    )
    return WorldState(
        current_tick=tick,
        calendar={"day_seconds": 86_400, "year_days": 360},
        entities={
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Garden"),
            OTHER_PLACE_ID: WorldEntity(id=OTHER_PLACE_ID, kind="place", name="Road"),
            PROPOSER_ID: character(PROPOSER_ID, "Ari", proposer_location),
            INVITEE_ID: character(INVITEE_ID, "Bea", invitee_location),
            OTHER_ID: character(OTHER_ID, "Casey", PLACE_ID),
        },
        player_id=PROPOSER_ID,
        relationship_invitations=[invitation] if include_invitation else [],
    )


def _context(actor_id: UUID = PROPOSER_ID) -> RuleContext:
    return RuleContext(
        world_id=UUID("a4000000-0000-4000-8000-000000000001"),
        branch_id=UUID("a4000000-0000-4000-8000-000000000002"),
        command_id=UUID("a4000000-0000-4000-8000-000000000003"),
        actor_id=actor_id,
        ruleset_version="attendance-rules.v1",
    )


def _action(invitation_id: UUID = INVITATION_ID) -> dict[str, str]:
    return {"kind": "attend_relationship_meeting", "invitation_id": str(invitation_id)}


def test_attendance_intent_is_closed_and_requires_one_invitation_id() -> None:
    parsed = parse_action(_action())
    assert parsed.kind == "attend_relationship_meeting"
    assert parsed.invitation_id == INVITATION_ID
    assert "actor_id" not in parsed.model_dump()

    with pytest.raises(ValueError, match="registered schema"):
        parse_action({**_action(), "unexpected": True})


def test_requested_attendance_intent_is_closed_and_requires_one_invitation_id() -> None:
    action = {
        "kind": "request_relationship_meeting_attendance",
        "invitation_id": str(INVITATION_ID),
    }
    parsed = parse_action(action)
    assert parsed.kind == "request_relationship_meeting_attendance"
    assert parsed.invitation_id == INVITATION_ID
    with pytest.raises(ValueError, match="registered schema"):
        parse_action({**action, "unexpected": True})


def test_meeting_attendance_returns_exact_private_event_without_advancing_time() -> None:
    state = _state()
    context = _context()

    result = RuleEngine().evaluate(state, _action(), context)

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "relationship_invitation_attend"
    assert result.elapsed_ticks == 0
    assert not any(isinstance(event, TimeAdvancedEvent) for event in result.events)
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, RelationInvitationChangedEvent)
    assert event.event_id == invitation_event_id(
        context.command_id, context.ruleset_version, "attend", INVITATION_ID,
    )
    assert (event.world_id, event.branch_id, event.command_id, event.actor_id) == (
        context.world_id, context.branch_id, context.command_id, PROPOSER_ID,
    )
    assert event.operation == "attend"
    assert event.expected_revision == 2
    assert event.visibility == participant_visibility(PROPOSER_ID, INVITEE_ID)

    after = apply_event(state, event)
    invitation = after.relationship_invitations[0]
    assert after.current_tick == state.current_tick
    assert invitation.revision == 3
    assert invitation.status == "accepted"
    assert [(row.actor_id, row.attended_tick) for row in invitation.meeting_attendance] == [(PROPOSER_ID, 150)]


@pytest.mark.parametrize(
    ("state", "actor_id"),
    [
        (_state(include_invitation=False), PROPOSER_ID),
        (_state(tick=200), PROPOSER_ID),
        (_state(status="pending"), PROPOSER_ID),
        (_state(meeting=None), PROPOSER_ID),
        (_state(invitee_location=OTHER_PLACE_ID), PROPOSER_ID),
        (_state(), OTHER_ID),
        (
            _state(attendance=[InvitationMeetingAttendance(actor_id=PROPOSER_ID, attended_tick=120)]),
            PROPOSER_ID,
        ),
    ],
)
def test_unavailable_attendance_uses_one_safe_rejection(state: WorldState, actor_id: UUID) -> None:
    result = RuleEngine().evaluate(state, _action(), _context(actor_id))

    assert result.outcome.status == "rejected"
    assert result.outcome.code == "relationship_meeting_attendance_unavailable"
    assert result.events == []
    assert result.elapsed_ticks == 0
    assert result.outcome.details == {}
