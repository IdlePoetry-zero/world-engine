from dataclasses import replace
from decimal import Decimal
import json
from uuid import UUID, uuid4

import pytest

from world_engine.application.npc_meeting_attendance_context import (
    project_npc_meeting_attendance_context,
)
from world_engine.domain.components import (
    GoalRecord,
    KnowledgeRecord,
    LifeComponent,
    PersonalityComponent,
    RelationRecord,
)
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.relationship_invitations import (
    InvitationMeeting,
    InvitationMeetingAttendance,
    RelationshipInvitation,
)
from world_engine.domain.travel_journeys import TravelJourney
from world_engine.simulation.npc_meeting_attendance import NpcMeetingAttendanceCandidate


CURRENT_TICK = 10_000
DAY_SECONDS = 10
YEAR_TICKS = DAY_SECONDS * 360
PLACE_ID = UUID("81000000-0000-4000-8000-000000000001")
OTHER_PLACE_ID = UUID("81000000-0000-4000-8000-000000000002")
PLAYER_ID = UUID("82000000-0000-4000-8000-000000000001")
NPC_ID = UUID("82000000-0000-4000-8000-000000000002")
OTHER_ID = UUID("82000000-0000-4000-8000-000000000003")
INVITATION_ID = UUID("83000000-0000-4000-8000-000000000001")


def _fixture():
    invitation = RelationshipInvitation(
        invitation_id=INVITATION_ID,
        proposer_id=PLAYER_ID,
        invitee_id=NPC_ID,
        status="accepted",
        proposed_tick=CURRENT_TICK - 30,
        updated_tick=CURRENT_TICK - 20,
        revision=2,
        meeting=InvitationMeeting(start_tick=CURRENT_TICK, end_tick=CURRENT_TICK + 20),
    )
    actor_to_counterpart = RelationRecord(
        id=uuid4(),
        from_id=NPC_ID,
        to_id=PLAYER_ID,
        dimensions={
            "familiarity": Decimal("3"),
            "trust": Decimal("8"),
            "private_dimension": Decimal("77"),
        },
        label="PRIVATE_RELATION_LABEL",
        updated_tick=CURRENT_TICK,
    )
    reverse_relation = RelationRecord(
        id=uuid4(),
        from_id=PLAYER_ID,
        to_id=NPC_ID,
        dimensions={"familiarity": Decimal("99"), "trust": Decimal("-40")},
        label="REVERSE_RELATION_SECRET",
        updated_tick=CURRENT_TICK,
    )
    state = WorldState(
        current_tick=CURRENT_TICK,
        player_id=PLAYER_ID,
        calendar={"day_seconds": DAY_SECONDS, "year_days": 360},
        entities={
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Private Place Name"),
            PLAYER_ID: WorldEntity(
                id=PLAYER_ID,
                kind="character",
                name="Counterpart Display Name",
                location_id=PLACE_ID,
                components={
                    "life": LifeComponent(
                        birth_tick=CURRENT_TICK - 25 * YEAR_TICKS,
                        life_stage="adult",
                    ).model_dump(mode="json"),
                    "personality": PersonalityComponent(traits=["COUNTERPART_SECRET_TRAIT"]).model_dump(mode="json"),
                    "public_role": "COUNTERPART_PRIVATE_ROLE",
                },
            ),
            NPC_ID: WorldEntity(
                id=NPC_ID,
                kind="character",
                name="Attending NPC",
                location_id=PLACE_ID,
                components={
                    "life": LifeComponent(
                        birth_tick=CURRENT_TICK - 22 * YEAR_TICKS,
                        life_stage="adult",
                    ).model_dump(mode="json"),
                    "personality": PersonalityComponent(
                        openness=Decimal("0.8"),
                        conscientiousness=Decimal("0.6"),
                        traits=["patient", "thoughtful"],
                        values=["honesty"],
                    ).model_dump(mode="json"),
                    "public_role": "NPC_PRIVATE_ROLE_MARKER",
                    "public_description": "NPC_UNRELATED_DESCRIPTION_MARKER",
                },
            ),
            OTHER_ID: WorldEntity(
                id=OTHER_ID,
                kind="character",
                name="Unrelated Person",
                location_id=PLACE_ID,
                components={
                    "life": LifeComponent(
                        birth_tick=CURRENT_TICK - 22 * YEAR_TICKS,
                        life_stage="adult",
                    ).model_dump(mode="json"),
                },
            ),
        },
        relations=[actor_to_counterpart, reverse_relation],
        knowledge=[KnowledgeRecord(
            id=uuid4(),
            holder_id=NPC_ID,
            subject_id=PLAYER_ID,
            kind="memory",
            claim="UNSEEN_PRIVATE_MEMORY_MARKER",
            received_tick=CURRENT_TICK - 1,
        )],
        goals=[GoalRecord(
            id=uuid4(),
            owner_id=NPC_ID,
            title="UNRELATED_PRIVATE_GOAL_MARKER",
            description="UNRELATED_GOAL_DESCRIPTION_MARKER",
            created_tick=CURRENT_TICK - 100,
            updated_tick=CURRENT_TICK - 10,
        )],
        relationship_invitations=[invitation],
    )
    candidate = NpcMeetingAttendanceCandidate(
        invitation_id=INVITATION_ID,
        actor_id=NPC_ID,
        counterpart_id=PLAYER_ID,
        expected_invitation_revision=invitation.revision,
        at_tick=CURRENT_TICK,
    )
    return state, candidate, invitation


