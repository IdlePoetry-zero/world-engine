from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import pytest
from jsonschema import Draft202012Validator

from world_engine.adapters.models import RoleTask
from world_engine.api.contracts import MessageRequest
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.conversation import ConversationApplication
from world_engine.domain.components import RelationRecord
from world_engine.domain.errors import AccessDenied
from world_engine.domain.gameplay_events import (
    AccusationRepairOfferRecordedEvent,
    AccusationRepairResolvedEvent,
    AccusationRepairTrustProof,
    RelationDimensionChangedEvent,
)
from world_engine.domain.investigation_cases import (
    AccusationRepairOffer,
    CaseEvidenceRequirement,
    InvestigationCaseDefinition,
    InvestigationCaseFile,
    InvestigationCaseRoute,
    PublicAccusationCorrection,
    case_definition_digest,
)
from world_engine.domain.models import AccessScope, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.rules import RuleContext, RuleEngine
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER


RULESET = "1.0.0"


def _fixture(*, player_id: UUID | None = None, include_offer: bool = True):
    world_id, branch_id = uuid4(), uuid4()
    place_id, accuser_id, suspect_id, anchor_id = (uuid4() for _ in range(4))
    case_id, case_file_id = uuid4(), uuid4()
    closure_event_id, correction_event_id, offer_id = (uuid4() for _ in range(3))
    requirement = CaseEvidenceRequirement(
        subject_id=suspect_id,
        predicate="entity_status",
        effective_tick=0,
        finding="corroborated",
        observation_provenance=[{"kind": "pack", "reference_id": "fixture.observation"}],
        claim_provenance=[{"kind": "pack", "reference_id": "fixture.claim"}],
    )
    definition = InvestigationCaseDefinition(
        case_id=case_id,
        title="The Bell Tower Report",
        brief="A private investigation record.",
        routes=[
            InvestigationCaseRoute(
                route_id="supports-a", suspect_id=suspect_id, finding="supports",
                requirements=[requirement],
            ),
            InvestigationCaseRoute(
                route_id="rules-out-a", suspect_id=suspect_id, finding="rules_out",
                requirements=[requirement.model_copy(update={"effective_tick": 1})],
            ),
        ],
    )
    case_file = InvestigationCaseFile(
        id=case_file_id,
        case_id=case_id,
        anchor_id=anchor_id,
        owner_id=accuser_id,
        definition_hash=case_definition_digest(definition),
        title=definition.title,
        brief=definition.brief,
        status="closed",
        revision=2,
        opened_tick=0,
        updated_tick=4,
        closed_tick=4,
        outcome="disconfirmed",
        suspect_id=suspect_id,
        public_accusation=True,
    )
    correction = PublicAccusationCorrection(
        event_id=correction_event_id,
        case_file_id=case_file_id,
        expected_revision=case_file.revision,
        source_closure_event_id=closure_event_id,
        actor_id=accuser_id,
        suspect_id=suspect_id,
        corrected_tick=8,
        location_id=place_id,
    )
    offer = AccusationRepairOffer(
        offer_id=offer_id,
        revision=1,
        correction_event_id=correction_event_id,
        source_closure_event_id=closure_event_id,
        case_file_id=case_file_id,
        case_revision=case_file.revision,
        accuser_id=accuser_id,
        suspect_id=suspect_id,
        statement="I should have listened.",
        offered_tick=9,
        updated_tick=9,
        status="pending",
    )
    entities = {
        place_id: WorldEntity(id=place_id, kind="place", name="Old Square"),
        accuser_id: WorldEntity(id=accuser_id, kind="character", name="Mara", location_id=place_id),
        suspect_id: WorldEntity(
            id=suspect_id, kind="character", name="Bellkeeper", location_id=place_id,
            components={"personality": {"traits": ["guarded"], "values": ["fairness"]}},
        ),
        anchor_id: WorldEntity(
            id=anchor_id, kind="item", name="Notice Board", location_id=place_id,
            components={
                "public_description": "A notice board.",
                "investigation_case": definition.model_dump(mode="json"),
            },
        ),
    }
    relation = RelationRecord(
        id=uuid4(), from_id=suspect_id, to_id=accuser_id,
        dimensions={"familiarity": 3, "trust": -12}, updated_tick=9,
    )
    state = WorldState(
        current_tick=10,
        entities=entities,
        relations=[relation],
        investigation_cases=[case_file],
        public_accusation_corrections=[correction],
        accusation_repair_offers=[offer] if include_offer else [],
        player_id=player_id or accuser_id,
    )
    world = SimpleNamespace(world_id=world_id, ruleset_version=RULESET)
    branch = SimpleNamespace(branch_id=branch_id, state_version=0, current_tick=10)
    view = SimpleNamespace(state=state, world=world, branch=branch)
    return SimpleNamespace(
        world_id=world_id, branch_id=branch_id, place_id=place_id,
        accuser_id=accuser_id, suspect_id=suspect_id, anchor_id=anchor_id,
        case_file_id=case_file_id, closure_event_id=closure_event_id,
        correction_event_id=correction_event_id, offer_id=offer_id,
        state=state, view=view, relation=relation,
    )


