from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator

from world_engine.adapters.models import RoleTask
from world_engine.api.contracts import MessageRequest
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import OperationsStore
from world_engine.application.worlds import WorldApplication
from world_engine.domain.components import HistoricalEvidenceArtifact
from world_engine.domain.gameplay_events import HistoricalEvidenceDestroyedEvent
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    EntityCreatedEvent,
    NewWorld,
    WorldEntity,
    WorldState,
)
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, RuleContext
from world_engine.rules.engine import RuleEngine
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


HIDDEN_ARCHIVE_REQUEST = "把诊所档案里关于药品失窃的隐藏记录彻底删掉。"
ARRAY_REQUEST = "撤掉这座阵里不需要的阵眼，哪些多余的你判断。"
AUTHORIZED_DESTROY_REQUEST = "请销毁「Old register」。"


@pytest.fixture
def authority_case(tmp_path):
    identity = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player_id, place_id = uuid4(), uuid4()
    database = Database(f"sqlite:///{(tmp_path / 'world.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    created = repository.create_world(
        identity,
        NewWorld(
            title="Action authority boundary",
            state=WorldState(
                player_id=player_id,
                entities={
                    place_id: WorldEntity(id=place_id, kind="place", name="Clinic yard"),
                    player_id: WorldEntity(
                        id=player_id, kind="character", name="Player", location_id=place_id,
                    ),
                },
            ),
        ),
    )

    def evidence_artifact(claim: str) -> HistoricalEvidenceArtifact:
        return HistoricalEvidenceArtifact(
            evidence_id=uuid4(),
            subject_id=player_id,
            claim=claim,
            fact={
                "predicate": "entity_status",
                "value": "active",
                "effective_tick": 0,
            },
            provenance=[{"kind": "pack", "reference_id": "pack.test.clinic-register"}],
        )

    legal_id, hidden_id, array_item_id = uuid4(), uuid4(), uuid4()
    legal_artifact = evidence_artifact("A visible register records an earlier clinic delivery.")
    hidden_artifact = evidence_artifact("A concealed note records an unverified theft allegation.")
    seeded_entities = [
        WorldEntity(
            id=legal_id,
            kind="item",
            name="Old register",
            location_id=place_id,
            components={
                "public_description": "A physical register with a worn cover.",
                "historical_evidence": legal_artifact.model_dump(mode="json"),
            },
        ),
        WorldEntity(
            id=hidden_id,
            kind="item",
            name="Concealed archive packet",
            location_id=place_id,
            components={
                "hidden": True,
                "historical_evidence": hidden_artifact.model_dump(mode="json"),
            },
        ),
        WorldEntity(
            id=array_item_id,
            kind="item",
            name="Array eye",
            location_id=place_id,
            components={"public_description": "A stone set among the array markers."},
        ),
    ]
    repository.commit_command(
        identity,
        created.world.world_id,
        created.branch.branch_id,
        CommandEnvelope(
            command_id=uuid4(),
            actor_id=player_id,
            idempotency_key="seed-authority-boundary-fixture",
            expected_state_version=0,
            command_type="test.seed_action_authority_boundary",
            payload={},
        ),
        [EntityCreatedEvent(entity=entity) for entity in seeded_entities],
    )

    try:
        yield {
            "identity": identity,
            "database": database,
            "repository": repository,
            "world_id": created.world.world_id,
            "branch_id": created.branch.branch_id,
            "player_id": player_id,
            "legal_id": legal_id,
            "hidden_id": hidden_id,
            "array_item_id": array_item_id,
            "operations_path": tmp_path / "operations.db",
        }
    finally:
        database.dispose()


def _snapshot(case) -> tuple[str, int, int, int]:
    view = case["repository"].read_world(case["identity"], case["world_id"], case["branch_id"])
    return view.state_hash, view.branch.current_tick, view.last_seq, view.branch.state_version


def _message(case, *, text: str, mode: str, request_id: UUID | None = None) -> MessageRequest:
    state_version = _snapshot(case)[3]
    return MessageRequest(
        request_id=request_id or uuid4(),
        world_id=case["world_id"],
        branch_id=case["branch_id"],
        session_id=uuid4(),
        actor_id=case["player_id"],
        expected_state_version=str(state_version),
        mode=mode,
        text=text,
    )


class RoutingModels:
    """Deterministic intent/author outputs; this stub never plans actions."""

    def __init__(self, routes: dict[str, str]):
        self.routes = routes
        self.calls: list[RoleTask] = []

    async def generate(self, _scope, request):
        self.calls.append(request.role_task)
        if request.role_task is RoleTask.INTENT:
            text = request.messages[-1].content
            output = {"intent": self.routes[text], "explanation": "请按已知范围处理。"}
        elif request.role_task is RoleTask.AUTHOR_ASSIST:
            output = {"answer": "这条设定建议仍待作者在编辑器中审核。", "references": []}
        else:
            raise AssertionError(f"unexpected conversation model role: {request.role_task}")
        Draft202012Validator(dict(request.output_schema)).validate(output)
        return SimpleNamespace(normalized_output=output)


class NeverActionModel:
    async def generate(self, _scope, _request):
        raise AssertionError("a read-only/clarification route must not invoke the action planner")


