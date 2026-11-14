"""DeepSeek and explicitly configured OpenAI Chat Completions adapters."""

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
    usage_from_openai,
)
from .contracts import (
    Capability,
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


class ChatCompletionsAdapter(ProviderAdapter):
    """Generic chat-completions adapter.

    Except for text generation (the protocol's basic operation), capabilities
    remain unknown until probed or supplied as operator overrides.
    """

    kind = ProviderKind.OPENAI_COMPATIBLE

    def __init__(self, secret_resolver: SecretResolver | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(secret_resolver, transport)

    def capabilities_for(self, config: ProviderConfig, model: str) -> ModelCapabilities:
        return capability_model(config, defaults={Capability.TEXT_GENERATION: True})

    def resolve_model(self, config: ProviderConfig, request: ModelRequest | None = None) -> str:
        model = (request.model if request else None) or config.default_model
        if model is None and self.kind is ProviderKind.DEEPSEEK:
            model = "deepseek-flash"
        if not model:
            raise ProviderError(ProviderErrorCode.CLIENT, "A model id is required")
        return model

    def _request_payload(self, config: ProviderConfig, request: ModelRequest, model: str, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": _messages_with_schema_instruction(request),
            "stream": stream,
        }
        if request.profile.max_output_tokens is not None:
            payload["max_tokens"] = request.profile.max_output_tokens
        caps = self.capabilities_for(config, model)
        if request.output_schema is not None:
            if caps.state(Capability.JSON_SCHEMA) is CapabilityState.SUPPORTED:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "model_result", "strict": True, "schema": dict(request.output_schema)},
                }
            elif caps.state(Capability.JSON_MODE) is CapabilityState.SUPPORTED:
                payload["response_format"] = {"type": "json_object"}
        if self.kind is ProviderKind.DEEPSEEK and caps.state(Capability.THINKING) is CapabilityState.SUPPORTED:
            payload["reasoning_effort"] = {
                "standard": "low",
                "deep": "high",
                "exhaustive": "max",
            }[request.profile.depth.value]
        elif request.temperature is not None:
            payload["temperature"] = request.temperature
        if stream and caps.state(Capability.USAGE_REPORTING) is CapabilityState.SUPPORTED:
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def discover(self, config: ProviderConfig, timeout: TimeoutPolicy) -> list[DiscoveredModel]:
        headers = {"Content-Type": "application/json", **self.auth_headers(config, required=config.kind is ProviderKind.DEEPSEEK)}
        async with self.new_client(config, timeout, headers=headers) as client:
            response = await client.get(self.endpoint(config, "models"))
        if not response.is_success:
            raise self.http_error(response)
        data = await decode_json_response(response)
        rows = data.get("data")
        if not isinstance(rows, list):
            raise ProviderError(ProviderErrorCode.PROTOCOL, "Model catalog has no data list", status_code=response.status_code)
        models = []
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
                continue
            model_id = row["id"]
            metadata = {k: v for k, v in row.items() if k not in {"id", "object", "owned_by", "created"} and isinstance(v, (str, int, float, bool, type(None)))}
            models.append(DiscoveredModel(
                model_id=model_id,
                provider_id=config.provider_id,
                display_name=model_id,
                owned_by=row.get("owned_by") if isinstance(row.get("owned_by"), str) else None,
                capabilities=self.capabilities_for(config, model_id),
                metadata=metadata,
            ))
        return models

    async def generate_once(self, config: ProviderConfig, request: ModelRequest, model: str) -> RawModelResult:
        headers = {"Content-Type": "application/json", **self.auth_headers(config, required=config.kind is ProviderKind.DEEPSEEK)}
        async with self.new_client(config, request.timeout_policy, headers=headers) as client:
            response = await client.post(
                self.endpoint(config, "chat/completions"),
                json=self._request_payload(config, request, model, stream=False),
            )
        if not response.is_success:
            raise self.http_error(response)
        data = await decode_json_response(response)
        if isinstance(data.get("error"), Mapping):
            raise ProviderError(ProviderErrorCode.SERVER, "Provider reported a generation error", status_code=response.status_code, provider_request_id=safe_identifier(data.get("id")), possibly_billed=True)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ProviderError(ProviderErrorCode.PROTOCOL, "Provider returned no completion choice", status_code=response.status_code)
        choice = choices[0]
        message = choice.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        text = _content_text(content)
        return RawModelResult(
            text=text,
            usage=usage_from_openai(data.get("usage")),
            finish_reason=finish_reason(choice.get("finish_reason")),
            provider_request_id=safe_identifier(data.get("id") or response.headers.get("x-request-id")),
            reported_model=data.get("model") if isinstance(data.get("model"), str) else None,
        )

    async def stream_once(self, config: ProviderConfig, request: ModelRequest, model: str) -> AsyncIterator[RawStreamEvent]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            **self.auth_headers(config, required=config.kind is ProviderKind.DEEPSEEK),
        }
        done = False
        request_id: str | None = None
        reported_model: str | None = None
        final_reason: FinishReason | None = None
        async with self.new_client(config, request.timeout_policy, headers=headers) as client:
            async with client.stream(
                "POST",
                self.endpoint(config, "chat/completions"),
                json=self._request_payload(config, request, model, stream=True),
            ) as response:
                if not response.is_success:
                    raise self.http_error(response)
                async for event in parse_sse(response):
                    if event.data.strip() == "[DONE]":
                        done = True
                        break
                    try:
                        data = __import__("json").loads(event.data)
                    except (ValueError, TypeError):
                        raise ProviderError(ProviderErrorCode.PROTOCOL, "Provider sent an invalid stream event", possibly_billed=True) from None
                    if not isinstance(data, Mapping):
                        continue
                    if isinstance(data.get("error"), Mapping):
                        raise ProviderError(ProviderErrorCode.SERVER, "Provider reported a stream generation error", possibly_billed=True)
                    request_id = safe_identifier(data.get("id") or request_id)
                    reported_model = data.get("model") if isinstance(data.get("model"), str) else reported_model
                    usage = usage_from_openai(data.get("usage"))
                    if usage.source.value == "reported":
                        yield RawStreamEvent("usage", usage=usage, provider_request_id=request_id, reported_model=reported_model)
                    choices = data.get("choices")
                    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
                        choice = choices[0]
                        delta = choice.get("delta")
                        content = delta.get("content") if isinstance(delta, Mapping) else None
                        text = _content_text(content)
                        if text:
                            yield RawStreamEvent("text_delta", text=text, provider_request_id=request_id, reported_model=reported_model)
                        if choice.get("finish_reason") is not None:
                            final_reason = finish_reason(choice.get("finish_reason"))
                if not done:
                    raise ProviderError(ProviderErrorCode.TRUNCATED, "Provider stream ended before its completion marker", retryable=False, possibly_billed=True, provider_request_id=request_id)
        yield RawStreamEvent("done", finish_reason=final_reason or FinishReason.OTHER, provider_request_id=request_id, reported_model=reported_model)


