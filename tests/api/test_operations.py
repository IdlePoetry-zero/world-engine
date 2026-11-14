from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.operations import ModelOperations, OperationsStore
from world_engine.adapters.models.contracts import ChatMessage, ModelRequest, ProviderKind, RoleTask
from world_engine.adapters.models.errors import ProviderError
from world_engine.adapters.models.gateway import ModelGateway
from world_engine.domain.models import AccessScope


def scope():
    return AccessScope(tenant_id=uuid4(), user_id=uuid4())


def provider_document(url="http://127.0.0.1:9999/v1"):
    return {"kind": "openai_compatible", "name": "Test endpoint", "base_url": url,
            "default_model": "fixture", "enabled": True, "secret_ref": None}


def test_configuration_persists_and_is_account_scoped(tmp_path):
    path = tmp_path / "ops.db"
    store, user = OperationsStore(path), scope()
    provider = store.save_provider(user, provider_document())
    settings = {"speed": "fast", "depth": "deep", "local_only": True,
                "routes": {"gm_answer": {"provider_id": provider["provider_id"], "model": "fixture"}}}
    store.save_settings(user, settings)
    reopened = OperationsStore(path)
    assert reopened.settings(user) == settings
    assert reopened.provider(user, provider["provider_id"])["name"] == "Test endpoint"
    another_user = AccessScope(tenant_id=user.tenant_id, user_id=uuid4())
    assert reopened.list_providers(another_user) == []
    assert reopened.settings(another_user)["routes"] == {}
    with pytest.raises(ValueError):
        store.save_provider(user, provider_document("http://remote.example/v1"))


@pytest.mark.asyncio
async def test_calls_are_audited_without_prompts_and_locality_blocks_network(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"model": "fixture", "choices": [{"message": {"content": "A safe answer"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 17, "completion_tokens": 4, "total_tokens": 21}})

    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    gateway = ModelGateway(transports={ProviderKind.OPENAI_COMPATIBLE: httpx.MockTransport(handle)})
    operations = ModelOperations(store, gateway)
    provider = store.save_provider(user, provider_document("https://cloud.example/v1"))
    settings = {"speed": "balanced", "depth": "deep", "local_only": False,
                "routes": {"gm_answer": {"provider_id": provider["provider_id"]}}}
    store.save_settings(user, settings)
    request = ModelRequest(role_task=RoleTask.GM_ANSWER, messages=(ChatMessage("user", "private story text"),))
    result = await operations.generate(user, request)
    assert result.normalized_output == "A safe answer"
    traces = store.list_calls(user)
    assert len(traces) == 1 and traces[0]["usage"]["total_tokens"] == 21
    assert traces[0]["cost"]["amount"] is None
    assert "private story text" not in str(traces)
    assert store.list_calls(scope()) == []
    store.save_settings(user, settings | {"local_only": True})
    with pytest.raises(ProviderError):
        await operations.generate(user, ModelRequest(role_task=RoleTask.GM_ANSWER, messages=(ChatMessage("user", "second"),)))
    assert len(calls) == 1
    assert len(store.list_calls(user)) == 2


def test_http_configuration_rejects_secret_values_and_unknown_roles(tmp_path):
    user = scope()
    operations = ModelOperations(OperationsStore(tmp_path / "ops.db"), ModelGateway())
    session = LocalSession(str(user.user_id), str(user.tenant_id), frozenset({"http://testserver"}))
    # These routes do not use world services. World API behavior is independently
    # covered by real repository integration tests.
    with TestClient(create_app(None, session, operations=operations)) as client:
        client.get("/api/v1/session")
        body = provider_document() | {"secret_ref": "literal-secret-value"}
        response = client.post("/api/v1/providers", json=body)
        assert response.status_code == 422 and "literal-secret-value" not in response.text
        response = client.post("/api/v1/providers", json=provider_document())
        assert response.status_code == 201
        response = client.put("/api/v1/settings", json={"routes": {"typo_role": {}}})
        assert response.status_code == 422
        assert client.get("/api/v1/settings").json()["routes"] == {}