class ProposalModel:
    """A controlled action proposal stub, deliberately independent of routing."""

    def __init__(self, case):
        self.case = case
        self.target_id: UUID | None = None
        self.scenes: list[dict] = []

    async def generate(self, _scope, request):
        body = json.loads(request.messages[-1].content)
        scene = body["scene"]
        self.scenes.append(scene)
        destroyable_ids = {row["target_id"] for row in scene["destroyable_sources"]}
        assert str(self.case["legal_id"]) in destroyable_ids
        assert str(self.case["hidden_id"]) not in destroyable_ids
        assert str(self.case["array_item_id"]) not in destroyable_ids
        assert str(self.case["hidden_id"]) not in scene["entities"]
        assert self.target_id is not None
        output = {
            "action": {"kind": "destroy_historical_evidence", "target_id": str(self.target_id)},
            "clarification": "",
        }
        Draft202012Validator(dict(request.output_schema)).validate(output)
        return SimpleNamespace(normalized_output=output)


class RecordingRuleEngine(RuleEngine):
    def __init__(self):
        super().__init__()
        self.resolutions = []

    def evaluate(self, state, intent, context):
        resolution = super().evaluate(state, intent, context)
        self.resolutions.append((intent, resolution))
        return resolution


def _service(case, conversation_models, action_models, engine):
    repository = case["repository"]
    operations = OperationsStore(case["operations_path"])
    actions = RulesActionDispatcher(
        repository, action_models, engine, ACTION_INTENT_ADAPTER, RuleContext,
    )
    return ConversationApplication(
        WorldApplication(repository, None),
        conversation_models,
        ConversationStore(operations),
        actions=actions,
    )


@pytest.mark.asyncio
async def test_classifier_routes_hidden_archive_edit_to_author_and_array_choice_to_clarification(authority_case):
    case = authority_case
    before = _snapshot(case)
    routes = RoutingModels({
        HIDDEN_ARCHIVE_REQUEST: "author",
        ARRAY_REQUEST: "clarification",
    })
    service = _service(case, routes, NeverActionModel(), RuleEngine())

    author_result = await service.send(
        case["identity"], _message(case, text=HIDDEN_ARCHIVE_REQUEST, mode="auto"),
    )
    assert author_result["kind"] == "author"
    assert author_result["world_effect"] is False
    assert _snapshot(case) == before

    clarification_result = await service.send(
        case["identity"], _message(case, text=ARRAY_REQUEST, mode="auto"),
    )
    assert clarification_result["kind"] == "clarification"
    assert clarification_result["world_effect"] is False
    assert routes.calls == [RoleTask.INTENT, RoleTask.AUTHOR_ASSIST, RoleTask.INTENT]
    assert _snapshot(case) == before


@pytest.mark.asyncio
async def test_evidence_destruction_requires_a_named_visible_source(authority_case):
    case = authority_case
    baseline = _snapshot(case)
    proposals = ProposalModel(case)
    engine = RecordingRuleEngine()

    class NoClassifierCalls:
        async def generate(self, _scope, _request):
            raise AssertionError("explicit action-mode checks must bypass intent routing")

    service = _service(case, NoClassifierCalls(), proposals, engine)

    proposals.target_id = case["legal_id"]
    clarification = await service.send(
        case["identity"], _message(case, text=HIDDEN_ARCHIVE_REQUEST, mode="action"),
    )
    assert clarification["kind"] == "clarification"
    assert clarification["world_effect"] is False
    assert engine.resolutions == []
    assert _snapshot(case) == baseline

    rejected_targets = [
        (HIDDEN_ARCHIVE_REQUEST, case["hidden_id"]),
        (ARRAY_REQUEST, case["array_item_id"]),
        (ARRAY_REQUEST, uuid4()),
    ]
    for text, target_id in rejected_targets:
        proposals.target_id = target_id
        result = await service.send(
            case["identity"], _message(case, text=text, mode="action"),
        )
        assert result["kind"] == "action"
        assert result["world_effect"] is False
        assert result["action_status"] == "rejected"
        assert _snapshot(case) == baseline

    assert len(engine.resolutions) == len(rejected_targets)
    for proposed, resolution in engine.resolutions:
        assert proposed.kind == "destroy_historical_evidence"
        assert resolution.outcome.status == "rejected"
        assert resolution.outcome.code == "historical_evidence_unavailable"
        assert resolution.events == () or len(resolution.events) == 0

    # The following legal action uses the same branch, actor, tick and state
    # version left behind by the no-effect forced proposals above.
    proposals.target_id = case["legal_id"]
    request = _message(case, text=AUTHORIZED_DESTROY_REQUEST, mode="action")
    assert int(request.expected_state_version) == baseline[3]
    result = await service.send(case["identity"], request)

    assert result["kind"] == "action"
    assert result["world_effect"] is True
    assert result["action_status"] == "succeeded"
    assert result["elapsed_ticks"] == "0"
    assert result["references"] == [str(case["legal_id"])]
    assert len(engine.resolutions) == len(rejected_targets) + 1
    assert engine.resolutions[-1][1].outcome.status == "succeeded"
    assert len(engine.resolutions[-1][1].events) == 1
    assert isinstance(engine.resolutions[-1][1].events[0], HistoricalEvidenceDestroyedEvent)

    after = case["repository"].read_world(case["identity"], case["world_id"], case["branch_id"])
    assert after.state.current_tick == baseline[1]
    assert after.last_seq == baseline[2] + 1
    assert after.branch.state_version == baseline[3] + 1
    assert after.state_hash != baseline[0]
    assert after.state.entities[case["legal_id"]].status == "destroyed"
    assert after.state.entities[case["hidden_id"]].status == "active"
    assert after.state.entities[case["array_item_id"]].status == "active"

    event_page = case["repository"].list_events(
        case["identity"], case["world_id"], case["branch_id"], after_seq=baseline[2], limit=10,
    )
    assert len(event_page.events) == 1
    assert event_page.events[0].type == "core.historical_evidence_destroyed.v1"
    assert event_page.events[0].payload["target_id"] == str(case["legal_id"])
