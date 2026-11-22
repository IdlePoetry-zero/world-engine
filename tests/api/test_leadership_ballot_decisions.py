from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from world_engine.adapters.models import DepthProfile, RoleTask, SpeedProfile
from world_engine.api.contracts import MessageRequest, QualityProfile
from world_engine.application.leadership_ballot_decisions import (
    LEADERSHIP_VOTE_SCHEMA,
    InvalidLeadershipVoteDecision,
    choose_leadership_vote,
)
from world_engine.domain.components import (
    GovernanceComponent,
    KnowledgeRecord,
    OfficeRecord,
    PersonalityComponent,
    RelationRecord,
)
from world_engine.domain.leadership_ballots import (
    cast_leadership_ballot_vote,
    open_leadership_ballot,
)
from world_engine.domain.models import AccessScope, WorldEntity, WorldState
from world_engine.domain.reducers import state_json


WORLD_ID = UUID("e1000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("e2000000-0000-4000-8000-000000000001")
PLACE_ID = UUID("e3000000-0000-4000-8000-000000000001")
PLAYER_ID = UUID("e4000000-0000-4000-8000-000000000001")
INCUMBENT_ID = UUID("e4000000-0000-4000-8000-000000000002")
NOMINEE_ID = UUID("e4000000-0000-4000-8000-000000000003")
VOTER_ID = UUID("e4000000-0000-4000-8000-000000000004")
PRIOR_VOTER_ID = UUID("e4000000-0000-4000-8000-000000000005")
ORGANIZATION_ID = UUID("e5000000-0000-4000-8000-000000000001")


class FakeModels:
    def __init__(self, normalized_output=None, error=None):
        self.normalized_output = normalized_output
        self.error = error
        self.calls = []

    async def generate(self, scope, request):
        self.calls.append((scope, request))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(normalized_output=self.normalized_output)


def _fixture():
    governance = GovernanceComponent(
        revision=7,
        members=[INCUMBENT_ID, NOMINEE_ID, VOTER_ID, PRIOR_VOTER_ID],
        leader_id=INCUMBENT_ID,
        succession_policy="election",
        decision_threshold=Decimal("0.5"),
        charter="Only active members vote; the named nominee takes the leader's office if approved.",
        offices=[OfficeRecord(office_id="office.leader", title="Leader", holder_id=INCUMBENT_ID)],
    )
    state = WorldState(
        current_tick=18_000,
        player_id=PLAYER_ID,
        entities={
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Council Hall"),
            PLAYER_ID: WorldEntity(
                id=PLAYER_ID, kind="character", name="Player",
                components={"personality": PersonalityComponent(traits=["PLAYER_PRIVATE_TRAIT"]).model_dump(mode="json")},
            ),
            INCUMBENT_ID: WorldEntity(
                id=INCUMBENT_ID, kind="character", name="Incumbent",
                location_id=PLACE_ID,
                components={
                    "personality": PersonalityComponent(traits=["INCUMBENT_PRIVATE_TRAIT"]).model_dump(mode="json"),
                    "public_role": "Current steward",
                    "public_description": "Has managed the district council.",
                },
            ),
            NOMINEE_ID: WorldEntity(
                id=NOMINEE_ID, kind="character", name="Nominee",
                location_id=PLACE_ID,
                components={
                    "personality": PersonalityComponent(traits=["NOMINEE_PRIVATE_TRAIT"]).model_dump(mode="json"),
                    "public_role": "Harbor mediator",
                    "public_description": "Coordinates agreements among the harbor wards.",
                },
            ),
            VOTER_ID: WorldEntity(
                id=VOTER_ID, kind="character", name="Mira",
                location_id=PLACE_ID,
                components={"personality": PersonalityComponent(
                    openness=Decimal("0.7"), conscientiousness=Decimal("0.8"),
                    traits=["careful"], values=["fairness"],
                ).model_dump(mode="json")},
            ),
            PRIOR_VOTER_ID: WorldEntity(
                id=PRIOR_VOTER_ID, kind="character", name="Prior Voter",
                location_id=PLACE_ID,
                components={"personality": PersonalityComponent(traits=["PRIOR_VOTER_SECRET"]).model_dump(mode="json")},
            ),
            ORGANIZATION_ID: WorldEntity(
                id=ORGANIZATION_ID,
                kind="organization",
                name="Harbor Council",
                components={
                    "governance": governance.model_dump(mode="json"),
                    "public_role": "Civic council",
                    "public_description": "Coordinates shared harbor services.",
                },
            ),
        },
        relations=[
            RelationRecord(
                id=uuid4(), from_id=VOTER_ID, to_id=NOMINEE_ID,
                dimensions={
                    "familiarity": Decimal("5"), "trust": Decimal("12"),
                    "private_dimension": Decimal("77"),
                },
                label="VOTER_RELATION_LABEL_SECRET", updated_tick=18_000,
            ),
            RelationRecord(
                id=uuid4(), from_id=NOMINEE_ID, to_id=VOTER_ID,
                dimensions={"trust": Decimal("-41"), "familiarity": Decimal("99")},
                label="REVERSE_RELATION_SECRET", updated_tick=18_000,
            ),
            RelationRecord(
                id=uuid4(), from_id=VOTER_ID, to_id=INCUMBENT_ID,
                dimensions={"trust": Decimal("-22")}, label="UNRELATED_RELATION_SECRET", updated_tick=18_000,
            ),
        ],
        knowledge=[
            KnowledgeRecord(
                id=uuid4(), holder_id=VOTER_ID, subject_id=NOMINEE_ID,
                kind="memory", claim="VOTER_MEMORY_SECRET", received_tick=18_000,
            ),
            KnowledgeRecord(
                id=uuid4(), holder_id=PLAYER_ID, subject_id=NOMINEE_ID,
                kind="memory", claim="PLAYER_PRIVATE_MEMORY", received_tick=18_000,
            ),
        ],
    )
    ballot = open_leadership_ballot(
        state, ballot_id=uuid4(), organization_id=ORGANIZATION_ID,
        nominee_id=NOMINEE_ID, actor_id=INCUMBENT_ID, existing_ballots=(),
    )
    ballot = cast_leadership_ballot_vote(
        state, ballot, voter_id=PRIOR_VOTER_ID, choice="yes", expected_ballot_revision=1,
    )
    state = WorldState.model_validate({
        **state.model_dump(mode="json"),
        "leadership_ballots": [ballot.model_dump(mode="json")],
    })
    return state, ballot


def _request():
    return MessageRequest(
        request_id=uuid4(),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        session_id=uuid4(),
        actor_id=INCUMBENT_ID,
        expected_state_version="9",
        text="PLAYER_REQUEST_TEXT_MUST_NOT_ENTER_NPC_PROMPT",
        profile=QualityProfile(speed="patient", depth="exhaustive"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["yes", "no", "abstain", "defer"])
async def test_vote_decision_has_closed_npc_schema_and_directional_allowlisted_context(choice):
    state, ballot = _fixture()
    before = state_json(state)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    request = _request()
    models = FakeModels({"choice": choice})

    result = await choose_leadership_vote(models, scope, request, state, ballot, VOTER_ID)

    assert result == choice
    assert len(models.calls) == 1
    observed_scope, model_request = models.calls[0]
    assert observed_scope == scope
    assert model_request.role_task is RoleTask.NPC_DECISION
    assert model_request.output_schema == LEADERSHIP_VOTE_SCHEMA
    assert model_request.output_schema["properties"]["choice"]["enum"] == [
        "yes", "no", "abstain", "defer",
    ]
    assert model_request.profile.speed is SpeedProfile.PATIENT
    assert model_request.profile.depth is DepthProfile.EXHAUSTIVE
    assert model_request.trace_context.parent_trace == str(request.request_id)
    assert model_request.trace_context.world_id == str(WORLD_ID)
    assert model_request.trace_context.branch_id == str(BRANCH_ID)

    assert len(model_request.messages) == 2
    context = json.loads(model_request.messages[1].content)
    assert set(context) == {
        "voter", "eligibility", "organization_public", "nominee_public",
        "voter_to_nominee_relation", "ballot", "current_tick",
    }
    assert context["voter"]["name"] == "Mira"
    assert context["voter"]["personality"]["traits"] == ["careful"]
    assert context["voter"]["personality"]["values"] == ["fairness"]
    assert context["eligibility"] == {"voter_id": str(VOTER_ID), "eligible": True, "already_voted": False}
    assert context["organization_public"] == {
        "name": "Harbor Council",
        "public_role": "Civic council",
        "public_description": "Coordinates shared harbor services.",
        "charter": "Only active members vote; the named nominee takes the leader's office if approved.",
    }
    assert context["nominee_public"] == {
        "name": "Nominee",
        "public_role": "Harbor mediator",
        "public_description": "Coordinates agreements among the harbor wards.",
    }
    assert context["voter_to_nominee_relation"] == {"familiarity": "5", "trust": "12"}
    assert context["ballot"] == {
        "ballot_id": str(ballot.ballot_id),
        "organization_id": str(ORGANIZATION_ID),
        "incumbent_id": str(INCUMBENT_ID),
        "nominee_id": str(NOMINEE_ID),
        "opened_tick": ballot.opened_tick,
        "updated_tick": ballot.updated_tick,
        "governance_revision": ballot.governance_revision,
        "policy": "election",
        "threshold": "0.5",
        "revision": ballot.revision,
        "status": "open",
    }
    assert "votes" not in context["ballot"]
    assert "eligible_voter_ids" not in context["ballot"]

    prompt = "\n".join(message.content for message in model_request.messages)
    for forbidden in (
        "PLAYER_REQUEST_TEXT_MUST_NOT_ENTER_NPC_PROMPT",
        "VOTER_MEMORY_SECRET",
        "PLAYER_PRIVATE_MEMORY",
        "VOTER_RELATION_LABEL_SECRET",
        "REVERSE_RELATION_SECRET",
        "UNRELATED_RELATION_SECRET",
        "NOMINEE_PRIVATE_TRAIT",
        "INCUMBENT_PRIVATE_TRAIT",
        "PLAYER_PRIVATE_TRAIT",
        "PRIOR_VOTER_SECRET",
        str(PRIOR_VOTER_ID),
        "private_dimension",
        "explanation",
    ):
        assert forbidden not in prompt
    assert state_json(state) == before


@pytest.mark.asyncio
async def test_stale_or_unfit_ballot_voter_defers_without_model_call():
    state, ballot = _fixture()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    request = _request()

    stale = ballot.model_copy(update={"revision": ballot.revision + 1})
    models = FakeModels({"choice": "yes"})
    assert await choose_leadership_vote(models, scope, request, state, stale, VOTER_ID) == "defer"
    assert not models.calls

    assert await choose_leadership_vote(models, scope, request, state, ballot, PLAYER_ID) == "defer"
    assert not models.calls

    assert await choose_leadership_vote(models, scope, request, state, ballot, PRIOR_VOTER_ID) == "defer"
    assert not models.calls

    invalid = ballot.model_copy(update={"revision": 0})
    assert await choose_leadership_vote(models, scope, request, state, invalid, VOTER_ID) == "defer"
    assert not models.calls


@pytest.mark.asyncio
async def test_malformed_model_output_defers_and_provider_failure_propagates():
    state, ballot = _fixture()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    request = _request()
    malformed = FakeModels({"choice": "yes", "explanation": "I know private facts."})
    assert await choose_leadership_vote(malformed, scope, request, state, ballot, VOTER_ID) == "defer"

    invalid_choice = FakeModels({"choice": "maybe"})
    assert await choose_leadership_vote(invalid_choice, scope, request, state, ballot, VOTER_ID) == "defer"

    failure = RuntimeError("provider unavailable")
    broken_provider = FakeModels(error=failure)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await choose_leadership_vote(broken_provider, scope, request, state, ballot, VOTER_ID)


@pytest.mark.asyncio
async def test_no_defer_vote_mode_restricts_schema_and_rejects_invalid_output():
    state, ballot = _fixture()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    request = _request()

    valid = FakeModels({"choice": "abstain"})
    assert await choose_leadership_vote(
        valid, scope, request, state, ballot, VOTER_ID, allow_defer=False,
    ) == "abstain"
    assert valid.calls[0][1].output_schema["properties"]["choice"]["enum"] == [
        "yes", "no", "abstain",
    ]
    assert "defer" not in valid.calls[0][1].messages[0].content

    for output in ({"choice": "defer"}, {"choice": "maybe"}, {"choice": "yes", "reason": "secret"}, None):
        malformed = FakeModels(output)
        with pytest.raises(InvalidLeadershipVoteDecision):
            await choose_leadership_vote(
                malformed, scope, request, state, ballot, VOTER_ID, allow_defer=False,
            )

