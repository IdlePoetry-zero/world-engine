import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from world_engine.api.app import create_app
from world_engine.api.contracts import RoleRoute
from world_engine.api.security import LocalSession
from world_engine.application.errors import ApplicationFailure
from world_engine.application.operations import ModelOperations, OperationsStore
from world_engine.adapters.models.contracts import (
    AttemptRecord, CallTrace, ChatMessage, DataPolicy, DepthProfile, FinishReason, GenerationProfile, ModelRequest,
    ProviderKind, ProviderUsage, RoleTask, SpeedProfile, TraceContext, utc_now,
)
from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode, TransportFailureKind
from world_engine.adapters.models.gateway import ModelGateway
from world_engine.domain.errors import Conflict, NotFound
from world_engine.domain.models import AccessScope


def scope():
    return AccessScope(tenant_id=uuid4(), user_id=uuid4())


def provider_document(url="http://127.0.0.1:9999/v1"):
    return {"kind": "openai_compatible", "name": "Test endpoint", "base_url": url,
            "default_model": "fixture", "enabled": True, "secret_ref": None}


@dataclass(frozen=True)
class _SyntheticCallTrace:
    request_id: str
    started_at: datetime

    def to_public_dict(self):
        return {
            "request_id": self.request_id,
            "parent_trace": None,
            "world_id": None,
            "branch_id": None,
            "job_id": None,
            "role_task": RoleTask.GM_ANSWER.value,
            "provider_id": "fixture",
            "provider_kind": ProviderKind.OPENAI_COMPATIBLE.value,
            "requested_model": "fixture",
            "reported_model": "fixture",
            "started_at": self.started_at.isoformat(),
            "ended_at": self.started_at.isoformat(),
            "queue_seconds": None,
            "first_token_seconds": None,
            "validation_seconds": None,
            "attempts": [],
            "finish_reason": "stop",
            "usage": {"source": "reported", "total_tokens": 2, "input_tokens": 1, "output_tokens": 1},
            "cost": {"state": "unknown", "amount": None, "currency": None, "components": {}},
            "error_code": None,
            "end_reason": "completed",
        }


@dataclass(frozen=True)
class _StubResult:
    normalized_output: str
    call_trace: CallTrace


def _stub_trace(config, request, *, error=None):
    now = utc_now()
    attempts = () if error is None else (AttemptRecord(
        attempt_number=1,
        started_at=now,
        ended_at=now,
        elapsed_seconds=0.0,
        outcome="error",
        retryable=error.retryable,
        possibly_billed=error.possibly_billed,
        usage=ProviderUsage(),
        error_code=error.code.value,
        transport_failure_kind=error.transport_failure_kind,
    ),)
    return CallTrace(
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
        requested_model=request.model or config.default_model or "unknown",
        reported_model=None if error else request.model or config.default_model,
        started_at=now,
        ended_at=now,
        queue_seconds=None,
        first_token_seconds=None,
        validation_seconds=None,
        attempts=attempts,
        finish_reason=None if error else FinishReason.STOP,
        usage=ProviderUsage(),
        error_code=error.code.value if error else None,
        error_message=error.safe_message if error else None,
        end_reason="provider_error" if error else "completed",
        max_output_tokens=request.profile.max_output_tokens,
        transport_failure_kind=error.transport_failure_kind if error else None,
    )


class _RecordingGateway:
    def __init__(self, outcome_by_provider=None):
        self.outcome_by_provider = outcome_by_provider or {}
        self.calls = []

    async def generate(self, config, request):
        self.calls.append((config, request))
        outcome = self.outcome_by_provider.get(config.provider_id)
        if isinstance(outcome, BaseException):
            if isinstance(outcome, ProviderError):
                outcome.call_trace = _stub_trace(config, request, error=outcome)
            raise outcome
        return _StubResult("safe answer", _stub_trace(config, request))


class _RecordingScheduler:
    def __init__(self):
        self.providers = []

    @asynccontextmanager
    async def slot(self, config, speed):
        self.providers.append(config.provider_id)
        yield 0.0


def _npc_plan_review_request(*, max_output_tokens=2048):
    return ModelRequest(
        role_task=RoleTask.NPC_PLAN_REVIEW,
        messages=(ChatMessage("user", "review this NPC plan"),),
        profile=GenerationProfile(SpeedProfile.PATIENT, DepthProfile.EXHAUSTIVE, max_output_tokens),
        trace_context=TraceContext(request_id="review-request", parent_trace="review-parent"),
    )


def test_role_route_legacy_roundtrip_omits_empty_fallbacks_and_validates_targets():
    route = RoleRoute.model_validate({"provider_id": "primary", "model": "fixture"})
    assert route.model_dump() == {"provider_id": "primary", "model": "fixture"}

    with pytest.raises(ValidationError, match="primary provider"):
        RoleRoute.model_validate({"fallbacks": [{"provider_id": "fallback"}]})
    with pytest.raises(ValidationError, match="repeat"):
        RoleRoute.model_validate({
            "provider_id": "primary", "model": "fixture",
            "fallbacks": [{"provider_id": "primary", "model": "fixture"}],
        })
    with pytest.raises(ValidationError):
        RoleRoute.model_validate({
            "provider_id": "primary",
            "fallbacks": [{"provider_id": f"fallback-{index}"} for index in range(4)],
        })