class DeepSeekAdapter(ChatCompletionsAdapter):
    kind = ProviderKind.DEEPSEEK

    def capabilities_for(self, config: ProviderConfig, model: str) -> ModelCapabilities:
        # The official DeepSeek catalog documents these deepseek-flash limits.
        # Explicit operator overrides still take precedence for model-specific changes.
        docs_source = "https://api-docs.deepseek.com/api/create-chat-completion/"
        defaults: dict[Capability, bool | None] = {
            Capability.TEXT_GENERATION: True,
            Capability.STREAMING: True,
            Capability.JSON_MODE: True,
            Capability.JSON_SCHEMA: False,
            Capability.TOOL_USE: True,
            Capability.EMBEDDINGS: None,
            Capability.IMAGE_INPUT: model == "deepseek-flash",
            Capability.THINKING: True,
            Capability.USAGE_REPORTING: True,
            Capability.CANCELLATION: None,
        }
        if model == "deepseek-flash":
            return capability_model(config, defaults=defaults, sources={cap: docs_source for cap in defaults}, max_context=1_000_000, max_output=384_000)
        return capability_model(config, defaults=defaults, sources={cap: docs_source for cap in defaults})


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, Mapping) and isinstance(part.get("text"), str))
    return ""


def _messages_with_schema_instruction(request: ModelRequest) -> list[dict[str, str]]:
    messages = [{"role": message.role, "content": message.content} for message in request.messages]
    if request.output_schema is None:
        return messages
    instruction = "Return exactly one JSON object that satisfies this JSON Schema. Do not include Markdown or text outside the JSON value. Schema: " + __import__("json").dumps(request.output_schema, ensure_ascii=False, separators=(",", ":"))
    if messages and messages[0]["role"] in {"system", "developer"}:
        messages[0]["content"] = instruction + "\n\n" + messages[0]["content"]
    else:
        messages.insert(0, {"role": "system", "content": instruction})
    return messages

