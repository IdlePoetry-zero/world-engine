from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

import pytest

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
from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode
from world_engine.application.model_scheduler import EndpointAdmissionScheduler
from world_engine.application.operations import ModelOperations
from world_engine.domain.models import AccessScope


def _config(provider_id: str, kind: ProviderKind, base_url: str) -> ProviderConfig:
    return ProviderConfig(provider_id=provider_id, kind=kind, base_url=base_url)


def _trace(provider_id: str, kind: ProviderKind) -> CallTrace:
    now = utc_now()
    return CallTrace(
        request_id=str(uuid4()),
        parent_trace=None,
        world_id=None,
        branch_id=None,
        job_id=None,
        role_task=RoleTask.NPC_DECISION,
        speed_profile=SpeedProfile.BALANCED,
        depth_profile=DepthProfile.STANDARD,
        data_policy=DataPolicy.CLOUD_ALLOWED,
        provider_id=provider_id,
        provider_kind=kind,
        requested_model="test-model",
        reported_model=None,
        started_at=now,
        ended_at=now,
        queue_seconds=None,
        first_token_seconds=None,
        validation_seconds=None,
        attempts=(),
        finish_reason=FinishReason.STOP,
        usage=ProviderUsage(),
    )


def _result(provider_id: str, kind: ProviderKind) -> ModelResult:
    return ModelResult(
        normalized_output='{"option_id":"one"}',
        provider_usage=ProviderUsage(),
        estimated_usage=None,
        cost=CostRecord(),
        finish_reason=FinishReason.STOP,
        provider_request_id=None,
        requested_model="test-model",
        reported_model=None,
        timings={"total_seconds": 0.01},
        validation={"valid": True},
        call_trace=_trace(provider_id, kind),
    )


@pytest.mark.asyncio
async def test_aliases_share_cloud_slots_and_operation_trace_records_queue_wait():
    configs = {
        "cloud-a": _config("cloud-a", ProviderKind.DEEPSEEK, "https://model.example/v1"),
        "cloud-b": _config("cloud-b", ProviderKind.OPENAI_COMPATIBLE, "https://MODEL.example:443/compatible"),
    }

    class Store:
        traces = []

        def settings(self, scope):
            provider_id = "cloud-a" if scope.user_id.int % 2 else "cloud-b"
            return {"routes": {"npc_decision": {"provider_id": provider_id}}, "local_only": False}

        def provider(self, scope, provider_id):
            config = configs[provider_id]
            return config.public_dict() | {"enabled": True}

        def config(self, document):
            return configs[document["provider_id"]]

        def record_trace(self, scope, trace):
            self.traces.append(trace)

    class Gateway:
        active = 0
        max_active = 0
        guard = threading.Lock()

        async def generate(self, config, request):
            assert request.profile == GenerationProfile(
                speed=SpeedProfile.FAST,
                depth=DepthProfile.EXHAUSTIVE,
            )
            assert request.temperature == 0.6
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.03)
                return _result(config.provider_id, config.kind)
            finally:
                with self.guard:
                    self.active -= 1

    store, gateway = Store(), Gateway()
    operations = ModelOperations(store, gateway, scheduler=EndpointAdmissionScheduler())
    request = ModelRequest(
        role_task=RoleTask.NPC_DECISION,
        messages=(ChatMessage("user", "Choose one."),),
        profile=GenerationProfile(speed=SpeedProfile.FAST, depth=DepthProfile.EXHAUSTIVE),
        temperature=0.6,
    )

    scopes = [AccessScope(tenant_id=uuid4(), user_id=uuid4()) for _ in range(6)]
    results = await asyncio.gather(*(operations.generate(scope, request) for scope in scopes))

    assert gateway.max_active == 4  # bounded cloud admission; the sixth call waits
    assert all(result.call_trace.queue_seconds is not None for result in results)
    assert any(result.call_trace.queue_seconds > 0 for result in results)
    assert len(store.traces) == len(scopes)
    assert all(trace.queue_seconds is not None for trace in store.traces)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["provider_error", "provider_cancel", "queue_cancel"])
async def test_operation_records_queue_time_for_failures_and_cancellations(failure_mode):
    config = _config("p", ProviderKind.DEEPSEEK, "https://model.example")

    class Store:
        traces = []

        def settings(self, scope):
            return {"routes": {"npc_decision": {"provider_id": "p"}}, "local_only": False}

        def provider(self, scope, provider_id):
            return config.public_dict() | {"enabled": True}

        def config(self, document):
            return config

        def record_trace(self, scope, trace):
            self.traces.append(trace)

    class FailingGateway:
        calls = 0

        async def generate(self, _config, _request):
            self.calls += 1
            trace = _trace(config.provider_id, config.kind)
            if failure_mode == "provider_cancel":
                error = asyncio.CancelledError()
                error.call_trace = trace
                raise error
            raise ProviderError(
                ProviderErrorCode.NETWORK,
                "safe failure",
                call_trace=trace,
            )

    store = Store()
    gateway = FailingGateway()
    scheduler = EndpointAdmissionScheduler(cloud_capacity=1)
    operations = ModelOperations(store, gateway, scheduler=scheduler)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    request = ModelRequest(
        role_task=RoleTask.NPC_DECISION,
        messages=(ChatMessage("user", "Choose one."),),
    )
    if failure_mode == "queue_cancel":
        async with scheduler.slot(config, SpeedProfile.BALANCED):
            operation = asyncio.create_task(operations.generate(scope, request))
            await asyncio.sleep(0)
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        assert gateway.calls == 0
        assert store.traces[0].request_id == request.trace_context.request_id
        assert store.traces[0].end_reason == "cancelled" and store.traces[0].attempts == ()
    else:
        exception_type = asyncio.CancelledError if failure_mode == "provider_cancel" else ProviderError
        with pytest.raises(exception_type):
            await operations.generate(scope, request)
        assert gateway.calls == 1

    assert len(store.traces) == 1
    assert store.traces[0].queue_seconds is not None


