"""Native Anthropic Messages API adapter (not Chat Completions compatible)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from .base import (
    ProviderAdapter,
    RawModelResult,
    RawStreamEvent,
    capability_model,
    decode_json_response,
    finish_reason,
    safe_identifier,
    usage_from_anthropic,
)
from .contracts import (
    Capability,
    CapabilityEvidence,
    CapabilityState,
    DiscoveredModel,
    FinishReason,
    ModelCapabilities,
    ModelRequest,
    ProviderConfig,
    ProviderKind,
    TimeoutPolicy,
)
from .errors import ProviderError, ProviderErrorCode
from .secrets import SecretResolver
from .sse import parse_sse


_API_VERSION = "2026-10-21"


class AnthropicMessagesAdapter(ProviderAdapter):
    kind = ProviderKind.ANTHROPIC

    def __init__(
        self,
        secret_resolver: SecretResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        api_version: str = _API_VERSION,
    ) -> None:
        super().__init__(secret_resolver, transport)
        self.api_version = api_version
        self._discovered: dict[tuple[str, str], ModelCapabilities] = {}

    def capabilities_for(self, config: ProviderConfig, model: str) -> ModelCapabilities:
        return self._discovered.get((config.provider_id, model), capability_model(config, defaults={
            Capability.TEXT_GENERATION: True,
            Capability.STREAMING: True,
            Capability.JSON_SCHEMA: True,
            Capability.USAGE_REPORTING: True,
        }))

    def _headers(self, config: ProviderConfig, *, streaming: bool = False) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "anthropic-version": self.api_version,
            "Accept": "text/event-stream" if streaming else "application/json",
            **self.auth_headers(config, auth_style="x-api-key", required=True),
        }

    async def discover(self, config: ProviderConfig, timeout: TimeoutPolicy) -> list[DiscoveredModel]:
        models: list[DiscoveredModel] = []
        after_id: str | None = None
        # Provider pagination is followed with a hard bound against a broken cursor.
        for _ in range(100):
            params = {"limit": "1000"}
            if after_id:
                params["after_id"] = after_id
            async with self.new_client(config, timeout, headers=self._headers(config)) as client:
                response = await client.get(_versioned_endpoint(config, "models"), params=params)
            if not response.is_success:
                raise self.http_error(response)
            data = await decode_json_response(response)
            rows = data.get("data")
            if not isinstance(rows, list):
                raise ProviderError(ProviderErrorCode.PROTOCOL, "Model catalog has no data list", status_code=response.status_code)
            for row in rows:
                if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
                    continue
                model_id = row["id"]
                caps = self._capabilities_from_catalog(config, row)
                self._discovered[(config.provider_id, model_id)] = caps
                metadata = {
                    key: value for key, value in row.items()
                    if key not in {"id", "capabilities"} and isinstance(value, (str, int, float, bool, type(None)))
                }
                models.append(DiscoveredModel(
                    model_id=model_id,
                    provider_id=config.provider_id,
                    display_name=row.get("display_name") if isinstance(row.get("display_name"), str) else model_id,
                    owned_by="anthropic",
                    capabilities=caps,
                    metadata=metadata,
                ))
            next_id = data.get("last_id")
            if not data.get("has_more") or not isinstance(next_id, str) or next_id == after_id:
                break
            after_id = next_id
        return models

    async def generate_once(self, config: ProviderConfig, request: ModelRequest, model: str) -> RawModelResult:
        payload = self._payload(config, request, model, stream=False)
        async with self.new_client(config, request.timeout_policy, headers=self._headers(config)) as client:
            response = await client.post(_versioned_endpoint(config, "messages"), json=payload)
        if not response.is_success:
            raise self.http_error(response)
        data = await decode_json_response(response)
        if data.get("type") == "error" or isinstance(data.get("error"), Mapping):
            raise ProviderError(ProviderErrorCode.SERVER, "Provider reported a generation error", status_code=response.status_code, provider_request_id=safe_identifier(data.get("request_id") or data.get("id")), possibly_billed=True)
        content = data.get("content")
        if not isinstance(content, list):
            raise ProviderError(ProviderErrorCode.PROTOCOL, "Provider response has no content blocks", status_code=response.status_code)
        text = "".join(block.get("text", "") for block in content if isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str))
        return RawModelResult(
            text=text,
            usage=usage_from_anthropic(data.get("usage")),
            finish_reason=finish_reason(data.get("stop_reason")),
            provider_request_id=safe_identifier(data.get("request_id") or data.get("id") or response.headers.get("request-id")),
            reported_model=data.get("model") if isinstance(data.get("model"), str) else None,
        )

    async def stream_once(self, config: ProviderConfig, request: ModelRequest, model: str) -> AsyncIterator[RawStreamEvent]:
        payload = self._payload(config, request, model, stream=True)
        request_id: str | None = None
        reported_model: str | None = None
        final_usage = None
        final_reason: FinishReason | None = None
        saw_stop = False
        async with self.new_client(config, request.timeout_policy, headers=self._headers(config, streaming=True)) as client:
            async with client.stream("POST", _versioned_endpoint(config, "messages"), json=payload) as response:
                if not response.is_success:
                    raise self.http_error(response)
                async for event in parse_sse(response):
                    try:
                        data = __import__("json").loads(event.data)
                    except (ValueError, TypeError):
                        raise ProviderError(ProviderErrorCode.PROTOCOL, "Provider sent an invalid stream event", possibly_billed=True) from None
                    if not isinstance(data, Mapping):
                        continue
                    event_type = data.get("type") or event.event
                    if event_type == "error":
                        error_type = data.get("error", {}).get("type") if isinstance(data.get("error"), Mapping) else None
                        retryable = error_type in {"overloaded_error", "api_error"}
                        code = ProviderErrorCode.SERVER if retryable else ProviderErrorCode.PROTOCOL
                        raise ProviderError(code, "Provider reported a stream generation error", retryable=retryable, possibly_billed=True, provider_request_id=request_id)
                    if event_type == "message_start":
                        message = data.get("message")
                        if isinstance(message, Mapping):
                            request_id = safe_identifier(message.get("id"))
                            reported_model = message.get("model") if isinstance(message.get("model"), str) else None
                    elif event_type == "content_block_delta":
                        delta = data.get("delta")
                        if isinstance(delta, Mapping) and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                            yield RawStreamEvent("text_delta", text=delta["text"], provider_request_id=request_id, reported_model=reported_model)
                    elif event_type == "message_delta":
                        if isinstance(data.get("usage"), Mapping):
                            # message_delta usage is cumulative; do not add message_start usage.
                            final_usage = usage_from_anthropic(data["usage"])
                        delta = data.get("delta")
                        if isinstance(delta, Mapping) and delta.get("stop_reason") is not None:
                            final_reason = finish_reason(delta.get("stop_reason"))
                    elif event_type == "message_stop":
                        saw_stop = True
        if not saw_stop:
            raise ProviderError(ProviderErrorCode.TRUNCATED, "Provider stream ended before message_stop", retryable=False, possibly_billed=True, provider_request_id=request_id)
        if final_usage is not None:
            yield RawStreamEvent("usage", usage=final_usage, provider_request_id=request_id, reported_model=reported_model)
        yield RawStreamEvent("done", finish_reason=final_reason or FinishReason.OTHER, provider_request_id=request_id, reported_model=reported_model)

    def _payload(self, config: ProviderConfig, request: ModelRequest, model: str, *, stream: bool) -> dict[str, Any]:
        system_parts: list[str] = []
        messages: list[dict[str, str]] = []
        for message in request.messages:
            if message.role in {"system", "developer"}:
                system_parts.append(message.content)
            else:
                messages.append({"role": message.role, "content": message.content})
        if not messages:
            raise ProviderError(ProviderErrorCode.CLIENT, "Anthropic Messages requires at least one user or assistant message")
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": request.profile.max_output_tokens or 2048,
            "messages": messages,
            "stream": stream,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.output_schema is not None:
            payload["output_config"] = {"format": {"type": "json_schema", "schema": dict(request.output_schema)}}
        # Model-specific thinking/effort is applied only after the Models API or
        # an operator override establishes support. A depth profile by itself is
        # never translated into an unsupported provider parameter.
        caps = self.capabilities_for(config, model)
        if caps.state(Capability.THINKING) is CapabilityState.SUPPORTED:
            payload["thinking"] = {"type": "adaptive"}
            if caps.state(Capability.EFFORT_CONTROL) is CapabilityState.SUPPORTED:
                payload["output_config"] = {**payload.get("output_config", {}), "effort": {
                    "standard": "low", "deep": "high", "exhaustive": "max",
                }[request.profile.depth.value]}
            payload.pop("temperature", None)
        return payload

    def _capabilities_from_catalog(self, config: ProviderConfig, row: Mapping[str, Any]) -> ModelCapabilities:
        raw = row.get("capabilities")
        known: dict[Capability, bool | None] = {
            Capability.TEXT_GENERATION: True,
            Capability.STREAMING: True,
            Capability.USAGE_REPORTING: True,
        }
        if isinstance(raw, Mapping):
            known.update({
                Capability.JSON_SCHEMA: _supported(raw.get("structured_outputs")),
                Capability.IMAGE_INPUT: _supported(raw.get("image_input")),
                Capability.THINKING: _supported(raw.get("thinking")),
                Capability.EFFORT_CONTROL: _supported(raw.get("effort")),
                Capability.TOOL_USE: _supported(raw.get("tool_use")),
            })
        base = capability_model(config, defaults=known)
        facts = dict(base.facts)
        if isinstance(raw, Mapping):
            for cap, key in (
                (Capability.JSON_SCHEMA, "structured_outputs"),
                (Capability.IMAGE_INPUT, "image_input"),
                (Capability.THINKING, "thinking"),
                (Capability.EFFORT_CONTROL, "effort"),
                (Capability.TOOL_USE, "tool_use"),
            ):
                val = _supported(raw.get(key))
                if val is not None and cap.value not in config.capability_overrides:
                    facts[cap] = CapabilityEvidence(cap, CapabilityState.SUPPORTED if val else CapabilityState.UNSUPPORTED, "provider_model_catalog", f"{key}.supported={val}")
        return ModelCapabilities(
            facts,
            max_context_tokens=_positive_int(row.get("max_input_tokens")),
            max_output_tokens=_positive_int(row.get("max_tokens")),
        )


def _versioned_endpoint(config: ProviderConfig, path: str) -> str:
    base = config.base_url.rstrip("/")
    return f"{base}/{path.lstrip('/')}" if base.endswith("/v1") else f"{base}/v1/{path.lstrip('/')}"


def _supported(value: Any) -> bool | None:
    if isinstance(value, Mapping):
        status = value.get("supported")
        return status if isinstance(status, bool) else None
    return value if isinstance(value, bool) else None


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

