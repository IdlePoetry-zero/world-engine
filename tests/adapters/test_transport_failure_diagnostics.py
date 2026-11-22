from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from world_engine.adapters.models import (
    AttemptRecord,
    CallTrace,
    ChatMessage,
    DataPolicy,
    DepthProfile,
    EnvironmentSecretResolver,
    ModelGateway,
    ModelRequest,
    ProviderConfig,
    ProviderError,
    ProviderErrorCode,
    ProviderKind,
    ProviderUsage,
    RoleTask,
    SpeedProfile,
    TimeoutPolicy,
)
from world_engine.adapters.models.errors import TransportFailureKind


_DANGEROUS_MESSAGE = (
    "failed at https://private.example.invalid/v1?token=sk-internal-secret-key-123 "
    "Authorization: Bearer auth-header-secret-do-not-log"
)
_SECRET_FRAGMENTS = (
    "private.example.invalid",
    "sk-internal-secret-key-123",
    "Bearer auth-header-secret-do-not-log",
    "Authorization",
)


def _config() -> ProviderConfig:
    return ProviderConfig(
        provider_id="deepseek-test",
        kind=ProviderKind.DEEPSEEK,
        base_url="https://api.deepseek.com",
        default_model="deepseek-flash",
        secret_ref="env:TEST_DEEPSEEK_KEY",
        max_retries=0,
    )


def _request() -> ModelRequest:
    return ModelRequest(
        role_task=RoleTask.GM_ANSWER,
        messages=(ChatMessage("user", "diagnostics probe"),),
        timeout_policy=TimeoutPolicy(deadline_seconds=10.0),
    )


def _gateway(exception: BaseException, counter: dict[str, int]) -> ModelGateway:
    def handler(_request: httpx.Request) -> httpx.Response:
        counter["calls"] += 1
        raise exception

    return ModelGateway(
        secret_resolver=EnvironmentSecretResolver(
            {"TEST_DEEPSEEK_KEY"}, environ={"TEST_DEEPSEEK_KEY": "fake-provider-key"},
        ),
        transports={ProviderKind.DEEPSEEK: httpx.MockTransport(handler)},
        sleeper=lambda _seconds: None,
    )


def _exception_cases():
    return [
        pytest.param(
            lambda: httpx.ConnectTimeout(_DANGEROUS_MESSAGE),
            TransportFailureKind.CONNECT_TIMEOUT,
            ProviderErrorCode.NETWORK,
            True,
            False,
            id="connect-timeout-is-unissued",
        ),
        pytest.param(
            lambda: httpx.ConnectError(_DANGEROUS_MESSAGE),
            TransportFailureKind.CONNECT,
            ProviderErrorCode.NETWORK,
            True,
            False,
            id="connect-error-is-unissued",
        ),
        pytest.param(
            lambda: httpx.ReadTimeout(_DANGEROUS_MESSAGE),
            TransportFailureKind.READ_TIMEOUT,
            ProviderErrorCode.TIMEOUT,
            True,
            True,
            id="read-timeout-remains-possibly-billed",
        ),
        pytest.param(
            lambda: httpx.WriteTimeout(_DANGEROUS_MESSAGE),
            TransportFailureKind.WRITE_TIMEOUT,
            ProviderErrorCode.TIMEOUT,
            True,
            True,
            id="write-timeout-remains-possibly-billed",
        ),
        pytest.param(
            lambda: httpx.PoolTimeout(_DANGEROUS_MESSAGE),
            TransportFailureKind.POOL_TIMEOUT,
            ProviderErrorCode.TIMEOUT,
            True,
            True,
            id="pool-timeout-retains-existing-billing-semantics",
        ),
        pytest.param(
            lambda: httpx.RemoteProtocolError(_DANGEROUS_MESSAGE),
            TransportFailureKind.REMOTE_PROTOCOL,
            ProviderErrorCode.NETWORK,
            False,
            None,
            id="remote-protocol-remains-non-retryable-and-ambiguous",
        ),
        pytest.param(
            lambda: httpx.ReadError(_DANGEROUS_MESSAGE),
            TransportFailureKind.NETWORK,
            ProviderErrorCode.NETWORK,
            True,
            None,
            id="generic-network-error-remains-ambiguous",
        ),
        pytest.param(
            lambda: httpx.RequestError(_DANGEROUS_MESSAGE),
            TransportFailureKind.REQUEST,
            ProviderErrorCode.NETWORK,
            False,
            None,
            id="generic-request-error-remains-non-retryable",
        ),
    ]