class _Repository:
    def __init__(self, fixture, *, hosted_authorized=True, trust_proof=None, timeline=None):
        self.fixture = fixture
        self.state = fixture.state
        self.view = fixture.view
        self.hosted_authorized = hosted_authorized
        self.trust_proof = trust_proof
        self.timeline = timeline if timeline is not None else []
        self.calls: list[str] = []
        self.commits = []

    def read_world(self, _scope, _world_id, _branch_id):
        return self.view

    def authorize_hosted_npc(self, _scope, _world_id, actor_id):
        self.calls.append("authorize_hosted_npc")
        self.timeline.append("authorize_hosted_npc")
        assert actor_id == self.fixture.suspect_id
        if not self.hosted_authorized:
            raise AccessDenied("hosted NPC is player-controlled")

    def find_accusation_repair_trust_proof(
        self, _scope, _world_id, _branch_id, offer_id, suspect_id, *, requested_by=None,
    ):
        self.calls.append("find_accusation_repair_trust_proof")
        self.timeline.append("find_accusation_repair_trust_proof")
        assert offer_id == self.fixture.offer_id
        assert suspect_id == self.fixture.suspect_id
        assert requested_by is None
        return self.trust_proof

    def commit_command(self, _scope, _world_id, _branch_id, command, events):
        self.calls.append("commit_command")
        self.timeline.append("commit_command")
        self.commits.append((command, list(events)))
        for event in events:
            self.state = apply_event(self.state, event)
        self.view.state = self.state
        self.view.branch.state_version += 1
        return SimpleNamespace(command_id=command.command_id, state_version=self.view.branch.state_version)


class _Worlds:
    def __init__(self, repository):
        self.repository = repository

    def assert_player_actor(self, _scope, view, actor_id):
        assert actor_id == view.state.player_id


class _Store:
    def __init__(self):
        self.results = {}

    def begin(self, _scope, request, *, recover_committed=None):
        return self.results.get(request.request_id)

    def finish(self, _scope, request_id, result, *, succeeded):
        self.results[request_id] = result


class _Models:
    def __init__(self, action_proposals, *, hosted_choice="defer", timeline=None):
        self.action_proposals = action_proposals
        self.hosted_choice = hosted_choice
        self.timeline = timeline if timeline is not None else []
        self.role_tasks: list[str] = []
        self.action_scenes = []
        self.hosted_contexts = []

    async def generate(self, _scope, request):
        role = request.role_task.value
        self.role_tasks.append(role)
        self.timeline.append(f"model:{role}")
        if role == RoleTask.INTENT.value:
            payload = json.loads(request.messages[-1].content)
            self.action_scenes.append(payload["scene"])
            output = {"action": self.action_proposals[payload["input"]], "clarification": ""}
        elif role == RoleTask.NPC_DECISION.value:
            payload = json.loads(request.messages[-1].content)
            self.hosted_contexts.append(payload)
            output = {"choice": self.hosted_choice}
        else:
            raise AssertionError(f"Unexpected model role {role!r}.")
        Draft202012Validator(dict(request.output_schema)).validate(output)
        return SimpleNamespace(normalized_output=output)


def _service(fixture, *, action_proposals, player_id=None, hosted_authorized=True,
             trust_proof=None, hosted_choice="defer"):
    timeline: list[str] = []
    if player_id is not None and fixture.state.player_id != player_id:
        fixture.state = fixture.state.model_copy(update={"player_id": player_id})
        fixture.view.state = fixture.state
    repository = _Repository(
        fixture, hosted_authorized=hosted_authorized, trust_proof=trust_proof, timeline=timeline,
    )
    models = _Models(action_proposals, hosted_choice=hosted_choice, timeline=timeline)
    dispatcher = RulesActionDispatcher(
        repository, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext,
    )
    service = ConversationApplication(_Worlds(repository), models, _Store(), dispatcher)
    return service, repository, models, timeline


def _request(fixture, actor_id, text, *, version=0):
    return MessageRequest(
        request_id=uuid4(), world_id=fixture.world_id, branch_id=fixture.branch_id,
        session_id=uuid4(), actor_id=actor_id, expected_state_version=str(version),
        mode="action", text=text,
    )


