from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.adapters.models.contracts import (
    CallTrace,
    ChatMessage,
    CostRecord,
    DataPolicy,
    DepthProfile,
    FinishReason,
    GenerationProfile,
    ModelRequest,
    ModelResult,
    ProviderConfig,
    ProviderKind,
    ProviderUsage,
    RoleTask,
    SpeedProfile,
    utc_now,
)
from world_engine.adapters.models.gateway import ModelGateway
from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.model_scheduler import EndpointAdmissionScheduler
from world_engine.application.operations import ModelOperations, OperationsStore
from world_engine.domain.models import AccessScope


def _scope() -> AccessScope:
    return AccessScope(tenant_id=uuid4(), user_id=uuid4())


def test_http_settings_accepts_bounded_strict_route_output_limit_and_legacy_routes(tmp_path):
    identity = _scope()
    operations = ModelOperations(OperationsStore(tmp_path / "operations.db"), ModelGateway())
    session = LocalSession(str(identity.user_id), str(identity.tenant_id), frozenset({"http://testserver"}))

    with TestClient(create_app(None, session, operations=operations)) as client:
        client.get("/api/v1/session")
        provider = client.post(
            "/api/v1/providers",
            json={
                "kind": "openai_compatible",
                "name": "Test endpoint",
                "base_url": "http://127.0.0.1:9999/v1",
                "default_model": "fixture",
            },
        )
        assert provider.status_code == 201

        for limit in (1, 2_147_483_647):
            response = client.put(
                "/api/v1/settings",
                json={"routes": {"gm_answer": {"max_output_tokens": limit}}},
            )
            assert response.status_code == 200
            assert response.json()["routes"]["gm_answer"]["max_output_tokens"] == limit

        legacy = client.put(
            "/api/v1/settings",
            json={"routes": {"gm_answer": {"provider_id": provider.json()["provider_id"], "model": "old-model"}}},
        )
        assert legacy.status_code == 200
        route = legacy.json()["routes"]["gm_answer"]
        assert route["provider_id"] == provider.json()["provider_id"]
        assert route["model"] == "old-model"
        assert "max_output_tokens" not in route

        for invalid_limit in (0, 2_147_483_648, True, 128.0, "128"):
            response = client.put(
                "/api/v1/settings",
                json={"routes": {"gm_answer": {"max_output_tokens": invalid_limit}}},
            )
            assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "route", "request_limit", "expected_limit"),
    [
        (RoleTask.GM_ANSWER, {"provider_id": "fixture", "max_output_tokens": 8192}, 123, 8192),
        # A legacy route document omits the new key; request-specific caps survive.
        (RoleTask.NPC_DECISION, {"provider_id": "fixture"}, 4096, 4096),
        # Output-token budgets do not apply to embeddings.
        (RoleTask.EMBEDDING, {"provider_id": "fixture", "max_output_tokens": 8192}, 64, 64),
    ],
)
async def test_model_operations_applies_only_explicit_generation_route_limit(
    role: RoleTask,
    route: dict[str, object],
    request_limit: int,
    expected_limit: int,
):
    config = ProviderConfig(provider_id="fixture", kind=ProviderKind.OLLAMA, base_url="http://localhost:11434")

    class Store:
        traces: list[CallTrace] = []

        def settings(self, _scope):
            return {"routes": {role.value: route}, "local_only": False}

        def provider(self, _scope, _provider_id):
            return config.public_dict() | {"enabled": True}

        def config(self, _document):
            return config

        def record_trace(self, _scope, trace):
            self.traces.append(trace)

    class Gateway:
        outgoing: ModelRequest | None = None

        async def generate(self, _config, request):
            self.outgoing = request
            now = utc_now()
            trace = CallTrace(
                request_id=request.trace_context.request_id,
                parent_trace=request.trace_context.parent_trace,
                world_id=request.trace_context.world_id,
                branch_id=request.trace_context.branch_id,
                job_id=request.trace_context.job_id,
                role_task=request.role_task,
                speed_profile=request.profile.speed,
                depth_profile=request.profile.depth,
                data_policy=request.data_policy,
                provider_id=config.provider_id,
                provider_kind=config.kind,
                requested_model=request.model or "fixture-model",
                reported_model=None,
                started_at=now,
                ended_at=now + timedelta(milliseconds=1),
                queue_seconds=None,
                first_token_seconds=None,
                validation_seconds=None,
                attempts=(),
                finish_reason=FinishReason.STOP,
                usage=ProviderUsage(),
                max_output_tokens=request.profile.max_output_tokens,
            )
            return ModelResult(
                normalized_output="ok",
                provider_usage=ProviderUsage(),
                estimated_usage=None,
                cost=CostRecord(),
                finish_reason=FinishReason.STOP,
                provider_request_id=None,
                requested_model=request.model or "fixture-model",
                reported_model=None,
                timings={"total_seconds": 0.001},
                validation={"valid": True},
                call_trace=trace,
            )

    store, gateway = Store(), Gateway()
    operations = ModelOperations(store, gateway, scheduler=EndpointAdmissionScheduler())
    profile = GenerationProfile(
        speed=SpeedProfile.FAST,
        depth=DepthProfile.EXHAUSTIVE,
        max_output_tokens=request_limit,
    )
    await operations.generate(
        _scope(),
        ModelRequest(role_task=role, messages=(ChatMessage("user", "test"),), profile=profile),
    )

    assert gateway.outgoing is not None
    assert gateway.outgoing.profile.max_output_tokens == expected_limit
    assert gateway.outgoing.profile.speed is SpeedProfile.FAST
    assert gateway.outgoing.profile.depth is DepthProfile.EXHAUSTIVE
    if route.get("max_output_tokens") is None or role is RoleTask.EMBEDDING:
        assert gateway.outgoing.profile is profile
    else:
        assert gateway.outgoing.profile is not profile
    assert store.traces[0].max_output_tokens == expected_limit
