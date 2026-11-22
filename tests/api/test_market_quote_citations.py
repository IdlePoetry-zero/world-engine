import json
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from world_engine.adapters.models import ModelGateway, ProviderKind
from world_engine.api.app import create_app
from world_engine.api.contracts import MessageRequest
from world_engine.api.security import LocalSession
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.operations import ModelOperations, OperationsStore
from world_engine.application.worlds import WorldApplication
from world_engine.domain.models import (
    AccessScope,
    NewWorld,
    ResourceAccount,
    WorldEntity,
    WorldState,
)
from world_engine.rules.contracts import RuleContext
from world_engine.rules.engine import RuleEngine
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def test_gm_accepts_only_projected_local_listing_citations_without_world_writes(tmp_path):
    identity = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player_id, market_id, remote_place_id = uuid4(), uuid4(), uuid4()
    local_seller_id, hidden_seller_id, remote_seller_id = uuid4(), uuid4(), uuid4()
    visible_listing_id, hidden_listing_id, remote_listing_id = uuid4(), uuid4(), uuid4()
    unlisted_listing_id = uuid4()

    def offer(listing_id, seller_id, account_id, price):
        return {
            "listing_id": listing_id,
            "resource_id": "resource.wheat",
            "stock_account_id": account_id,
            "seller_id": seller_id,
            "unit_price_minor": price,
            "minimum_quantity": "1",
            "active": True,
        }

    local_account_id, hidden_account_id, remote_account_id = uuid4(), uuid4(), uuid4()
    entities = {
        market_id: WorldEntity(
            id=market_id,
            kind="place",
            name="Town Market",
            components={
                "place": {"place_type": "settlement"},
                "market": {
                    "currency_resource_id": "core.coin",
                    "offers": [
                        offer(visible_listing_id, local_seller_id, local_account_id, 200),
                        offer(hidden_listing_id, hidden_seller_id, hidden_account_id, 999),
                        offer(remote_listing_id, remote_seller_id, remote_account_id, 777),
                    ],
                },
            },
        ),
        remote_place_id: WorldEntity(
            id=remote_place_id,
            kind="place",
            name="Remote Settlement",
            components={"place": {"place_type": "settlement"}},
        ),
        player_id: WorldEntity(
            id=player_id,
            kind="character",
            name="Player",
            location_id=market_id,
        ),
        local_seller_id: WorldEntity(
            id=local_seller_id,
            kind="character",
            name="Visible Merchant",
            location_id=market_id,
        ),
        hidden_seller_id: WorldEntity(
            id=hidden_seller_id,
            kind="character",
            name="Hidden Merchant",
            location_id=market_id,
            components={"hidden": True},
        ),
        remote_seller_id: WorldEntity(
            id=remote_seller_id,
            kind="character",
            name="Remote Merchant",
            location_id=remote_place_id,
        ),
    }
    state = WorldState(
        player_id=player_id,
        entities=entities,
        resources={
            local_account_id: ResourceAccount(
                resource_id="resource.wheat", balance="0", owner_id=local_seller_id,
            ),
            hidden_account_id: ResourceAccount(
                resource_id="resource.wheat", balance="0", owner_id=hidden_seller_id,
            ),
            remote_account_id: ResourceAccount(
                resource_id="resource.wheat", balance="0", owner_id=remote_seller_id,
            ),
        },
    )

    database = Database(f"sqlite:///{(tmp_path / 'world.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    created = repository.create_world(identity, NewWorld(title="Quote citation fixture", state=state))

    requested_references = [str(visible_listing_id)]
    model_calls = []

    def fake_gm(request):
        payload = json.loads(request.content)
        model_calls.append(payload)
        answer = {"answer": "A quote is posted at the local market.", "references": requested_references.copy()}
        return httpx.Response(200, json={
            "model": "fixture",
            "choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        })

    operations = OperationsStore(tmp_path / "operations.db")
    provider = operations.save_provider(identity, {
        "kind": "openai_compatible",
        "name": "Fixture GM",
        "base_url": "http://127.0.0.1:9999/v1",
        "default_model": "fixture",
        "capability_overrides": {"json_mode": True},
    })
    operations.save_settings(identity, {
        "speed": "balanced",
        "depth": "deep",
        "local_only": True,
        "routes": {
            role: {"provider_id": provider["provider_id"]}
            for role in ("intent", "gm_answer", "author_assist")
        },
    })
    models = ModelOperations(operations, ModelGateway(
        transports={ProviderKind.OPENAI_COMPATIBLE: httpx.MockTransport(fake_gm)},
    ))
    service = ConversationApplication(
        WorldApplication(repository, None), models, ConversationStore(operations),
    )
    session = LocalSession(str(identity.user_id), str(identity.tenant_id), frozenset({"http://testserver"}))
    app = create_app(service.worlds, session, operations=models, conversations=service)
    endpoint = f"/api/v1/worlds/{created.world.world_id}/messages"
    baseline = repository.read_world(identity, created.world.world_id, created.branch.branch_id)

    def submit():
        request = MessageRequest(
            request_id=uuid4(),
            world_id=created.world.world_id,
            branch_id=created.branch.branch_id,
            session_id=uuid4(),
            actor_id=player_id,
            expected_state_version="0",
            mode="ask_gm",
            text="What price is posted for wheat?",
        )
        return client.post(
            endpoint,
            json=request.model_dump(mode="json"),
            headers={"Idempotency-Key": str(request.request_id)},
        )

    try:
        with TestClient(app) as client:
            client.get("/api/v1/session")

            response = submit()
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["world_effect"] is False
            assert result["references"] == [str(visible_listing_id)]
            assert len(model_calls) == 1
            valid_input = json.loads(model_calls[0]["messages"][-1]["content"])
            assert valid_input["intent"] == "query"
            assert "citation_repair" not in valid_input

            # The same fake endpoint then attempts citations outside the
            # currently projected local quotes. Each answer must be rejected
            # after exactly one bounded citation repair.
            calls_per_case = {"valid_local_listing": 1}
            invalid_call_inputs = {}
            for unauthorized_id in (hidden_listing_id, remote_listing_id, unlisted_listing_id):
                requested_references[:] = [str(unauthorized_id)]
                first_call_index = len(model_calls)
                rejected = submit()
                assert rejected.status_code == 422, rejected.text
                assert rejected.json()["error"]["code"] == "RULE_REJECTED"
                calls_per_case[str(unauthorized_id)] = len(model_calls) - first_call_index
                assert calls_per_case[str(unauthorized_id)] == 2

                original_payload, repair_payload = model_calls[first_call_index:]
                original_input = json.loads(original_payload["messages"][-1]["content"])
                repair_input = json.loads(repair_payload["messages"][-1]["content"])
                assert set(original_input) == {"intent", "context", "discussion", "question"}
                assert original_input["intent"] == "query"
                assert original_input["question"] == valid_input["question"]
                assert set(repair_input) == set(original_input) | {"citation_repair"}
                assert {key: value for key, value in repair_input.items() if key != "citation_repair"} == original_input
                repair_instruction = repair_input["citation_repair"]
                assert isinstance(repair_instruction, str) and repair_instruction.strip()
                assert "context.reference_ids" in repair_instruction
                assert repair_input["context"] == original_input["context"] == valid_input["context"]
                invalid_call_inputs[str(unauthorized_id)] = (original_payload, repair_payload)

        assert calls_per_case == {
            "valid_local_listing": 1,
            str(hidden_listing_id): 2,
            str(remote_listing_id): 2,
            str(unlisted_listing_id): 2,
        }
        assert len(model_calls) == 7

        protected_ids = (
            hidden_listing_id,
            remote_listing_id,
            unlisted_listing_id,
            hidden_seller_id,
            remote_seller_id,
            hidden_account_id,
            remote_account_id,
            remote_place_id,
        )
        for payload in model_calls:
            serialized_payload = json.dumps(payload, ensure_ascii=False)
            assert all(str(protected_id) not in serialized_payload for protected_id in protected_ids)

        visible_context = valid_input
        listed_ids = {
            row["listing_id"] for row in visible_context["context"]["state"]["market_offers"]["offers"]
        }
        assert listed_ids == {str(visible_listing_id)}
        assert str(visible_listing_id) in visible_context["context"]["reference_ids"]
        assert not any(
            str(protected_id) in json.dumps(visible_context, ensure_ascii=False)
            for protected_id in protected_ids
        )
        for original_payload, repair_payload in invalid_call_inputs.values():
            for payload in (original_payload, repair_payload):
                call_input = json.loads(payload["messages"][-1]["content"])
                offers = call_input["context"]["state"]["market_offers"]["offers"]
                assert {row["listing_id"] for row in offers} == {str(visible_listing_id)}
                assert str(visible_listing_id) in call_input["context"]["reference_ids"]

        after = repository.read_world(identity, created.world.world_id, created.branch.branch_id)
        assert after.state_hash == baseline.state_hash
        assert after.branch.current_tick == baseline.branch.current_tick
        assert after.branch.state_version == baseline.branch.state_version
        assert after.last_seq == baseline.last_seq
    finally:
        database.dispose()


def test_guessed_hidden_seller_listing_is_rejected_without_rule_events():
    player_id, market_id, seller_id = uuid4(), uuid4(), uuid4()
    listing_id, stock_account_id = uuid4(), uuid4()
    state = WorldState(
        player_id=player_id,
        entities={
            market_id: WorldEntity(
                id=market_id,
                kind="place",
                components={
                    "place": {"place_type": "settlement"},
                    "market": {"currency_resource_id": "core.coin", "offers": [{
                        "listing_id": listing_id,
                        "resource_id": "resource.wheat",
                        "stock_account_id": stock_account_id,
                        "seller_id": seller_id,
                        "unit_price_minor": 200,
                        "minimum_quantity": "1",
                    }]},
                },
            ),
            player_id: WorldEntity(
                id=player_id, kind="character", name="Player", location_id=market_id,
            ),
            seller_id: WorldEntity(
                id=seller_id, kind="character", name="Unseen Merchant", location_id=market_id,
                components={"hidden": True},
            ),
        },
        resources={stock_account_id: ResourceAccount(
            resource_id="resource.wheat", balance="0", owner_id=seller_id,
        )},
    )
    context = RuleContext(
        world_id=uuid4(), branch_id=uuid4(), command_id=uuid4(),
        actor_id=player_id, ruleset_version="1.0.0",
    )

    result = RuleEngine().evaluate(
        state,
        {"kind": "trade", "listing_id": str(listing_id), "quantity": "1"},
        context,
    )

    assert result.outcome.status == "rejected"
    assert result.outcome.code == "listing_unavailable"
    assert result.events == []