def test_settings_validate_fallback_provider_references_and_effective_duplicates(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    primary = store.save_provider(user, provider_document())
    disabled = store.save_provider(user, provider_document() | {"enabled": False})

    with pytest.raises(Conflict, match="disabled"):
        store.save_settings(user, {
            "speed": "balanced", "depth": "deep", "local_only": False,
            "routes": {"gm_answer": {
                "provider_id": primary["provider_id"],
                "fallbacks": [{"provider_id": disabled["provider_id"]}],
            }},
        })
    with pytest.raises(NotFound):
        store.save_settings(user, {
            "speed": "balanced", "depth": "deep", "local_only": False,
            "routes": {"gm_answer": {
                "provider_id": primary["provider_id"],
                "fallbacks": [{"provider_id": "missing-provider"}],
            }},
        })
    with pytest.raises(ValueError, match="repeat"):
        store.save_settings(user, {
            "speed": "balanced", "depth": "deep", "local_only": False,
            "routes": {"gm_answer": {
                "provider_id": primary["provider_id"], "model": "fixture",
                "fallbacks": [{"provider_id": primary["provider_id"]}],
            }},
        })


@pytest.mark.asyncio
async def test_real_gateway_fallback_sends_target_caps_and_audits_both_http_calls(tmp_path):
    sent = []

    def handle(request):
        payload = json.loads(request.content)
        sent.append((request.url.host, payload))
        if request.url.host == "primary.example":
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return httpx.Response(200, json={
            "id": "fallback-call",
            "model": "fallback-reported",
            "choices": [{"message": {"content": "fallback succeeded"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        })

    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    primary = store.save_provider(user, provider_document("https://primary.example/v1") | {"max_retries": 0})
    fallback = store.save_provider(user, provider_document("https://fallback.example/v1") | {"max_retries": 0})
    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {"gm_answer": {
            "provider_id": primary["provider_id"], "model": "primary-model", "max_output_tokens": 101,
            "fallbacks": [{
                "provider_id": fallback["provider_id"], "model": "fallback-model", "max_output_tokens": 202,
            }],
        }},
    })
    gateway = ModelGateway(transports={ProviderKind.OPENAI_COMPATIBLE: httpx.MockTransport(handle)})

    result = await ModelOperations(store, gateway).generate(
        user,
        ModelRequest(
            role_task=RoleTask.GM_ANSWER,
            messages=(ChatMessage("user", "question"),),
            trace_context=TraceContext(request_id="http-root", parent_trace="chat-parent"),
        ),
    )

    assert result.normalized_output == "fallback succeeded"
    assert [(host, payload["model"], payload["max_tokens"]) for host, payload in sent] == [
        ("primary.example", "primary-model", 101),
        ("fallback.example", "fallback-model", 202),
    ]
    traces = {trace["request_id"]: trace for trace in store.list_calls(user)}
    assert set(traces) == {"http-root", result.call_trace.request_id}
    assert traces["http-root"]["provider_id"] == primary["provider_id"]
    assert traces["http-root"]["error_code"] == ProviderErrorCode.SERVER.value
    assert traces["http-root"]["attempts"][0]["status_code"] == 503
    assert traces[result.call_trace.request_id]["provider_id"] == fallback["provider_id"]
    assert traces[result.call_trace.request_id]["parent_trace"] == "chat-parent"
    assert traces[result.call_trace.request_id]["reported_model"] == "fallback-reported"
    assert traces[result.call_trace.request_id]["usage"]["total_tokens"] == 13


@pytest.mark.asyncio
async def test_ordered_fallback_preserves_request_profile_correlation_and_call_traces(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    primary = store.save_provider(user, provider_document("http://127.0.0.1:9101/v1"))
    fallback = store.save_provider(user, provider_document("http://127.0.0.1:9102/v1"))
    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {"gm_answer": {
            "provider_id": primary["provider_id"], "model": "primary-model", "max_output_tokens": 111,
            "fallbacks": [{
                "provider_id": fallback["provider_id"], "model": "fallback-model", "max_output_tokens": 222,
            }],
        }},
    })
    gateway = _RecordingGateway({
        primary["provider_id"]: ProviderError(
            ProviderErrorCode.NETWORK,
            "Connection closed before response",
            retryable=False,
            transport_failure_kind=TransportFailureKind.REMOTE_PROTOCOL,
            possibly_billed=True,
        ),
    })
    operations = ModelOperations(store, gateway)
    original = TraceContext(
        request_id="root-request", parent_trace="existing-parent", world_id="world",
        branch_id="branch", job_id="job", correlation_id="chat-session",
    )
    request = ModelRequest(
        role_task=RoleTask.GM_ANSWER,
        messages=(ChatMessage("user", "private question"),),
        profile=GenerationProfile(SpeedProfile.PATIENT, DepthProfile.EXHAUSTIVE, 17),
        trace_context=original,
    )

    result = await operations.generate(user, request)

    assert result.normalized_output == "safe answer"
    assert [config.provider_id for config, _ in gateway.calls] == [primary["provider_id"], fallback["provider_id"]]
    first_request, second_request = (entry[1] for entry in gateway.calls)
    assert (first_request.model, first_request.profile.max_output_tokens) == ("primary-model", 111)
    assert (second_request.model, second_request.profile.max_output_tokens) == ("fallback-model", 222)
    assert all(item.profile.speed is SpeedProfile.PATIENT for _, item in gateway.calls)
    assert all(item.profile.depth is DepthProfile.EXHAUSTIVE for _, item in gateway.calls)
    assert first_request.trace_context.request_id == "root-request"
    assert first_request.trace_context.parent_trace == "existing-parent"
    assert second_request.trace_context.request_id != first_request.trace_context.request_id
    assert second_request.trace_context.parent_trace == "existing-parent"
    assert second_request.trace_context.world_id == "world"
    assert second_request.trace_context.branch_id == "branch"
    assert second_request.trace_context.job_id == "job"
    assert second_request.trace_context.correlation_id == "chat-session"

    traces = {trace["request_id"]: trace for trace in store.list_calls(user)}
    assert set(traces) == {first_request.trace_context.request_id, second_request.trace_context.request_id}
    assert traces["root-request"]["provider_id"] == primary["provider_id"]
    assert traces["root-request"]["error_code"] == ProviderErrorCode.NETWORK.value
    assert traces["root-request"]["transport_failure_kind"] == TransportFailureKind.REMOTE_PROTOCOL.value
    assert traces["root-request"]["attempts"][0]["possibly_billed"] is True
    assert traces[second_request.trace_context.request_id]["provider_id"] == fallback["provider_id"]
    assert traces[second_request.trace_context.request_id]["end_reason"] == "completed"


@pytest.mark.asyncio
async def test_fallback_request_links_to_original_when_caller_has_no_parent(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    primary = store.save_provider(user, provider_document("http://127.0.0.1:9111/v1"))
    fallback = store.save_provider(user, provider_document("http://127.0.0.1:9112/v1"))
    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {"gm_answer": {
            "provider_id": primary["provider_id"],
            "fallbacks": [{"provider_id": fallback["provider_id"]}],
        }},
    })
    gateway = _RecordingGateway({
        primary["provider_id"]: ProviderError(ProviderErrorCode.SERVER, "temporary server failure", retryable=True),
    })
    request = ModelRequest(
        role_task=RoleTask.GM_ANSWER,
        messages=(ChatMessage("user", "question"),),
        trace_context=TraceContext(request_id="initial-without-parent"),
    )

    await ModelOperations(store, gateway).generate(user, request)

    first, second = (entry[1].trace_context for entry in gateway.calls)
    assert first.request_id == "initial-without-parent" and first.parent_trace is None
    assert second.request_id != first.request_id
    assert second.parent_trace == first.request_id


