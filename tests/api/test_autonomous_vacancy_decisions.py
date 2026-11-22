from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest

from world_engine.adapters.models import DepthProfile, RoleTask, SpeedProfile
from world_engine.application.autonomous_vacancy_decisions import invoke_autonomous_vacancy_vote
from world_engine.application.model_decisions import InvalidModelDecisionOutput
from world_engine.domain.autonomous_vacancy_events import (
    LeadershipVacancyReviewRecord,
    leadership_vacancy_review_ballot_id,
)
from world_engine.domain.components import GovernanceComponent, OfficeRecord, PersonalityComponent
from world_engine.domain.leadership_ballots import LeadershipBallot, LeadershipBallotVote
from world_engine.domain.models import AccessScope, CalendarConfig, WorldEntity, WorldState
from world_engine.simulation.contracts import DecisionOption, DecisionRequest, DecisionResponse
from world_engine.simulation.engine import StaleDecisionResponse


WORLD_ID = UUID("e1000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("e2000000-0000-4000-8000-000000000001")
PLAYER_ID = UUID("e3000000-0000-4000-8000-000000000001")
PROPOSER_ID = UUID("e4000000-0000-4000-8000-000000000001")
NOMINEE_ID = UUID("e4000000-0000-4000-8000-000000000002")
VOTER_ID = UUID("e4000000-0000-4000-8000-000000000003")
PRIOR_VOTER_ID = UUID("e4000000-0000-4000-8000-000000000004")
ORGANIZATION_ID = UUID("e5000000-0000-4000-8000-000000000001")
TICK = 86_400
STATE_VERSION = 12


class FakeModels:
    def __init__(self, choice="abstain"):
        self.choice = choice
        self.calls = []

    async def generate(self, scope, request):
        self.calls.append((scope, request))
        return SimpleNamespace(normalized_output={"choice": self.choice})


class FakeRepository:
    def __init__(self, state, *, state_version=STATE_VERSION, current_tick=TICK, authorization_error=None):
        self.view = SimpleNamespace(
            state=state,
            branch=SimpleNamespace(state_version=state_version, current_tick=current_tick),
            world=SimpleNamespace(ruleset_version="1.0.0"),
        )
        self.authorization_error = authorization_error
        self.authorized = []

    def read_world(self, scope, world_id, branch_id):
        assert world_id == WORLD_ID
        assert branch_id == BRANCH_ID
        return self.view

    def authorize_hosted_npc(self, scope, world_id, actor_id):
        self.authorized.append((scope, world_id, actor_id))
        if self.authorization_error is not None:
            raise self.authorization_error


def _fixture(*, tracked=True):
    request_id = uuid4()
    review_request_id = uuid4()
    ballot_id = leadership_vacancy_review_ballot_id(review_request_id)
    governance = GovernanceComponent(
        revision=7,
        members=[PROPOSER_ID, NOMINEE_ID, VOTER_ID, PRIOR_VOTER_ID],
        succession_policy="election",
        decision_threshold=Decimal("0.5"),
        charter="Active members decide by vote.",
        offices=[OfficeRecord(office_id="office.leader", title="Leader", holder_id=None)],
    )
    state = WorldState(
        current_tick=TICK,
        calendar=CalendarConfig(year_days=1),
        player_id=PLAYER_ID,
        entities={
            PLAYER_ID: WorldEntity(id=PLAYER_ID, kind="character", name="Player"),
            PROPOSER_ID: WorldEntity(id=PROPOSER_ID, kind="character", name="Proposer"),
            NOMINEE_ID: WorldEntity(id=NOMINEE_ID, kind="character", name="Nominee"),
            VOTER_ID: WorldEntity(
                id=VOTER_ID,
                kind="character",
                name="Voter",
                components={"personality": PersonalityComponent(traits=["careful"]).model_dump(mode="json")},
            ),
            PRIOR_VOTER_ID: WorldEntity(id=PRIOR_VOTER_ID, kind="character", name="Other Voter"),
            ORGANIZATION_ID: WorldEntity(
                id=ORGANIZATION_ID,
                kind="organization",
                name="Council",
                components={"governance": governance.model_dump(mode="json")},
            ),
        },
        leadership_ballots=[LeadershipBallot(
            ballot_id=ballot_id,
            organization_id=ORGANIZATION_ID,
            incumbent_id=None,
            vacancy_proposer_id=PROPOSER_ID,
            nominee_id=NOMINEE_ID,
            opened_tick=TICK,
            updated_tick=TICK,
            governance_revision=governance.revision,
            policy="election",
            eligible_voter_ids=tuple(sorted((PROPOSER_ID, NOMINEE_ID, VOTER_ID, PRIOR_VOTER_ID), key=str)),
            threshold=Decimal("0.5"),
            votes=(LeadershipBallotVote(voter_id=PRIOR_VOTER_ID, choice="yes"),),
        )],
        leadership_vacancy_reviews=(
            {ORGANIZATION_ID: LeadershipVacancyReviewRecord(
                organization_id=ORGANIZATION_ID,
                period_index=1,
                expected_governance_revision=governance.revision,
                proposer_id=PROPOSER_ID,
                decision_request_id=review_request_id,
                ballot_id=ballot_id,
                reviewed_tick=TICK,
            )} if tracked else {}
        ),
    )
    decision = DecisionRequest(
        request_id=request_id,
        kind="organization.vacancy_vote",
        actor_id=VOTER_ID,
        at_tick=TICK,
        state_version=STATE_VERSION,
        title="Council: independent vote",
        reason_code="vacancy_ballot_vote_due",
        options=(
            DecisionOption(option_id="yes", label="Vote in favor"),
            DecisionOption(option_id="no", label="Vote against"),
            DecisionOption(option_id="abstain", label="Abstain from the vote"),
        ),
        evidence_ids=(ORGANIZATION_ID, ballot_id),
    )
    lease = SimpleNamespace(job_id=uuid4(), world_id=WORLD_ID, branch_id=BRANCH_ID)
    job = SimpleNamespace(
        job_id=lease.job_id,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        payload={"speed": "patient", "depth": "exhaustive"},
    )
    return state, decision, lease, job, request_id, ballot_id


@pytest.mark.asyncio
async def test_vacancy_vote_dispatch_is_snapshot_bound_private_and_narrow():
    state, decision, lease, job, request_id, ballot_id = _fixture()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    models = FakeModels("abstain")
    repository = FakeRepository(state)

    response = await invoke_autonomous_vacancy_vote(
        models, repository, scope, lease, job, decision,
    )

    assert isinstance(response, DecisionResponse)
    assert response == DecisionResponse(
        request_id=request_id,
        state_version=STATE_VERSION,
        at_tick=TICK,
        source="model",
        option_id="abstain",
    )
    assert repository.authorized == [(scope, WORLD_ID, VOTER_ID)]
    assert len(models.calls) == 1
    observed_scope, model_request = models.calls[0]
    assert observed_scope == scope
    assert model_request.role_task is RoleTask.NPC_DECISION
    assert model_request.output_schema["properties"]["choice"]["enum"] == ["yes", "no", "abstain"]
    assert model_request.profile.speed is SpeedProfile.PATIENT
    assert model_request.profile.depth is DepthProfile.EXHAUSTIVE
    assert model_request.trace_context.parent_trace == str(request_id)
    assert model_request.trace_context.world_id == str(WORLD_ID)
    assert model_request.trace_context.branch_id == str(BRANCH_ID)
    assert model_request.trace_context.job_id == str(lease.job_id)
    prompt = "\n".join(message.content for message in model_request.messages)
    context = json.loads(model_request.messages[1].content)
    assert context["ballot"]["ballot_id"] == str(ballot_id)
    assert "votes" not in context["ballot"]
    assert "eligible_voter_ids" not in context["ballot"]
    assert str(PRIOR_VOTER_ID) not in prompt
    assert state.leadership_ballots[0].votes[0].voter_id == PRIOR_VOTER_ID


@pytest.mark.asyncio
async def test_vacancy_vote_dispatch_rejects_untracked_ballots_stale_snapshots_and_user_control():
    state, decision, lease, job, *_ = _fixture(tracked=False)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    models = FakeModels()
    repository = FakeRepository(state)
    with pytest.raises(InvalidModelDecisionOutput, match="tracked"):
        await invoke_autonomous_vacancy_vote(models, repository, scope, lease, job, decision)
    assert not models.calls

    state, decision, lease, job, *_ = _fixture()
    repository = FakeRepository(state, state_version=STATE_VERSION + 1)
    with pytest.raises(StaleDecisionResponse):
        await invoke_autonomous_vacancy_vote(models, repository, scope, lease, job, decision)
    assert not models.calls

    repository = FakeRepository(state, authorization_error=PermissionError("controlled actor"))
    with pytest.raises(PermissionError, match="controlled actor"):
        await invoke_autonomous_vacancy_vote(models, repository, scope, lease, job, decision)
    assert not models.calls


@pytest.mark.asyncio
async def test_vacancy_vote_dispatch_rejects_wrong_actor_kind_and_choices():
    state, decision, lease, job, *_ = _fixture()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    models = FakeModels()
    repository = FakeRepository(state)

    with pytest.raises(InvalidModelDecisionOutput, match="required bounded choices"):
        await invoke_autonomous_vacancy_vote(
            models, repository, scope, lease, job,
            decision.model_copy(update={"options": (*decision.options, DecisionOption(option_id="defer", label="defer"))}),
        )

    with pytest.raises(InvalidModelDecisionOutput, match="eligible autonomous"):
        await invoke_autonomous_vacancy_vote(
            models, repository, scope, lease, job,
            decision.model_copy(update={"actor_id": PLAYER_ID}),
        )
    assert not models.calls


@pytest.mark.asyncio
async def test_vacancy_vote_dispatch_never_converts_invalid_model_choice_to_a_vote():
    state, decision, lease, job, *_ = _fixture()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    models = FakeModels("defer")
    with pytest.raises(ValueError, match="outside the allowed choices"):
        await invoke_autonomous_vacancy_vote(
            models, FakeRepository(state), scope, lease, job, decision,
        )
