from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from world_engine.domain.components import LifeComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.relationship_invitations import (
    InvitationMeeting,
    RelationshipInvitation,
    accept_relationship_invitation,
    attend_relationship_meeting,
    end_relationship_invitation,
    propose_relationship_invitation,
)
from world_engine.domain.relationship_reflections import MeetingReflection, plan_meeting_reflection


PLACE = UUID("c1000000-0000-4000-8000-000000000001")
ALICE = UUID("c2000000-0000-4000-8000-000000000001")
BOB = UUID("c2000000-0000-4000-8000-000000000002")
CASEY = UUID("c2000000-0000-4000-8000-000000000003")
INVITATION = UUID("c3000000-0000-4000-8000-000000000001")
DAY_SECONDS = 10
YEAR_DAYS = 360
MEETING_START = 10
MEETING_END = 20


def _character(actor_id: UUID, name: str, *, status: str = "active") -> WorldEntity:
    return WorldEntity(
        id=actor_id,
        kind="character",
        name=name,
        location_id=PLACE,
        status=status,
        components={
            "life": LifeComponent(
                birth_tick=-20 * DAY_SECONDS * YEAR_DAYS,
                life_stage="adult",
            ).model_dump(mode="json"),
        },
    )


def _base_state(*, tick: int = 0, alice_status: str = "active") -> WorldState:
    return WorldState(
        current_tick=tick,
        entities={
            PLACE: WorldEntity(id=PLACE, kind="place", name="Garden"),
            ALICE: _character(ALICE, "Alice", status=alice_status),
            BOB: _character(BOB, "Bob"),
            CASEY: _character(CASEY, "Casey"),
        },
        calendar={"day_seconds": DAY_SECONDS, "year_days": YEAR_DAYS},
    )


def _at_tick(state: WorldState, tick: int) -> WorldState:
    return WorldState.model_validate({**state.model_dump(mode="python"), "current_tick": tick})


def _accepted_invitation(*, meeting: InvitationMeeting | None = InvitationMeeting(
    start_tick=MEETING_START,
    end_tick=MEETING_END,
)) -> tuple[WorldState, RelationshipInvitation]:
    state = _base_state()
    invitation = propose_relationship_invitation(
        state,
        [],
        invitation_id=INVITATION,
        proposer_id=ALICE,
        invitee_id=BOB,
        meeting=meeting,
    )
    accepted = accept_relationship_invitation(
        state,
        [invitation],
        invitation_id=INVITATION,
        actor_id=BOB,
        expected_revision=1,
    )
    return state, accepted


def _completed_meeting(*, tick: int = MEETING_END, alice_status: str = "active") -> WorldState:
    state, invitation = _accepted_invitation()
    alice_attendance = attend_relationship_meeting(
        _at_tick(state, MEETING_START),
        [invitation],
        invitation_id=INVITATION,
        actor_id=ALICE,
        expected_revision=2,
    )
    bob_attendance = attend_relationship_meeting(
        _at_tick(state, MEETING_START + 1),
        [alice_attendance],
        invitation_id=INVITATION,
        actor_id=BOB,
        expected_revision=3,
    )
    payload = state.model_dump(mode="python")
    payload["current_tick"] = tick
    payload["relationship_invitations"] = [bob_attendance]
    if alice_status != "active":
        payload["entities"][ALICE]["status"] = alice_status
    return WorldState.model_validate(payload)


def test_plan_returns_only_the_actor_private_outlook_at_the_current_tick():
    state = _completed_meeting(tick=25)
    before = state.model_dump(mode="python")

    reflection = plan_meeting_reflection(
        state,
        invitation_id=INVITATION,
        actor_id=ALICE,
        outlook="closer",
    )

    assert reflection == MeetingReflection(
        invitation_id=INVITATION,
        actor_id=ALICE,
        outlook="closer",
        recorded_tick=25,
    )
    assert state.model_dump(mode="python") == before


def test_each_participant_may_record_one_separate_reflection():
    state = _completed_meeting()
    first = plan_meeting_reflection(
        state,
        invitation_id=INVITATION,
        actor_id=ALICE,
        outlook="uneasy",
    )
    payload = state.model_dump(mode="python")
    payload["relationship_reflections"] = [first]
    reflected_state = WorldState.model_validate(payload)

    second = plan_meeting_reflection(
        reflected_state,
        invitation_id=INVITATION,
        actor_id=BOB,
        outlook="unchanged",
    )

    assert second.actor_id == BOB
    assert second.outlook == "unchanged"


def test_a_participant_cannot_record_a_second_reflection_for_the_invitation():
    state = _completed_meeting()
    first = plan_meeting_reflection(
        state,
        invitation_id=INVITATION,
        actor_id=ALICE,
        outlook="closer",
    )
    payload = state.model_dump(mode="python")
    payload["relationship_reflections"] = [first]
    reflected_state = WorldState.model_validate(payload)

    with pytest.raises(InvalidEvent, match="already recorded"):
        plan_meeting_reflection(
            reflected_state,
            invitation_id=INVITATION,
            actor_id=ALICE,
            outlook="uneasy",
        )


