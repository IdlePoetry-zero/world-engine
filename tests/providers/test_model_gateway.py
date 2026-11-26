"""Mock-transport provider contract tests; no live API or provider credentials are used."""

from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from world_engine.adapters.models import (
    AnthropicMessagesAdapter,
    Capability,
    CapabilityState,
    ChatMessage,
    CostState,
    DataPolicy,
    DeepSeekAdapter,
    DepthProfile,
    EnvironmentSecretResolver,
    GenerationProfile,
    ModelGateway,
    ModelRequest,
    PriceVersion,
    ProviderConfig,
    ProviderError,
    ProviderErrorCode,
    ProviderKind,
    ProviderUsage,
    ProviderTarget,
    RoleRoutingConfig,
    RoleTask,
    SpeedProfile,
    StreamEventKind,
    TimeoutPolicy,
    UsageSource,
    default_deepseek_routing,
    execution_profile,
)
from world_engine.adapters.models.costs import estimate_cost
from world_engine.adapters.models.ollama import OllamaAdapter


def _deepseek_config(**kwargs) -> ProviderConfig:
    return ProviderConfig(
        provider_id="deepseek-test",
        kind=ProviderKind.DEEPSEEK,
        base_url="https://api.deepseek.com",
        default_model="deepseek-flash",
        secret_ref="env:TEST_DEEPSEEK_KEY",
        max_retries=kwargs.pop("max_retries", 0),
        **kwargs,
    )


def _text_request(**kwargs) -> ModelRequest:
    return ModelRequest(
        role_task=RoleTask.GM_ANSWER,
        messages=(ChatMessage("user", "What happened?"),),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_deepseek_chat_completions_generation_normalizes_usage_and_redacts_secret():
    supplied_secret = "provider-test-token-do-not-log"
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["auth"] = request.headers.get("Authorization")
        observed["payload"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "cmpl-test-1",
            "model": "deepseek-flash",
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "A brief answer."}}],
            "usage": {"prompt_tokens": 14, "completion_tokens": 4, "total_tokens": 18},
        })

    resolver = EnvironmentSecretResolver({"TEST_DEEPSEEK_KEY"}, environ={"TEST_DEEPSEEK_KEY": supplied_secret})
    gateway = ModelGateway(secret_resolver=resolver, transports={ProviderKind.DEEPSEEK: httpx.MockTransport(handler)})
    result = await gateway.generate(_deepseek_config(), _text_request(profile=GenerationProfile(speed=SpeedProfile.FAST, depth=DepthProfile.DEEP, max_output_tokens=64)))

    assert observed["url"] == "https://api.deepseek.com/chat/completions"
    assert observed["auth"] == f"Bearer {supplied_secret}"
    assert observed["payload"]["model"] == "deepseek-flash"
    assert observed["payload"]["max_tokens"] == 64
    assert observed["payload"]["reasoning_effort"] == "high"
    assert "temperature" not in observed["payload"]
    assert result.normalized_output == "A brief answer."
    assert result.provider_usage.input_tokens == 14
    assert result.provider_usage.output_tokens == 4
    assert result.provider_usage.source.value == "reported"
    assert result.cost.state is CostState.UNKNOWN
    assert result.call_trace.max_output_tokens == 64
    assert result.call_trace.to_public_dict()["max_output_tokens"] == 64
    serialized = json.dumps(result.call_trace.to_public_dict())
    assert supplied_secret not in serialized
    assert supplied_secret not in repr(result.call_trace)


@pytest.mark.asyncio
async def test_discovery_uses_provider_catalog_and_reports_deepseek_capabilities():
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "deepseek-flash", "owned_by": "deepseek", "object": "model"}]})

    gateway = ModelGateway(
        secret_resolver=EnvironmentSecretResolver({"TEST_DEEPSEEK_KEY"}, environ={"TEST_DEEPSEEK_KEY": "test"}),
        transports={ProviderKind.DEEPSEEK: httpx.MockTransport(handler)},
    )
    models = await gateway.discover(_deepseek_config())
    flash = models[0]
    assert requested == ["https://api.deepseek.com/models"]
    assert flash.model_id == "deepseek-flash"
    assert flash.capabilities.state(Capability.STREAMING) is CapabilityState.SUPPORTED
    assert flash.capabilities.state(Capability.JSON_MODE) is CapabilityState.SUPPORTED
    assert flash.capabilities.state(Capability.JSON_SCHEMA) is CapabilityState.UNSUPPORTED
    assert flash.capabilities.max_context_tokens == 1_000_000
    assert "api-docs.deepseek.com" in flash.capabilities.facts[Capability.JSON_MODE].source