@pytest.mark.asyncio
async def test_explicit_npc_plan_review_route_and_fallback_keep_review_role(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    decision = store.save_provider(user, provider_document("http://127.0.0.1:9151/v1"))
    reviewer = store.save_provider(user, provider_document("http://127.0.0.1:9152/v1"))
    reviewer_fallback = store.save_provider(user, provider_document("http://127.0.0.1:9153/v1"))
    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {
            RoleTask.NPC_DECISION.value: {"provider_id": decision["provider_id"], "model": "decision-model"},
            RoleTask.NPC_PLAN_REVIEW.value: {
                "provider_id": reviewer["provider_id"], "model": "review-model",
                "fallbacks": [{"provider_id": reviewer_fallback["provider_id"], "model": "review-fallback"}],
            },
        },
    })
    gateway = _RecordingGateway({
        reviewer["provider_id"]: ProviderError(ProviderErrorCode.SERVER, "temporary failure", retryable=True),
    })

    await ModelOperations(store, gateway).generate(user, _npc_plan_review_request())

    assert [config.provider_id for config, _ in gateway.calls] == [
        reviewer["provider_id"], reviewer_fallback["provider_id"],
    ]
    assert all(request.role_task is RoleTask.NPC_PLAN_REVIEW for _, request in gateway.calls)
    traces = store.list_calls(user)
    assert {trace["role_task"] for trace in traces} == {RoleTask.NPC_PLAN_REVIEW.value}