@pytest.mark.asyncio
async def test_offer_requires_literal_player_text_and_records_private_zero_time_receipt():
    fixture = _fixture(include_offer=False)
    invented_text = "I want to explain myself."
    statement = "I should have listened."
    action_text = f"I want to tell Bellkeeper this: {statement}"
    proposals = {
        invented_text: {
            "kind": "offer_accusation_repair",
            "correction_event_id": str(fixture.correction_event_id),
            "statement": statement,
        },
        action_text: {
            "kind": "offer_accusation_repair",
            "correction_event_id": str(fixture.correction_event_id),
            "statement": statement,
        },
    }
    service, repository, models, _timeline = _service(fixture, action_proposals=proposals)
    initial_state = repository.state.model_dump(mode="json")

    rejected = await service.send(
        AccessScope(tenant_id=uuid4(), user_id=uuid4()),
        _request(fixture, fixture.accuser_id, invented_text),
    )
    assert rejected["kind"] == "clarification"
    assert rejected["world_effect"] is False
    assert repository.commits == []
    assert repository.state.model_dump(mode="json") == initial_state

    accepted_request = _request(fixture, fixture.accuser_id, action_text)
    result = await service.send(AccessScope(tenant_id=uuid4(), user_id=uuid4()), accepted_request)

    assert result["kind"] == "action"
    assert result["action_status"] == "succeeded"
    assert result["world_effect"] is True
    assert result["elapsed_ticks"] == "0"
    assert result["references"] == [str(repository.state.accusation_repair_offers[-1].offer_id)]
    assert len(repository.commits) == 1
    command, events = repository.commits[0]
    (event,) = events
    assert isinstance(event, AccusationRepairOfferRecordedEvent)
    assert event.event_id == uuid5(accepted_request.request_id, f"{RULESET}:accusation_repair.offer")
    assert event.command_id == command.command_id == accepted_request.request_id
    assert event.actor_id == command.actor_id == fixture.accuser_id
    assert event.record.statement == statement
    assert event.record.offered_tick == event.record.updated_tick == fixture.state.current_tick
    assert event.visibility == {"policy": "author_only"}
    assert command.command_type == "action.offer_accusation_repair"
    assert command.causation_ids == [fixture.correction_event_id]
    assert fixture.state.current_tick == 10
    assert fixture.state.investigation_cases[0].id == fixture.case_file_id
    assert fixture.state.investigation_cases[0].revision == 2
    assert fixture.state.relations == [fixture.relation]
    assert models.action_scenes[-1]["accusation_repair_options"] == [{
        "correction_event_id": str(fixture.correction_event_id),
        "suspect_name": "Bellkeeper",
        "corrected_tick": 8,
    }]
    assert "case_file_id" not in models.action_scenes[-1]["accusation_repair_options"][0]
    assert "source_closure_event_id" not in models.action_scenes[-1]["accusation_repair_options"][0]


@pytest.mark.asyncio
async def test_only_target_can_directly_accept_and_positive_recovery_cites_offer_and_proof():
    fixture = _fixture()
    direct_accept = "I accept the explanation."
    action = {
        "kind": "resolve_accusation_repair", "offer_id": str(fixture.offer_id),
        "expected_revision": 1, "operation": "accept",
    }
    proof = AccusationRepairTrustProof(
        case_file_id=fixture.case_file_id,
        source_closure_event_id=fixture.closure_event_id,
        source_relation_effect_event_id=uuid4(),
        accuser_id=fixture.accuser_id,
        suspect_id=fixture.suspect_id,
        baseline_trust=0,
        post_effect_trust=-12,
    )
    service, repository, models, _timeline = _service(
        fixture, action_proposals={direct_accept: action}, trust_proof=proof,
    )
    result = await service.send(
        AccessScope(tenant_id=uuid4(), user_id=uuid4()),
        _request(fixture, fixture.accuser_id, direct_accept),
    )
    assert result["kind"] == "clarification"
    assert result["world_effect"] is False
    assert repository.commits == []
    assert "find_accusation_repair_trust_proof" not in repository.calls

    # In a world where the target is the controlled actor, that same direct intent succeeds.
    service, repository, models, _timeline = _service(
        fixture, action_proposals={direct_accept: action}, player_id=fixture.suspect_id,
        trust_proof=proof,
    )
    result = await service.send(
        AccessScope(tenant_id=uuid4(), user_id=uuid4()),
        _request(fixture, fixture.suspect_id, direct_accept),
    )

    assert result["kind"] == "action"
    assert result["action_status"] == "succeeded"
    assert result["world_effect"] is True
    assert result["elapsed_ticks"] == "0"
    assert result["references"] == [str(fixture.offer_id)]
    assert len(repository.commits) == 1
    command, events = repository.commits[0]
    resolution = events[0]
    relation_change = events[1]
    assert isinstance(resolution, AccusationRepairResolvedEvent)
    assert isinstance(relation_change, RelationDimensionChangedEvent)
    assert resolution.actor_id == fixture.suspect_id
    assert resolution.operation == "accept"
    assert resolution.resulting_offer.status == "accepted"
    assert resolution.resulting_offer.revision == 2
    assert resolution.trust_recovery is not None
    assert resolution.trust_recovery.applied_delta == 4
    assert command.command_type == "action.resolve_accusation_repair"
    assert command.actor_id == fixture.suspect_id
    assert command.causation_ids == [fixture.offer_id, proof.source_relation_effect_event_id]
    assert repository.state.current_tick == 10
    assert repository.state.accusation_repair_offers[0].status == "accepted"
    assert repository.state.relations[0].dimensions["trust"] == -8
    assert models.role_tasks == [RoleTask.INTENT.value]


