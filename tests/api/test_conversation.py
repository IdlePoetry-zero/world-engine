from contextlib import closing
import json
import sqlite3
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from jsonschema import Draft202012Validator
from fastapi.testclient import TestClient
from sqlalchemy import event

from world_engine.adapters.models import ModelGateway, ProviderKind, RoleTask
from world_engine.api.contracts import MessageRequest, QualityProfile
from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.authoring import AuthoringApplication
from world_engine.application.conversation import (
    INTENT_SYSTEM_PROMPT,
    ConversationApplication,
    ConversationStore,
)
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.operations import ModelOperations, OperationsStore
from world_engine.application.worlds import WorldApplication
from world_engine.domain.errors import AccessDenied, Conflict, InvalidEvent, StaleVersion
from world_engine.domain.components import HistoricalEvidenceArtifact, KnowledgeRecord, RelationRecord
from world_engine.domain.gameplay_events import KnowledgeAcquiredEvent
from world_engine.domain.investigation_cases import InvestigationCaseFile
from world_engine.domain.models import AccessScope, CommandEnvelope, ComponentReplacedEvent, EntityCreatedEvent, MembershipRole, NewWorld, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.adjudication import improvised_examine_chance, improvised_roll_digest
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, RuleContext
from world_engine.rules.engine import RuleEngine