@pytest.mark.asyncio
async def test_legacy_npc_plan_review_inherits_decision_route_without_settings_write(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    decision = store.save_provider(user, provider_document("http://127.0.0.1:9161/v1"))
    fallback = store.save_provider(user, provider_document("http://127.0.0.1:9162/v1"))
    settings = {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {RoleTask.NPC_DECISION.value: {
            "provider_id": decision["provider_id"],
            "fallbacks": [{"provider_id": fallback["provider_id"]}],
        }},
    }
    store.save_settings(user, settings)
    settings_before = store.settings(user)
    gateway = _RecordingGateway({
        decision["provider_id"]: ProviderError(ProviderErrorCode.SERVER, "temporary failure", retryable=True),
    })

    await ModelOperations(store, gateway).generate(user, _npc_plan_review_request())

    assert [config.provider_id for config, _ in gateway.calls] == [decision["provider_id"], fallback["provider_id"]]
    assert all(request.role_task is RoleTask.NPC_PLAN_REVIEW for _, request in gateway.calls)
    assert store.settings(user) == settings_before
    assert {trace["role_task"] for trace in store.list_calls(user)} == {RoleTask.NPC_PLAN_REVIEW.value}


@pytest.mark.asyncio
async def test_npc_plan_review_requires_route_when_both_reviewer_and_decision_are_unset(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    store.save_settings(user, {"speed": "balanced", "depth": "deep", "local_only": False, "routes": {}})
    gateway = _RecordingGateway()

    with pytest.raises(ApplicationFailure) as caught:
        await ModelOperations(store, gateway).generate(user, _npc_plan_review_request())

    assert caught.value.code == "MODEL_ROUTE_REQUIRED"
    assert gateway.calls == []
    assert store.list_calls(user) == []


@pytest.mark.asyncio
async def test_inherited_npc_plan_review_route_obeys_local_only_before_scheduler(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    cloud = store.save_provider(user, provider_document("https://cloud.example/v1"))
    local = store.save_provider(user, provider_document("http://127.0.0.1:9172/v1"))
    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": True,
        "routes": {RoleTask.NPC_DECISION.value: {
            "provider_id": cloud["provider_id"],
            "fallbacks": [{"provider_id": local["provider_id"]}],
        }},
    })
    gateway = _RecordingGateway()
    scheduler = _RecordingScheduler()

    await ModelOperations(store, gateway, scheduler=scheduler).generate(user, _npc_plan_review_request())

    assert [config.provider_id for config, _ in gateway.calls] == [local["provider_id"]]
    assert scheduler.providers == [local["provider_id"]]
    assert gateway.calls[0][1].role_task is RoleTask.NPC_PLAN_REVIEW
    assert gateway.calls[0][1].data_policy is DataPolicy.LOCAL_ONLY
    assert store.list_calls(user)[0]["role_task"] == RoleTask.NPC_PLAN_REVIEW.value


@pytest.mark.asyncio
async def test_npc_plan_review_route_caps_cannot_exceed_request_but_can_reduce_it(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    primary = store.save_provider(user, provider_document("http://127.0.0.1:9181/v1"))
    fallback = store.save_provider(user, provider_document("http://127.0.0.1:9182/v1"))
    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {RoleTask.NPC_PLAN_REVIEW.value: {
            "provider_id": primary["provider_id"], "max_output_tokens": 5000,
            "fallbacks": [{"provider_id": fallback["provider_id"], "max_output_tokens": 1024}],
        }},
    })
    gateway = _RecordingGateway({
        primary["provider_id"]: ProviderError(ProviderErrorCode.SERVER, "temporary failure", retryable=True),
    })

    await ModelOperations(store, gateway).generate(user, _npc_plan_review_request(max_output_tokens=3000))

    assert [request.profile.max_output_tokens for _, request in gateway.calls] == [3000, 1024]
    assert all(request.profile.speed is SpeedProfile.PATIENT for _, request in gateway.calls)
    assert all(request.profile.depth is DepthProfile.EXHAUSTIVE for _, request in gateway.calls)


