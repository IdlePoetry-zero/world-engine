from __future__ import annotations

from uuid import UUID

import pytest

from world_engine.domain.components import LifeComponent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.domain.relationship_reflection_events import (
    MeetingReflectionRecordedEvent,
    meeting_reflection_event_id,
    meeting_reflection_visibility,
)
from world_engine.domain.relationship_reflections import MeetingReflection
from world_engine.domain.relationship_invitations import (
    InvitationMeeting,
    InvitationMeetingAttendance,
    RelationshipInvitation,
)
from world_engine.rules import RuleContext, RuleEngine, parse_action


PLACE_ID = UUID("b1000000-0000-4000-8000-000000000001")
PROPOSER_ID = UUID("b2000000-0000-4000-8000-000000000001")
INVITEE_ID = UUID("b2000000-0000-4000-8000-000000000002")
OTHER_ID = UUID("b2000000-0000-4000-8000-000000000003")
INVITATION_ID = UUID("b3000000-0000-4000-8000-000000000001")


def _state(*, include_invitation: bool = True, reflection: MeetingReflection | None = None) -> WorldState:
    def character(entity_id: UUID, name: str) -> WorldEntity:
        return WorldEntity(
            id=entity_id,
            kind="character",
            name=name,
            location_id=PLACE_ID,
            components={
                "life": LifeComponent(birth_tick=-100, life_stage="adult").model_dump(mode="json"),
            },
        )

    invitation = RelationshipInvitation(
        invitation_id=INVITATION_ID,
        proposer_id=PROPOSER_ID,
        invitee_id=INVITEE_ID,
        status="accepted",
        proposed_tick=0,
        updated_tick=50,
        revision=2,
        meeting=InvitationMeeting(start_tick=100, end_tick=200),
        meeting_attendance=[
            InvitationMeetingAttendance(actor_id=PROPOSER_ID, attended_tick=150),
            InvitationMeetingAttendance(actor_id=INVITEE_ID, attended_tick=180),
        ],
    )
    return WorldState(
        current_tick=200,
        calendar={"day_seconds": 86_400, "year_days": 360},
        entities={
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Garden"),
            PROPOSER_ID: character(PROPOSER_ID, "Ari"),
            INVITEE_ID: character(INVITEE_ID, "Bea"),
            OTHER_ID: character(OTHER_ID, "Casey"),
        },
        player_id=PROPOSER_ID,
        relationship_invitations=[invitation] if include_invitation else [],
        relationship_reflections=[reflection] if reflection is not None else [],
    )


def _context(actor_id: UUID = PROPOSER_ID) -> RuleContext:
    return RuleContext(
        world_id=UUID("b4000000-0000-4000-8000-000000000001"),
        branch_id=UUID("b4000000-0000-4000-8000-000000000002"),
        command_id=UUID("b4000000-0000-4000-8000-000000000003"),
        actor_id=actor_id,
        ruleset_version="meeting-reflection-rules.v1",
    )


def _action(invitation_id: UUID = INVITATION_ID, outlook: str = "closer") -> dict[str, str]:
    return {
        "kind": "record_meeting_reflection",
        "invitation_id": str(invitation_id),
        "outlook": outlook,
    }


def test_reflection_intent_is_closed_and_limits_outlook_values() -> None:
    parsed = parse_action(_action(outlook="uneasy"))

    assert parsed.kind == "record_meeting_reflection"
    assert parsed.invitation_id == INVITATION_ID
    assert parsed.outlook == "uneasy"
    assert "actor_id" not in parsed.model_dump()

    with pytest.raises(ValueError, match="registered schema"):
        parse_action({**_action(), "outlook": "romantic"})
    with pytest.raises(ValueError, match="registered schema"):
        parse_action({**_action(), "actor_id": str(PROPOSER_ID)})


def test_record_meeting_reflection_emits_one_actor_private_event_without_time_advance() -> None:
    state = _state()
    context = _context()

    result = RuleEngine().evaluate(state, _action(outlook="uneasy"), context)

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "meeting_reflection_recorded"
    assert result.outcome.details == {}
    assert "uneasy" not in result.outcome.message
    assert result.elapsed_ticks == 0
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, MeetingReflectionRecordedEvent)
    assert event.event_id == meeting_reflection_event_id(
        context.command_id, context.ruleset_version, INVITATION_ID,
    )
    assert (event.world_id, event.branch_id, event.command_id, event.actor_id) == (
        context.world_id, context.branch_id, context.command_id, PROPOSER_ID,
    )
    assert event.ruleset_version == context.ruleset_version
    assert event.reflection == MeetingReflection(
        invitation_id=INVITATION_ID,
        actor_id=PROPOSER_ID,
        outlook="uneasy",
        recorded_tick=state.current_tick,
    )
    assert event.visibility == meeting_reflection_visibility(PROPOSER_ID)

    after = apply_event(state, event)
    assert after.current_tick == state.current_tick
    assert after.relationship_reflections == [event.reflection]


@pytest.mark.parametrize(
    ("state", "actor_id"),
    [
        (_state(include_invitation=False), PROPOSER_ID),
        (_state(), OTHER_ID),
        (
            _state(reflection=MeetingReflection(
                invitation_id=INVITATION_ID,
                actor_id=PROPOSER_ID,
                outlook="unchanged",
                recorded_tick=200,
            )),
            PROPOSER_ID,
        ),
    ],
)
def test_unavailable_reflection_returns_a_safe_rejection(state: WorldState, actor_id: UUID) -> None:
    result = RuleEngine().evaluate(state, _action(), _context(actor_id))

    assert result.outcome.status == "rejected"
    assert result.outcome.code == "meeting_reflection_unavailable"
    assert result.outcome.details == {}
    assert result.events == []
    assert result.elapsed_ticks == 0