def test_projection_contains_only_the_allowlisted_current_attendance_facts():
    state, candidate, invitation = _fixture()

    context = project_npc_meeting_attendance_context(state, candidate)

    assert set(context) == {
        "actor",
        "counterpart",
        "own_relationship_impression",
        "meeting_interval",
        "current_tick",
        "co_location_fact",
    }
    assert context["actor"] == {
        "name": "Attending NPC",
        "traits": ["patient", "thoughtful"],
        "values": ["honesty"],
    }
    assert context["counterpart"] == {"display_name": "Counterpart Display Name"}
    assert context["own_relationship_impression"] == {"familiarity": "3", "trust": "8"}
    assert context["meeting_interval"] == {
        "start_tick": CURRENT_TICK,
        "end_tick": CURRENT_TICK + 20,
        "start_inclusive": True,
        "end_exclusive": True,
    }
    assert context["current_tick"] == CURRENT_TICK
    assert context["co_location_fact"] == {
        "both_invitation_participants_are_currently_co_located": True,
        "validated_at_tick": CURRENT_TICK,
    }

    encoded = json.dumps(context, ensure_ascii=False)
    for secret in (
        "UNSEEN_PRIVATE_MEMORY_MARKER",
        "UNRELATED_PRIVATE_GOAL_MARKER",
        "UNRELATED_GOAL_DESCRIPTION_MARKER",
        "PRIVATE_RELATION_LABEL",
        "REVERSE_RELATION_SECRET",
        "NPC_PRIVATE_ROLE_MARKER",
        "NPC_UNRELATED_DESCRIPTION_MARKER",
        "COUNTERPART_PRIVATE_ROLE",
        "COUNTERPART_SECRET_TRAIT",
        "Private Place Name",
        str(INVITATION_ID),
        str(NPC_ID),
        str(PLAYER_ID),
    ):
        assert secret not in encoded

    assert state.relationship_invitations == [invitation]
    assert invitation.meeting_attendance == []


