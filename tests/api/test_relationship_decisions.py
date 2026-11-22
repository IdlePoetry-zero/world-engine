import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from world_engine.adapters.models import DepthProfile, RoleTask, SpeedProfile
from world_engine.api.contracts import MessageRequest, QualityProfile
from world_engine.application.relationship_decisions import (
    INVITATION_RESPONSE_SCHEMA,
    choose_invitation_response,
    choose_partnership_response,
)
from world_engine.domain.components import (
    KnowledgeRecord,
    LifeComponent,
    PersonalityComponent,
    ProvenanceReference,
    RelationRecord,
)
from world_engine.domain.models import AccessScope, WorldEntity, WorldState
from world_engine.domain.promise_trust import PROMISE_TRUST_POLICY_ID, promise_trust_relation_id
from world_engine.domain.promise_trust_state import PromiseTrustBond, PromiseTrustProjection
from world_engine.domain.relationship_invitations import RelationshipInvitation
from world_engine.domain.relationship_partnerships import RelationshipPartnership


WORLD_ID = UUID("d1000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("d2000000-0000-4000-8000-000000000001")
PLACE_ID = UUID("d3000000-0000-4000-8000-000000000001")
PROPOSER_ID = UUID("d4000000-0000-4000-8000-000000000001")
INVITEE_ID = UUID("d4000000-0000-4000-8000-000000000002")
OTHER_ID = UUID("d4000000-0000-4000-8000-000000000003")
INVITATION_ID = UUID("d5000000-0000-4000-8000-000000000001")
OTHER_INVITATION_ID = UUID("d5000000-0000-4000-8000-000000000002")
YEAR_TICKS = 86_400 * 360
CURRENT_TICK = YEAR_TICKS * 20


class FakeModels:
    def __init__(self, normalized_output):
        self.normalized_output = normalized_output
        self.calls = []

    async def generate(self, scope, request):
        self.calls.append((scope, request))
        return SimpleNamespace(normalized_output=self.normalized_output)


def _world(*, include_invitation=True, player_id=PROPOSER_ID):
    invitation = RelationshipInvitation(
        invitation_id=INVITATION_ID,
        proposer_id=PROPOSER_ID,
        invitee_id=INVITEE_ID,
        status="pending",
        proposed_tick=CURRENT_TICK - 20,
        updated_tick=CURRENT_TICK - 20,
        revision=1,
    )
    other_invitation = RelationshipInvitation(
        invitation_id=OTHER_INVITATION_ID,
        proposer_id=INVITEE_ID,
        invitee_id=OTHER_ID,
        status="pending",
        proposed_tick=CURRENT_TICK - 10,
        updated_tick=CURRENT_TICK - 10,
        revision=1,
    )
    invitee_to_proposer = RelationRecord(
        id=uuid4(),
        from_id=INVITEE_ID,
        to_id=PROPOSER_ID,
        dimensions={"familiarity": Decimal("3"), "trust": Decimal("8"), "private_dimension": Decimal("77")},
        label="PRIVATE_RELATION_LABEL",
        updated_tick=CURRENT_TICK,
    )
    proposer_to_invitee = RelationRecord(
        id=uuid4(),
        from_id=PROPOSER_ID,
        to_id=INVITEE_ID,
        dimensions={"familiarity": Decimal("99"), "trust": Decimal("-40")},
        label="REVERSE_RELATION_SECRET",
        updated_tick=CURRENT_TICK,
    )
    bond_id = promise_trust_relation_id(WORLD_ID, INVITEE_ID, PROPOSER_ID)
    reverse_bond_id = promise_trust_relation_id(WORLD_ID, PROPOSER_ID, INVITEE_ID)
    projection = PromiseTrustProjection(
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        bonds={
            bond_id: PromiseTrustBond(
                id=bond_id,
                world_id=WORLD_ID,
                from_id=INVITEE_ID,
                to_id=PROPOSER_ID,
                revision=1,
                trust=Decimal("17"),
                policy_id=PROMISE_TRUST_POLICY_ID,
                success_budget_spent=Decimal("0"),
            ),
            reverse_bond_id: PromiseTrustBond(
                id=reverse_bond_id,
                world_id=WORLD_ID,
                from_id=PROPOSER_ID,
                to_id=INVITEE_ID,
                revision=1,
                trust=Decimal("-29"),
                policy_id=PROMISE_TRUST_POLICY_ID,
                success_budget_spent=Decimal("0"),
            ),
        },
    )
    state = WorldState(
        current_tick=CURRENT_TICK,
        player_id=player_id,
        calendar={"day_seconds": 86_400, "year_days": 360},
        metadata={"promise_trust_policy_id": PROMISE_TRUST_POLICY_ID},
        entities={
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Atrium"),
            PROPOSER_ID: WorldEntity(
                id=PROPOSER_ID,
                kind="character",
                name="Morgan",
                location_id=PLACE_ID,
                components={
                    "life": LifeComponent(
                        birth_tick=CURRENT_TICK - 30 * YEAR_TICKS,
                        life_stage="adult",
                    ).model_dump(mode="json"),
                    "personality": PersonalityComponent(
                        traits=["PROPOSER_PRIVATE_PERSONALITY"],
                    ).model_dump(mode="json"),
                    "public_role": "Night dispatcher",
                    "public_description": "Coordinates late service at the station.",
                },
            ),
            INVITEE_ID: WorldEntity(
                id=INVITEE_ID,
                kind="character",
                name="Rae",
                location_id=PLACE_ID,
                components={
                    "life": LifeComponent(
                        birth_tick=CURRENT_TICK - 28 * YEAR_TICKS,
                        life_stage="adult",
                    ).model_dump(mode="json"),
                    "personality": PersonalityComponent(
                        openness=Decimal("0.8"),
                        conscientiousness=Decimal("0.6"),
                        traits=["patient"],
                        values=["honesty"],
                    ).model_dump(mode="json"),
                },
            ),
            OTHER_ID: WorldEntity(
                id=OTHER_ID,
                kind="character",
                name="Kai",
                location_id=PLACE_ID,
                components={
                    "life": LifeComponent(
                        birth_tick=CURRENT_TICK - 29 * YEAR_TICKS,
                        life_stage="adult",
                    ).model_dump(mode="json"),
                },
            ),
        },
        relations=[invitee_to_proposer, proposer_to_invitee],
        knowledge=[KnowledgeRecord(
            id=uuid4(),
            holder_id=INVITEE_ID,
            subject_id=PROPOSER_ID,
            kind="memory",
            claim="PRIVATE_MEMORY_MARKER: the proposer once failed an unrelated promise.",
            received_tick=CURRENT_TICK,
        )],
        relationship_invitations=[invitation, other_invitation] if include_invitation else [other_invitation],
        promise_trust=projection.to_payload(),
    )
    return state, invitation


def _request():
    return MessageRequest(
        request_id=uuid4(),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        session_id=uuid4(),
        actor_id=PROPOSER_ID,
        expected_state_version="2",
        text="PLAYER_REQUEST_TEXT_MUST_NOT_ENTER_NPC_PROMPT",
        profile=QualityProfile(speed="patient", depth="exhaustive"),
    )


@pytest.mark.asyncio
async def test_partnership_decision_includes_only_the_recipient_recent_dialogue_with_proposer():
    state, _ = _world()
    observed = KnowledgeRecord(
        id=uuid4(), holder_id=INVITEE_ID, subject_id=PROPOSER_ID, kind="memory",
        claim="We talked about seeing other people.", received_tick=CURRENT_TICK,
        provenance=[ProvenanceReference(kind="observation", reference_id="previous-dialogue")],
    )
    blocked = [
        {"holder_id": OTHER_ID},
        {"subject_id": OTHER_ID},
        {"owner_id": OTHER_ID},
        {"visibility": {"policy": "author_only"}},
        {"visibility": {"policy": "subject_ids", "subject_ids": [OTHER_ID]}},
        {"received_tick": CURRENT_TICK + 1},
        {"provenance": []},
    ]
    state.knowledge.extend([observed, *[
        KnowledgeRecord.model_validate(observed.model_dump() | properties | {"id": uuid4(), "claim": f"BLOCKED_{index}"})
        for index, properties in enumerate(blocked)
    ]])
    partnership = RelationshipPartnership(
        partnership_id=uuid4(), proposer_id=PROPOSER_ID, partner_id=INVITEE_ID, revision=1,
        proposed_tick=CURRENT_TICK, updated_tick=CURRENT_TICK, proposal_terms=("We may both date other people.",),
    )
    state.relationship_partnerships.append(partnership)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    models = FakeModels({"choice": "defer"})
    assert await choose_partnership_response(models, scope, _request(), state, partnership, can_accept=True) == "defer"
    message = models.calls[0][1].messages[1].content
    context = json.loads(message)
    assert context["own_dialogue_memories"] == [{"tick": CURRENT_TICK, "utterance": observed.claim}]
    assert context["proposal_terms"] == list(partnership.proposal_terms)
    assert "BLOCKED_" not in message and "PLAYER_REQUEST_TEXT_MUST_NOT_ENTER_NPC_PROMPT" not in message


@pytest.mark.asyncio
async def test_invitation_decision_uses_closed_npc_decision_schema_and_directional_safe_context():
    state, invitation = _world()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    models = FakeModels({"choice": "accept"})
    request = _request()

    choice = await choose_invitation_response(models, scope, request, state, invitation)

    assert choice == "accept"
    assert len(models.calls) == 1
    observed_scope, model_request = models.calls[0]
    assert observed_scope == scope
    assert model_request.role_task is RoleTask.NPC_DECISION
    assert model_request.output_schema == INVITATION_RESPONSE_SCHEMA
    assert model_request.profile.speed is SpeedProfile.PATIENT
    assert model_request.profile.depth is DepthProfile.EXHAUSTIVE
    assert model_request.trace_context.parent_trace == str(request.request_id)
    assert model_request.trace_context.world_id == str(WORLD_ID)
    assert model_request.trace_context.branch_id == str(BRANCH_ID)

    context = json.loads(model_request.messages[1].content)
    assert context["invitee"]["name"] == "Rae"
    assert context["invitee"]["personality"]["traits"] == ["patient"]
    assert context["proposer_public"] == {
        "name": "Morgan",
        "public_role": "Night dispatcher",
        "public_description": "Coordinates late service at the station.",
    }
    assert context["invitation"] == invitation.model_dump(mode="json")
    assert context["invitee_to_proposer_relation"] == {"familiarity": "3", "trust": "8"}
    assert context["invitee_to_proposer_reliability_impression"] == "你觉得对方过往更守约。"

    prompt = "\n".join(message.content for message in model_request.messages)
    for forbidden in (
        "PLAYER_REQUEST_TEXT_MUST_NOT_ENTER_NPC_PROMPT",
        "PRIVATE_MEMORY_MARKER",
        "PRIVATE_RELATION_LABEL",
        "REVERSE_RELATION_SECRET",
        "PROPOSER_PRIVATE_PERSONALITY",
        "private_dimension",
        str(OTHER_INVITATION_ID),
        "-29",
        "17",
    ):
        assert forbidden not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["accept", "decline", "defer"])