@pytest.mark.asyncio
async def test_only_internal_npc_reflection_memory_summary_schema_clamps_route_cap(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    provider = store.save_provider(user, provider_document("http://127.0.0.1:9191/v1"))
    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {RoleTask.MEMORY_SUMMARY.value: {
            "provider_id": provider["provider_id"], "max_output_tokens": 1200,
        }},
    })
    gateway = _RecordingGateway()
    operations = ModelOperations(store, gateway)
    reflection_request = ModelRequest(
        role_task=RoleTask.MEMORY_SUMMARY,
        schema_ref="npc_reflection_goal_completion_v1",
        output_schema={"type": "object", "properties": {"outlook": {"enum": ["defer"]}}},
        messages=(ChatMessage("user", "authorized evidence"),),
        profile=GenerationProfile(SpeedProfile.BALANCED, DepthProfile.DEEP, 768),
    )
    generic_request = ModelRequest(
        role_task=RoleTask.MEMORY_SUMMARY,
        schema_ref="ordinary_memory_summary_v1",
        messages=(ChatMessage("user", "ordinary summary"),),
        profile=GenerationProfile(SpeedProfile.BALANCED, DepthProfile.DEEP, 128),
    )

    await operations.generate(user, reflection_request)
    await operations.generate(user, generic_request)

    assert [request.profile.max_output_tokens for _, request in gateway.calls] == [768, 1200]
    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {RoleTask.MEMORY_SUMMARY.value: {
            "provider_id": provider["provider_id"], "max_output_tokens": 256,
        }},
    })
    await operations.generate(user, reflection_request)
    assert gateway.calls[2][1].profile.max_output_tokens == 256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        (ProviderErrorCode.NETWORK, True),
        (ProviderErrorCode.TIMEOUT, True),
        (ProviderErrorCode.RATE_LIMITED, True),
        (ProviderErrorCode.SERVER, True),
        (ProviderErrorCode.UNAVAILABLE, False),
    ],
)
async def test_retryable_provider_error_classes_advance_in_configured_order(tmp_path, code, retryable):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    primary = store.save_provider(user, provider_document("http://127.0.0.1:9113/v1"))
    fallback = store.save_provider(user, provider_document("http://127.0.0.1:9114/v1"))
    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {"gm_answer": {
            "provider_id": primary["provider_id"],
            "fallbacks": [{"provider_id": fallback["provider_id"]}],
        }},
    })
    gateway = _RecordingGateway({primary["provider_id"]: ProviderError(code, "temporary failure", retryable=retryable)})

    result = await ModelOperations(store, gateway).generate(
        user, ModelRequest(role_task=RoleTask.GM_ANSWER, messages=(ChatMessage("user", "question"),)),
    )

    assert result.normalized_output == "safe answer"
    assert [config.provider_id for config, _ in gateway.calls] == [primary["provider_id"], fallback["provider_id"]]
    assert len(store.list_calls(user)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "retryable", "transport_kind"),
    [
        (ProviderErrorCode.NETWORK, False, TransportFailureKind.REQUEST),
        (ProviderErrorCode.AUTHENTICATION, False, None),
        (ProviderErrorCode.CLIENT, False, None),
        (ProviderErrorCode.SECRET, False, None),
        (ProviderErrorCode.INVALID_OUTPUT, False, None),
        (ProviderErrorCode.TRUNCATED, False, None),
        (ProviderErrorCode.UNSUPPORTED_CAPABILITY, False, None),
    ],
)
async def test_hard_failures_do_not_advance_to_fallback(tmp_path, code, retryable, transport_kind):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    primary = store.save_provider(user, provider_document("http://127.0.0.1:9121/v1"))
    fallback = store.save_provider(user, provider_document("http://127.0.0.1:9122/v1"))
    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {"gm_answer": {
            "provider_id": primary["provider_id"],
            "fallbacks": [{"provider_id": fallback["provider_id"]}],
        }},
    })
    gateway = _RecordingGateway({primary["provider_id"]: ProviderError(
        code, "configured primary failure", retryable=retryable, transport_failure_kind=transport_kind,
    )})

    with pytest.raises(ProviderError) as caught:
        await ModelOperations(store, gateway).generate(
            user, ModelRequest(role_task=RoleTask.GM_ANSWER, messages=(ChatMessage("user", "question"),)),
        )

    assert caught.value.code is code
    assert [config.provider_id for config, _ in gateway.calls] == [primary["provider_id"]]
    assert len(store.list_calls(user)) == 1


@pytest.mark.asyncio
async def test_local_only_filters_cloud_primary_before_scheduler_and_uses_local_fallback(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    cloud = store.save_provider(user, provider_document("https://cloud.example/v1"))
    local = store.save_provider(user, provider_document("http://127.0.0.1:9132/v1"))
    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": True,
        "routes": {"gm_answer": {
            "provider_id": cloud["provider_id"],
            "fallbacks": [{"provider_id": local["provider_id"]}],
        }},
    })
    gateway = _RecordingGateway()
    scheduler = _RecordingScheduler()

    await ModelOperations(store, gateway, scheduler=scheduler).generate(
        user, ModelRequest(role_task=RoleTask.GM_ANSWER, messages=(ChatMessage("user", "question"),)),
    )

    assert [config.provider_id for config, _ in gateway.calls] == [local["provider_id"]]
    assert scheduler.providers == [local["provider_id"]]
    assert gateway.calls[0][1].data_policy is DataPolicy.LOCAL_ONLY
    assert [trace["provider_id"] for trace in store.list_calls(user)] == [local["provider_id"]]


@pytest.mark.asyncio
async def test_disabled_saved_primary_is_skipped_only_when_enabled_fallback_exists(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    primary = store.save_provider(user, provider_document("http://127.0.0.1:9141/v1"))
    fallback = store.save_provider(user, provider_document("http://127.0.0.1:9142/v1"))
    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {"gm_answer": {
            "provider_id": primary["provider_id"],
            "fallbacks": [{"provider_id": fallback["provider_id"]}],
        }},
    })
    store.save_provider(user, provider_document("http://127.0.0.1:9141/v1") | {"enabled": False}, primary["provider_id"])
    gateway = _RecordingGateway()

    await ModelOperations(store, gateway).generate(
        user, ModelRequest(role_task=RoleTask.GM_ANSWER, messages=(ChatMessage("user", "question"),)),
    )

    assert [config.provider_id for config, _ in gateway.calls] == [fallback["provider_id"]]

    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {"gm_answer": {"provider_id": fallback["provider_id"]}},
    })
    store.save_provider(user, provider_document("http://127.0.0.1:9142/v1") | {"enabled": False}, fallback["provider_id"])
    with pytest.raises(ApplicationFailure) as caught:
        await ModelOperations(store, _RecordingGateway()).generate(
            user, ModelRequest(role_task=RoleTask.GM_ANSWER, messages=(ChatMessage("user", "question"),)),
        )
    assert caught.value.code == "PROVIDER_DISABLED"


