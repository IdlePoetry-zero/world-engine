from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from world_engine.adapters.models import RoleTask
from world_engine.api.contracts import MessageRequest, QualityProfile
from world_engine.application.meeting_reflection_decisions import (
    MEETING_REFLECTION_SCHEMA,
    choose_meeting_reflection,
)
from world_engine.domain.components import (
    KnowledgeRecord,
    LifeComponent,
    PersonalityComponent,
    ProvenanceReference,
    RelationRecord,
)
from world_engine.domain.models import AccessScope, WorldEntity, WorldState
from world_engine.domain.relationship_invitations import (
    InvitationMeeting,
    InvitationMeetingAttendance,
    RelationshipInvitation,
)
from world_engine.domain.relationship_reflections import MeetingReflection, MeetingOutlook


WORLD_ID = UUID("e1000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("e2000000-0000-4000-8000-000000000001")
PLACE_ID = UUID("e3000000-0000-4000-8000-000000000001")
PLAYER_ID = UUID("e4000000-0000-4000-8000-000000000001")
NPC_ID = UUID("e4000000-0000-4000-8000-000000000002")
OTHER_ID = UUID("e4000000-0000-4000-8000-000000000003")
INVITATION_ID = UUID("e5000000-0000-4000-8000-000000000001")
YEAR_TICKS = 86_400 * 360
MEETING_START = 200
MEETING_END = 240
CURRENT_TICK = 250

PLAYER_REQUEST_MARKER = "CURRENT_PLAYER_REQUEST_MUST_NOT_ENTER_NPC_PROMPT"
PRIOR_DIALOGUE_MARKER = "PRIOR_IN_WORLD_WORDS_HEARD_BY_NPC"
OTHER_ACTOR_MEMORY_MARKER = "OTHER_ACTOR_PRIVATE_MEMORY_MUST_NOT_ENTER"
UNRELATED_MEMORY_MARKER = "UNRELATED_PRIVATE_FACT_MUST_NOT_ENTER"
UNPROVEN_MEMORY_MARKER = "MEMORY_WITHOUT_OBSERVATION_MUST_NOT_ENTER"
REVERSE_RELATION_MARKER = "REVERSE_RELATION_PRIVATE_LABEL_MUST_NOT_ENTER"


class FakeModels:
    def __init__(self, normalized_output):
        self.normalized_output = normalized_output
        self.calls = []

    async def generate(self, scope, request):
        self.calls.append((scope, request))
        return SimpleNamespace(normalized_output=self.normalized_output)


def _character(actor_id: UUID, name: str, *, personality=None) -> WorldEntity:
    components = {
        "life": LifeComponent(
            birth_tick=CURRENT_TICK - 30 * YEAR_TICKS,
            life_stage="adult",
        ).model_dump(mode="json"),
    }
    if personality is not None:
        components["personality"] = personality.model_dump(mode="json")
    return WorldEntity(
        id=actor_id,
        kind="character",
        name=name,
        location_id=PLACE_ID,
        components=components,
    )


def _invitation(
    *,
    status: str = "accepted",
    attendance: list[InvitationMeetingAttendance] | None = None,
) -> RelationshipInvitation:
    return RelationshipInvitation(
        invitation_id=INVITATION_ID,
        proposer_id=PLAYER_ID,
        invitee_id=NPC_ID,
        status=status,
        proposed_tick=100,
        updated_tick=120 if status == "accepted" else CURRENT_TICK,
        revision=4 if status == "accepted" else 5,
        meeting=InvitationMeeting(start_tick=MEETING_START, end_tick=MEETING_END),
        meeting_attendance=attendance if attendance is not None else [
            InvitationMeetingAttendance(actor_id=PLAYER_ID, attended_tick=205),
            InvitationMeetingAttendance(actor_id=NPC_ID, attended_tick=215),
        ],
    )


def _dialogue_records() -> list[KnowledgeRecord]:
    observation = [ProvenanceReference(kind="observation", reference_id="dialogue-event-1")]
    return [
        KnowledgeRecord(
            id=UUID("e6000000-0000-4000-8000-000000000001"),
            holder_id=NPC_ID,
            subject_id=PLAYER_ID,
            kind="memory",
            claim=PRIOR_DIALOGUE_MARKER,
            received_tick=216,
            provenance=observation,
        ),
        KnowledgeRecord(
            id=UUID("e6000000-0000-4000-8000-000000000002"),
            holder_id=PLAYER_ID,
            subject_id=NPC_ID,
            kind="memory",
            claim=OTHER_ACTOR_MEMORY_MARKER,
            received_tick=217,
            provenance=observation,
        ),
        KnowledgeRecord(
            id=UUID("e6000000-0000-4000-8000-000000000003"),
            holder_id=NPC_ID,
            subject_id=OTHER_ID,
            kind="memory",
            claim=UNRELATED_MEMORY_MARKER,
            received_tick=218,
            provenance=observation,
        ),
        KnowledgeRecord(
            id=UUID("e6000000-0000-4000-8000-000000000004"),
            holder_id=NPC_ID,
            subject_id=PLAYER_ID,
            kind="memory",
            claim=UNPROVEN_MEMORY_MARKER,
            received_tick=219,
            provenance=[ProvenanceReference(kind="genesis", reference_id="authored-seed")],
        ),
        KnowledgeRecord(
            id=UUID("e6000000-0000-4000-8000-000000000005"),
            holder_id=NPC_ID,
            subject_id=PLAYER_ID,
            kind="fact",
            claim="FACT_KIND_NOT_DIALOGUE_MEMORY",
            received_tick=220,
            provenance=observation,
        ),
    ]


def _world(
    *,
    invitation: RelationshipInvitation | None = None,
    include_invitation: bool = True,
    player_id: UUID = PLAYER_ID,
    relationship_reflections: list[MeetingReflection] | None = None,
    knowledge: list[KnowledgeRecord] | None = None,
) -> tuple[WorldState, RelationshipInvitation]:
    invitation = invitation or _invitation()
    npc_relation = RelationRecord(
        id=UUID("e7000000-0000-4000-8000-000000000001"),
        from_id=NPC_ID,
        to_id=PLAYER_ID,
        dimensions={"familiarity": Decimal("3"), "trust": Decimal("8"), "private_dimension": Decimal("77")},
        label="NPC_OWN_RELATION_LABEL_SHOULD_NOT_BE_SENT",
        updated_tick=CURRENT_TICK,
    )
    reverse_relation = RelationRecord(
        id=UUID("e7000000-0000-4000-8000-000000000002"),
        from_id=PLAYER_ID,
        to_id=NPC_ID,
        dimensions={"familiarity": Decimal("99"), "trust": Decimal("-40")},
        label=REVERSE_RELATION_MARKER,
        updated_tick=CURRENT_TICK,
    )
    player_reflection = MeetingReflection(
        invitation_id=INVITATION_ID,
        actor_id=PLAYER_ID,
        outlook="uneasy",
        recorded_tick=CURRENT_TICK,
    )
    state = WorldState(
        current_tick=CURRENT_TICK,
        player_id=player_id,
        calendar={"day_seconds": 86_400, "year_days": 360},
        entities={
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Garden"),
            PLAYER_ID: _character(PLAYER_ID, "Morgan"),
            NPC_ID: _character(
                NPC_ID,
                "Rae",
                personality=PersonalityComponent(
                    openness=Decimal("0.8"),
                    conscientiousness=Decimal("0.6"),
                    traits=["patient"],
                    values=["honesty"],
                ),
            ),
            OTHER_ID: _character(OTHER_ID, "Kai"),
        },
        relations=[npc_relation, reverse_relation],
        knowledge=_dialogue_records() if knowledge is None else knowledge,
        relationship_invitations=[invitation] if include_invitation else [],
        relationship_reflections=(
            [player_reflection]
            if relationship_reflections is None and include_invitation
            else relationship_reflections or []
        ),
    )
    return state, invitation


def _request(text: str = PLAYER_REQUEST_MARKER) -> MessageRequest:
    return MessageRequest(
        request_id=uuid4(),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        session_id=uuid4(),
        actor_id=PLAYER_ID,
        expected_state_version="7",
        text=text,
        profile=QualityProfile(speed="balanced", depth="deep"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["closer", "unchanged", "uneasy"])
async def test_meeting_reflection_uses_npc_owned_context_and_a_closed_choice_schema(choice):
    state, invitation = _world()
    before = state.model_dump(mode="json")
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    request = _request()
    models = FakeModels({"choice": choice})

    result = await choose_meeting_reflection(models, scope, request, state, invitation, NPC_ID)

    assert result == choice
    assert len(models.calls) == 1
    observed_scope, model_request = models.calls[0]
    assert observed_scope == scope
    assert model_request.role_task is RoleTask.NPC_DECISION
    assert model_request.output_schema == MEETING_REFLECTION_SCHEMA
    assert model_request.trace_context.parent_trace == str(request.request_id)
    assert model_request.trace_context.world_id == str(WORLD_ID)
    assert model_request.trace_context.branch_id == str(BRANCH_ID)

    context = json.loads(model_request.messages[1].content)
    assert set(context) == {
        "npc", "counterpart", "npc_to_counterpart_relation", "completed_meeting", "own_dialogue_memories",
    }
    assert context["npc"]["name"] == "Rae"
    assert context["npc"]["personality"]["traits"] == ["patient"]
    assert context["npc"]["personality"]["values"] == ["honesty"]
    assert context["counterpart"] == {"name": "Morgan"}
    assert context["npc_to_counterpart_relation"] == {"familiarity": "3", "trust": "8"}
    assert context["completed_meeting"] == {
        "start_tick": MEETING_START,
        "end_tick": MEETING_END,
        "npc_attended_tick": 215,
        "counterpart_attended_tick": 205,
        "current_tick": CURRENT_TICK,
    }
    assert context["own_dialogue_memories"] == [{
        "tick": 216,
        "utterance": PRIOR_DIALOGUE_MARKER,
    }]

    prompt = "\n".join(message.content for message in model_request.messages)
    for forbidden in (
        PLAYER_REQUEST_MARKER,
        OTHER_ACTOR_MEMORY_MARKER,
        UNRELATED_MEMORY_MARKER,
        UNPROVEN_MEMORY_MARKER,
        REVERSE_RELATION_MARKER,
        "private_dimension",
        "NPC_OWN_RELATION_LABEL_SHOULD_NOT_BE_SENT",
        "own_meeting_reflections",
        "relationship_reflections",
    ):
        assert forbidden not in prompt
    assert state.model_dump(mode="json") == before


@pytest.mark.asyncio
async def test_defer_and_malformed_output_return_none_without_mutation():
    state, invitation = _world()
    before = state.model_dump(mode="json")
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())

    for output in (
        {"choice": "defer"},
        None,
        "closer",
        {},
        {"choice": "unknown"},
        {"choice": "closer", "reason": "unbounded explanation"},
    ):
        models = FakeModels(output)
        result = await choose_meeting_reflection(models, scope, _request(), state, invitation, NPC_ID)
        assert result is None

    assert state.model_dump(mode="json") == before


@pytest.mark.asyncio
async def test_dialogue_memory_context_is_provenance_filtered_count_bounded_and_length_bounded():
    memories = [
        KnowledgeRecord(
            id=uuid4(),
            holder_id=NPC_ID,
            subject_id=PLAYER_ID,
            kind="memory",
            claim=f"{index}:" + ("x" * 700),
            received_tick=220 + index,
            provenance=[ProvenanceReference(kind="observation", reference_id=f"dialogue-{index}")],
        )
        for index in range(10)
    ]
    state, invitation = _world(knowledge=memories)
    models = FakeModels({"choice": "unchanged"})

    result = await choose_meeting_reflection(
        models,
        AccessScope(tenant_id=uuid4(), user_id=uuid4()),
        _request(),
        state,
        invitation,
        NPC_ID,
    )

    assert result == "unchanged"
    context = json.loads(models.calls[0][1].messages[1].content)
    rows = context["own_dialogue_memories"]
    assert len(rows) == 8
    assert [row["tick"] for row in rows] == list(range(222, 230))
    assert all(len(row["utterance"]) == 500 for row in rows)


@pytest.mark.asyncio
async def test_invalid_or_already_reflected_meeting_fails_closed_without_model_call():
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    models = FakeModels({"choice": "closer"})
    valid_invitation = _invitation()

    missing_state, _ = _world(include_invitation=False)
    assert await choose_meeting_reflection(
        models, scope, _request(), missing_state, valid_invitation, NPC_ID,
    ) is None

    changed_copy = valid_invitation.model_copy(update={"revision": valid_invitation.revision + 1})
    state, _ = _world()
    assert await choose_meeting_reflection(models, scope, _request(), state, changed_copy, NPC_ID) is None

    partial = _invitation(attendance=[InvitationMeetingAttendance(actor_id=PLAYER_ID, attended_tick=205)])
    partial_state, _ = _world(invitation=partial, relationship_reflections=[])
    assert await choose_meeting_reflection(models, scope, _request(), partial_state, partial, NPC_ID) is None

    ended = _invitation(status="ended")
    ended_state, _ = _world(invitation=ended)
    assert await choose_meeting_reflection(models, scope, _request(), ended_state, ended, NPC_ID) is None

    player_controlled_state, _ = _world(player_id=NPC_ID)
    assert await choose_meeting_reflection(models, scope, _request(), player_controlled_state, valid_invitation, NPC_ID) is None

    npc_reflection = MeetingReflection(
        invitation_id=INVITATION_ID,
        actor_id=NPC_ID,
        outlook="closer",
        recorded_tick=CURRENT_TICK,
    )
    reflected_state, _ = _world(relationship_reflections=[npc_reflection])
    assert await choose_meeting_reflection(models, scope, _request(), reflected_state, valid_invitation, NPC_ID) is None

    assert models.calls == []