async def test_all_three_closed_choices_are_returned_without_state_mutation(choice):
    state, invitation = _world()
    before = state.model_dump(mode="json")
    models = FakeModels({"choice": choice})

    result = await choose_invitation_response(models, AccessScope(tenant_id=uuid4(), user_id=uuid4()), _request(), state, invitation)

    assert result == choice
    assert state.model_dump(mode="json") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [None, "accept", {}, {"choice": "unknown"}, {"choice": "accept", "reason": "because"}])
async def test_malformed_or_explanatory_model_output_fails_closed_to_defer(output):
    state, invitation = _world()
    models = FakeModels(output)

    result = await choose_invitation_response(models, AccessScope(tenant_id=uuid4(), user_id=uuid4()), _request(), state, invitation)

    assert result == "defer"


@pytest.mark.asyncio
async def test_stale_nonpending_or_player_invitee_is_deferred_without_model_call():
    state, invitation = _world(include_invitation=False)
    models = FakeModels({"choice": "accept"})
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())

    assert await choose_invitation_response(models, scope, _request(), state, invitation) == "defer"
    assert models.calls == []

    state, invitation = _world(player_id=INVITEE_ID)
    assert await choose_invitation_response(models, scope, _request(), state, invitation) == "defer"
    assert models.calls == []

    accepted = invitation.model_copy(update={"status": "accepted", "revision": 2})
    accepted_state = state.model_copy(update={"relationship_invitations": [accepted]})
    assert await choose_invitation_response(models, scope, _request(), accepted_state, accepted) == "defer"
    assert models.calls == []