@pytest.mark.asyncio
async def test_generic_compatible_capabilities_stay_unknown_until_configured():
    sent = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent = True
        return httpx.Response(200, json={"data": []})

    config = ProviderConfig("generic", ProviderKind.OPENAI_COMPATIBLE, "https://compatible.example/v1", default_model="some-model")
    gateway = ModelGateway(transports={ProviderKind.OPENAI_COMPATIBLE: httpx.MockTransport(handler)})
    model = await gateway.discover(config)
    # This fixture only exercises direct capability metadata since the model list itself is mocked below.
    assert model == []
    with pytest.raises(ProviderError) as error:
        await gateway.generate(config, _text_request(output_schema={"type": "object"}))
    assert error.value.code is ProviderErrorCode.UNSUPPORTED_CAPABILITY
    assert sent is True  # catalog discovery happened; schema-gated generation did not send a second request.


@pytest.mark.asyncio
async def test_anthropic_native_messages_discovery_and_structured_generation():
    calls: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        assert request.headers.get("x-api-key") == "anthropic-test-secret"
        assert request.headers.get("anthropic-version") == "2026-10-21"
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={
                "data": [{
                    "id": "claude-test",
                    "display_name": "Claude test",
                    "capabilities": {"structured_outputs": {"supported": True}, "thinking": {"supported": False}},
                    "max_input_tokens": 64000,
                    "max_tokens": 8192,
                }],
                "has_more": False,
                "last_id": "claude-test",
            })
        assert request.url.path == "/v1/messages"
        return httpx.Response(200, json={
            "id": "msg-test-1",
            "model": "claude-test",
            "content": [{"type": "text", "text": '{"answer":"clear"}'}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 5},
        })

    resolver = EnvironmentSecretResolver({"TEST_ANTHROPIC_KEY"}, environ={"TEST_ANTHROPIC_KEY": "anthropic-test-secret"})
    gateway = ModelGateway(secret_resolver=resolver, transports={ProviderKind.ANTHROPIC: httpx.MockTransport(handler)})
    config = ProviderConfig("anthropic-test", ProviderKind.ANTHROPIC, "https://api.anthropic.com", default_model="claude-test", secret_ref="env:TEST_ANTHROPIC_KEY", max_retries=0)
    models = await gateway.discover(config)
    assert models[0].capabilities.state(Capability.JSON_SCHEMA) is CapabilityState.SUPPORTED
    result = await gateway.generate(config, _text_request(output_schema={
        "type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False,
    }))
    assert result.normalized_output == {"answer": "clear"}
    assert calls[0][1] == "/v1/models"
    method, path, body = calls[1]
    assert (method, path) == ("POST", "/v1/messages")
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["max_tokens"] == 2048
    assert result.provider_usage.input_tokens == 3
    assert result.provider_usage.total_tokens == 8