@pytest.mark.asyncio
async def test_deleted_saved_primary_is_skipped_when_enabled_fallback_exists_and_preserved_for_single_route(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    primary = store.save_provider(user, provider_document("http://127.0.0.1:9143/v1"))
    fallback = store.save_provider(user, provider_document("http://127.0.0.1:9144/v1"))
    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {"gm_answer": {
            "provider_id": primary["provider_id"],
            "fallbacks": [{"provider_id": fallback["provider_id"]}],
        }},
    })
    with store.connect() as db:
        db.execute(
            "DELETE FROM providers WHERE tenant=? AND principal=? AND id=?",
            (*store.identity(user), primary["provider_id"]),
        )
    gateway = _RecordingGateway()

    await ModelOperations(store, gateway).generate(
        user, ModelRequest(role_task=RoleTask.GM_ANSWER, messages=(ChatMessage("user", "question"),)),
    )

    assert [config.provider_id for config, _ in gateway.calls] == [fallback["provider_id"]]

    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {"gm_answer": {"provider_id": fallback["provider_id"]}},
    })
    with store.connect() as db:
        db.execute(
            "DELETE FROM providers WHERE tenant=? AND principal=? AND id=?",
            (*store.identity(user), fallback["provider_id"]),
        )
    with pytest.raises(NotFound):
        await ModelOperations(store, _RecordingGateway()).generate(
            user, ModelRequest(role_task=RoleTask.GM_ANSWER, messages=(ChatMessage("user", "question"),)),
        )


@pytest.mark.asyncio
async def test_cancellation_never_advances_to_fallback(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    primary = store.save_provider(user, provider_document("http://127.0.0.1:9151/v1"))
    fallback = store.save_provider(user, provider_document("http://127.0.0.1:9152/v1"))
    store.save_settings(user, {
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {"gm_answer": {
            "provider_id": primary["provider_id"],
            "fallbacks": [{"provider_id": fallback["provider_id"]}],
        }},
    })
    gateway = _RecordingGateway({primary["provider_id"]: asyncio.CancelledError()})

    with pytest.raises(asyncio.CancelledError):
        await ModelOperations(store, gateway).generate(
            user, ModelRequest(role_task=RoleTask.GM_ANSWER, messages=(ChatMessage("user", "question"),)),
        )

    assert [config.provider_id for config, _ in gateway.calls] == [primary["provider_id"]]


def test_configuration_persists_and_is_account_scoped(tmp_path):
    path = tmp_path / "ops.db"
    store, user = OperationsStore(path), scope()
    provider = store.save_provider(user, provider_document())
    settings = {"speed": "fast", "depth": "deep", "local_only": True,
                "routes": {"gm_answer": {"provider_id": provider["provider_id"], "model": "fixture"}}}
    store.save_settings(user, settings)
    reopened = OperationsStore(path)
    assert reopened.settings(user) == settings | {"revision": 1}
    assert reopened.provider(user, provider["provider_id"])["name"] == "Test endpoint"
    another_user = AccessScope(tenant_id=user.tenant_id, user_id=uuid4())
    assert reopened.list_providers(another_user) == []
    assert reopened.settings(another_user)["routes"] == {}
    assert reopened.settings(another_user)["revision"] == 0
    with pytest.raises(ValueError):
        store.save_provider(user, provider_document("http://remote.example/v1"))


def test_settings_compare_and_swap_serializes_concurrent_writers_on_legacy_row(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    initial = {"speed": "balanced", "depth": "deep", "local_only": False, "routes": {}}
    with store.connect() as db:
        db.execute("INSERT INTO preferences VALUES(?,?,?)", (*store.identity(user), json.dumps(initial)))
    assert store.settings(user) == initial | {"revision": 0}

    def save(patch):
        try:
            return store.save_settings(user, patch, expected_revision=0)
        except ApplicationFailure as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [{"local_only": True}, {"speed": "fast"}]))
    assert results.count("SETTINGS_STALE") == 1
    assert next(result for result in results if isinstance(result, dict)) == store.settings(user)
    assert store.settings(user)["revision"] == 1