@pytest.mark.parametrize(
    "candidate_update",
    [
        {"at_tick": CURRENT_TICK - 1},
        {"expected_invitation_revision": 3},
        {"counterpart_id": OTHER_ID},
        {"actor_id": OTHER_ID},
        {"actor_id": PLAYER_ID, "counterpart_id": NPC_ID},
        {"expected_invitation_revision": True},
        {"at_tick": True},
    ],
)
def test_stale_or_tampered_candidates_fail_closed(candidate_update):
    state, candidate, _ = _fixture()
    assert project_npc_meeting_attendance_context(state, replace(candidate, **candidate_update)) == {}


def test_non_candidate_and_stale_world_tick_fail_closed():
    state, candidate, _ = _fixture()
    assert project_npc_meeting_attendance_context(state, object()) == {}
    advanced = state.model_copy(update={"current_tick": CURRENT_TICK + 1})
    assert project_npc_meeting_attendance_context(advanced, candidate) == {}


def test_nonaccepted_or_missing_meeting_fails_closed():
    state, candidate, invitation = _fixture()
    pending = invitation.model_copy(update={"status": "pending", "revision": 1, "updated_tick": invitation.proposed_tick})
    pending_state = state.model_copy(update={"relationship_invitations": [pending]})
    assert project_npc_meeting_attendance_context(pending_state, replace(candidate, expected_invitation_revision=1)) == {}

    unscheduled = invitation.model_copy(update={"meeting": None})
    unscheduled_state = state.model_copy(update={"relationship_invitations": [unscheduled]})
    assert project_npc_meeting_attendance_context(unscheduled_state, candidate) == {}


def test_half_open_interval_rejects_the_end_tick():
    state, candidate, _ = _fixture()
    end_tick = CURRENT_TICK + 20
    at_end = state.model_copy(update={"current_tick": end_tick})
    assert project_npc_meeting_attendance_context(at_end, replace(candidate, at_tick=end_tick)) == {}


def test_noncolocation_and_active_travel_fail_closed():
    state, candidate, _ = _fixture()
    place = WorldEntity(id=OTHER_PLACE_ID, kind="place", name="Other Place")
    moved_actor = state.entities[NPC_ID].model_copy(update={"location_id": OTHER_PLACE_ID})
    moved_state = state.model_copy(update={
        "entities": {**state.entities, OTHER_PLACE_ID: place, NPC_ID: moved_actor},
    })
    assert project_npc_meeting_attendance_context(moved_state, candidate) == {}

    journey = TravelJourney(
        journey_id=uuid4(),
        actor_id=NPC_ID,
        route_id=uuid4(),
        from_place_id=PLACE_ID,
        to_place_id=OTHER_PLACE_ID,
        departed_tick=CURRENT_TICK - 1,
        arrival_tick=CURRENT_TICK + 10,
    )
    traveling_state = state.model_copy(update={"travel_journeys": [journey]})
    assert project_npc_meeting_attendance_context(traveling_state, candidate) == {}


def test_missing_display_name_or_invalid_persona_fails_closed():
    state, candidate, _ = _fixture()
    unnamed = state.entities[PLAYER_ID].model_copy(update={"name": None})
    unnamed_state = state.model_copy(update={"entities": {**state.entities, PLAYER_ID: unnamed}})
    assert project_npc_meeting_attendance_context(unnamed_state, candidate) == {}

    malformed_actor = state.entities[NPC_ID].model_copy(update={
        "components": {**state.entities[NPC_ID].components, "personality": {"traits": [1]}},
    })
    malformed_state = state.model_copy(update={"entities": {**state.entities, NPC_ID: malformed_actor}})
    assert project_npc_meeting_attendance_context(malformed_state, candidate) == {}


def test_duplicate_same_directional_relations_are_omitted_as_ambiguous():
    state, candidate, _ = _fixture()
    duplicate = next(row for row in state.relations if row.from_id == NPC_ID and row.to_id == PLAYER_ID)
    duplicated_state = state.model_copy(update={"relations": [*state.relations, duplicate]})

    context = project_npc_meeting_attendance_context(duplicated_state, candidate)

    assert context["own_relationship_impression"] == {}