def _assert_safe_failure(error, kind, code, retryable, possibly_billed):
    assert error.transport_failure_kind is kind
    assert error.code is code
    assert error.retryable is retryable
    assert error.possibly_billed is possibly_billed
    assert error.safe_message == str(error)
    assert error.call_trace is not None
    assert error.call_trace.transport_failure_kind is kind
    assert len(error.call_trace.attempts) == 1
    attempt = error.call_trace.attempts[0]
    assert attempt.transport_failure_kind is kind
    assert attempt.error_code == code.value
    assert attempt.retryable is retryable
    assert attempt.possibly_billed is possibly_billed

    serialized = json.dumps(error.call_trace.to_public_dict(), ensure_ascii=False)
    for fragment in _SECRET_FRAGMENTS:
        assert fragment not in error.safe_message
        assert fragment not in error.call_trace.error_message
        assert fragment not in serialized
    assert error.call_trace.to_public_dict()["transport_failure_kind"] == kind.value
    assert error.call_trace.to_public_dict()["attempts"][0]["transport_failure_kind"] == kind.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_factory", "kind", "code", "retryable", "possibly_billed"),
    _exception_cases(),
)
async def test_generate_records_safe_transport_phase_on_error_attempt_and_final_trace(
    exception_factory, kind, code, retryable, possibly_billed,
):
    counter = {"calls": 0}
    gateway = _gateway(exception_factory(), counter)

    with pytest.raises(ProviderError) as caught:
        await gateway.generate(_config(), _request())

    assert counter["calls"] == 1
    _assert_safe_failure(caught.value, kind, code, retryable, possibly_billed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_factory", "kind", "code", "retryable", "possibly_billed"),
    _exception_cases(),
)
async def test_stream_records_safe_transport_phase_on_error_attempt_and_final_trace(
    exception_factory, kind, code, retryable, possibly_billed,
):
    counter = {"calls": 0}
    gateway = _gateway(exception_factory(), counter)
    observed_events = []

    with pytest.raises(ProviderError) as caught:
        async for event in gateway.stream(_config(), _request()):
            observed_events.append(event)

    assert counter["calls"] == 1
    assert observed_events and observed_events[0].kind.value == "started"
    _assert_safe_failure(caught.value, kind, code, retryable, possibly_billed)


def test_provider_error_rejects_arbitrary_transport_text_and_old_trace_fields_default_to_none():
    error = ProviderError(
        ProviderErrorCode.NETWORK,
        "fixed safe message",
        transport_failure_kind=_DANGEROUS_MESSAGE,
    )
    assert error.transport_failure_kind is None

    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # This represents an older serialized trace with no transport diagnostic keys.
    old_attempt = AttemptRecord(
        attempt_number=1,
        started_at=started,
        ended_at=started,
        elapsed_seconds=0.0,
        outcome="error",
        error_code=ProviderErrorCode.NETWORK.value,
    )
    old_trace = CallTrace(
        request_id="legacy-call",
        parent_trace=None,
        world_id=None,
        branch_id=None,
        job_id=None,
        role_task=RoleTask.GM_ANSWER,
        speed_profile=SpeedProfile.BALANCED,
        depth_profile=DepthProfile.STANDARD,
        data_policy=DataPolicy.CLOUD_ALLOWED,
        provider_id="deepseek-test",
        provider_kind=ProviderKind.DEEPSEEK,
        requested_model="deepseek-flash",
        reported_model=None,
        started_at=started,
        ended_at=started,
        queue_seconds=None,
        first_token_seconds=None,
        validation_seconds=None,
        attempts=(old_attempt,),
        finish_reason=None,
        usage=ProviderUsage(),
    )
    assert old_attempt.transport_failure_kind is None
    assert old_trace.transport_failure_kind is None
    assert old_trace.to_public_dict()["transport_failure_kind"] is None

    old_json = json.loads(json.dumps(old_trace.to_public_dict()))
    old_json.pop("transport_failure_kind")
    old_json["attempts"][0].pop("transport_failure_kind")
    reloaded_old_trace = CallTrace(**old_json)
    assert reloaded_old_trace.transport_failure_kind is None
    assert "transport_failure_kind" not in reloaded_old_trace.attempts[0]
