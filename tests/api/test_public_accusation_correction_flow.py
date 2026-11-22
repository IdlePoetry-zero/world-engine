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
from world_engine.application.visibility import project_player_state
from world_engine.domain.components import RelationRecord
from world_engine.domain.gameplay_events import PublicAccusationCorrectionRecordedEvent
from world_engine.domain.investigation_cases import (
    CaseEvidenceRequirement,
    InvestigationCaseDefinition,
    InvestigationCaseFile,
    InvestigationCaseRoute,
    case_definition_digest,
)
from world_engine.domain.models import AccessScope, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.rules import RuleContext, RuleEngine
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER


def _fixture(*, case_owner_id: UUID | None = None):
    world_id, branch_id = uuid4(), uuid4()
    place_id, actor_id, suspect_id, bystander_id, other_owner_id, anchor_id = (uuid4() for _ in range(6))
    case_id, case_file_id = uuid4(), uuid4()
    source_closure_event_id = uuid4()
    observation_requirement = CaseEvidenceRequirement(
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
                route_id="supports-a",
                suspect_id=suspect_id,
                finding="supports",
                requirements=[observation_requirement],
            ),
            InvestigationCaseRoute(
                route_id="rules-out-a",
                suspect_id=suspect_id,
                finding="rules_out",
                requirements=[observation_requirement.model_copy(update={"effective_tick": 1})],
            ),
        ],
    )
    case_file = InvestigationCaseFile(
        id=case_file_id,
        case_id=case_id,
        anchor_id=anchor_id,
        owner_id=case_owner_id or actor_id,
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
    entities = {
        place_id: WorldEntity(id=place_id, kind="place", name="Old Square"),
        actor_id: WorldEntity(id=actor_id, kind="character", name="Player", location_id=place_id),
        suspect_id: WorldEntity(
            id=suspect_id, kind="character", name="Bellkeeper", location_id=place_id,
            components={"public_role": "bellkeeper"},
        ),
        bystander_id: WorldEntity(
            id=bystander_id, kind="character", name="Bystander", location_id=place_id,
            components={"public_role": "neighbor"},
        ),
        other_owner_id: WorldEntity(
            id=other_owner_id, kind="character", name="Other Accuser", location_id=place_id,
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
        id=uuid4(), from_id=suspect_id, to_id=actor_id,
        dimensions={"familiarity": 3, "trust": -12}, updated_tick=10,
    )
    state = WorldState(
        current_tick=10,
        entities=entities,
        relations=[relation],
        investigation_cases=[case_file],
        player_id=actor_id,
    )
    world = SimpleNamespace(world_id=world_id, ruleset_version="1.0.0")
    branch = SimpleNamespace(branch_id=branch_id, state_version=0, current_tick=10)
    view = SimpleNamespace(state=state, world=world, branch=branch)
    return SimpleNamespace(
        world_id=world_id,
        branch_id=branch_id,
        place_id=place_id,
        actor_id=actor_id,
        suspect_id=suspect_id,
        bystander_id=bystander_id,
        other_owner_id=other_owner_id,
        anchor_id=anchor_id,
        case_id=case_id,
        case_file_id=case_file_id,
        source_closure_event_id=source_closure_event_id,
        state=state,
        view=view,
    )


class _RepositoryFixture:
    def __init__(self, fixture):
        self.fixture = fixture
        self.state = fixture.state
        self.view = fixture.view
        self.closure_lookups: list[tuple[UUID, ...]] = []
        self.commits = []

    def read_world(self, _scope, _world_id, _branch_id):
        return self.view

    def find_public_accusation_closures(self, _scope, _world_id, _branch_id, actor_id, case_ids):
        assert isinstance(case_ids, tuple)
        assert all(isinstance(case_id, UUID) for case_id in case_ids)
        self.closure_lookups.append(case_ids)
        if actor_id != self.fixture.actor_id:
            return {}
        return {
            case_id: self.fixture.source_closure_event_id
            for case_id in case_ids if case_id == self.fixture.case_file_id
        }

    def commit_command(self, _scope, _world_id, _branch_id, command, events):
        self.commits.append((command, list(events)))
        for event in events:
            self.state = apply_event(self.state, event)
        self.view.state = self.state
        self.view.branch.state_version += 1
        return SimpleNamespace(
            command_id=command.command_id,
            state_version=self.view.branch.state_version,
            events=tuple(events),
        )


class _WorldsFixture:
    def __init__(self, repository):
        self.repository = repository

    def assert_player_actor(self, _scope, view, actor_id):
        assert actor_id == view.state.player_id


class _MemoryConversationStore:
    def __init__(self):
        self.results = {}

    def begin(self, _scope, request, *, recover_committed=None):
        return self.results.get(request.request_id)

    def finish(self, _scope, request_id, result, *, succeeded):
        self.results[request_id] = result


class _ModelsFixture:
    def __init__(self, fixture, action_proposals):
        self.fixture = fixture
        self.action_proposals = action_proposals
        self.action_scenes = []
        self.dialogue_payloads = []

    async def generate(self, _scope, request):
        role = request.role_task.value
        if role == RoleTask.INTENT.value:
            user_input = json.loads(request.messages[-1].content)
            if "scene" in user_input:
                scene = user_input["scene"]
                self.action_scenes.append(scene)
                case_file_id, source_id = self.action_proposals[user_input["input"]]
                output = {
                    "action": {
                        "kind": "correct_public_accusation",
                        "case_file_id": str(case_file_id),
                        "expected_revision": 2,
                        "source_closure_event_id": str(source_id),
                    },
                    "clarification": "",
                }
            else:
                speech = user_input["speech"]
                target_id = (
                    self.fixture.suspect_id
                    if "Bellkeeper" in speech else self.fixture.bystander_id
                )
                output = {
                    "target_id": str(target_id),
                    "purpose": "dialogue",
                    "subject_id": None,
                    "predicate": None,
                    "effective_tick": None,
                    "clarification": "",
                }
        elif role == RoleTask.SOCIAL_ADJUDICATION.value:
            payload = json.loads(request.messages[-1].content)
            self.dialogue_payloads.append(payload)
            output = {"utterance": "I heard what you said."}
        else:
            raise AssertionError(f"Unexpected model role {role!r}.")
        Draft202012Validator(dict(request.output_schema)).validate(output)
        return SimpleNamespace(normalized_output=output)


def _service(fixture, *, owner_id=None, action_proposals=None):
    if owner_id is not None:
        case_file = fixture.state.investigation_cases[0].model_copy(update={"owner_id": owner_id})
        state = fixture.state.model_copy(update={"investigation_cases": [case_file]})
        fixture.state = state
        fixture.view.state = state
    repository = _RepositoryFixture(fixture)
    models = _ModelsFixture(fixture, action_proposals or {})
    dispatcher = RulesActionDispatcher(
        repository, models, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext,
    )
    service = ConversationApplication(
        _WorldsFixture(repository), models, _MemoryConversationStore(), dispatcher,
    )
    return service, repository, models


def _request(fixture, *, mode, text, version=0):
    return MessageRequest(
        request_id=uuid4(),
        world_id=fixture.world_id,
        branch_id=fixture.branch_id,
        session_id=uuid4(),
        actor_id=fixture.actor_id,
        expected_state_version=str(version),
        mode=mode,
        text=text,
    )


@pytest.mark.asyncio
async def test_public_accusation_correction_uses_exact_source_pair_and_dialogue_is_suspect_scoped():
    fixture = _fixture()
    action_text = "I want to correct my public accusation to Bellkeeper."
    service, repository, models = _service(
        fixture,
        action_proposals={action_text: (fixture.case_file_id, fixture.source_closure_event_id)},
    )

    before = repository.state
    before_case = before.investigation_cases[0]
    before_relation = before.relations[0]
    action = _request(fixture, mode="action", text=action_text)
    result = await service.send(AccessScope(tenant_id=uuid4(), user_id=uuid4()), action)

    assert result["kind"] == "action"
    assert result["world_effect"] is True
    assert result["action_status"] == "succeeded"
    assert result["elapsed_ticks"] == "0"
    assert len(repository.commits) == 1
    command, events = repository.commits[0]
    correction_event, = events
    assert isinstance(correction_event, PublicAccusationCorrectionRecordedEvent)
    assert correction_event.event_id == uuid5(
        action.request_id, "1.0.0:investigation_public_accusation_corrected",
    )
    assert correction_event.command_id == command.command_id == action.request_id
    assert correction_event.record.case_file_id == fixture.case_file_id
    assert correction_event.record.expected_revision == before_case.revision == 2
    assert correction_event.record.source_closure_event_id == fixture.source_closure_event_id
    assert correction_event.record.actor_id == fixture.actor_id
    assert correction_event.record.suspect_id == fixture.suspect_id
    assert correction_event.record.corrected_tick == before.current_tick
    assert correction_event.record.location_id == fixture.place_id
    assert command.causation_ids == [fixture.source_closure_event_id]
    assert models.action_scenes[0]["public_accusation_correction_options"] == [{
        "case_file_id": str(fixture.case_file_id),
        "expected_revision": 2,
        "source_closure_event_id": str(fixture.source_closure_event_id),
        "title": "The Bell Tower Report",
        "suspect_name": "Bellkeeper",
    }]

    corrected = repository.state
    assert corrected.current_tick == before.current_tick
    assert corrected.investigation_cases[0] == before_case
    assert corrected.relations[0] == before_relation
    assert len(corrected.public_accusation_corrections) == 1
    own_projection = project_player_state(corrected.model_dump(mode="json"), str(fixture.actor_id))
    assert own_projection["case_files"][0]["public_accusation_corrected_tick"] == before.current_tick
    other_projection = project_player_state(corrected.model_dump(mode="json"), str(fixture.other_owner_id))
    assert other_projection["case_files"] == []

    suspect_dialogue = _request(
        fixture, mode="roleplay", text="I want to talk to Bellkeeper about something else.", version=1,
    )
    await service.send(AccessScope(tenant_id=uuid4(), user_id=uuid4()), suspect_dialogue)
    bystander_dialogue = _request(
        fixture, mode="roleplay", text="I want to talk to the bystander.", version=2,
    )
    await service.send(AccessScope(tenant_id=uuid4(), user_id=uuid4()), bystander_dialogue)

    suspect_context, = [
        row for row in models.dialogue_payloads
        if row["speaker"]["id"] == str(fixture.suspect_id)
    ]
    assert suspect_context["historical_public_accusation_corrections"] == [{
        "tick": before.current_tick,
        "semantic": "the current interlocutor publicly corrected an earlier accusation about this person",
    }]
    assert "case_file_id" not in json.dumps(suspect_context)
    assert str(fixture.case_file_id) not in json.dumps(suspect_context)
    assert "outcome" not in suspect_context
    assert "disconfirmed" not in json.dumps(suspect_context)
    assert "trust" not in suspect_context

    bystander_context, = [
        row for row in models.dialogue_payloads
        if row["speaker"]["id"] == str(fixture.bystander_id)
    ]
    assert "historical_public_accusation_corrections" not in bystander_context
    assert bystander_context["historical_public_accusations_by_current_interlocutor"] == []
    assert str(fixture.case_file_id) not in json.dumps(bystander_context)
    assert "outcome" not in bystander_context
    final_relation = next(
        relation for relation in repository.state.relations
        if relation.from_id == fixture.suspect_id and relation.to_id == fixture.actor_id
    )
    assert final_relation.dimensions["trust"] == before_relation.dimensions["trust"]


@pytest.mark.parametrize("source_is_authorized,case_is_owned", [(False, True), (True, False)])
@pytest.mark.asyncio
async def test_wrong_source_or_unowned_case_has_no_matching_option_or_world_commit(
    source_is_authorized, case_is_owned,
):
    fixture = _fixture()
    wrong_source_id = uuid4()
    action_text = "Correct my public accusation against Bellkeeper."
    proposal_source_id = fixture.source_closure_event_id if source_is_authorized else wrong_source_id
    service, repository, models = _service(
        fixture,
        owner_id=fixture.actor_id if case_is_owned else fixture.other_owner_id,
        action_proposals={action_text: (fixture.case_file_id, proposal_source_id)},
    )
    initial_hash = repository.state.model_dump(mode="json")
    request = _request(fixture, mode="action", text=action_text)

    result = await service.send(AccessScope(tenant_id=uuid4(), user_id=uuid4()), request)

    assert result["kind"] == "clarification"
    assert result["world_effect"] is False
    assert repository.commits == []
    assert repository.state.model_dump(mode="json") == initial_hash
    if case_is_owned:
        assert len(repository.closure_lookups) == 1
        assert repository.closure_lookups[0] == (fixture.case_file_id,)
        options = models.action_scenes[0]["public_accusation_correction_options"]
        assert options[0]["source_closure_event_id"] == str(fixture.source_closure_event_id)
        if not source_is_authorized:
            assert options[0]["source_closure_event_id"] != str(proposal_source_id)
    else:
        assert repository.closure_lookups == []
        assert "public_accusation_correction_options" not in models.action_scenes[0]