def test_a_recorded_reflection_remains_historical_after_the_invitation_ends():
    state = _completed_meeting()
    reflection = plan_meeting_reflection(
        state,
        invitation_id=INVITATION,
        actor_id=ALICE,
        outlook="unchanged",
    )
    payload = state.model_dump(mode="python")
    payload["relationship_reflections"] = [reflection]
    with_reflection = WorldState.model_validate(payload)

    ended = end_relationship_invitation(
        with_reflection,
        with_reflection.relationship_invitations,
        invitation_id=INVITATION,
        actor_id=ALICE,
        expected_revision=4,
    )
    ended_payload = with_reflection.model_dump(mode="python")
    ended_payload["relationship_invitations"] = [ended]
    historical_state = WorldState.model_validate(ended_payload)

    assert historical_state.relationship_reflections == [reflection]


def test_meeting_reflection_requires_world_time_at_or_after_meeting_end():
    state = _completed_meeting(tick=MEETING_END - 1)

    with pytest.raises(InvalidEvent, match="at or after the meeting ends"):
        plan_meeting_reflection(
            state,
            invitation_id=INVITATION,
            actor_id=ALICE,
            outlook="closer",
        )


def test_meeting_reflection_requires_both_attendance_records():
    state, invitation = _accepted_invitation()
    payload = state.model_dump(mode="python")
    payload["current_tick"] = MEETING_END
    payload["relationship_invitations"] = [invitation]
    state = WorldState.model_validate(payload)

    with pytest.raises(InvalidEvent, match="Both invitation participants"):
        plan_meeting_reflection(
            state,
            invitation_id=INVITATION,
            actor_id=ALICE,
            outlook="closer",
        )


def test_meeting_reflection_requires_a_still_accepted_timed_invitation():
    state, invitation = _accepted_invitation(meeting=None)
    payload = state.model_dump(mode="python")
    payload["current_tick"] = MEETING_END
    payload["relationship_invitations"] = [invitation]
    untimed = WorldState.model_validate(payload)
    with pytest.raises(InvalidEvent, match="timed invitation"):
        plan_meeting_reflection(
            untimed,
            invitation_id=INVITATION,
            actor_id=ALICE,
            outlook="closer",
        )

    completed = _completed_meeting()
    ended = end_relationship_invitation(
        completed,
        completed.relationship_invitations,
        invitation_id=INVITATION,
        actor_id=ALICE,
        expected_revision=4,
    )
    ended_payload = completed.model_dump(mode="python")
    ended_payload["relationship_invitations"] = [ended]
    ended_state = WorldState.model_validate(ended_payload)
    with pytest.raises(InvalidEvent, match="still-accepted"):
        plan_meeting_reflection(
            ended_state,
            invitation_id=INVITATION,
            actor_id=ALICE,
            outlook="closer",
        )


def test_meeting_reflection_requires_a_participant_and_both_alive():
    state = _completed_meeting()
    with pytest.raises(InvalidEvent, match="Only an invitation participant"):
        plan_meeting_reflection(
            state,
            invitation_id=INVITATION,
            actor_id=CASEY,
            outlook="closer",
        )

    dead_participant_state = _completed_meeting(alice_status="dead")
    with pytest.raises(InvalidEvent, match="currently be active characters"):
        plan_meeting_reflection(
            dead_participant_state,
            invitation_id=INVITATION,
            actor_id=BOB,
            outlook="closer",
        )


def test_meeting_reflection_rejects_an_unknown_invitation():
    state = _completed_meeting()
    with pytest.raises(InvalidEvent, match="does not exist exactly once"):
        plan_meeting_reflection(
            state,
            invitation_id=UUID("c3000000-0000-4000-8000-000000000099"),
            actor_id=ALICE,
            outlook="closer",
        )


def test_meeting_reflection_schema_is_frozen_strict_and_bounded():
    values = {
        "invitation_id": INVITATION,
        "actor_id": ALICE,
        "outlook": "closer",
        "recorded_tick": 0,
    }
    reflection = MeetingReflection.model_validate(values)
    serialized = reflection.model_dump(mode="json")
    assert MeetingReflection.model_validate(serialized) == reflection
    with pytest.raises(ValidationError):
        reflection.recorded_tick = 1
    with pytest.raises(ValidationError):
        MeetingReflection.model_validate({**values, "recorded_tick": True})
    with pytest.raises(ValidationError):
        MeetingReflection.model_validate({**values, "recorded_tick": 2**63})
    with pytest.raises(ValidationError):
        MeetingReflection.model_validate({**values, "outlook": "happy"})
    with pytest.raises(ValidationError):
        MeetingReflection.model_validate({**values, "extra": "no"})