@pytest.mark.asyncio
async def test_ollama_native_chat_and_discovery_do_not_assume_model_specific_tools():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "tiny:latest", "model": "tiny:latest", "size": 1200, "details": {"family": "test", "quantization_level": "Q4"}}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "vision"], "model_info": {"test.context_length": 8192}})
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        assert body["model"] == "tiny:latest"
        assert body["stream"] is False
        assert body["options"]["num_ctx"] == 16384
        assert body["options"]["num_predict"] == 2048
        return httpx.Response(200, json={
            "model": "tiny:latest", "message": {"role": "assistant", "content": "Local response."},
            "done": True, "done_reason": "stop", "prompt_eval_count": 6, "eval_count": 3,
            "total_duration": 1000, "eval_duration": 800,
        })

    config = ProviderConfig("ollama", ProviderKind.OLLAMA, "http://127.0.0.1:11434", default_model="tiny:latest", max_retries=0, ollama_context_tokens=16384)
    gateway = ModelGateway(transports={ProviderKind.OLLAMA: httpx.MockTransport(handler)})
    discovered = await gateway.discover(config)
    assert discovered[0].capabilities.state(Capability.IMAGE_INPUT) is CapabilityState.SUPPORTED
    assert discovered[0].capabilities.state(Capability.TOOL_USE) is CapabilityState.UNSUPPORTED
    assert discovered[0].capabilities.max_context_tokens == 8192
    result = await gateway.generate(config, _text_request(data_policy=DataPolicy.LOCAL_ONLY))
    assert result.normalized_output == "Local response."
    assert result.provider_usage.input_tokens == 6
    assert result.provider_usage.output_tokens == 3
    assert result.timings["total_duration"] == 0.000001
    assert paths == ["/api/tags", "/api/show", "/api/chat"]