@pytest.mark.asyncio
async def test_hosted_response_authorizes_before_npc_model_call():
    fixture = _fixture()
    text = "Please let Bellkeeper decide whether to accept my explanation."
    proposal = {
        "kind": "request_accusation_repair_response",
        "offer_id": str(fixture.offer_id),
        "expected_revision": 1,
    }
    service, repository, models, timeline = _service(
        fixture, action_proposals={text: proposal}, hosted_authorized=False,
    )

    with pytest.raises(AccessDenied):
        await service.send(
            AccessScope(tenant_id=uuid4(), user_id=uuid4()),
            _request(fixture, fixture.accuser_id, text),
        )

    assert timeline == ["model:intent", "authorize_hosted_npc"]
    assert models.role_tasks == [RoleTask.INTENT.value]
    assert repository.commits == []


@pytest.mark.asyncio
async def test_hosted_defer_persists_pending_revision_with_safe_model_context():
    fixture = _fixture()
    text = "Please let Bellkeeper decide whether to accept my explanation."
    proposal = {
        "kind": "request_accusation_repair_response",
        "offer_id": str(fixture.offer_id),
        "expected_revision": 1,
    }
    service, repository, models, timeline = _service(
        fixture, action_proposals={text: proposal}, hosted_choice="defer",
    )

    result = await service.send(
        AccessScope(tenant_id=uuid4(), user_id=uuid4()),
        _request(fixture, fixture.accuser_id, text),
    )

    assert result["kind"] == "action"
    assert result["action_status"] == "succeeded"
    assert result["world_effect"] is True
    assert result["elapsed_ticks"] == "0"
    assert result["references"] == [str(fixture.offer_id)]
    assert timeline == [
        "model:intent", "authorize_hosted_npc", "model:npc_decision", "commit_command",
    ]
    assert len(repository.commits) == 1
    command, events = repository.commits[0]
    (event,) = events
    assert isinstance(event, AccusationRepairResolvedEvent)
    assert event.actor_id == fixture.suspect_id
    assert event.requested_by == fixture.accuser_id
    assert event.operation == "defer"
    assert event.resulting_offer.status == "pending"
    assert event.resulting_offer.revision == 2
    assert event.resulting_offer.last_operation == "defer"
    assert event.resulting_offer.updated_tick == fixture.state.current_tick == 10
    assert event.trust_recovery is None
    assert command.command_type == "action.request_accusation_repair_response"
    assert command.actor_id == fixture.accuser_id
    assert command.causation_ids == [fixture.offer_id]
    assert repository.state.accusation_repair_offers[0].status == "pending"
    assert repository.state.accusation_repair_offers[0].revision == 2
    assert repository.state.current_tick == 10

    context = models.hosted_contexts[0]
    serialized = json.dumps(context, ensure_ascii=False)
    assert context["witness"] == "Bellkeeper"
    assert context["accuser"] == "Mara"
    assert context["accusation_tick"] == 4
    assert context["correction_tick"] == 8
    assert context["impression"] == "仍心存戒备"
    assert context["explanation"] == "I should have listened."
    assert "case_file_id" not in context
    assert "source_closure_event_id" not in context
    assert str(fixture.case_file_id) not in serialized
    assert str(fixture.closure_event_id) not in serialized
    assert str(fixture.offer_id) not in serialized
    assert "trust" not in context
    assert "-12" not in serialized
    assert "baseline_trust" not in serialized
