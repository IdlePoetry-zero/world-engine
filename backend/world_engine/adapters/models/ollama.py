"""Native Ollama `/api/tags` and `/api/chat` adapter."""

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
    usage_from_ollama,
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


class OllamaAdapter(ProviderAdapter):
    kind = ProviderKind.OLLAMA

    def __init__(self, secret_resolver: SecretResolver | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__(secret_resolver, transport)
        self._discovered: dict[tuple[str, str], ModelCapabilities] = {}

    def capabilities_for(self, config: ProviderConfig, model: str) -> ModelCapabilities:
        return self._discovered.get((config.provider_id, model), capability_model(config, defaults={
            Capability.TEXT_GENERATION: True,
            Capability.STREAMING: True,
            Capability.JSON_SCHEMA: True,
            Capability.USAGE_REPORTING: True,
        }))

    async def discover(self, config: ProviderConfig, timeout: TimeoutPolicy) -> list[DiscoveredModel]:
        headers = self.auth_headers(config)
        async with self.new_client(config, timeout, headers=headers) as client:
            response = await client.get(self.endpoint(config, "api/tags"))
        if not response.is_success:
            raise self.http_error(response)
        data = await decode_json_response(response)
        rows = data.get("models")
        if not isinstance(rows, list):
            raise ProviderError(ProviderErrorCode.PROTOCOL, "Ollama catalog has no models list", status_code=response.status_code)
        models: list[DiscoveredModel] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            model_id = row.get("model") or row.get("name")
            if not isinstance(model_id, str):
                continue
            caps = await self._probe_model_capabilities(config, model_id, timeout, headers)
            self._discovered[(config.provider_id, model_id)] = caps
            details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
            metadata = {
                key: value for key, value in row.items()
                if key in {"size", "digest", "modified_at"} and isinstance(value, (str, int, float, bool, type(None)))
            }
            metadata["details"] = {k: v for k, v in details.items() if isinstance(v, (str, int, float, bool, type(None)))}
            models.append(DiscoveredModel(
                model_id=model_id,
                provider_id=config.provider_id,
                display_name=row.get("name") if isinstance(row.get("name"), str) else model_id,
                owned_by="ollama",
                capabilities=caps,
                metadata=metadata,
            ))
        return models

    async def _probe_model_capabilities(
        self,
        config: ProviderConfig,
        model: str,
        timeout: TimeoutPolicy,
        headers: Mapping[str, str],
    ) -> ModelCapabilities:
        # /api/tags establishes installed model identities. /api/show probes the
        # selected model's own advertised features; on older servers, retain
        # unknowns instead of promoting assumed support to a pass.
        try:
            async with self.new_client(config, timeout, headers={"Content-Type": "application/json", **headers}) as client:
                response = await client.post(self.endpoint(config, "api/show"), json={"model": model, "verbose": False})
            if not response.is_success:
                return self.capabilities_for(config, model)
            data = await decode_json_response(response)
        except (httpx.RequestError, ProviderError):
            return self.capabilities_for(config, model)
        raw = data.get("capabilities")
        capabilities_were_listed = isinstance(raw, list)
        names = {value.lower() for value in raw if isinstance(value, str)} if isinstance(raw, list) else set()
        defaults: dict[Capability, bool | None] = {
            Capability.TEXT_GENERATION: "completion" in names if capabilities_were_listed else True,
            Capability.STREAMING: True,
            Capability.JSON_SCHEMA: True,
            Capability.USAGE_REPORTING: True,
            Capability.TOOL_USE: ("tools" in names) if capabilities_were_listed else None,
            Capability.IMAGE_INPUT: ("vision" in names) if capabilities_were_listed else None,
            Capability.THINKING: ("thinking" in names) if capabilities_were_listed else None,
            Capability.EMBEDDINGS: ("embedding" in names or "embeddings" in names) if capabilities_were_listed else None,
        }
        facts = dict(capability_model(config, defaults=defaults).facts)
        for cap in (Capability.TOOL_USE, Capability.IMAGE_INPUT, Capability.THINKING, Capability.EMBEDDINGS):
            if cap.value not in config.capability_overrides and capabilities_were_listed:
                supported = defaults[cap] is True
                facts[cap] = CapabilityEvidence(cap, CapabilityState.SUPPORTED if supported else CapabilityState.UNSUPPORTED, "ollama_show", f"capabilities={','.join(sorted(names))}")
        model_info = data.get("model_info") if isinstance(data.get("model_info"), Mapping) else {}
        context = next((v for k, v in model_info.items() if k.endswith("context_length") and isinstance(v, int) and not isinstance(v, bool) and v > 0), None)
        return ModelCapabilities(facts, max_context_tokens=context)

    async def generate_once(self, config: ProviderConfig, request: ModelRequest, model: str) -> RawModelResult:
        headers = {"Content-Type": "application/json", **self.auth_headers(config)}
        async with self.new_client(config, request.timeout_policy, headers=headers) as client:
            response = await client.post(
                self.endpoint(config, "api/chat"),
                json=self._payload(config, request, model, stream=False),
            )
        if not response.is_success:
            raise self.http_error(response)
        data = await decode_json_response(response)
        if isinstance(data.get("error"), str):
            raise ProviderError(ProviderErrorCode.SERVER, "Ollama reported a generation error", status_code=response.status_code, possibly_billed=True)
        message = data.get("message")
        if not isinstance(message, Mapping) or data.get("done") is not True:
            raise ProviderError(ProviderErrorCode.TRUNCATED, "Ollama response did not finish", status_code=response.status_code, possibly_billed=True)
        content = message.get("content")
        return RawModelResult(
            text=content if isinstance(content, str) else "",
            usage=usage_from_ollama(data),
            finish_reason=finish_reason(data.get("done_reason")),
            provider_request_id=safe_identifier(data.get("id")),
            reported_model=data.get("model") if isinstance(data.get("model"), str) else None,
            provider_timings={
                key: value / 1_000_000_000 if isinstance(value, int) else None
                for key, value in data.items()
                if key in {"total_duration", "load_duration", "prompt_eval_duration", "eval_duration"}
            },
        )

    async def stream_once(self, config: ProviderConfig, request: ModelRequest, model: str) -> AsyncIterator[RawStreamEvent]:
        headers = {"Content-Type": "application/json", "Accept": "application/x-ndjson", **self.auth_headers(config)}
        request_id: str | None = None
        reported_model: str | None = None
        final_usage = None
        final_reason: FinishReason | None = None
        done = False
        async with self.new_client(config, request.timeout_policy, headers=headers) as client:
            async with client.stream("POST", self.endpoint(config, "api/chat"), json=self._payload(config, request, model, stream=True)) as response:
                if not response.is_success:
                    raise self.http_error(response)
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = __import__("json").loads(line)
                    except (ValueError, TypeError):
                        raise ProviderError(ProviderErrorCode.PROTOCOL, "Ollama sent invalid stream JSON", possibly_billed=True) from None
                    if not isinstance(data, Mapping):
                        continue
                    if isinstance(data.get("error"), str):
                        raise ProviderError(ProviderErrorCode.SERVER, "Ollama reported a stream generation error", possibly_billed=True)
                    request_id = safe_identifier(data.get("id") or request_id)
                    reported_model = data.get("model") if isinstance(data.get("model"), str) else reported_model
                    message = data.get("message")
                    text = message.get("content") if isinstance(message, Mapping) else None
                    if isinstance(text, str) and text:
                        yield RawStreamEvent("text_delta", text=text, provider_request_id=request_id, reported_model=reported_model)
                    if data.get("done") is True:
                        done = True
                        final_usage = usage_from_ollama(data)
                        final_reason = finish_reason(data.get("done_reason"))
                        if final_usage.source.value == "reported":
                            yield RawStreamEvent("usage", usage=final_usage, provider_request_id=request_id, reported_model=reported_model)
                        break
        if not done:
            raise ProviderError(ProviderErrorCode.TRUNCATED, "Ollama stream ended before done=true", retryable=False, possibly_billed=True, provider_request_id=request_id)
        yield RawStreamEvent("done", finish_reason=final_reason or FinishReason.OTHER, provider_request_id=request_id, reported_model=reported_model)

    def _payload(self, config: ProviderConfig, request: ModelRequest, model: str, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": message.role, "content": message.content} for message in request.messages],
            "stream": stream,
        }
        if request.output_schema is not None and self.capabilities_for(config, model).state(Capability.JSON_SCHEMA) is CapabilityState.SUPPORTED:
            payload["format"] = dict(request.output_schema)
        options: dict[str, Any] = {}
        if request.profile.max_output_tokens is not None:
            options["num_predict"] = request.profile.max_output_tokens
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if options:
            payload["options"] = options
        return payload


def local_ollama_config(provider_id: str = "ollama", base_url: str = "http://127.0.0.1:11434", model: str | None = None) -> ProviderConfig:
    """Build the documented local default. Does not start or probe a service."""
    return ProviderConfig(provider_id=provider_id, kind=ProviderKind.OLLAMA, base_url=base_url, default_model=model)