def test_http_settings_refresh_and_reapply_only_the_stale_tabs_intent(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    operations = ModelOperations(store, ModelGateway())
    session = LocalSession(str(user.user_id), str(user.tenant_id), frozenset({"http://testserver"}))
    app = create_app(None, session, operations=operations)
    with TestClient(app) as client:
        client.get("/api/v1/session")
        initial = client.get("/api/v1/settings").json()
        route = {"gm_answer": {"max_output_tokens": 128}}
        updated = client.put("/api/v1/settings", json=initial | {"local_only": True, "routes": route})
        assert updated.status_code == 200
        for stale in [initial, {key: value for key, value in initial.items() if key != "revision"}]:
            response = client.put("/api/v1/settings", json=stale | {"speed": "fast"})
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "SETTINGS_STALE"
        assert client.get("/api/v1/settings").json() == updated.json()

        updated = client.put("/api/v1/settings", json=updated.json() | {"speed": "fast"})
        assert updated.status_code == 200
        assert updated.json()["speed"] == "fast"
        assert updated.json()["local_only"] is True
        assert updated.json()["routes"] == route
        legacy_field_update = client.put("/api/v1/settings", json={"depth": "exhaustive"})
        assert legacy_field_update.status_code == 200
        assert legacy_field_update.json()["local_only"] is True
        assert legacy_field_update.json()["routes"] == route
        opted_in = client.put("/api/v1/settings", json={"revision": legacy_field_update.json()["revision"], "local_only": False})
        assert opted_in.status_code == 200
        assert opted_in.json()["local_only"] is False


def test_ollama_context_configuration_roundtrips_and_is_provider_specific(tmp_path):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    document = {
        "kind": "ollama", "name": "Local Ollama", "base_url": "http://127.0.0.1:11434",
        "default_model": "qwen3:8b", "enabled": True, "ollama_context_tokens": 16384,
    }

    provider = store.save_provider(user, document)
    assert provider["ollama_context_tokens"] == 16384
    reopened = OperationsStore(tmp_path / "ops.db")
    assert reopened.provider(user, provider["provider_id"])["ollama_context_tokens"] == 16384
    assert OperationsStore.config(provider).ollama_context_tokens == 16384

    unset = store.save_provider(user, {
        "kind": "ollama", "name": "Default context", "base_url": "http://127.0.0.1:11434",
    })
    assert "ollama_context_tokens" not in unset

    with pytest.raises(ValueError, match="only valid for Ollama"):
        store.save_provider(user, provider_document() | {"ollama_context_tokens": 16384})


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


def test_call_pages_are_stable_when_newer_calls_arrive_and_api_returns_next_cursor(tmp_path):
    store, user = OperationsStore(tmp_path / "ops-pages.db"), scope()
    start = datetime(2026, 11, 20, tzinfo=timezone.utc)
    expected = []
    for index in range(101):
        trace = _SyntheticCallTrace(f"call-{index:03}", start + timedelta(seconds=index // 4))
        expected.append(trace)
        store.record_trace(user, trace)
    expected_ids = [item.request_id for item in sorted(expected, key=lambda item: (item.started_at.isoformat(), item.request_id), reverse=True)]

    page = store.list_calls_page(user, limit=17)
    paged_ids = [item["request_id"] for item in page["items"]]
    assert page["has_more"] is True
    assert page["next_cursor"]

    # A new, more recent call sorts before the saved cursor and must not be
    # inserted into later pages or cause an existing row to repeat.
    store.record_trace(user, _SyntheticCallTrace("call-late", start + timedelta(days=1)))
    while page["next_cursor"]:
        page = store.list_calls_page(user, limit=17, cursor=page["next_cursor"])
        paged_ids.extend(item["request_id"] for item in page["items"])

    assert paged_ids == expected_ids
    assert len(paged_ids) == len(set(paged_ids)) == 101
    assert "call-late" not in paged_ids
    assert store.list_calls_page(scope(), limit=17)["items"] == []
    with pytest.raises(ValueError, match="Invalid call cursor"):
        store.list_calls_page(user, cursor="not-a-cursor")

    session = LocalSession(str(user.user_id), str(user.tenant_id), frozenset({"http://testserver"}))
    operations = ModelOperations(store, _RecordingGateway())
    with TestClient(create_app(None, session, operations=operations)) as client:
        client.get("/api/v1/session")
        response = client.get("/api/v1/operations/calls?limit=3")
        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 3
        assert body["has_more"] is True
        next_page = client.get("/api/v1/operations/calls", params={"limit": 3, "cursor": body["next_cursor"]})
        assert next_page.status_code == 200
        next_body = next_page.json()
        assert len(next_body["items"]) == 3
        assert not ({item["request_id"] for item in body["items"]} & {item["request_id"] for item in next_body["items"]})
        assert client.get("/api/v1/operations/calls", params={"cursor": "bad"}).status_code == 422


def test_local_policy_blocks_remote_catalogue_probe_and_generation_at_transport(tmp_path):
    outbound = []

    def handle(request):
        outbound.append((request.url.host, request.url.path, request.method, bool(request.content)))
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "fixture"}]})
        return httpx.Response(200, json={
            "model": "fixture", "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
        })

    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    local = store.save_provider(user, provider_document())
    remote = store.save_provider(user, provider_document("https://cloud.example/v1"))
    gateway = ModelGateway(transports={ProviderKind.OPENAI_COMPATIBLE: httpx.MockTransport(handle)})
    operations = ModelOperations(store, gateway)
    session = LocalSession(str(user.user_id), str(user.tenant_id), frozenset({"http://testserver"}))
    with TestClient(create_app(None, session, operations=operations)) as client:
        client.get("/api/v1/session")
        providers = client.get("/api/v1/providers").json()["items"]
        assert {item["provider_id"]: item["is_local_endpoint"] for item in providers} == {
            local["provider_id"]: True, remote["provider_id"]: False,
        }
        initial = client.get("/api/v1/settings").json()
        routes = {"gm_answer": {
            "provider_id": remote["provider_id"], "fallbacks": [{"provider_id": local["provider_id"]}],
        }}
        local_mode = client.put("/api/v1/settings", json=initial | {"local_only": True, "routes": routes}).json()
        for endpoint, method in [
            ("/api/v1/models?provider_id=" + remote["provider_id"], client.get),
            (f"/api/v1/providers/{remote['provider_id']}/probe", client.post),
        ]:
            response = method(endpoint)
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "MODEL_LOCAL_ONLY"
        assert outbound == []
        assert client.post(f"/api/v1/providers/{local['provider_id']}/probe").status_code == 200
        request = ModelRequest(role_task=RoleTask.GM_ANSWER, messages=(ChatMessage("user", "fixture input"),))
        assert asyncio.run(operations.generate(user, request)).normalized_output == "answer"
        assert outbound == [
            ("127.0.0.1", "/v1/models", "GET", False),
            ("127.0.0.1", "/v1/chat/completions", "POST", True),
        ]
        assert client.put("/api/v1/settings", json=local_mode | {"local_only": False}).status_code == 200
        assert client.get("/api/v1/models", params={"provider_id": remote["provider_id"]}).status_code == 200
        assert outbound[-1] == ("cloud.example", "/v1/models", "GET", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_url", ["https://primary.example/v1", "http://127.0.0.1:9999/v1"])
async def test_enabling_local_policy_during_call_blocks_next_cloud_attempt(tmp_path, primary_url):
    store, user = OperationsStore(tmp_path / "ops.db"), scope()
    primary = store.save_provider(user, provider_document(primary_url) | {"max_retries": 1})
    fallback = store.save_provider(user, provider_document("https://fallback.example/v1"))
    routes = {"gm_answer": {
        "provider_id": primary["provider_id"], "fallbacks": [{"provider_id": fallback["provider_id"]}],
    }}
    store.save_settings(user, {"routes": routes})
    outbound_hosts = []

    async def handle(request):
        outbound_hosts.append(request.url.host)
        store.save_settings(user, {"local_only": True})
        return httpx.Response(503, json={"error": "temporarily unavailable"})

    gateway = ModelGateway(
        transports={ProviderKind.OPENAI_COMPATIBLE: httpx.MockTransport(handle)},
        sleeper=lambda _: asyncio.sleep(0),
    )
    operations = ModelOperations(store, gateway)
    with pytest.raises(ProviderError) as raised:
        await operations.generate(user, ModelRequest(
            role_task=RoleTask.GM_ANSWER, messages=(ChatMessage("user", "fixture input"),),
        ))
    assert raised.value.code is ProviderErrorCode.UNAVAILABLE
    assert raised.value.call_trace.data_policy is DataPolicy.LOCAL_ONLY
    assert outbound_hosts == (["primary.example"] if primary_url.startswith("https:") else ["127.0.0.1"] * 2)


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
        response = client.post("/api/v1/providers", json=provider_document() | {"secret_ref": "os:stale-provider-reference"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PROVIDER_CREDENTIAL_REFERENCE_CONFLICT"
        response = client.post("/api/v1/providers", json=provider_document() | {"secret_ref": "env:TEST_MODEL_KEY"})
        assert response.status_code == 201
        assert response.json()["secret_ref"] == "env:TEST_MODEL_KEY"
        ollama = client.post("/api/v1/providers", json={
            "kind": "ollama", "name": "Local Ollama", "base_url": "http://127.0.0.1:11434",
            "ollama_context_tokens": 16384,
        })
        assert ollama.status_code == 201
        assert ollama.json()["ollama_context_tokens"] == 16384
        invalid_context = client.post("/api/v1/providers", json=provider_document() | {"ollama_context_tokens": 16384})
        assert invalid_context.status_code == 422
        for value in (0, -1, 2_147_483_648, 8192.0, "8192", True):
            invalid_context = client.post("/api/v1/providers", json={
                "kind": "ollama", "name": "Bad context", "base_url": "http://127.0.0.1:11434",
                "ollama_context_tokens": value,
            })
            assert invalid_context.status_code == 422
        response = client.put("/api/v1/settings", json={"routes": {"typo_role": {}}})
        assert response.status_code == 422
        assert client.get("/api/v1/settings").json()["routes"] == {}