@pytest.fixture
def setup_conversation(tmp_path):
    identity = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player, npc, place = uuid4(), uuid4(), uuid4()
    database = Database(f"sqlite:///{(tmp_path / 'world.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    world = repository.create_world(identity, NewWorld(title="Conversation fixture", state=WorldState(
        player_id=player, entities={
            place: WorldEntity(id=place, kind="place", name="Square"),
            player: WorldEntity(id=player, kind="character", name="Player", location_id=place),
            npc: WorldEntity(id=npc, kind="character", name="Neighbor", location_id=place,
                             components={"personality": {"values": ["HIDDEN_CONSPIRACY"]}, "public_role": "artisan"}),
        })))
    captured = []

    def respond(request):
        payload = json.loads(request.content)
        captured.append(payload)
        # The adapter prepends its JSON schema instruction to the system message.
        _, _, system_prompt = payload["messages"][0]["content"].partition("\n\n")
        if system_prompt == INTENT_SYSTEM_PROMPT:
            output = {"intent": "hypothetical", "explanation": "这是条件讨论。"}
        else:
            output = {"answer": "这是假设讨论，实际结果需要条件信息。", "references": []}
        return httpx.Response(200, json={"model": "fixture", "choices": [{"message": {"content": json.dumps(output)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}})

    operations = OperationsStore(tmp_path / "operations.db")
    provider = operations.save_provider(identity, {"kind": "openai_compatible", "name": "Fixture", "base_url": "http://127.0.0.1:9999/v1", "default_model": "fixture", "capability_overrides": {"json_mode": True}})
    operations.save_settings(identity, {"speed": "balanced", "depth": "deep", "local_only": True,
        "routes": {role: {"provider_id": provider["provider_id"]} for role in ("intent", "gm_answer", "author_assist")}})
    models = ModelOperations(operations, ModelGateway(transports={ProviderKind.OPENAI_COMPATIBLE: httpx.MockTransport(respond)}))
    service = ConversationApplication(WorldApplication(repository, None), models, ConversationStore(operations))
    request = MessageRequest(request_id=uuid4(), world_id=world.world.world_id, branch_id=world.branch.branch_id,
        session_id=uuid4(), actor_id=player, expected_state_version="0", text="如果我闭关十年，会怎样？")
    yield identity, repository, service, request, captured
    database.dispose()


@pytest.mark.asyncio
async def test_hypothetical_query_never_changes_world_and_retry_does_not_call_model(setup_conversation):
    identity, repository, service, request, captured = setup_conversation
    before = repository.read_world(identity, request.world_id, request.branch_id)
    result = await service.send(identity, request)
    assert result["kind"] == "hypothetical" and result["world_effect"] is False
    after = repository.read_world(identity, request.world_id, request.branch_id)
    assert after.state_hash == before.state_hash
    assert after.branch.current_tick == before.branch.current_tick
    assert after.last_seq == before.last_seq
    assert len(captured) == 2
    assert "HIDDEN_CONSPIRACY" not in json.dumps(captured)
    assert "artisan" in json.dumps(captured)
    assert await service.send(identity, request) == result
    assert len(captured) == 2
    with pytest.raises(Conflict):
        await service.send(identity, request.model_copy(update={"text": "不同请求"}))
    rows = service.store.history(identity, request.world_id, request.branch_id, request.session_id, request.actor_id)
    assert len(rows) == 1 and rows[0]["result"] == result
    assert service.default_conversation(identity, request.world_id, request.branch_id)["session_id"] == str(request.session_id)


def test_busy_world_write_can_retry_same_chat_request_once(setup_conversation):
    identity, repository, service, request, _ = setup_conversation
    attempts = []

    class RestProposal:
        async def generate(self, _scope, model_request):
            attempts.append(model_request.role_task)
            return SimpleNamespace(normalized_output={"action": {"kind": "rest", "duration_ticks": 60}, "clarification": ""})

    service.actions = RulesActionDispatcher(repository, RestProposal(), RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext)
    request = request.model_copy(update={"mode": "action", "text": "休息一分钟。"})
    before = repository.read_world(identity, request.world_id, request.branch_id)
    session = LocalSession(str(identity.user_id), str(identity.tenant_id), frozenset({"http://testserver"}))
    world_path = repository.database.engine.url.database

    @event.listens_for(repository.database.engine, "checkout")
    def short_lock_timeout(connection, *_):
        connection.execute("PRAGMA busy_timeout=20")

    with TestClient(create_app(service.worlds, session, conversations=service)) as client:
        client.get("/api/v1/session").raise_for_status()
        path = f"/api/v1/worlds/{request.world_id}/messages"
        headers = {"Origin": "http://testserver", "Idempotency-Key": str(request.request_id)}
        payload = request.model_dump(mode="json")
        with closing(sqlite3.connect(world_path)) as held:
            held.execute("BEGIN IMMEDIATE")
            blocked = client.post(path, headers=headers, json=payload)
            assert blocked.status_code == 503
            assert blocked.headers["retry-after"] == "1"
            error = blocked.json()["error"]
            assert error["code"] == "STORAGE_BUSY" and error["retryable"]
            assert repository.read_world(identity, request.world_id, request.branch_id).branch == before.branch
            held.rollback()
        assert client.post(path, headers=headers, json=payload | {"text": "改为等待。"}).status_code == 409
        result = client.post(path, headers=headers, json=payload).raise_for_status().json()
        assert result["world_effect"] and result["elapsed_ticks"] == "60"
        assert client.post(path, headers=headers, json=payload).raise_for_status().json() == result
    after = repository.read_world(identity, request.world_id, request.branch_id)
    assert after.branch.state_version == before.branch.state_version + 1
    assert after.state.current_tick == before.state.current_tick + 60
    assert len(attempts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("text", "expected_kind", "expected_model_calls"), [
    ("夜间出宗门需要报备吗？如果要就替我把手续办了。", "clarification", 0),
    ("这药能治好我的伤吗？能的话就替我买一份。", "clarification", 0),
    ("现在出发能赶上日落吗？赶得上就带我走。", "clarification", 0),
    ("总价不超过十个信用点就买两份燃料", "hypothetical", 2),
])
async def test_auto_distinguishes_question_with_conditional_action_from_price_limit(
    setup_conversation, text, expected_kind, expected_model_calls,
):
    identity, repository, service, request, captured = setup_conversation
    request = request.model_copy(update={"mode": "auto", "text": text})
    before = repository.read_world(identity, request.world_id, request.branch_id)

    result = await service.send(identity, request)

    assert result["kind"] == expected_kind
    assert result["world_effect"] is False
    assert len(captured) == expected_model_calls
    after = repository.read_world(identity, request.world_id, request.branch_id)
    assert after.branch.state_version == before.branch.state_version
    assert after.branch.current_tick == before.branch.current_tick
    assert after.last_seq == before.last_seq
    assert after.state_hash == before.state_hash


@pytest.mark.asyncio
async def test_explicit_query_bypasses_classifier_and_actions_are_not_disguised_as_queries(setup_conversation):
    identity, repository, service, request, captured = setup_conversation
    explicit = request.model_copy(update={"mode": "ask_gm", "text": "作为作者，我想先了解商人的身份。"})
    result = await service.send(identity, explicit)
    assert result["kind"] == "query" and len(captured) == 1
    action = request.model_copy(update={"request_id": uuid4(), "mode": "action", "text": "现在闭关十年。"})
    with pytest.raises(Conflict):
        await service.send(identity, action)
    # The action backend is deliberately absent in this isolated test; rejection
    # must not masquerade as a completed game action or a successful GM answer.
    assert len(captured) == 1
    assert repository.read_world(identity, request.world_id).branch.current_tick == 0
    history = service.store.history(identity, request.world_id, request.branch_id, request.session_id, request.actor_id)
    assert history[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_direct_rest_clock_question_uses_rule_explanation_without_a_model_answer(setup_conversation):
    identity, repository, service, request, captured = setup_conversation
    request = request.model_copy(update={
        "text": "如果我从现在开始休息一小时，世界时钟会怎么走？先告诉我规则。",
    })
    before = repository.read_world(identity, request.world_id, request.branch_id)
    result = await service.send(identity, request)
    assert result["kind"] == "hypothetical" and result["world_effect"] is False
    assert "实际休息时长" in result["content"] and result["references"] == []
    assert len(captured) == 1
    after = repository.read_world(identity, request.world_id, request.branch_id)
    assert (after.state_hash, after.last_seq, after.state.current_tick) == (
        before.state_hash, before.last_seq, before.state.current_tick,
    )


def test_narrative_rewrite_remains_separate_from_original_message_and_world(setup_conversation, monkeypatch):
    identity, repository, service, request, _captured = setup_conversation
    calls = []
    original = "这是假设讨论，实际结果需要条件信息。"
    rewritten = "这是一个假设，结果还要看实际条件。"
    reply = {"content": rewritten}
    generate = service.models.generate

    async def model(scope, model_request):
        if model_request.role_task != RoleTask.NARRATION:
            return await generate(scope, model_request)
        calls.append(model_request)
        return SimpleNamespace(normalized_output=reply.copy())

    monkeypatch.setattr(service.models, "generate", model)
    app = create_app(service.worlds, LocalSession(
        str(identity.user_id), str(identity.tenant_id), frozenset({"http://testserver"}),
    ), conversations=service)
    world_path = f"/api/v1/worlds/{request.world_id}"
    path = f"{world_path}/narratives/{request.request_id}/rewrite"
    headers = {"Origin": "http://testserver"}
    before = repository.read_world(identity, request.world_id, request.branch_id)
    with TestClient(app) as client:
        client.get("/api/v1/session").raise_for_status()
        source = client.post(f"{world_path}/messages", headers=headers, json=request.model_dump(mode="json"))
        assert source.raise_for_status().json()["content"] == original
        response = client.post(path, headers=headers, json={"style": "concise"}).raise_for_status().json()
        assert response == {"narrative_id": str(request.request_id), "style": "concise", "content": rewritten,
                            "world_effect": False}
        assert client.post(path, headers=headers, json={"style": "concise"}).raise_for_status().json() == response
        assert len(calls) == 1
        model_input = calls[0].messages[-1].content
        assert original in model_input
        assert "HIDDEN_CONSPIRACY" not in model_input and request.text not in model_input

        reply["content"] = "这里需要 80 金币。"
        assert client.post(path, headers=headers, json={"style": "literary"}).status_code == 422
        assert len(calls) == 2
        history = client.get(f"{world_path}/conversation", params={"branch_id": str(request.branch_id)})
        result = history.raise_for_status().json()["items"][0]["result"]
        assert result["content"] == original
        assert result["rewrites"] == {"concise": rewritten}
        assert result["references"] == []
        assert client.post(f"/api/v1/worlds/{uuid4()}/narratives/{request.request_id}/rewrite",
                           headers=headers, json={"style": "concise"}).status_code == 404
        assert len(calls) == 2

    after = repository.read_world(identity, request.world_id, request.branch_id)
    assert (after.state_hash, after.last_seq, after.state.current_tick) == (
        before.state_hash, before.last_seq, before.state.current_tick,
    )


@pytest.mark.asyncio
async def test_gm_does_not_offer_remote_remembered_person_as_present_dialogue_target(setup_conversation):
    identity, repository, service, request, captured = setup_conversation
    original = repository.read_world(identity, request.world_id, request.branch_id).state
    remote_place, witness = uuid4(), uuid4()
    memory = KnowledgeRecord(
        id=uuid4(), holder_id=request.actor_id, subject_id=witness,
        claim="The traveler remembers meeting a witness.", summary="A past meeting.",
        confidence="0.8", received_tick=0, kind="memory",
        provenance=[{"kind": "pack", "reference_id": "pack.test.1.0"}],
    )
    state = WorldState(player_id=request.actor_id, entities=original.entities | {
        remote_place: WorldEntity(id=remote_place, kind="place", name="Remote village"),
        witness: WorldEntity(id=witness, kind="character", name="Remembered witness", location_id=remote_place),
    }, knowledge=[memory])
    world = repository.create_world(identity, NewWorld(title="Witness elsewhere", state=state))
    query = request.model_copy(update={"world_id": world.world.world_id, "branch_id": world.branch.branch_id,
                                       "mode": "ask_gm", "text": "我现在能找谁当面问话？"})
    before = repository.read_world(identity, query.world_id, query.branch_id)
    result = await service.send(identity, query)
    context = json.loads(captured[-1]["messages"][-1]["content"])["context"]
    assert "The traveler remembers meeting a witness." in json.dumps(context["state"])
    assert [target["name"] for target in context["present_dialogue_targets"]] == ["Neighbor"]
    assert result["world_effect"] is False
    assert repository.read_world(identity, query.world_id, query.branch_id).state_hash == before.state_hash


@pytest.mark.asyncio
async def test_gm_citations_require_treaty_and_shipment_records_in_actor_projection(setup_conversation, monkeypatch):
    from world_engine.application import conversation

    identity, repository, service, request, _ = setup_conversation
    treaty, shipment, unseen = (str(uuid4()) for _ in range(3))
    project = conversation.project_player_state

    def projection(state, actor_id):
        return project(state, actor_id) | {
            "own_safe_conduct_treaties": [{"treaty_id": treaty, "expired": True}],
            "own_shipments": [{"shipment_id": shipment, "status": "delivered"}],
        }

    citations = [treaty, shipment]

    async def generate(_scope, model_request):
        context = json.loads(model_request.messages[-1].content)["context"]
        assert context["state"]["own_safe_conduct_treaties"][0]["treaty_id"] == treaty
        assert context["state"]["own_shipments"][0]["shipment_id"] == shipment
        assert {treaty, shipment} <= set(context["reference_ids"])
        assert unseen not in context["reference_ids"]
        return SimpleNamespace(normalized_output={"answer": "协定已到期，货物已到达。", "references": citations.copy()})

    monkeypatch.setattr(conversation, "project_player_state", projection)
    monkeypatch.setattr(service.models, "generate", generate)
    request = request.model_copy(update={"mode": "ask_gm"})
    before = repository.read_world(identity, request.world_id, request.branch_id)
    answer = await service.send(identity, request)
    assert answer["references"] == [treaty, shipment] and not answer["world_effect"]
    citations[:] = [unseen]
    with pytest.raises(InvalidEvent, match="authorized references"):
        await service.send(identity, request.model_copy(update={"request_id": uuid4()}))
    after = repository.read_world(identity, request.world_id, request.branch_id)
    assert (after.last_seq, after.state_hash) == (before.last_seq, before.state_hash)


@pytest.mark.asyncio
async def test_gm_partnership_and_disclosure_citations_are_limited_to_projected_records(setup_conversation, monkeypatch):
    from world_engine.application import conversation

    identity, repository, service, request, _ = setup_conversation
    own_partnership, received_statement, hidden_source_partnership = (str(uuid4()) for _ in range(3))
    project = conversation.project_player_state

    def projection(state, actor_id):
        return project(state, actor_id) | {
            "own_relationship_partnerships": [{"partnership_id": own_partnership, "status": "active"}],
            "own_relationship_disclosures": [{"record_id": received_statement, "statement": "我当时说过已建立关系。"}],
        }

    citations = [own_partnership, received_statement]

    async def generate(_scope, model_request):
        context = json.loads(model_request.messages[-1].content)["context"]
        assert context["state"]["own_relationship_partnerships"][0]["partnership_id"] == own_partnership
        assert context["state"]["own_relationship_disclosures"][0]["record_id"] == received_statement
        assert {own_partnership, received_statement} <= set(context["reference_ids"])
        assert hidden_source_partnership not in context["reference_ids"]
        return SimpleNamespace(normalized_output={"answer": "这条记录显示关系已建立。", "references": citations.copy()})

    monkeypatch.setattr(conversation, "project_player_state", projection)
    monkeypatch.setattr(service.models, "generate", generate)
    request = request.model_copy(update={"mode": "ask_gm"})
    before = repository.read_world(identity, request.world_id, request.branch_id)

    answer = await service.send(identity, request)

    assert answer["references"] == [own_partnership, received_statement] and not answer["world_effect"]
    citations[:] = [hidden_source_partnership]
    with pytest.raises(InvalidEvent, match="authorized references"):
        await service.send(identity, request.model_copy(update={"request_id": uuid4()}))

    after = repository.read_world(identity, request.world_id, request.branch_id)
    assert (after.last_seq, after.state_hash, after.branch.current_tick) == (
        before.last_seq, before.state_hash, before.branch.current_tick,
    )


@pytest.mark.asyncio
async def test_gm_memory_limit_filters_discussion_before_counting_and_is_independent_of_speed(setup_conversation):
    identity, repository, service, request, captured = setup_conversation
    before = repository.read_world(identity, request.world_id, request.branch_id)
    for index in range(42):
        older = request.model_copy(update={"request_id": uuid4(), "text": f"prior discussion {index}"})
        service.store.begin(identity, older)
        service.store.finish(identity, older.request_id, {"kind": "hypothetical", "content": f"reply {index}"}, succeeded=True)
    for index in range(42):
        unrelated = request.model_copy(update={"request_id": uuid4(), "text": f"unrelated action {index}"})
        service.store.begin(identity, unrelated)
        service.store.finish(identity, unrelated.request_id, {"kind": "action", "content": "unrelated"}, succeeded=True)

    for speed in ("fast", "balanced", "patient"):
        for depth, count in (("standard", 12), ("deep", 24), ("exhaustive", 40)):
            current = request.model_copy(update={
                "request_id": uuid4(),
                "mode": "ask_gm",
                "profile": QualityProfile(speed=speed, depth=depth),
            })
            assert (await service.send(identity, current))["world_effect"] is False
            discussion = json.loads(captured[-1]["messages"][-1]["content"])["discussion"]
            assert len(discussion) == count
            assert all("unrelated" not in item["question"] for item in discussion)
    assert repository.read_world(identity, request.world_id, request.branch_id).state_hash == before.state_hash


@pytest.mark.asyncio
async def test_action_dispatch_commits_once_and_rejects_stale_input_before_model(setup_conversation):
    identity, repository, service, request, captured = setup_conversation
    before_rest = repository.fork(identity, request.world_id, request.branch_id, 0, "Before rest")

    model_calls = []

    class ModelFixture:
        async def generate(self, scope, model_request):
            model_calls.append(model_request)
            output = {
                "action": {"kind": "rest", "duration_ticks": 60}, "clarification": "",
            }
            validator = Draft202012Validator(dict(model_request.output_schema))
            validator.validate(output)
            assert not validator.is_valid({
                "action": output["action"] | {"actor_id": str(uuid4())}, "clarification": "",
            })
            return SimpleNamespace(normalized_output=output)

    class RuleFixture:
        def evaluate(self, state, action, context):
            assert context.actor_id == request.actor_id
            return SimpleNamespace(
                events=[TimeAdvancedEvent(to_tick=state.current_tick + action.duration_ticks)],
                elapsed_ticks=action.duration_ticks,
                outcome=SimpleNamespace(status="succeeded"),
            )

    service.actions = RulesActionDispatcher(
        repository, ModelFixture(), RuleFixture(), ACTION_INTENT_ADAPTER, RuleContext,
    )
    command = request.model_copy(update={"mode": "action", "text": "休息一分钟。"})
    result = await service.send(identity, command)
    assert result["world_effect"] is True and result["elapsed_ticks"] == "60"
    assert result["action_status"] == "succeeded"
    assert await service.send(identity, command) == result
    stale = command.model_copy(update={"request_id": uuid4()})
    with pytest.raises(StaleVersion):
        await service.send(identity, stale)
    failure = service.status(identity, stale.request_id)
    assert failure["status"] == "failed"
    assert failure["result"]["error"] == "STALE_VERSION"
    assert failure["result"]["world_effect"] is False
    assert len(model_calls) == 1
    persisted = repository.read_world(identity, command.world_id, command.branch_id)
    assert persisted.state.current_tick == 60 and persisted.last_seq == 1
    assert repository.replay(identity, command.world_id, command.branch_id).state_hash == persisted.state_hash
    after_rest = repository.fork(identity, request.world_id, request.branch_id, persisted.last_seq, "After rest")

    event, = repository.list_events(identity, request.world_id, request.branch_id).events
    for fork, expected in ((before_rest, []), (after_rest, [{
        "event_id": str(event.event_id), "started_tick": 0, "ended_tick": 60, "duration_seconds": 60,
    }])):
        query = request.model_copy(update={
            "request_id": uuid4(), "branch_id": fork.branch_id, "mode": "ask_gm", "text": "我刚才休息了多久？",
        })
        assert (await service.send(identity, query))["world_effect"] is False
        context = json.loads(captured[-1]["messages"][-1]["content"])["context"]
        assert context["own_completed_rest"] == expected
        assert (str(event.event_id) in context["reference_ids"]) is bool(expected)


@pytest.mark.asyncio
async def test_vacant_ballot_chat_commits_receipt_and_retry_once(setup_conversation):
    identity, repository, service, request, _ = setup_conversation
    member_id, organization_id = uuid4(), uuid4()
    actor_id = request.actor_id
    state = WorldState(player_id=actor_id, entities={
        actor_id: WorldEntity(id=actor_id, kind="character", name="Member"),
        member_id: WorldEntity(id=member_id, kind="character", name="Nominee"),
        organization_id: WorldEntity(id=organization_id, kind="organization", name="Assembly",
            components={"governance": {
                "members": [str(actor_id), str(member_id)], "leader_id": None,
                "succession_policy": "election",
                "offices": [{"office_id": "office.leader", "title": "Leader", "holder_id": None}],
            }}),
    })
    created = repository.create_world(identity, NewWorld(title="Vacant assembly", state=state))
    calls = []

    class ModelFixture:
        async def generate(self, scope, model_request):
            calls.append(model_request)
            return SimpleNamespace(normalized_output={
                "action": {"kind": "open_vacant_leadership_ballot",
                           "organization_id": str(organization_id), "nominee_id": str(member_id)},
                "clarification": "",
            })

    service.actions = RulesActionDispatcher(repository, ModelFixture(), RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext)
    command = request.model_copy(update={
        "world_id": created.world.world_id, "branch_id": created.branch.branch_id,
        "mode": "action", "text": "我提名 Nominee 填补 Assembly 的空缺领导席位。",
    })
    result = await service.send(identity, command)
    assert result["world_effect"] and result["action_status"] == "succeeded"
    assert "空缺席位开票" in result["content"]
    assert await service.send(identity, command) == result
    assert len(calls) == 1
    head = repository.read_world(identity, command.world_id, command.branch_id)
    assert head.last_seq == 1 and head.state.current_tick == 0
    assert head.state.leadership_ballots[0].vacancy_proposer_id == actor_id
    assert repository.replay(identity, command.world_id, command.branch_id, from_genesis=True).state_hash == head.state_hash


@pytest.mark.asyncio
async def test_text_action_weighs_only_actor_known_typed_sources_and_replays(setup_conversation):
    identity, repository, service, request, _ = setup_conversation
    initial = repository.read_world(identity, request.world_id, request.branch_id)
    actor_id = request.actor_id
    subject = next(entity for entity in initial.state.entities.values() if entity.name == "Neighbor")
    observation_id, claim_id, private_id = uuid4(), uuid4(), uuid4()
    observation = KnowledgeRecord(
        id=observation_id, owner_id=actor_id, subject_id=subject.id,
        claim="I saw the neighbor at the square.", kind="observation",
        source_event_id=uuid4(), received_tick=0,
        fact={"predicate": "entity_status", "value": "active", "effective_tick": 0},
    )
    claim = KnowledgeRecord(
        id=claim_id, owner_id=actor_id, subject_id=subject.id,
        claim="A rumor says the neighbor had left.", kind="rumor",
        source_event_id=uuid4(), received_tick=0,
        fact={"predicate": "entity_status", "value": "retired", "effective_tick": 0},
    )
    private_claim = KnowledgeRecord(
        id=private_id, holder_id=subject.id, subject_id=subject.id,
        claim="A private assertion.", kind="rumor", source_event_id=uuid4(), received_tick=0,
        fact={"predicate": "entity_status", "value": "active", "effective_tick": 0},
    )
    seed = CommandEnvelope(
        command_id=uuid4(), actor_id=actor_id, idempotency_key="seed-investigation-evidence",
        expected_state_version=0, command_type="test.seed_evidence", payload={},
    )
    repository.commit_command(identity, request.world_id, request.branch_id, seed, [
        KnowledgeAcquiredEvent(record=observation),
        KnowledgeAcquiredEvent(record=claim),
        KnowledgeAcquiredEvent(record=private_claim),
    ])

    captured = []

    class ModelFixture:
        async def generate(self, scope, model_request):
            captured.append(model_request)
            user_payload = json.loads(model_request.messages[-1].content)
            records = user_payload["scene"]["investigation_records"]
            assert {item["record_id"] for item in records} == {str(observation_id), str(claim_id)}
            assert str(private_id) not in json.dumps(user_payload)
            output = {
                "action": {
                    "kind": "weigh_evidence",
                    "observation_id": str(observation_id),
                    "claim_id": str(claim_id),
                },
                "clarification": "",
            }
            Draft202012Validator(dict(model_request.output_schema)).validate(output)
            return SimpleNamespace(normalized_output=output)

    service.actions = RulesActionDispatcher(
        repository, ModelFixture(), RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext,
    )
    action = request.model_copy(update={
        "request_id": uuid4(), "expected_state_version": "1", "mode": "action",
        "text": "请把这条观察和传闻核对一下。",
    })
    result = await service.send(identity, action)

    assert result["kind"] == "action" and result["world_effect"] is True
    assert result["action_status"] == "succeeded"
    assert "冲突" in result["content"] and "尚不能判断哪一项正确" in result["content"]
    after = repository.read_world(identity, action.world_id, action.branch_id)
    assert after.state.current_tick == 300
    assessment = next(record for record in after.state.knowledge if record.assessment is not None)
    assert assessment.assessment.finding == "conflict"
    assert assessment.source_refs == [observation_id, claim_id]
    assert assessment.owner_id == actor_id and assessment.visibility.policy == "private"
    assert repository.replay(identity, action.world_id, action.branch_id).state_hash == after.state_hash
    assert len(captured) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("lock_traits", [False, True])
async def test_dialogue_records_speaker_memories_without_advancing_time(setup_conversation, lock_traits):
    identity, repository, service, request, _ = setup_conversation
    before = repository.read_world(identity, request.world_id, request.branch_id)
    npc = next(entity for entity in before.state.entities.values() if entity.name == "Neighbor")
    if lock_traits:
        author = AuthoringApplication(repository, service.store.operations)
        personality = npc.components["personality"] | {"traits": ["谨慎"], "locked_fields": ["traits"]}
        preview = author.preview(identity, request.world_id, request.branch_id, npc.id, "personality", personality, "保留谨慎性格")
        author.commit(identity, request.world_id, UUID(preview["preview_id"]), preview["expected_version"])
        before = repository.read_world(identity, request.world_id, request.branch_id)
        request = request.model_copy(update={"expected_state_version": str(before.branch.state_version)})
    calls = []

    class ModelFixture:
        async def generate(self, scope, model_request):
            calls.append(model_request)
            outputs = {
                "intent": {"target_id": str(npc.id), "clarification": ""},
                "social_adjudication": {"utterance": "我是这里的工匠。"},
            }
            output = outputs[model_request.role_task.value]
            Draft202012Validator(dict(model_request.output_schema)).validate(output)
            assert all("HIDDEN_CONSPIRACY" not in message.content for message in model_request.messages)
            if model_request.role_task is RoleTask.SOCIAL_ADJUDICATION:
                scoped = json.loads(model_request.messages[-1].content)
                assert scoped.get("fixed_personality") == ({"traits": ["谨慎"]} if lock_traits else None)
            return SimpleNamespace(normalized_output=output)

    service.actions = RulesActionDispatcher(repository, ModelFixture(), None, ACTION_INTENT_ADAPTER, RuleContext)
    exchange = request.model_copy(update={"mode": "roleplay", "text": "我问工匠：你在这里做什么？"})
    result = await service.send(identity, exchange)
    assert result["kind"] == "dialogue" and result["world_effect"] and result["elapsed_ticks"] == "0"
    assert result["references"] == [str(npc.id)]
    after = repository.read_world(identity, request.world_id, request.branch_id)
    assert after.state.current_tick == before.state.current_tick
    assert after.state.entities[npc.id].components["personality"] == before.state.entities[npc.id].components["personality"]
    assert {record.holder_id: record.claim for record in after.state.knowledge} == {
        npc.id: exchange.text, request.actor_id: "我是这里的工匠。",
    }
    assert {(relation.from_id, relation.to_id): relation.dimensions["familiarity"]
            for relation in after.state.relations} == {(npc.id, request.actor_id): 1, (request.actor_id, npc.id): 1}
    assert all(relation.evidence_refs for relation in after.state.relations)
    assert repository.replay(identity, request.world_id, request.branch_id).state_hash == after.state_hash
    assert await service.send(identity, exchange) == result
    recovered = service._committed_status(identity, request.world_id, request.branch_id,
                                          request.request_id, request.actor_id)
    assert recovered["result"]["kind"] == "dialogue" and recovered["result"]["content"] == "我是这里的工匠。"
    assert len(calls) == 2
    second = exchange.model_copy(update={"request_id": uuid4(), "expected_state_version": str(after.branch.state_version)})
    await service.send(identity, second)
    later = repository.read_world(identity, request.world_id, request.branch_id)
    assert {relation.id for relation in later.state.relations} == {relation.id for relation in after.state.relations}
    assert all(relation.dimensions["familiarity"] == 2 and len(relation.evidence_refs) == 2
               for relation in later.state.relations)
    assert repository.replay(identity, request.world_id, request.branch_id).state_hash == later.state_hash


@pytest.mark.parametrize(
    ("case_kind", "expected_accusations"),
    [
        ("private", []),
        ("direct_public", [
            {"tick": 4, "semantic": "the current interlocutor accused this person"},
            {"tick": 6, "semantic": "the current interlocutor accused this person"},
            {"tick": 8, "semantic": "the current interlocutor accused this person"},
        ]),
        ("unrelated_third_party", []),
    ],
)
@pytest.mark.asyncio
async def test_dialogue_accusation_context_is_private_direct_historical_and_bounded(
    setup_conversation, case_kind, expected_accusations,
):
    identity, repository, service, request, _ = setup_conversation
    initial = repository.read_world(identity, request.world_id, request.branch_id)
    actor_id = request.actor_id
    npc = next(entity for entity in initial.state.entities.values() if entity.name == "Neighbor")
    third_party = WorldEntity(
        id=uuid4(), kind="character", name="Bystander",
        location_id=npc.location_id,
    )

    def closed_case(owner_id, suspect_id, *, tick, public, title):
        return InvestigationCaseFile(
            id=uuid4(), case_id=uuid4(), anchor_id=uuid4(), owner_id=owner_id,
            definition_hash="a" * 64, title=title, brief=f"PRIVATE_BRIEF_{title}",
            status="closed", opened_tick=max(0, tick - 1), updated_tick=tick,
            closed_tick=tick, outcome="disconfirmed", suspect_id=suspect_id,
            public_accusation=public,
        )

    if case_kind == "private":
        cases = [closed_case(actor_id, npc.id, tick=8, public=False, title="PRIVATE_CASE_TITLE")]
    elif case_kind == "direct_public":
        cases = [
            closed_case(actor_id, npc.id, tick=tick, public=True, title=f"SECRET_CASE_{tick}")
            for tick in (2, 4, 6, 8)
        ]
        cases.extend((
            closed_case(actor_id, npc.id, tick=11, public=True, title="FUTURE_CASE_TITLE"),
            closed_case(actor_id, npc.id, tick=9, public=False, title="PRIVATE_CASE_TITLE"),
            closed_case(third_party.id, npc.id, tick=9, public=True, title="THIRD_PARTY_CASE_TITLE"),
            closed_case(actor_id, third_party.id, tick=9, public=True, title="OTHER_SUSPECT_CASE_TITLE"),
        ))
    else:
        cases = [closed_case(third_party.id, npc.id, tick=8, public=True, title="THIRD_PARTY_CASE_TITLE")]

    entities = dict(initial.state.entities)
    entities[third_party.id] = third_party
    # The NPC has a directed numerical trust relation. Dialogue may use the
    # established familiarity behavior, but must not pass the trust score on.
    relation = RelationRecord(
        id=uuid4(), from_id=npc.id, to_id=actor_id,
        dimensions={"familiarity": 2, "trust": -15}, updated_tick=10,
    )
    state = initial.state.model_copy(update={
        "current_tick": 10,
        "entities": entities,
        "relations": [relation],
        "investigation_cases": cases,
    })
    view = SimpleNamespace(state=state, world=initial.world, branch=initial.branch)
    calls = []

    class ModelFixture:
        async def generate(self, _scope, model_request):
            calls.append(model_request)
            output = (
                {"target_id": str(npc.id), "clarification": ""}
                if model_request.role_task.value == "intent"
                else {"utterance": "你是来问话的吗？"}
            )
            Draft202012Validator(dict(model_request.output_schema)).validate(output)
            return SimpleNamespace(normalized_output=output)

    class RepositoryFixture:
        @staticmethod
        def commit_command(_scope, _world_id, _branch_id, command, _events):
            return SimpleNamespace(command_id=command.command_id, state_version=1)

    dispatcher = RulesActionDispatcher(
        RepositoryFixture(), ModelFixture(), None, ACTION_INTENT_ADAPTER, RuleContext,
    )
    exchange = request.model_copy(update={
        "mode": "roleplay", "text": "我看着工匠，问他最近如何。",
    })
    result = await dispatcher._dialogue(identity, view, exchange, 0)

    assert result["kind"] == "dialogue"
    assert len(calls) == 2
    reply_input = json.loads(calls[1].messages[-1].content)
    assert reply_input == {
        "speaker": {
            "id": str(npc.id), "kind": "character", "name": "Neighbor",
            "location_id": str(npc.location_id), "public_description": None,
            "appearance": None, "public_role": "artisan",
        },
        "temperament": {
            "openness": "0.5", "conscientiousness": "0.5", "extraversion": "0.5",
            "agreeableness": "0.5", "neuroticism": "0.5",
        },
        "other_speaker": "Player",
        "familiarity_with_other_speaker": "2",
        "historical_public_accusations_by_current_interlocutor": expected_accusations,
        "shared_knowledge": [],
        "earlier_words_from_this_speaker": [],
        "speech": exchange.text,
    }
    assert "trust" not in reply_input


@pytest.mark.asyncio
async def test_roleplay_claim_request_discloses_only_after_rule_and_scopes_later_discussion(setup_conversation):
    identity, repository, service, request, _ = setup_conversation
    initial = repository.read_world(identity, request.world_id, request.branch_id)
    actor_id = request.actor_id
    neighbor = next(entity for entity in initial.state.entities.values() if entity.name == "Neighbor")
    subject = initial.state.entities[actor_id]
    place = initial.state.entities[neighbor.location_id]
    farmer_id = uuid4()
    farmer = WorldEntity(id=farmer_id, kind="character", name="Farmer", location_id=place.id)
    secret_claim = KnowledgeRecord(
        id=uuid4(),
        holder_id=neighbor.id,
        subject_id=actor_id,
        claim="PRIVATE_WITNESS_ACCOUNT: the player was seen at the old winter feast.",
        summary="A private witness account.",
        confidence="0.7",
        source_event_id=uuid4(),
        provenance=[{"kind": "pack", "reference_id": "pack.test.1.0"}],
        received_tick=0,
        kind="rumor",
        fact={"predicate": "entity_status", "value": "active", "effective_tick": 0},
        disclosure_policy="if_asked",
        visibility={"policy": "private"},
    )
    seed = CommandEnvelope(
        command_id=uuid4(), actor_id=actor_id, idempotency_key="seed-npc-claim",
        expected_state_version=0, command_type="test.seed_npc_claim", payload={},
    )
    repository.commit_command(identity, request.world_id, request.branch_id, seed, [
        EntityCreatedEvent(entity=farmer),
        TimeAdvancedEvent(to_tick=3_600),
        KnowledgeAcquiredEvent(record=secret_claim),
    ])

    calls = []

    class ModelFixture:
        async def generate(self, scope, model_request):
            calls.append(model_request)
            if model_request.role_task.value == "intent":
                assert all("PRIVATE_WITNESS_ACCOUNT" not in message.content for message in model_request.messages)
                user = json.loads(model_request.messages[-1].content)
                if "请分享" in user["speech"]:
                    output = {
                        "target_id": str(neighbor.id),
                        "purpose": "ask_npc_claim",
                        "subject_id": str(subject.id),
                        "predicate": "entity_status",
                        "effective_tick": 0,
                        "clarification": "",
                    }
                elif "农夫" in user["speech"]:
                    output = {"target_id": str(farmer_id), "purpose": "dialogue", "clarification": ""}
                else:
                    output = {"target_id": str(neighbor.id), "purpose": "dialogue", "clarification": ""}
            else:
                output = {"utterance": "我听到了。"}
            Draft202012Validator(dict(model_request.output_schema)).validate(output)
            return SimpleNamespace(normalized_output=output)

    service.actions = RulesActionDispatcher(
        repository, ModelFixture(), RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext,
    )
    ask = request.model_copy(update={
        "request_id": uuid4(), "expected_state_version": "1", "mode": "roleplay",
        "text": "我问邻居，请分享你对我的状态说法。",
    })
    result = await service.send(identity, ask)
    assert result["kind"] == "dialogue" and result["world_effect"] is True
    assert "PRIVATE_WITNESS_ACCOUNT" in result["content"]
    assert result["elapsed_ticks"] == "0"
    after_ask = repository.read_world(identity, request.world_id, request.branch_id)
    disclosed = next(record for record in after_ask.state.knowledge if record.disclosure is not None)
    assert disclosed.fact is not None and disclosed.fact.effective_tick == 0
    assert disclosed.disclosure is not None and disclosed.disclosure.disclosed_tick == 3_600
    assert disclosed.received_tick == 3_600
    assert after_ask.state.current_tick == 3_600
    # The intent compiler saw only the public scene and the request, never the held claim.
    assert "PRIVATE_WITNESS_ACCOUNT" not in calls[0].messages[-1].content

    same_speaker = ask.model_copy(update={
        "request_id": uuid4(), "expected_state_version": "2", "text": "我继续和邻居交谈。",
    })
    await service.send(identity, same_speaker)
    same_speaker_context = json.loads(calls[-1].messages[-1].content)
    assert any(item["claim"] == secret_claim.claim for item in same_speaker_context["shared_knowledge"])

    other_speaker = ask.model_copy(update={
        "request_id": uuid4(), "expected_state_version": "3", "text": "我问农夫最近的消息。",
    })
    await service.send(identity, other_speaker)
    other_speaker_context = json.loads(calls[-1].messages[-1].content)
    assert all(item["claim"] != secret_claim.claim for item in other_speaker_context["shared_knowledge"])
    assert repository.replay(identity, request.world_id, request.branch_id).state_hash == repository.read_world(
        identity, request.world_id, request.branch_id,
    ).state_hash


@pytest.mark.asyncio
async def test_action_response_reveals_historical_archive_only_after_successful_check(setup_conversation):
    identity, repository, service, request, _ = setup_conversation
    initial = repository.read_world(identity, request.world_id, request.branch_id)
    actor_id = request.actor_id
    place = next(entity for entity in initial.state.entities.values() if entity.kind == "place")
    item_id = uuid4()
    artifact = HistoricalEvidenceArtifact(
        evidence_id=uuid4(),
        subject_id=actor_id,
        claim="The archive entry records the player at the winter feast.",
        fact={"predicate": "entity_status", "value": "active", "effective_tick": -3_600},
        provenance=[{"kind": "pack", "reference_id": "pack.test.archive.player-feast"}],
    )
    item = WorldEntity(
        id=item_id, kind="item", name="Old archive box", location_id=place.id,
        components={
            "public_description": "A small box with a faded label.",
            "historical_evidence": artifact.model_dump(mode="json"),
        },
    )
    seed = CommandEnvelope(
        command_id=uuid4(), actor_id=actor_id, idempotency_key="seed-local-historical-item",
        expected_state_version=0, command_type="test.seed_historical_item", payload={},
    )
    repository.commit_command(identity, request.world_id, request.branch_id, seed, [
        ComponentReplacedEvent(
            entity_id=actor_id, component_name="skills", expected_revision=None,
            replacement={"revision": 1, "skills": {"skill.investigation": {"level": "100"}}},
        ),
        EntityCreatedEvent(entity=item),
    ])
    state_after_seed = repository.read_world(identity, request.world_id, request.branch_id)

    chance = improvised_examine_chance(Decimal("100"))
    request_id = next(
        UUID(int=value) for value in range(1, 10_000)
        if int(improvised_roll_digest(
            world_id=request.world_id,
            branch_id=request.branch_id,
            command_id=UUID(int=value),
            ruleset_version="1.0.0",
        ), 16) % 100 + 1 <= chance
    )
    calls = []

    class ModelFixture:
        async def generate(self, scope, model_request):
            calls.append(model_request)
            scene = json.loads(model_request.messages[-1].content)["scene"]
            assert artifact.claim not in json.dumps(scene)
            assert "historical_evidence" not in json.dumps(scene)
            output = {
                "action": {
                    "kind": "improvised_check",
                    "target_id": str(item_id),
                    "skill_id": "skill.investigation",
                },
                "clarification": "",
            }
            Draft202012Validator(dict(model_request.output_schema)).validate(output)
            return SimpleNamespace(normalized_output=output)

    service.actions = RulesActionDispatcher(
        repository, ModelFixture(), RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext,
    )
    inspect = request.model_copy(update={
        "request_id": request_id, "expected_state_version": str(state_after_seed.branch.state_version),
        "mode": "action", "text": "我仔细检查这个旧档案盒。",
    })
    result = await service.send(identity, inspect)

    assert result["kind"] == "action" and result["world_effect"] is True
    assert result["content"] == artifact.claim
    assert result["elapsed_ticks"] == "60"
    assert result["references"] == [str(item_id)]
    assert len(calls) == 1
    after = repository.read_world(identity, inspect.world_id, inspect.branch_id)
    observation = next(record for record in after.state.knowledge if record.source_refs == [artifact.evidence_id])
    assert observation.fact == artifact.fact and observation.received_tick == 60
    assert repository.replay(identity, inspect.world_id, inspect.branch_id).state_hash == after.state_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "text"), [
    ("author", "给商人拟一个新的身份。"),
    ("auto", "作为作者，给商人拟一个新的身份。"),
    ("auto", "从作者设定角度，现有身份有哪些限制？"),
])
async def test_author_advice_cannot_commit_or_send_private_character_data(setup_conversation, mode, text):
    identity, repository, service, request, captured = setup_conversation
    request = request.model_copy(update={"mode": mode, "text": text})
    before = repository.read_world(identity, request.world_id, request.branch_id)
    result = await service.send(identity, request)
    after = repository.read_world(identity, request.world_id, request.branch_id)
    assert result["kind"] == "author" and result["world_effect"] is False
    assert before.state_hash == after.state_hash and before.last_seq == after.last_seq
    assert len(captured) == 1
    assert "HIDDEN_CONSPIRACY" not in json.dumps(captured)
    assert service.models.store.list_calls(identity)[0]["role_task"] == "author_assist"


@pytest.mark.asyncio
async def test_player_cannot_enter_author_advice_by_declaring_author_role(setup_conversation):
    identity, repository, service, request, captured = setup_conversation
    guest = identity.model_copy(update={"user_id": uuid4()})
    repository.grant_membership(identity, request.world_id, guest.user_id, MembershipRole.PLAYER,
                                controlled_actor_ids=[request.actor_id])
    request = request.model_copy(update={"mode": "auto", "text": "作为作者，请分析商人的身份设定。"})
    before = repository.read_world(identity, request.world_id, request.branch_id)
    with pytest.raises(AccessDenied):
        await service.send(guest, request)
    after = repository.read_world(identity, request.world_id, request.branch_id)
    assert (after.state_hash, after.last_seq) == (before.state_hash, before.last_seq)
    assert captured == []


def test_interrupted_request_is_preserved_without_automatic_reexecution(setup_conversation):
    identity, _, service, request, captured = setup_conversation
    assert service.store.begin(identity, request) is None
    assert service.store.recover_inflight() == 1
    record = service.store.read(identity, request.request_id)
    assert record["status"] == "failed"
    assert record["result"]["world_effect_unknown"] is True
    with pytest.raises(Conflict):
        service.store.begin(identity, request)
    assert captured == []


@pytest.mark.asyncio
async def test_request_status_recovers_commit_after_reply_recording_is_interrupted(setup_conversation):
    identity, repository, service, request, captured = setup_conversation
    request = request.model_copy(update={"mode": "action", "text": "休息一分钟。"})
    assert service.store.begin(identity, request) is None
    repository.commit_command(identity, request.world_id, request.branch_id, CommandEnvelope(
        command_id=request.request_id, actor_id=request.actor_id,
        idempotency_key=f"message:{request.request_id}", expected_state_version=0,
        command_type="action.rest",
    ), [TimeAdvancedEvent(to_tick=60)])

    fresh_store = ConversationStore(OperationsStore(service.store.operations.path))
    fresh_service = ConversationApplication(service.worlds, service.models, fresh_store)
    assert fresh_store.recover_inflight() == 1
    status = fresh_service.status(identity, request.request_id)
    assert status["status"] == "succeeded"
    assert status["result"]["state_version"] == "1"
    assert status["result"]["world_effect"] is True
    history = fresh_service.default_conversation(identity, request.world_id, request.branch_id)["items"]
    assert history[0]["status"] == "succeeded" and history[0]["result"] == status["result"]
    assert await fresh_service.send(identity, request) == status["result"]
    with pytest.raises(Conflict):
        await fresh_service.send(identity, request.model_copy(update={"text": "改成休息一天。"}))
    assert repository.read_world(identity, request.world_id, request.branch_id).last_seq == 1
    assert captured == []


@pytest.mark.asyncio
async def test_search_without_a_discovery_reports_time_and_no_new_clue(setup_conversation):
    identity, repository, service, request, _ = setup_conversation
    view = repository.read_world(identity, request.world_id, request.branch_id)
    origin = view.state.entities[request.actor_id].location_id

    class IntentModel:
        async def generate(self, scope, model_request):
            return SimpleNamespace(normalized_output={
                "action": {"kind": "explore", "place_id": str(origin)}, "clarification": "",
            })

    service.actions = RulesActionDispatcher(repository, IntentModel(), RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext)
    search = request.model_copy(update={"mode": "action", "text": "仔细搜查当前地点。"})
    result = await service.send(identity, search)
    assert result["world_effect"] is True
    assert result["content"] == "你花了 600 世界秒搜查，暂未发现新线索。"
    assert result["elapsed_ticks"] == "600" and not result.get("references")
    assert await service.send(identity, search) == result
    after = repository.read_world(identity, request.world_id, request.branch_id)
    assert after.state.current_tick == view.state.current_tick + 600
    assert after.state.knowledge == view.state.knowledge


def test_interrupted_discovery_recovers_the_observation(setup_conversation):
    identity, repository, service, request, _ = setup_conversation
    view = repository.read_world(identity, request.world_id, request.branch_id)
    origin = view.state.entities[request.actor_id].location_id
    evidence = WorldEntity(id=uuid4(), kind="place", name="钟楼账房", location_id=origin,
                           components={"hidden": True, "public_description": "落款日期比停钟晚一天。"})
    repository.commit_command(identity, request.world_id, request.branch_id, CommandEnvelope(
        actor_id=request.actor_id, idempotency_key="seed", expected_state_version=0,
        command_type="test.seed",
    ), [EntityCreatedEvent(entity=evidence)])
    view = repository.read_world(identity, request.world_id, request.branch_id)
    request = request.model_copy(update={"mode": "action", "text": "搜查旧账页", "expected_state_version": "1"})
    context = RuleContext(world_id=UUID(int=1), branch_id=UUID(int=2), command_id=UUID(int=3),
                          actor_id=request.actor_id, ruleset_version="1.0.0")
    result = RuleEngine().evaluate(view.state, {"kind": "explore", "place_id": origin}, context)
    assert result.outcome.code == "discovered"
    assert service.store.begin(identity, request) is None
    repository.commit_command(identity, request.world_id, request.branch_id, CommandEnvelope(
        command_id=request.request_id, actor_id=request.actor_id,
        idempotency_key=f"message:{request.request_id}", expected_state_version=1,
        command_type="action.explore",
    ), result.events)
    assert service.store.recover_inflight() == 1
    recovered = service.status(identity, request.request_id)["result"]
    assert recovered["content"] == "你发现了钟楼账房。落款日期比停钟晚一天。"
    assert recovered["references"] == [str(evidence.id)]


def test_http_session_history_and_request_identity_contract(setup_conversation):
    identity, _, service, request, captured = setup_conversation
    session = LocalSession(str(identity.user_id), str(identity.tenant_id), frozenset({"http://testserver"}))
    app = create_app(service.worlds, session, conversations=service, operations=service.models)
    base = f"/api/v1/worlds/{request.world_id}"
    with TestClient(app) as client:
        client.get("/api/v1/session")
        conversation = client.get(base + f"/conversation?branch_id={request.branch_id}").json()
        assert conversation["actor_id"] == str(request.actor_id)
        assert conversation["items"] == []
        body = request.model_dump(mode="json") | {"session_id": conversation["session_id"], "mode": "ask_gm"}
        mismatch = client.post(base + "/messages", json=body, headers={"Idempotency-Key": str(uuid4())})
        assert mismatch.status_code == 422 and captured == []
        response = client.post(base + "/messages", json=body, headers={"Idempotency-Key": str(request.request_id)})
        assert response.status_code == 200, response.text
        assert response.json()["world_effect"] is False
        status = client.get(f"/api/v1/commands/{request.request_id}").json()
        assert status["status"] == "succeeded" and status["result"] == response.json()
        reloaded = client.get(base + f"/conversation?branch_id={request.branch_id}").json()
        assert reloaded["session_id"] == conversation["session_id"]
        assert len(reloaded["items"]) == 1
        assert reloaded["items"][0]["result"]["message_id"] == str(request.request_id)


def _shipment_relief_world_state():
    from world_engine.domain.components import (
        GovernanceComponent, InventoryComponent, InventoryLot, PlaceComponent, ScheduledEventRecord,
    )
    from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
    from world_engine.domain.shipment import (
        ShipmentCargoManifest, ShipmentComponent, ShipmentDeliveredLot, ShipmentLotManifest,
    )

    donor, recipient, outsider, organization, region, depot, origin, shipment = (uuid4() for _ in range(8))
    source_account, transit_account, depot_account, recipient_account = (uuid4() for _ in range(4))
    source_lots = sorted((uuid4(), uuid4()), key=str)
    transit_lots = sorted((uuid4(), uuid4()), key=str)
    receipt_lots, unrelated_lot = [uuid4() for _ in range(2)], uuid4()
    schedule = uuid4()
    food = "feudal.food"
    manifest = ShipmentCargoManifest(
        resource_id=food,
        source_account_id=source_account,
        transit_account_id=transit_account,
        destination_account_id=depot_account,
        quantity=Decimal("5"),
        lots=[
            ShipmentLotManifest(source_lot_id=source_lots[0], transit_lot_id=transit_lots[0], quantity=Decimal("2")),
            ShipmentLotManifest(source_lot_id=source_lots[1], transit_lot_id=transit_lots[1], quantity=Decimal("3")),
        ],
    )
    delivered = ShipmentComponent(
        dispatch_event_id=uuid4(), arrival_schedule_id=schedule,
        sender_id=donor, source_owner_id=donor, destination_owner_id=depot,
        origin_place_id=origin, destination_place_id=depot, route_id=uuid4(),
        departure_tick=0, arrival_tick=10, status="delivered", arrived_tick=10,
        cargo=[manifest], delivered_lots=[
            ShipmentDeliveredLot(transit_lot_id=transit_lots[0], destination_lot_id=receipt_lots[0],
                                 quantity=Decimal("2"), received_tick=10),
            ShipmentDeliveredLot(transit_lot_id=transit_lots[1], destination_lot_id=receipt_lots[1],
                                 quantity=Decimal("3"), received_tick=10),
        ],
    )
    state = WorldState(
        player_id=donor,
        current_tick=20,
        entities={
            donor: WorldEntity(id=donor, kind="character", location_id=depot),
            recipient: WorldEntity(id=recipient, kind="character", location_id=depot),
            outsider: WorldEntity(id=outsider, kind="character", location_id=depot),
            organization: WorldEntity(
                id=organization, kind="polity", location_id=region,
                components={"governance": GovernanceComponent(members=[donor, recipient], leader_id=donor).model_dump(mode="json")},
            ),
            region: WorldEntity(
                id=region, kind="place", components={"place": PlaceComponent(place_type="region").model_dump(mode="json")},
            ),
            depot: WorldEntity(
                id=depot, kind="place",
                components={
                    "place": PlaceComponent(place_type="building", region_id=region).model_dump(mode="json"),
                    "inventory": InventoryComponent(lots=[
                        InventoryLot(lot_id=unrelated_lot, account_id=depot_account, resource_id=food, quantity=Decimal("20")),
                        InventoryLot(lot_id=receipt_lots[0], account_id=depot_account, resource_id=food, quantity=Decimal("2")),
                        InventoryLot(lot_id=receipt_lots[1], account_id=depot_account, resource_id=food, quantity=Decimal("3")),
                    ]).model_dump(mode="json"),
                },
            ),
            origin: WorldEntity(
                id=origin, kind="place", components={"place": PlaceComponent(place_type="site").model_dump(mode="json")},
            ),
            shipment: WorldEntity(
                id=shipment, kind="shipment", location_id=depot,
                components={"shipment": delivered.model_dump(mode="json"), "inventory": InventoryComponent().model_dump(mode="json")},
            ),
        },
        resources={
            source_account: ResourceAccount(resource_id=food, balance=Decimal("0"), owner_id=donor),
            transit_account: ResourceAccount(resource_id=food, balance=Decimal("0"), owner_id=shipment),
            depot_account: ResourceAccount(resource_id=food, balance=Decimal("25"), owner_id=depot),
            recipient_account: ResourceAccount(resource_id=food, balance=Decimal("0"), owner_id=recipient),
        },
        scheduled_events=[ScheduledEventRecord(
            id=schedule, due_tick=10, phase="delivery", handler_id="shipment.arrival",
            handler_version="1.0.0", status="completed", completed_runs=1,
            payload={"shipment_id": str(shipment)},
        )],
        metadata={"resource_roles": {"food_resource_id": food}},
    )
    return state, {
        "donor": donor, "recipient": recipient, "outsider": outsider,
        "organization": organization, "depot": depot, "shipment": shipment,
        "receipt_lots": receipt_lots, "unrelated_lot": unrelated_lot,
    }


@pytest.mark.asyncio
async def test_gm_shipment_relief_history_is_recipient_private_persistent_and_branch_scoped(
    setup_conversation, monkeypatch,
):
    from types import SimpleNamespace

    from world_engine.domain.shipment_relief import plan_shipment_relief
    from world_engine.domain.shipment_relief_events import (
        ShipmentReliefDistributedEvent, shipment_relief_event_id, shipment_relief_visibility,
    )

    identity, repository, service, request, _ = setup_conversation
    state, ids = _shipment_relief_world_state()
    created = repository.create_world(identity, NewWorld(title="Relief history context", state=state))
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    before_distribution = repository.fork(identity, world_id, branch_id, 0, "Before distribution")
    view = repository.read_world(identity, world_id, branch_id)
    command = CommandEnvelope(
        actor_id=ids["donor"], idempotency_key="distribute-all-delivered-food",
        expected_state_version=view.branch.state_version,
        command_type="action.distribute_shipment_relief",
        payload={
            "kind": "distribute_shipment_relief",
            "organization_id": str(ids["organization"]),
            "shipment_id": str(ids["shipment"]),
            "recipient_id": str(ids["recipient"]),
            "quantity": "5",
        },
    )
    plan = plan_shipment_relief(
        view.state,
        actor_id=ids["donor"],
        organization_id=ids["organization"],
        shipment_id=ids["shipment"],
        recipient_id=ids["recipient"],
        quantity=Decimal("5"),
    )
    event = ShipmentReliefDistributedEvent(
        event_id=shipment_relief_event_id(
            command.command_id, view.world.ruleset_version, ids["shipment"], ids["recipient"],
        ),
        world_id=world_id,
        branch_id=branch_id,
        command_id=command.command_id,
        actor_id=ids["donor"],
        ruleset_version=view.world.ruleset_version,
        plan=plan,
        visibility=shipment_relief_visibility(ids["donor"], ids["recipient"]),
    )
    repository.commit_command(identity, world_id, branch_id, command, [event])
    after_distribution = repository.read_world(identity, world_id, branch_id)
    after_fork = repository.fork(identity, world_id, branch_id, 1, "After distribution")

    # The whole delivered lot set was consumed, but the historical receipt stays queryable.
    depot_inventory = after_distribution.state.entities[ids["depot"]].components["inventory"]["lots"]
    assert [lot["lot_id"] for lot in depot_inventory] == [str(ids["unrelated_lot"])]
    recipient_history = repository.list_shipment_relief_history(
        identity, world_id, branch_id, ids["recipient"], through_seq=after_distribution.last_seq,
    )
    assert len(recipient_history) == 1 and recipient_history[0].event_id == event.event_id
    assert repository.list_shipment_relief_history(
        identity, world_id, branch_id, ids["outsider"], through_seq=after_distribution.last_seq,
    ) == []
    assert repository.list_shipment_relief_history(
        identity, world_id, before_distribution.branch_id, ids["recipient"], through_seq=0,
    ) == []
    inherited_history = repository.list_shipment_relief_history(
        identity, world_id, after_fork.branch_id, ids["recipient"], through_seq=after_fork.head_seq,
    )
    assert [row.event_id for row in inherited_history] == [event.event_id]

    generated_contexts = []
    citations = [str(event.event_id)]

    async def answer(_scope, model_request):
        prompt = json.loads(model_request.messages[-1].content)
        generated_contexts.append((prompt["context"], model_request.messages[0].content))
        return SimpleNamespace(normalized_output={"answer": "你曾收到这批救济。", "references": citations.copy()})

    monkeypatch.setattr(service.models, "generate", answer)
    recipient_view = repository.read_world(identity, world_id, branch_id)
    recipient_request = request.model_copy(update={
        "world_id": world_id,
        "branch_id": branch_id,
        "actor_id": ids["recipient"],
        "mode": "ask_gm",
        "expected_state_version": str(recipient_view.branch.state_version),
        "text": "我之前收到过救济粮吗？",
    })
    recipient_answer = await service._answer(identity, recipient_view, recipient_request, "query")
    recipient_context, system_prompt = generated_contexts[-1]
    assert recipient_answer["references"] == [str(event.event_id)]
    assert len(recipient_context["past_shipment_relief"]) == 1
    receipt = recipient_context["past_shipment_relief"][0]
    assert receipt["event_id"] == str(event.event_id)
    assert receipt["payload"]["quantity"] == "5"
    assert receipt["payload"]["authorized_by_actor_id"] == str(ids["donor"])
    assert receipt["payload"]["authorizing_organization_id"] == str(ids["organization"])
    assert receipt["payload"]["authorized_by_name"] == recipient_view.state.entities[ids["donor"]].name
    assert receipt["payload"]["authorizing_organization_name"] == recipient_view.state.entities[ids["organization"]].name
    assert "sender_id" not in receipt["payload"] and "route_id" not in receipt["payload"]
    assert "future" not in system_prompt.lower()
    assert "未来还会发放" in system_prompt

    citations[:] = [str(event.event_id)]
    outsider_view = repository.read_world(identity, world_id, branch_id)
    outsider_request = recipient_request.model_copy(update={
        "request_id": uuid4(),
        "actor_id": ids["outsider"],
    })
    with pytest.raises(InvalidEvent, match="authorized references"):
        await service._answer(identity, outsider_view, outsider_request, "query")
    outsider_context, _ = generated_contexts[-1]
    assert outsider_context["past_shipment_relief"] == []
    assert str(event.event_id) not in outsider_context["reference_ids"]
