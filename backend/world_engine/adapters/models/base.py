"""HTTP adapter primitives and raw provider results."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
import json
import time
from typing import Any

import httpx

from .contracts import (
    Capability,
    CapabilityEvidence,
    CapabilityState,
    DiscoveredModel,
    FinishReason,
    ModelCapabilities,
    ModelRequest,
    ProviderConfig,
    ProviderUsage,
    TimeoutPolicy,
    UsageSource,
    utc_now,
)
from .errors import ProviderError, ProviderErrorCode
from .secrets import SecretResolver


@dataclass(frozen=True, slots=True)
class RawModelResult:
    text: str
    usage: ProviderUsage
    finish_reason: FinishReason
    provider_request_id: str | None
    reported_model: str | None
    received_at: datetime = field(default_factory=utc_now)
    provider_timings: Mapping[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RawStreamEvent:
    kind: str
    text: str | None = None
    usage: ProviderUsage | None = None
    finish_reason: FinishReason | None = None
    provider_request_id: str | None = None
    reported_model: str | None = None
    received_at: datetime = field(default_factory=utc_now)


class ProviderAdapter(ABC):
    kind: Any

    def __init__(self, secret_resolver: SecretResolver | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._secret_resolver = secret_resolver
        self._transport = transport

    @abstractmethod
    async def discover(self, config: ProviderConfig, timeout: TimeoutPolicy) -> list[DiscoveredModel]: ...

    @abstractmethod
    async def generate_once(self, config: ProviderConfig, request: ModelRequest, model: str) -> RawModelResult: ...

    @abstractmethod
    def stream_once(self, config: ProviderConfig, request: ModelRequest, model: str) -> AsyncIterator[RawStreamEvent]: ...

    @abstractmethod
    def capabilities_for(self, config: ProviderConfig, model: str) -> ModelCapabilities: ...

    def resolve_model(self, config: ProviderConfig, request: ModelRequest | None = None) -> str:
        model = (request.model if request else None) or config.default_model
        if not model:
            raise ProviderError(ProviderErrorCode.CLIENT, "A model id is required")
        return model

    def auth_headers(self, config: ProviderConfig, *, auth_style: str = "bearer", required: bool = False) -> dict[str, str]:
        if not config.secret_ref:
            if required:
                raise ProviderError(ProviderErrorCode.SECRET, "Provider credential reference is required")
            return {}
        if self._secret_resolver is None:
            raise ProviderError(ProviderErrorCode.SECRET, "No secret resolver is configured")
        secret = self._secret_resolver.resolve(config.secret_ref)
        if auth_style == "x-api-key":
            return {"x-api-key": secret}
        return {"Authorization": f"Bearer {secret}"}

    def new_client(self, config: ProviderConfig, timeout: TimeoutPolicy, *, headers: Mapping[str, str] | None = None) -> httpx.AsyncClient:
        base_headers = {"Accept": "application/json", **(headers or {})}
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(
                connect=timeout.connect_seconds,
                read=timeout.read_seconds,
                write=timeout.write_seconds,
                pool=timeout.pool_seconds,
            ),
            follow_redirects=False,
            # A proxy inherited from the process environment must not carry a
            # loopback-only request to an external service. Remote endpoints may
            # continue to use an explicitly managed system proxy.
            trust_env=not config.is_local_endpoint,
            headers=base_headers,
        )

    @staticmethod
    def endpoint(config: ProviderConfig, path: str) -> str:
        return f"{config.base_url.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def http_error(response: httpx.Response) -> ProviderError:
        status = response.status_code
        request_id = response.headers.get("x-request-id") or response.headers.get("request-id") or response.headers.get("anthropic-request-id")
        retry_after = parse_retry_after(response.headers.get("retry-after")) if status == 429 else None
        if status in (401, 403):
            code, message, retryable = ProviderErrorCode.AUTHENTICATION, "Provider rejected authentication", False
        elif status == 429:
            code, message, retryable = ProviderErrorCode.RATE_LIMITED, "Provider rate limit reached", True
        elif status >= 500:
            code, message, retryable = ProviderErrorCode.SERVER, "Provider returned a server error", True
        elif status == 404:
            code, message, retryable = ProviderErrorCode.UNAVAILABLE, "Provider endpoint or model was not found", False
        else:
            code, message, retryable = ProviderErrorCode.CLIENT, "Provider rejected the request", False
        return ProviderError(
            code,
            message,
            retryable=retryable,
            status_code=status,
            retry_after_seconds=retry_after,
            provider_request_id=safe_identifier(request_id),
            possibly_billed=True if status >= 500 else None,
        )


async def decode_json_response(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        # Do not chain a JSON decoder error: its text may include a response
        # fragment, which could echo sensitive prompt content.
        raise ProviderError(ProviderErrorCode.PROTOCOL, "Provider returned invalid JSON", status_code=response.status_code, provider_request_id=safe_identifier(response.headers.get("x-request-id"))) from None
    if not isinstance(data, dict):
        raise ProviderError(ProviderErrorCode.PROTOCOL, "Provider response must be a JSON object", status_code=response.status_code)
    return data


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return min(600.0, max(0.0, float(value)))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            return min(600.0, max(0.0, (date - utc_now()).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


def safe_identifier(value: str | None) -> str | None:
    if not value or len(value) > 200 or any(ord(c) < 0x20 for c in value):
        return None
    from .errors import redact_text
    if redact_text(value) != value:
        return None
    return value


def usage_from_openai(data: Any) -> ProviderUsage:
    if not isinstance(data, Mapping):
        return ProviderUsage()
    prompt = data.get("prompt_tokens", data.get("input_tokens"))
    output = data.get("completion_tokens", data.get("output_tokens"))
    total = data.get("total_tokens")
    prompt_details = data.get("prompt_tokens_details", data.get("input_tokens_details", {}))
    output_details = data.get("completion_tokens_details", data.get("output_tokens_details", {}))
    cache_tokens = _int_or_none(prompt_details.get("cached_tokens") if isinstance(prompt_details, Mapping) else None)
    if cache_tokens is None:
        # DeepSeek Chat Completions reports cache counters separately.
        hit = _int_or_none(data.get("prompt_cache_hit_tokens"))
        miss = _int_or_none(data.get("prompt_cache_miss_tokens"))
        if hit is not None:
            cache_tokens = hit
        elif miss is not None and _int_or_none(prompt) is not None:
            cache_tokens = max(0, int(prompt) - miss)
    prompt_tokens = _int_or_none(prompt)
    output_tokens = _int_or_none(output)
    return ProviderUsage(
        source=UsageSource.REPORTED if any(v is not None for v in (prompt_tokens, output_tokens, total)) else UsageSource.UNKNOWN,
        input_tokens=prompt_tokens,
        cached_input_tokens=cache_tokens,
        non_cached_input_tokens=max(0, prompt_tokens - cache_tokens) if prompt_tokens is not None and cache_tokens is not None else None,
        output_tokens=output_tokens,
        reasoning_tokens=_int_or_none(output_details.get("reasoning_tokens") if isinstance(output_details, Mapping) else None),
        total_tokens=_int_or_none(total),
        other_items={k: v for k, v in data.items() if k not in {"prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens", "prompt_tokens_details", "completion_tokens_details", "input_tokens_details", "output_tokens_details"} and isinstance(v, int) and not isinstance(v, bool)},
    )


def usage_from_anthropic(data: Any) -> ProviderUsage:
    if not isinstance(data, Mapping):
        return ProviderUsage()
    base_input = _int_or_none(data.get("input_tokens"))
    cache_read = _int_or_none(data.get("cache_read_input_tokens"))
    cache_create = _int_or_none(data.get("cache_creation_input_tokens"))
    cache_total = (cache_read + cache_create) if cache_read is not None and cache_create is not None else None
    output = _int_or_none(data.get("output_tokens"))
    total = base_input + cache_total + output if base_input is not None and cache_total is not None and output is not None else None
    return ProviderUsage(
        source=UsageSource.REPORTED if any(v is not None for v in (base_input, cache_read, cache_create, output)) else UsageSource.UNKNOWN,
        # Keep the API's base input count and cache counters distinct. It excludes cache-read tokens.
        input_tokens=base_input,
        cached_input_tokens=cache_total,
        non_cached_input_tokens=base_input,
        output_tokens=output,
        total_tokens=total,
        other_items={k: v for k, v in data.items() if k not in {"input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"} and isinstance(v, int) and not isinstance(v, bool)},
    )


def usage_from_ollama(data: Any) -> ProviderUsage:
    if not isinstance(data, Mapping):
        return ProviderUsage()
    prompt = _int_or_none(data.get("prompt_eval_count"))
    cached = _int_or_none(data.get("prompt_eval_cached_count"))
    output = _int_or_none(data.get("eval_count"))
    total = prompt + output if prompt is not None and output is not None else None
    return ProviderUsage(
        source=UsageSource.REPORTED if any(v is not None for v in (prompt, output)) else UsageSource.UNKNOWN,
        input_tokens=prompt,
        cached_input_tokens=cached,
        non_cached_input_tokens=max(0, prompt - cached) if prompt is not None and cached is not None else None,
        output_tokens=output,
        total_tokens=total,
        other_items={k: data[k] for k in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration") if isinstance(data.get(k), int)},
    )


def finish_reason(value: Any) -> FinishReason:
    if value in {"stop", "end_turn", "stop_sequence"}:
        return FinishReason.STOP
    if value in {"length", "max_tokens"}:
        return FinishReason.LENGTH
    if value in {"tool_calls", "tool_use"}:
        return FinishReason.TOOL_CALLS
    if value in {"content_filter", "refusal"}:
        return FinishReason.FILTERED
    if value in {"aborted", "cancelled"}:
        return FinishReason.ABORTED
    return FinishReason.OTHER


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def capability_model(
    config: ProviderConfig,
    *,
    defaults: Mapping[Capability, bool | None] | None = None,
    sources: Mapping[Capability, str] | None = None,
    max_context: int | None = None,
    max_output: int | None = None,
) -> ModelCapabilities:
    facts: dict[Capability, CapabilityEvidence] = {}
    defaults = defaults or {}
    for capability in Capability:
        is_override = capability.value in config.capability_overrides
        override = config.capability_overrides.get(capability.value, defaults.get(capability))
        if override is True:
            state, source, probe = CapabilityState.SUPPORTED, "operator_override" if is_override else (sources or {}).get(capability, "protocol"), "explicitly declared supported" if is_override else "documented or protocol-supported"
        elif override is False:
            state, source, probe = CapabilityState.UNSUPPORTED, "operator_override" if is_override else (sources or {}).get(capability, "protocol"), "explicitly declared unsupported" if is_override else "documented unsupported"
        else:
            state, source, probe = CapabilityState.UNKNOWN, "not_discovered", "not probed or declared"
        facts[capability] = CapabilityEvidence(capability, state, source, probe)
    return ModelCapabilities(facts, max_context_tokens=max_context, max_output_tokens=max_output)


def capability_overrides(config: ProviderConfig) -> dict[Capability, CapabilityEvidence]:
    result: dict[Capability, CapabilityEvidence] = {}
    for key, value in config.capability_overrides.items():
        try:
            cap = Capability(key)
        except ValueError:
            continue
        state = CapabilityState.SUPPORTED if value is True else CapabilityState.UNSUPPORTED if value is False else CapabilityState.UNKNOWN
        result[cap] = CapabilityEvidence(cap, state, "operator_override", "capability explicitly set in provider configuration")
    return result