@pytest.mark.asyncio
async def test_ollama_loopback_aliases_and_base_paths_share_one_local_slot():
    scheduler = EndpointAdmissionScheduler()
    configs = (
        _config("a", ProviderKind.OLLAMA, "http://localhost:11434/api"),
        _config("b", ProviderKind.OLLAMA, "http://127.0.0.1:11434/v1"),
        _config("c", ProviderKind.OLLAMA, "http://[::1]:11434/"),
    )
    assert len({scheduler.endpoint_key(config) for config in configs}) == 1

    active = 0
    max_active = 0
    guard = asyncio.Lock()

    async def call(config):
        nonlocal active, max_active
        async with scheduler.slot(config, SpeedProfile.FAST):
            async with guard:
                active += 1
                max_active = max(active, max_active)
            await asyncio.sleep(0.01)
            async with guard:
                active -= 1

    await asyncio.gather(*(call(config) for config in configs))
    assert max_active == 1


@pytest.mark.asyncio
async def test_nonloopback_ollama_endpoint_is_still_single_slot():
    scheduler = EndpointAdmissionScheduler()
    config = _config("lan-ollama", ProviderKind.OLLAMA, "https://10.0.0.25:11434/api")
    active = 0
    max_active = 0
    guard = asyncio.Lock()

    async def call():
        nonlocal active, max_active
        async with scheduler.slot(config, SpeedProfile.BALANCED):
            async with guard:
                active += 1
                max_active = max(active, max_active)
            await asyncio.sleep(0.01)
            async with guard:
                active -= 1

    await asyncio.gather(*(call() for _ in range(3)))
    assert max_active == 1


@pytest.mark.asyncio
async def test_weighted_speed_queues_are_fair_and_cancellation_removes_waiter():
    scheduler = EndpointAdmissionScheduler(local_capacity=1, cloud_capacity=1)
    config = _config("local", ProviderKind.OPENAI_COMPATIBLE, "http://localhost:9123/v1")
    slot = scheduler.slot(config, SpeedProfile.BALANCED)
    await slot.__aenter__()

    order: list[SpeedProfile] = []
    speeds = [
        SpeedProfile.PATIENT,
        SpeedProfile.BALANCED,
        SpeedProfile.FAST,
        SpeedProfile.PATIENT,
        SpeedProfile.BALANCED,
        SpeedProfile.FAST,
    ]

    async def waiting_call(speed):
        async with scheduler.slot(config, speed):
            order.append(speed)

    tasks = [asyncio.create_task(waiting_call(speed)) for speed in speeds]
    for _ in range(100):
        with scheduler._lock:
            count = sum(len(queue) for queue in scheduler._endpoints[scheduler.endpoint_key(config)].queues.values())
        if count == len(tasks):
            break
        await asyncio.sleep(0.001)
    await slot.__aexit__(None, None, None)
    await asyncio.gather(*tasks)

    assert order[:4] == [
        SpeedProfile.FAST,
        SpeedProfile.BALANCED,
        SpeedProfile.FAST,
        SpeedProfile.PATIENT,
    ]
    assert SpeedProfile.PATIENT in order[:4]

    holder = scheduler.slot(config, SpeedProfile.BALANCED)
    await holder.__aenter__()
    cancelled = asyncio.create_task(waiting_call(SpeedProfile.PATIENT))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    await holder.__aexit__(None, None, None)
    await asyncio.sleep(0)
    with scheduler._lock:
        state = scheduler._endpoints[scheduler.endpoint_key(config)]
        assert state.active == 0
        assert not any(state.queues.values())


def test_scheduler_coordinates_across_repeated_and_concurrent_asyncio_loops():
    scheduler = EndpointAdmissionScheduler()
    config = _config("ollama", ProviderKind.OLLAMA, "http://localhost:11434/api")
    active = 0
    max_active = 0
    guard = threading.Lock()
    first_entered = threading.Event()
    failures: list[BaseException] = []

    def run_call(entered=None):
        async def call():
            nonlocal active, max_active
            async with scheduler.slot(config, SpeedProfile.BALANCED):
                with guard:
                    active += 1
                    max_active = max(active, max_active)
                    if entered is not None:
                        entered.set()
                await asyncio.sleep(0.06)
                with guard:
                    active -= 1

        try:
            asyncio.run(call())
        except BaseException as error:
            failures.append(error)

    first = threading.Thread(target=run_call, args=(first_entered,))
    second = threading.Thread(target=run_call)
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    first.join(timeout=3)
    second.join(timeout=3)

    # Also reuse it after both prior loops are closed.
    asyncio.run(run_call_async(scheduler, config))
    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    assert max_active == 1


async def run_call_async(scheduler, config):
    async with scheduler.slot(config, SpeedProfile.PATIENT):
        await asyncio.sleep(0)

