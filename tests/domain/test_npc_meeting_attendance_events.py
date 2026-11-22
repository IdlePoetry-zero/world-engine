from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import LifeComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import parse_event
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.npc_meeting_attendance import (
    NPC_MEETING_ATTENDANCE_COMMAND,
    NpcMeetingAttendanceDecision,
    NpcMeetingAttendanceDecisionRecordedEvent,
    build_npc_meeting_attendance_decision_events,
    npc_meeting_attendance_command_payload,
    npc_meeting_attendance_event_id,
    npc_meeting_attendance_request_id,
    npc_meeting_attendance_visibility,
    validate_npc_meeting_attendance_decision_batch,
)
from world_engine.domain.reducers import apply_event, state_hash, state_json
from world_engine.domain.relationship_invitation_events import (
    RelationInvitationChangedEvent,
    invitation_event_id,
    participant_visibility,
)
from world_engine.domain.relationship_invitations import (
    RelationshipInvitation,
    attend_relationship_meeting,
)


WORLD_ID = UUID("11000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("22000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("33000000-0000-4000-8000-000000000003")
ACTOR_ID = UUID("44000000-0000-4000-8000-000000000004")
COUNTERPART_ID = UUID("55000000-0000-4000-8000-000000000005")
PLACE_ID = UUID("66000000-0000-4000-8000-000000000006")
INVITATION_ID = UUID("77000000-0000-4000-8000-000000000007")
RULESET = "npc-meeting-test.v1"
CURRENT_TICK = 12
SOURCE_REVISION = 2


def _state(*, player_id: UUID | None = None) -> WorldState:
    year_ticks = 86_400 * 360
    return WorldState(
        current_tick=CURRENT_TICK,
        player_id=player_id,
        calendar={"day_seconds": 86_400, "year_days": 360},
        entities={
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Station"),
            ACTOR_ID: WorldEntity(
                id=ACTOR_ID,
                kind="character",
                name="Ari",
                location_id=PLACE_ID,
                components={"life": LifeComponent(
                    birth_tick=-25 * year_ticks,
                    life_stage="adult",
                ).model_dump(mode="json")},
            ),
            COUNTERPART_ID: WorldEntity(
                id=COUNTERPART_ID,
                kind="character",
                name="Bea",
                location_id=PLACE_ID,
                components={"life": LifeComponent(
                    birth_tick=-25 * year_ticks,
                    life_stage="adult",
                ).model_dump(mode="json")},
            ),
        },
        relationship_invitations=[RelationshipInvitation(
            invitation_id=INVITATION_ID,
            proposer_id=ACTOR_ID,
            invitee_id=COUNTERPART_ID,
            status="accepted",
            proposed_tick=0,
            updated_tick=10,
            revision=SOURCE_REVISION,
            meeting={"start_tick": CURRENT_TICK, "end_tick": 20},
        )],
    )


def _events(state: WorldState | None = None, *, choice: str = "defer", **overrides: object):
    state = state or _state()
    return build_npc_meeting_attendance_decision_events(
        state,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=ACTOR_ID,
        ruleset_version=RULESET,
        invitation_id=INVITATION_ID,
        source_invitation_revision=SOURCE_REVISION,
        choice=choice,  # type: ignore[arg-type]
        **overrides,
    )


def _validate_batch(events, state: WorldState | None = None, **overrides: object) -> None:
    marker = next(event for event in events if isinstance(event, NpcMeetingAttendanceDecisionRecordedEvent))
    validate_npc_meeting_attendance_decision_batch(
        events,
        pre_state=state or _state(),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=ACTOR_ID,
        ruleset_version=RULESET,
        command_type=NPC_MEETING_ATTENDANCE_COMMAND,
        command_payload=npc_meeting_attendance_command_payload(marker.record),
        **overrides,
    )


def test_empty_decision_state_is_omitted_from_legacy_serialization_and_hash():
    state = _state()
    explicit_empty = _state().model_copy(update={"npc_meeting_attendance_decisions": []})

    assert "npc_meeting_attendance_decisions" not in state_json(state)
    assert state_json(state) == state_json(explicit_empty)
    assert state_hash(state) == state_hash(explicit_empty)


@pytest.mark.parametrize("choice", ["attend", "defer"])
def test_registered_private_event_and_request_are_bound_to_exact_opportunity(choice):
    events = _events(choice=choice)
    marker = events[-1]
    assert isinstance(marker, NpcMeetingAttendanceDecisionRecordedEvent)
    assert marker.type == "core.npc_meeting_attendance_decision_recorded.v1"
    assert marker.actor_id == marker.record.actor_id == ACTOR_ID
    assert marker.record.invitation_id == INVITATION_ID
    assert marker.record.source_invitation_revision == SOURCE_REVISION
    assert marker.record.as_of_tick == CURRENT_TICK
    assert marker.record.request_id == npc_meeting_attendance_request_id(
        COMMAND_ID, INVITATION_ID, ACTOR_ID, SOURCE_REVISION, CURRENT_TICK,
    )
    assert marker.event_id == npc_meeting_attendance_event_id(
        COMMAND_ID, RULESET, INVITATION_ID, ACTOR_ID, SOURCE_REVISION, CURRENT_TICK,
    )
    assert marker.visibility == npc_meeting_attendance_visibility() == {"policy": "author_only"}
    assert parse_event(marker.model_dump(mode="json")) == marker
    _validate_batch(events)
    assert marker.record.linked_attendance_event_id == (events[0].event_id if choice == "attend" else None)
    assert len(events) == (2 if choice == "attend" else 1)


def test_attend_batch_is_canonical_invitation_event_followed_by_marker_and_reduces():
    state = _state()
    events = _events(state, choice="attend")
    attendance_event, marker = events
    assert isinstance(attendance_event, RelationInvitationChangedEvent)
    assert attendance_event.operation == "attend"
    assert attendance_event.expected_revision == SOURCE_REVISION
    assert attendance_event.work_calendar_checked is True
    assert attendance_event.requested_by is None
    assert attendance_event.resulting_invitation.revision == SOURCE_REVISION + 1
    assert attendance_event.resulting_invitation.meeting_attendance[0].attended_tick == CURRENT_TICK
    assert marker.record.linked_attendance_event_id == attendance_event.event_id

    for event in events:
        state = apply_event(state, event)

    assert len(state.npc_meeting_attendance_decisions) == 1
    assert state.npc_meeting_attendance_decisions[0].choice == "attend"
    assert state.relationship_invitations[0].meeting_attendance[0].actor_id == ACTOR_ID


def test_defer_records_once_without_calendar_mutation_and_later_attendance_remains_legal():
    state = _state()
    events = _events(state, choice="defer")
    state_after_defer = apply_event(state, events[0])

    assert state_after_defer.relationship_invitations == state.relationship_invitations
    assert state_after_defer.npc_meeting_attendance_decisions[0].choice == "defer"
    with pytest.raises(InvalidEvent, match="only once"):
        apply_event(state_after_defer, events[0])

    attended = attend_relationship_meeting(
        state_after_defer,
        state_after_defer.relationship_invitations,
        invitation_id=INVITATION_ID,
        actor_id=ACTOR_ID,
        expected_revision=SOURCE_REVISION,
        check_work_calendar=True,
    )
    later_command_id = uuid4()
    attendance_event = RelationInvitationChangedEvent(
        event_id=invitation_event_id(later_command_id, RULESET, "attend", INVITATION_ID),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=later_command_id,
        actor_id=ACTOR_ID,
        work_calendar_checked=True,
        ruleset_version=RULESET,
        operation="attend",
        expected_revision=SOURCE_REVISION,
        resulting_invitation=attended,
        visibility=participant_visibility(ACTOR_ID, COUNTERPART_ID),
    )
    after_later_attendance = apply_event(state_after_defer, attendance_event)
    assert after_later_attendance.relationship_invitations[0].meeting_attendance[0].actor_id == ACTOR_ID
    assert after_later_attendance.npc_meeting_attendance_decisions[0].choice == "defer"

    player_after_history = WorldState.model_validate({
        **state_after_defer.model_dump(mode="python"),
        "player_id": ACTOR_ID,
    })
    assert player_after_history.player_id == ACTOR_ID
    assert player_after_history.npc_meeting_attendance_decisions[0].choice == "defer"


def test_duplicate_settlement_and_bad_state_links_fail_closed():
    state = _state()
    events = _events(state, choice="attend")
    marker = events[-1]
    corrupted = marker.model_copy(update={
        "record": marker.record.model_copy(update={"linked_attendance_event_id": uuid4()}),
    })
    with pytest.raises(InvalidEvent, match="link"):
        _validate_batch([events[0], corrupted], state)

    wrong_request = marker.model_copy(update={
        "record": marker.record.model_copy(update={"request_id": uuid4()}),
    })
    with pytest.raises(InvalidEvent, match="schema validation"):
        _validate_batch([events[0], wrong_request], state)

    reversed_events = list(reversed(events))
    with pytest.raises(InvalidEvent, match="canonical invitation attendance"):
        _validate_batch(reversed_events, state)
    with pytest.raises(InvalidEvent, match="canonical invitation attendance"):
        _validate_batch([*events, object()], state)

    already_settled = state.model_copy(update={"npc_meeting_attendance_decisions": [marker.record]})
    with pytest.raises(InvalidEvent, match="only once"):
        apply_event(already_settled, marker)


def test_scope_player_actor_tick_and_choice_are_rejected():
    state = _state()
    events = _events(state, choice="defer")
    marker = events[0]

    with pytest.raises(InvalidEvent, match="trusted command scope"):
        validate_npc_meeting_attendance_decision_batch(
            events,
            pre_state=state,
            world_id=WORLD_ID,
            branch_id=uuid4(),
            command_id=COMMAND_ID,
            actor_id=ACTOR_ID,
            ruleset_version=RULESET,
        )

    player_state = _state(player_id=ACTOR_ID)
    with pytest.raises(InvalidEvent, match="exact current accepted meeting opportunity"):
        _validate_batch(events, player_state)
    with pytest.raises(InvalidEvent, match="active participant"):
        apply_event(player_state, marker)

    later_tick_state = state.model_copy(update={"current_tick": CURRENT_TICK + 1})
    with pytest.raises(InvalidEvent, match="exact current world tick"):
        apply_event(later_tick_state, marker)

    raw = marker.model_dump(mode="python")
    raw["record"] = {**raw["record"], "choice": "attend_silently"}
    with pytest.raises(ValidationError):
        NpcMeetingAttendanceDecisionRecordedEvent.model_validate(raw)


def test_choice_requires_linked_attendance_only_for_attend_and_defer_batch_is_standalone():
    raw = {
        "invitation_id": INVITATION_ID,
        "actor_id": ACTOR_ID,
        "choice": "attend",
        "source_invitation_revision": SOURCE_REVISION,
        "as_of_tick": CURRENT_TICK,
        "request_id": npc_meeting_attendance_request_id(
            COMMAND_ID, INVITATION_ID, ACTOR_ID, SOURCE_REVISION, CURRENT_TICK,
        ),
    }
    with pytest.raises(ValidationError, match="if and only if"):
        NpcMeetingAttendanceDecision.model_validate(raw)
    with pytest.raises(InvalidEvent, match="must not change its invitation calendar"):
        _validate_batch([*_events(choice="defer"), object()])