@pytest.mark.asyncio
async def test_deepseek_stream_normalizes_deltas_usage_and_only_completes_after_done():
    stream_body = (
        'data: {"id":"stream-1","model":"deepseek-flash","choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
        'data: {"id":"stream-1","model":"deepseek-flash","choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}\n\n'
        'data: {"id":"stream-1","model":"deepseek-flash","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":8,"completion_tokens":1,"total_tokens":9}}\n\n'
        'data: [DONE]\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=stream_body, headers={"content-type": "text/event-stream"})

    gateway = ModelGateway(
        secret_resolver=EnvironmentSecretResolver({"TEST_DEEPSEEK_KEY"}, environ={"TEST_DEEPSEEK_KEY": "fake"}),
        transports={ProviderKind.DEEPSEEK: httpx.MockTransport(handler)},
    )
    events = [event async for event in gateway.stream(_deepseek_config(), _text_request())]
    assert [event.kind for event in events] == [
        StreamEventKind.STARTED,
        StreamEventKind.TEXT_DELTA,
        StreamEventKind.USAGE,
        StreamEventKind.COMPLETED,
    ]
    assert events[1].text == "Hello"
    assert events[1].provisional is True
    assert events[2].usage.input_tokens == 8
    assert events[-1].result.normalized_output == "Hello"
    assert events[-1].provisional is False
    assert events[-1].result.call_trace.attempts[0].usage.output_tokens == 1
    assert events[-1].result.call_trace.max_output_tokens == 2048


@pytest.mark.asyncio
async def test_partial_stream_failure_has_trace_and_never_emits_completed_event():
    stream_body = (
        'data: {"id":"stream-partial","model":"deepseek-flash","choices":[{"delta":{"content":"Part"},"finish_reason":null}]}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream_body, headers={"content-type": "text/event-stream"})

    gateway = ModelGateway(
        secret_resolver=EnvironmentSecretResolver({"TEST_DEEPSEEK_KEY"}, environ={"TEST_DEEPSEEK_KEY": "fake"}),
        transports={ProviderKind.DEEPSEEK: httpx.MockTransport(handler)},
    )
    events = []
    with pytest.raises(ProviderError) as error:
        async for event in gateway.stream(_deepseek_config(max_retries=2), _text_request()):
            events.append(event)
    assert [event.kind for event in events] == [StreamEventKind.STARTED, StreamEventKind.TEXT_DELTA]
    assert error.value.code is ProviderErrorCode.TRUNCATED
    assert error.value.call_trace.attempts[0].attempt_number == 1
    assert len(error.value.call_trace.attempts) == 1
    assert error.value.call_trace.max_output_tokens == 2048


@pytest.mark.asyncio
async def test_rate_limit_retries_with_bound_and_401_does_not_retry():
    calls = 0

    def retry_then_success(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"message": "private body ignored"}})
        return httpx.Response(200, json={"id": "ok", "model": "deepseek-flash", "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]})

    gateway = ModelGateway(
        secret_resolver=EnvironmentSecretResolver({"TEST_DEEPSEEK_KEY"}, environ={"TEST_DEEPSEEK_KEY": "fake"}),
        transports={ProviderKind.DEEPSEEK: httpx.MockTransport(retry_then_success)},
        sleeper=lambda _: None,
    )
    result = await gateway.generate(_deepseek_config(max_retries=2), _text_request())
    assert calls == 2
    assert len(result.call_trace.attempts) == 2
    assert result.call_trace.attempts[0].error_code == ProviderErrorCode.RATE_LIMITED.value

    auth_calls = 0

    def unauthorized(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        auth_calls += 1
        return httpx.Response(401, json={"error": {"message": "fake-key"}})

    no_retry_gateway = ModelGateway(
        secret_resolver=EnvironmentSecretResolver({"TEST_DEEPSEEK_KEY"}, environ={"TEST_DEEPSEEK_KEY": "fake-key"}),
        transports={ProviderKind.DEEPSEEK: httpx.MockTransport(unauthorized)},
        sleeper=lambda _: None,
    )
    with pytest.raises(ProviderError) as error:
        await no_retry_gateway.generate(_deepseek_config(max_retries=2), _text_request())
    assert auth_calls == 1
    assert error.value.code is ProviderErrorCode.AUTHENTICATION
    assert "fake-key" not in str(error.value)
    assert len(error.value.call_trace.attempts) == 1


@pytest.mark.asyncio
async def test_schema_mismatch_fails_locally_without_retry_and_keeps_trace():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={
            "model": "deepseek-flash", "choices": [{"finish_reason": "stop", "message": {"content": '{"answer":2}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    gateway = ModelGateway(
        secret_resolver=EnvironmentSecretResolver({"TEST_DEEPSEEK_KEY"}, environ={"TEST_DEEPSEEK_KEY": "fake"}),
        transports={ProviderKind.DEEPSEEK: httpx.MockTransport(handler)},
        sleeper=lambda _: None,
    )
    with pytest.raises(ProviderError) as error:
        await gateway.generate(_deepseek_config(max_retries=2), _text_request(output_schema={
            "type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False,
        }))
    assert calls == 1
    assert error.value.code is ProviderErrorCode.INVALID_OUTPUT
    assert error.value.call_trace.error_code == ProviderErrorCode.INVALID_OUTPUT.value
    assert error.value.call_trace.usage.output_tokens == 1


@pytest.mark.asyncio
async def test_local_only_policy_rejects_remote_provider_before_sending():
    sent = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent = True
        return httpx.Response(200, json={})

    gateway = ModelGateway(
        secret_resolver=EnvironmentSecretResolver({"TEST_DEEPSEEK_KEY"}, environ={"TEST_DEEPSEEK_KEY": "fake"}),
        transports={ProviderKind.DEEPSEEK: httpx.MockTransport(handler)},
    )
    with pytest.raises(ProviderError) as error:
        await gateway.generate(_deepseek_config(), _text_request(data_policy=DataPolicy.LOCAL_ONLY))
    assert sent is False
    assert error.value.code is ProviderErrorCode.UNAVAILABLE
    assert error.value.call_trace.attempts == ()


def test_usage_and_cost_keep_unknown_separate_from_estimated():
    unknown = estimate_cost(ProviderUsage(), None)
    assert unknown.state is CostState.UNKNOWN
    assert unknown.amount is None

    usage = ProviderUsage(
        source=UsageSource.REPORTED,
        input_tokens=100,
        cached_input_tokens=20,
        non_cached_input_tokens=80,
        output_tokens=25,
        total_tokens=125,
    )
    price = PriceVersion(
        provider_id="p",
        model_id="m",
        currency="USD",
        source_url="https://example.test/pricing",
        effective_from=datetime(2026, 11, 1, tzinfo=timezone.utc),
        input_per_million=Decimal("1"),
        cached_input_per_million=Decimal("0.2"),
        output_per_million=Decimal("2"),
    )
    cost = estimate_cost(usage, price)
    assert cost.state is CostState.ESTIMATED
    assert cost.amount == Decimal("0.000134")
    assert cost.currency == "USD"
    incomplete = estimate_cost(ProviderUsage(source=UsageSource.REPORTED, input_tokens=100, output_tokens=25), price)
    assert incomplete.state is CostState.UNKNOWN
    assert incomplete.amount is None
    inconsistent = estimate_cost(ProviderUsage(
        source=UsageSource.REPORTED, input_tokens=100, cached_input_tokens=20,
        non_cached_input_tokens=90, output_tokens=25,
    ), price)
    assert inconsistent.amount is None


def test_routing_and_profiles_keep_locality_and_speed_depth_independent():
    config = default_deepseek_routing()
    deepseek = ProviderConfig("deepseek", ProviderKind.DEEPSEEK, "https://api.deepseek.com", default_model="deepseek-flash")
    ollama = ProviderConfig("ollama", ProviderKind.OLLAMA, "http://127.0.0.1:11434", default_model="local")
    local_routes = RoleRoutingConfig(routes={RoleTask.GM_ANSWER: (  # explicit ordered fallback
        ProviderTarget("deepseek", "deepseek-flash"),
        ProviderTarget("ollama", "local"),
    )})
    selected = local_routes.resolve(RoleTask.GM_ANSWER, speed=SpeedProfile.PATIENT, depth=DepthProfile.DEEP, data_policy=DataPolicy.LOCAL_ONLY, providers={"deepseek": deepseek, "ollama": ollama})
    assert [target.provider_id for target in selected] == ["ollama"]
    profile = execution_profile(SpeedProfile.FAST, DepthProfile.EXHAUSTIVE)
    assert profile.candidate_count == 6
    assert profile.semantic_review_rounds == 2
    assert profile.concurrency_mode == "bounded_parallel"
    assert profile.output_mode == "progressive"
    assert RoleTask.INTENT in config.routes


def test_provider_config_refuses_embedded_credentials_and_arbitrary_secret_values():
    with pytest.raises(ValueError):
        ProviderConfig("bad", ProviderKind.OPENAI_COMPATIBLE, "https://user:password@example.test/v1")
    with pytest.raises(ValueError):
        ProviderConfig("bad", ProviderKind.OPENAI_COMPATIBLE, "https://example.test/v1?key=secret")
    with pytest.raises(ValueError):
        ProviderConfig("bad", ProviderKind.OPENAI_COMPATIBLE, "http://example.test/v1")


def test_bundled_deepseek_credential_stays_on_its_official_origin():
    ref = "env:WORLD_ENGINE_DEEPSEEK_API_KEY"
    official = ProviderConfig("deepseek", ProviderKind.DEEPSEEK, "https://api.deepseek.com:443", secret_ref=ref)
    assert official.secret_ref == ref
    for kind, endpoint in (
        (ProviderKind.DEEPSEEK, "https://proxy.example.test"),
        (ProviderKind.DEEPSEEK, "http://api.deepseek.com"),
        (ProviderKind.DEEPSEEK, "https://api.deepseek.com:8443"),
        (ProviderKind.OPENAI_COMPATIBLE, "https://api.deepseek.com"),
    ):
        with pytest.raises(ValueError, match="official HTTPS endpoint"):
            ProviderConfig("alternate", kind, endpoint, secret_ref=ref, allow_insecure_http=True)


@pytest.mark.asyncio
async def test_loopback_provider_disables_environment_proxy_inheritance():
    local = ProviderConfig("ollama", ProviderKind.OLLAMA, "http://127.0.0.1:11434")
    remote = ProviderConfig("remote", ProviderKind.OPENAI_COMPATIBLE, "https://provider.example/v1")
    adapter = OllamaAdapter()
    local_client = adapter.new_client(local, TimeoutPolicy())
    remote_client = adapter.new_client(remote, TimeoutPolicy())
    try:
        assert local_client._trust_env is False
        assert remote_client._trust_env is True
    finally:
        await local_client.aclose()
        await remote_client.aclose()


@pytest.mark.asyncio
async def test_ollama_stream_normalizes_ndjson_deltas_and_reports_final_usage():
    stream_body = (
        '{"model":"tiny:latest","message":{"role":"assistant","content":"Local "},"done":false}\n'
        '{"model":"tiny:latest","message":{"role":"assistant","content":"stream."},"done":true,"done_reason":"stop","prompt_eval_count":5,"eval_count":2}\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["options"]["num_ctx"] == 16384
        assert body["options"]["num_predict"] == 2048
        return httpx.Response(200, text=stream_body, headers={"content-type": "application/x-ndjson"})

    config = ProviderConfig("ollama", ProviderKind.OLLAMA, "http://127.0.0.1:11434", default_model="tiny:latest", max_retries=0, ollama_context_tokens=16384)
    gateway = ModelGateway(transports={ProviderKind.OLLAMA: httpx.MockTransport(handler)})
    events = [event async for event in gateway.stream(config, _text_request(data_policy=DataPolicy.LOCAL_ONLY))]
    assert [event.kind for event in events] == [StreamEventKind.STARTED, StreamEventKind.TEXT_DELTA, StreamEventKind.TEXT_DELTA, StreamEventKind.USAGE, StreamEventKind.COMPLETED]
    assert events[-1].result.normalized_output == "Local stream."
    assert events[-1].usage.total_tokens == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_ollama_context_overflow_is_safe_and_non_retryable(streaming):
    upstream_message = "request (9787 tokens) exceeds the available context size (8192 tokens), try increasing it"
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": upstream_message})

    config = ProviderConfig(
        "ollama", ProviderKind.OLLAMA, "http://127.0.0.1:11434",
        default_model="tiny:latest", max_retries=2,
    )
    gateway = ModelGateway(transports={ProviderKind.OLLAMA: httpx.MockTransport(handler)})
    with pytest.raises(ProviderError) as caught:
        if streaming:
            _ = [event async for event in gateway.stream(config, _text_request(data_policy=DataPolicy.LOCAL_ONLY))]
        else:
            await gateway.generate(config, _text_request(data_policy=DataPolicy.LOCAL_ONLY))

    error = caught.value
    assert calls == 1
    assert error.code is ProviderErrorCode.CONTEXT_LIMIT
    assert error.retryable is False
    assert error.status_code == 400
    assert error.safe_message == "Ollama context capacity was exceeded"
    assert error.call_trace.error_code == ProviderErrorCode.CONTEXT_LIMIT.value
    assert error.call_trace.attempts[0].error_code == ProviderErrorCode.CONTEXT_LIMIT.value
    assert upstream_message not in error.safe_message
    assert upstream_message not in error.call_trace.error_message
    assert upstream_message not in json.dumps(error.call_trace.to_public_dict(), ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_ollama_other_or_unbounded_context_errors_remain_client_errors(streaming):
    overflow = "request (9787 tokens) exceeds the available context size (8192 tokens), try increasing it"
    # JSON accepts trailing whitespace. Exercise a truly streamed response so
    # we can verify the diagnostic reader stops before consuming every chunk.
    chunks = [json.dumps({"error": overflow}).encode(), b" " * 4096, b" " * 4096, b" " * 4096]
    stream_state = {"yielded": 0}

    class ChunkedErrorBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                stream_state["yielded"] += 1
                yield chunk

        async def aclose(self):
            return None

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if streaming:
            return httpx.Response(400, stream=ChunkedErrorBody(), headers={"content-type": "application/json"})
        return httpx.Response(400, content=json.dumps({
            "error": "request (5 tokens) exceeds the available context size (8192 tokens), try increasing it",
        }))

    config = ProviderConfig(
        "ollama", ProviderKind.OLLAMA, "http://127.0.0.1:11434",
        default_model="tiny:latest", max_retries=2,
    )
    gateway = ModelGateway(transports={ProviderKind.OLLAMA: httpx.MockTransport(handler)})
    with pytest.raises(ProviderError) as caught:
        if streaming:
            _ = [event async for event in gateway.stream(config, _text_request(data_policy=DataPolicy.LOCAL_ONLY))]
        else:
            await gateway.generate(config, _text_request(data_policy=DataPolicy.LOCAL_ONLY))

    assert calls == 1
    assert caught.value.code is ProviderErrorCode.CLIENT
    assert caught.value.retryable is False
    if streaming:
        assert stream_state["yielded"] == 3
        assert stream_state["yielded"] < len(chunks)


@pytest.mark.asyncio
async def test_anthropic_stream_uses_message_delta_cumulative_usage():
    body = (
        'event: message_start\ndata: {"type":"message_start","message":{"id":"msg-stream","model":"claude-test","usage":{"input_tokens":3,"output_tokens":0}}}\n\n'
        'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Native"}}\n\n'
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":3,"cache_read_input_tokens":0,"cache_creation_input_tokens":0,"output_tokens":2}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/messages"
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    resolver = EnvironmentSecretResolver({"TEST_ANTHROPIC_KEY"}, environ={"TEST_ANTHROPIC_KEY": "fake"})
    gateway = ModelGateway(secret_resolver=resolver, transports={ProviderKind.ANTHROPIC: httpx.MockTransport(handler)})
    config = ProviderConfig("anthropic", ProviderKind.ANTHROPIC, "https://api.anthropic.com", default_model="claude-test", secret_ref="env:TEST_ANTHROPIC_KEY", max_retries=0)
    events = [event async for event in gateway.stream(config, _text_request())]
    assert [event.kind for event in events] == [StreamEventKind.STARTED, StreamEventKind.TEXT_DELTA, StreamEventKind.USAGE, StreamEventKind.COMPLETED]
    assert events[-1].result.normalized_output == "Native"
    assert events[-1].usage.output_tokens == 2


@pytest.mark.asyncio
async def test_timeout_exhaustion_has_attempt_trace_unknown_usage_and_no_zero_counts():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={})

    gateway = ModelGateway(
        secret_resolver=EnvironmentSecretResolver({"TEST_DEEPSEEK_KEY"}, environ={"TEST_DEEPSEEK_KEY": "fake"}),
        transports={ProviderKind.DEEPSEEK: httpx.MockTransport(handler)},
        sleeper=lambda _: None,
    )
    request = _text_request(timeout_policy=TimeoutPolicy(connect_seconds=1, read_seconds=1, write_seconds=1, pool_seconds=1, deadline_seconds=0.01))
    with pytest.raises(ProviderError) as error:
        await gateway.generate(_deepseek_config(max_retries=2), request)
    assert calls == 1  # The overall deadline includes retries and prevents another attempt.
    trace = error.value.call_trace
    assert len(trace.attempts) == 1
    assert trace.attempts[0].error_code == ProviderErrorCode.TIMEOUT.value
    assert trace.attempts[0].possibly_billed is True
    assert trace.usage.source is UsageSource.UNKNOWN
    assert trace.usage.input_tokens is None and trace.usage.output_tokens is None
    assert trace.max_output_tokens == 2048


@pytest.mark.asyncio
async def test_cancelled_generation_carries_trace_for_operation_persistence():
    entered = asyncio.Event()
    blocker = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await blocker.wait()
        return httpx.Response(200, json={})

    gateway = ModelGateway(
        secret_resolver=EnvironmentSecretResolver({"TEST_DEEPSEEK_KEY"}, environ={"TEST_DEEPSEEK_KEY": "fake"}),
        transports={ProviderKind.DEEPSEEK: httpx.MockTransport(handler)},
    )
    task = asyncio.create_task(gateway.generate(_deepseek_config(max_retries=0), _text_request()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as error:
        await task
    trace = error.value.call_trace
    assert trace.error_code == ProviderErrorCode.CANCELLED.value
    assert trace.end_reason == "cancelled"
    assert trace.attempts[0].outcome == "cancelled"
    assert trace.attempts[0].possibly_billed is True
    assert trace.usage.input_tokens is None


@pytest.mark.asyncio
async def test_unresolved_schema_ref_is_a_traced_preflight_failure():
    gateway = ModelGateway(schema_resolver=lambda _: None)
    request = _text_request(schema_ref="npc_decision.v1")
    with pytest.raises(ProviderError) as error:
        await gateway.generate(_deepseek_config(), request)
    assert error.value.code is ProviderErrorCode.INVALID_OUTPUT
    assert error.value.call_trace is not None
    assert error.value.call_trace.attempts == ()
    assert error.value.call_trace.end_reason == "preflight_rejected"

