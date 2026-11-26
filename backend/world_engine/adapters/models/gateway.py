"""Provider-neutral model gateway with bounded retry, validation and tracing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import replace
from datetime import datetime
import inspect
import json
import random
import time
from typing import Any

import httpx
from jsonschema import Draft202012Validator, SchemaError, ValidationError, validators

from .anthropic import AnthropicMessagesAdapter
from .base import ProviderAdapter, RawModelResult, RawStreamEvent, usage_from_openai
from .chat_completions import ChatCompletionsAdapter, DeepSeekAdapter
from .contracts import (
    AttemptRecord,
    CallTrace,
    Capability,
    CapabilityState,
    CostRecord,
    DepthProfile,
    FinishReason,
    ModelRequest,
    ModelResult,
    NormalizedEvent,
    PriceVersion,
    ProviderConfig,
    ProviderKind,
    ProviderUsage,
    SpeedProfile,
    StreamEventKind,
    TimeoutPolicy,
    TraceContext,
    UsageSource,
    utc_now,
)
from .costs import estimate_cost
from .errors import ProviderError, ProviderErrorCode, TransportFailureKind
from .ollama import OllamaAdapter
from .secrets import SecretResolver


class ModelGateway:
    """Async facade for model discovery, complete calls, and provisional streams.

    Only the terminal `COMPLETED` event contains a validated `ModelResult`.
    Earlier text deltas are marked provisional and must not be committed as
    world facts or finalized narratives.
    """

    def __init__(
        self,
        *,
        adapters: Mapping[ProviderKind, ProviderAdapter] | None = None,
        secret_resolver: SecretResolver | None = None,
        transports: Mapping[ProviderKind, httpx.AsyncBaseTransport] | None = None,
        price_versions: Mapping[tuple[str, str], PriceVersion] | None = None,
        price_resolver: Callable[[ProviderConfig, str, datetime], PriceVersion | None] | None = None,
        schema_resolver: Callable[[str], Mapping[str, Any] | None] | None = None,
        sleeper: Callable[[float], Any] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if adapters is None:
            transports = transports or {}
            adapters = {
                ProviderKind.DEEPSEEK: DeepSeekAdapter(secret_resolver, transports.get(ProviderKind.DEEPSEEK)),
                ProviderKind.OPENAI_COMPATIBLE: ChatCompletionsAdapter(secret_resolver, transports.get(ProviderKind.OPENAI_COMPATIBLE)),
                ProviderKind.OLLAMA: OllamaAdapter(secret_resolver, transports.get(ProviderKind.OLLAMA)),
                ProviderKind.ANTHROPIC: AnthropicMessagesAdapter(secret_resolver, transports.get(ProviderKind.ANTHROPIC)),
            }
        self._adapters = dict(adapters)
        self._prices = dict(price_versions or {})
        self._price_resolver = price_resolver
        self._schema_resolver = schema_resolver
        self._sleep = sleeper
        self._jitter = jitter

    async def discover(self, config: ProviderConfig, timeout: TimeoutPolicy | None = None) -> list[Any]:
        adapter = self._adapter(config)
        policy = timeout or TimeoutPolicy()
        try:
            async with asyncio.timeout(policy.deadline_seconds):
                return await adapter.discover(config, policy)
        except TimeoutError:
            raise ProviderError(ProviderErrorCode.TIMEOUT, "Model discovery exceeded its deadline", retryable=True, possibly_billed=False) from None
        except httpx.TimeoutException:
            raise ProviderError(ProviderErrorCode.TIMEOUT, "Model discovery timed out", retryable=True, possibly_billed=False) from None
        except httpx.NetworkError:
            raise ProviderError(ProviderErrorCode.NETWORK, "Model discovery could not reach the provider", retryable=True, possibly_billed=False) from None

    async def generate(self, config: ProviderConfig, request: ModelRequest) -> ModelResult:
        started_at = utc_now()
        started_clock = time.perf_counter()
        attempts: list[AttemptRecord] = []
        model = request.model or config.default_model or "unknown"
        try:
            request = self._resolve_schema(request)
            adapter = self._adapter(config)
            model = adapter.resolve_model(config, request)
            self._validate_policy_and_capabilities(adapter, config, request, streaming=False)
        except ProviderError as exc:
            trace = self._trace(config, request, model=getattr(request, "model", None) or config.default_model or "unknown", started_at=started_at, attempts=attempts, usage=ProviderUsage(), error=exc, end_reason="preflight_rejected")
            exc.call_trace = trace
            raise

        deadline = (
            request.timeout_policy.deadline_at
            if request.timeout_policy.deadline_at is not None
            else started_clock + request.timeout_policy.deadline_seconds
        )
        raw: RawModelResult | None = None
        final_error: ProviderError | None = None
        for attempt_number in range(1, config.max_retries + 2):
            request = replace(request, data_policy=request.current_data_policy())
            if not config.permits_data_policy(request.data_policy):
                final_error = self._local_policy_error()
                break
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                final_error = ProviderError(ProviderErrorCode.TIMEOUT, "Model call exceeded its deadline", retryable=False, possibly_billed=bool(attempts))
                break
            attempt_started_at = utc_now()
            attempt_started_clock = time.perf_counter()
            try:
                raw = await asyncio.wait_for(adapter.generate_once(config, request, model), timeout=remaining)
                attempt_ended_at = utc_now()
                attempts.append(AttemptRecord(
                    attempt_number=attempt_number,
                    started_at=attempt_started_at,
                    ended_at=attempt_ended_at,
                    elapsed_seconds=max(0.0, time.perf_counter() - attempt_started_clock),
                    outcome="response_received",
                    status_code=200,
                    provider_request_id=raw.provider_request_id,
                    retryable=False,
                    possibly_billed=True,
                    usage=raw.usage,
                ))
                break
            except asyncio.CancelledError as exc:
                attempts.append(AttemptRecord(
                    attempt_number=attempt_number,
                    started_at=attempt_started_at,
                    ended_at=utc_now(),
                    elapsed_seconds=max(0.0, time.perf_counter() - attempt_started_clock),
                    outcome="cancelled",
                    retryable=False,
                    possibly_billed=True,
                    usage=ProviderUsage(),
                    error_code=ProviderErrorCode.CANCELLED.value,
                ))
                cancelled = ProviderError(ProviderErrorCode.CANCELLED, "Model call was cancelled", possibly_billed=True)
                setattr(exc, "call_trace", self._trace(config, request, model=model, started_at=started_at, attempts=attempts, usage=ProviderUsage(), error=cancelled, end_reason="cancelled"))
                raise
            except (ProviderError, httpx.RequestError, TimeoutError) as exc:
                error = _normalize_exception(exc)
                attempt_ended_at = utc_now()
                attempts.append(AttemptRecord(
                    attempt_number=attempt_number,
                    started_at=attempt_started_at,
                    ended_at=attempt_ended_at,
                    elapsed_seconds=max(0.0, time.perf_counter() - attempt_started_clock),
                    outcome="error",
                    status_code=error.status_code,
                    provider_request_id=error.provider_request_id,
                    retry_after_seconds=error.retry_after_seconds,
                    retryable=error.retryable,
                    possibly_billed=error.possibly_billed,
                    usage=ProviderUsage(),
                    error_code=error.code.value,
                    transport_failure_kind=error.transport_failure_kind,
                ))
                final_error = error
                if not error.retryable or attempt_number > config.max_retries:
                    break
                delay = self._retry_delay(error, attempt_number)
                if deadline - time.perf_counter() <= delay:
                    break
                try:
                    await _maybe_await(self._sleep(delay))
                except asyncio.CancelledError as cancelled_exc:
                    cancelled = ProviderError(ProviderErrorCode.CANCELLED, "Model retry wait was cancelled", possibly_billed=error.possibly_billed)
                    setattr(cancelled_exc, "call_trace", self._trace(config, request, model=model, started_at=started_at, attempts=attempts, usage=ProviderUsage(), error=cancelled, end_reason="cancelled"))
                    raise

        if raw is None:
            assert final_error is not None
            trace = self._trace(config, request, model=model, started_at=started_at, attempts=attempts, usage=ProviderUsage(), error=final_error, end_reason="provider_error")
            final_error.call_trace = trace
            raise final_error

        validation_started = time.perf_counter()
        try:
            normalized, validation = self._validate_output(raw, request)
        except ProviderError as exc:
            validation_seconds = max(0.0, time.perf_counter() - validation_started)
            trace = self._trace(config, request, model=model, started_at=started_at, attempts=attempts, usage=raw.usage, error=exc, cost=self._cost(config, model, raw.reported_model, raw.usage, attempt_started_at), end_reason="output_rejected", validation_seconds=validation_seconds, raw=raw)
            exc.call_trace = trace
            raise
        validation_seconds = max(0.0, time.perf_counter() - validation_started)
        cost = self._cost(config, model, raw.reported_model, raw.usage, attempt_started_at)
        ended_at = utc_now()
        trace = self._trace(config, request, model=model, started_at=started_at, attempts=attempts, usage=raw.usage, raw=raw, validation_seconds=validation_seconds, cost=cost, ended_at=ended_at, end_reason="completed")
        return ModelResult(
            normalized_output=normalized,
            provider_usage=raw.usage,
            estimated_usage=None,
            cost=cost,
            finish_reason=raw.finish_reason,
            provider_request_id=raw.provider_request_id,
            requested_model=model,
            reported_model=raw.reported_model,
            timings={
                "total_seconds": max(0.0, time.perf_counter() - started_clock),
                "first_token_seconds": None,
                "validation_seconds": validation_seconds,
                **dict(raw.provider_timings),
            },
            validation=validation,
            call_trace=trace,
        )

    async def stream(self, config: ProviderConfig, request: ModelRequest) -> AsyncIterator[NormalizedEvent]:
        started_at = utc_now()
        started_clock = time.perf_counter()
        attempts: list[AttemptRecord] = []
        model = request.model or config.default_model or "unknown"
        try:
            request = self._resolve_schema(request)
            adapter = self._adapter(config)
            model = adapter.resolve_model(config, request)
            self._validate_policy_and_capabilities(adapter, config, request, streaming=True)
        except ProviderError as exc:
            exc.call_trace = self._trace(
                config, request,
                model=getattr(request, "model", None) or config.default_model or "unknown",
                started_at=started_at,
                attempts=attempts,
                usage=ProviderUsage(),
                error=exc,
                end_reason="preflight_rejected",
            )
            raise
        yield NormalizedEvent(
            kind=StreamEventKind.STARTED,
            request_id=request.trace_context.request_id,
            provider_id=config.provider_id,
            model=model,
        )
        deadline = (
            request.timeout_policy.deadline_at
            if request.timeout_policy.deadline_at is not None
            else started_clock + request.timeout_policy.deadline_seconds
        )
        text_chunks: list[str] = []
        first_token_seconds: float | None = None
        usage = ProviderUsage()
        reported_model: str | None = None
        provider_request_id: str | None = None
        final_reason: FinishReason | None = None
        final_error: ProviderError | None = None
        completed = False

        for attempt_number in range(1, config.max_retries + 2):
            request = replace(request, data_policy=request.current_data_policy())
            if not config.permits_data_policy(request.data_policy):
                final_error = self._local_policy_error()
                break
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                final_error = ProviderError(ProviderErrorCode.TIMEOUT, "Model stream exceeded its deadline", retryable=False, possibly_billed=bool(attempts))
                break
            attempt_started_at = utc_now()
            attempt_started_clock = time.perf_counter()
            saw_event = False
            try:
                async with asyncio.timeout(remaining):
                    async for raw_event in adapter.stream_once(config, request, model):
                        if raw_event.kind == "text_delta" and raw_event.text:
                            saw_event = True
                            if first_token_seconds is None:
                                first_token_seconds = max(0.0, time.perf_counter() - started_clock)
                            text_chunks.append(raw_event.text)
                            provider_request_id = raw_event.provider_request_id or provider_request_id
                            reported_model = raw_event.reported_model or reported_model
                            yield NormalizedEvent(
                                kind=StreamEventKind.TEXT_DELTA,
                                request_id=request.trace_context.request_id,
                                provider_id=config.provider_id,
                                model=reported_model or model,
                                text=raw_event.text,
                                provider_request_id=provider_request_id,
                                provisional=True,
                            )
                        elif raw_event.kind == "usage" and raw_event.usage is not None:
                            saw_event = True
                            usage = raw_event.usage
                            provider_request_id = raw_event.provider_request_id or provider_request_id
                            reported_model = raw_event.reported_model or reported_model
                            yield NormalizedEvent(
                                kind=StreamEventKind.USAGE,
                                request_id=request.trace_context.request_id,
                                provider_id=config.provider_id,
                                model=reported_model or model,
                                usage=usage,
                                provider_request_id=provider_request_id,
                            )
                        elif raw_event.kind == "done":
                            saw_event = True
                            completed = True
                            final_reason = raw_event.finish_reason
                            provider_request_id = raw_event.provider_request_id or provider_request_id
                            reported_model = raw_event.reported_model or reported_model
                ended = utc_now()
                attempts.append(AttemptRecord(
                    attempt_number=attempt_number,
                    started_at=attempt_started_at,
                    ended_at=ended,
                    elapsed_seconds=max(0.0, time.perf_counter() - attempt_started_clock),
                    outcome="stream_completed" if completed else "stream_incomplete",
                    status_code=200,
                    provider_request_id=provider_request_id,
                    retryable=False,
                    possibly_billed=True,
                    usage=usage,
                    error_code=None if completed else ProviderErrorCode.TRUNCATED.value,
                ))
                if not completed:
                    final_error = ProviderError(ProviderErrorCode.TRUNCATED, "Provider stream ended without completion", possibly_billed=True, provider_request_id=provider_request_id)
                break
            except (ProviderError, httpx.RequestError, TimeoutError) as exc:
                error = _normalize_exception(exc)
                attempts.append(AttemptRecord(
                    attempt_number=attempt_number,
                    started_at=attempt_started_at,
                    ended_at=utc_now(),
                    elapsed_seconds=max(0.0, time.perf_counter() - attempt_started_clock),
                    outcome="error",
                    status_code=error.status_code,
                    provider_request_id=error.provider_request_id or provider_request_id,
                    retry_after_seconds=error.retry_after_seconds,
                    retryable=error.retryable,
                    possibly_billed=error.possibly_billed,
                    usage=usage,
                    error_code=error.code.value,
                    transport_failure_kind=error.transport_failure_kind,
                ))
                final_error = error
                if saw_event or not error.retryable or attempt_number > config.max_retries:
                    break
                delay = self._retry_delay(error, attempt_number)
                if deadline - time.perf_counter() <= delay:
                    break
                try:
                    await _maybe_await(self._sleep(delay))
                except asyncio.CancelledError as cancelled_exc:
                    cancelled = ProviderError(ProviderErrorCode.CANCELLED, "Model stream retry wait was cancelled", possibly_billed=error.possibly_billed)
                    setattr(cancelled_exc, "call_trace", self._trace(config, request, model=model, started_at=started_at, attempts=attempts, usage=usage, error=cancelled, end_reason="cancelled", first_token_seconds=first_token_seconds))
                    raise
            except asyncio.CancelledError as exc:
                attempts.append(AttemptRecord(
                    attempt_number=attempt_number,
                    started_at=attempt_started_at,
                    ended_at=utc_now(),
                    elapsed_seconds=max(0.0, time.perf_counter() - attempt_started_clock),
                    outcome="cancelled",
                    provider_request_id=provider_request_id,
                    retryable=False,
                    possibly_billed=True,
                    usage=usage,
                    error_code=ProviderErrorCode.CANCELLED.value,
                ))
                cancelled = ProviderError(ProviderErrorCode.CANCELLED, "Model stream was cancelled", possibly_billed=True)
                setattr(exc, "call_trace", self._trace(config, request, model=model, started_at=started_at, attempts=attempts, usage=usage, error=cancelled, end_reason="cancelled", first_token_seconds=first_token_seconds))
                raise

        if not completed or final_error is not None:
            error = final_error or ProviderError(ProviderErrorCode.TRUNCATED, "Provider stream did not complete", possibly_billed=True)
            error.call_trace = self._trace(
                config, request, model=model, started_at=started_at, attempts=attempts,
                usage=usage, error=error, end_reason="stream_failed", first_token_seconds=first_token_seconds,
            )
            raise error

        raw = RawModelResult(
            text="".join(text_chunks),
            usage=usage,
            finish_reason=final_reason or FinishReason.OTHER,
            provider_request_id=provider_request_id,
            reported_model=reported_model,
        )
        validation_started = time.perf_counter()
        try:
            normalized, validation = self._validate_output(raw, request)
        except ProviderError as exc:
            exc.call_trace = self._trace(
                config, request, model=model, started_at=started_at, attempts=attempts,
                usage=usage, raw=raw, error=exc, cost=self._cost(config, model, reported_model, usage, attempt_started_at), end_reason="output_rejected",
                first_token_seconds=first_token_seconds,
                validation_seconds=max(0.0, time.perf_counter() - validation_started),
            )
            raise
        validation_seconds = max(0.0, time.perf_counter() - validation_started)
        cost = self._cost(config, model, reported_model, usage, attempt_started_at)
        trace = self._trace(
            config, request, model=model, started_at=started_at, attempts=attempts,
            usage=usage, raw=raw, cost=cost, end_reason="completed",
            first_token_seconds=first_token_seconds, validation_seconds=validation_seconds,
        )
        result = ModelResult(
            normalized_output=normalized,
            provider_usage=usage,
            estimated_usage=None,
            cost=cost,
            finish_reason=raw.finish_reason,
            provider_request_id=provider_request_id,
            requested_model=model,
            reported_model=reported_model,
            timings={
                "total_seconds": max(0.0, time.perf_counter() - started_clock),
                "first_token_seconds": first_token_seconds,
                "validation_seconds": validation_seconds,
            },
            validation=validation,
            call_trace=trace,
        )
        yield NormalizedEvent(
            kind=StreamEventKind.COMPLETED,
            request_id=request.trace_context.request_id,
            provider_id=config.provider_id,
            model=reported_model or model,
            usage=usage,
            provider_request_id=provider_request_id,
            finish_reason=raw.finish_reason,
            result=result,
            provisional=False,
        )

    def _adapter(self, config: ProviderConfig) -> ProviderAdapter:
        adapter = self._adapters.get(config.kind)
        if adapter is None:
            raise ProviderError(ProviderErrorCode.UNAVAILABLE, f"No adapter is registered for {config.kind.value}")
        return adapter

    def _resolve_schema(self, request: ModelRequest) -> ModelRequest:
        if request.output_schema is not None or request.schema_ref is None:
            return request
        if self._schema_resolver is None:
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "A schema_ref was supplied but no schema resolver is configured")
        schema = self._schema_resolver(request.schema_ref)
        if not isinstance(schema, Mapping):
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "The referenced output schema could not be resolved")
        return replace(request, output_schema=schema)

    @staticmethod
    def _local_policy_error() -> ProviderError:
        return ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "Data policy forbids sending this request to a non-loopback endpoint",
            possibly_billed=False,
        )

    def _validate_policy_and_capabilities(self, adapter: ProviderAdapter, config: ProviderConfig, request: ModelRequest, *, streaming: bool) -> None:
        if not config.permits_data_policy(request.current_data_policy()):
            raise self._local_policy_error()
        if request.output_schema is not None:
            try:
                validators.validator_for(request.output_schema).check_schema(dict(request.output_schema))
            except SchemaError:
                raise ProviderError(ProviderErrorCode.CLIENT, "Configured JSON Schema is invalid") from None
        model = adapter.resolve_model(config, request)
        caps = adapter.capabilities_for(config, model)
        required = set(request.capability_requirements)
        if streaming:
            required.add(Capability.STREAMING)
        for capability in required:
            state = caps.state(capability)
            if state is not CapabilityState.SUPPORTED:
                description = "unsupported" if state is CapabilityState.UNSUPPORTED else "unknown"
                raise ProviderError(ProviderErrorCode.UNSUPPORTED_CAPABILITY, f"Model capability {capability.value} is {description}")
        if request.output_schema is not None:
            schema_state = caps.state(Capability.JSON_SCHEMA)
            json_state = caps.state(Capability.JSON_MODE)
            if schema_state is not CapabilityState.SUPPORTED and json_state is not CapabilityState.SUPPORTED:
                raise ProviderError(ProviderErrorCode.UNSUPPORTED_CAPABILITY, "Structured output capability is unsupported or unknown")

    def _validate_output(self, raw: RawModelResult, request: ModelRequest) -> tuple[Any, Mapping[str, Any]]:
        if raw.finish_reason is FinishReason.LENGTH:
            raise ProviderError(ProviderErrorCode.TRUNCATED, "Provider stopped at its output limit", possibly_billed=True, provider_request_id=raw.provider_request_id)
        if raw.finish_reason in {FinishReason.FILTERED, FinishReason.ABORTED}:
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "Provider did not return a usable response", possibly_billed=True, provider_request_id=raw.provider_request_id)
        if not raw.text or not raw.text.strip():
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "Provider returned an empty response", possibly_billed=True, provider_request_id=raw.provider_request_id)
        if request.output_schema is None:
            return raw.text, {"requested": False, "valid": True}
        try:
            parsed = json.loads(raw.text)
        except (json.JSONDecodeError, TypeError):
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "Provider response was not valid JSON", possibly_billed=True, provider_request_id=raw.provider_request_id) from None
        schema = dict(request.output_schema)
        try:
            validator_type = validators.validator_for(schema)
            validator_type.check_schema(schema)
            validator_type(schema).validate(parsed)
        except SchemaError:
            raise ProviderError(ProviderErrorCode.CLIENT, "Configured JSON Schema is invalid") from None
        except ValidationError:
            raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "Provider JSON did not match the configured schema", possibly_billed=True, provider_request_id=raw.provider_request_id) from None
        return parsed, {"requested": True, "valid": True, "schema_ref": request.schema_ref, "validator": "jsonschema"}

    def _cost(self, config: ProviderConfig, requested_model: str, reported_model: str | None,
              usage: ProviderUsage, attempt_started_at) -> CostRecord:
        model_for_price = reported_model or requested_model
        # If a provider changed the model behind an alias, do not apply a stale
        # requested-model price card to the unknown actual model.
        price = self._prices.get((config.provider_id, model_for_price))
        if price is None and self._price_resolver is not None:
            price = self._price_resolver(config, model_for_price, attempt_started_at)
        if price is None or price.effective_from.tzinfo is None or price.effective_from > attempt_started_at:
            return CostRecord()
        return estimate_cost(usage, price)

    def _trace(
        self,
        config: ProviderConfig,
        request: ModelRequest,
        *,
        model: str,
        started_at: datetime,
        attempts: list[AttemptRecord],
        usage: ProviderUsage,
        raw: RawModelResult | None = None,
        error: ProviderError | None = None,
        cost: CostRecord | None = None,
        end_reason: str | None = None,
        first_token_seconds: float | None = None,
        validation_seconds: float | None = None,
        ended_at: datetime | None = None,
    ) -> CallTrace:
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
            requested_model=model,
            reported_model=raw.reported_model if raw else None,
            started_at=started_at,
            ended_at=ended_at or utc_now(),
            queue_seconds=None,
            first_token_seconds=first_token_seconds,
            validation_seconds=validation_seconds,
            attempts=tuple(attempts),
            finish_reason=raw.finish_reason if raw else None,
            usage=usage,
            estimated_usage=None,
            cost=cost or CostRecord(),
            error_code=error.code.value if error else None,
            error_message=error.safe_message if error else None,
            end_reason=end_reason,
            max_output_tokens=request.profile.max_output_tokens,
            transport_failure_kind=error.transport_failure_kind if error else None,
        )

    def _retry_delay(self, error: ProviderError, attempt_number: int) -> float:
        if error.retry_after_seconds is not None:
            return min(30.0, error.retry_after_seconds)
        base = min(4.0, 0.25 * (2 ** (attempt_number - 1)))
        return max(0.0, min(10.0, self._jitter(base / 2, base)))


def _normalize_exception(exc: BaseException) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    # Connect timeouts are a NetworkError as well as a TimeoutException.
    # Handle this concrete pre-send case first so it remains unbilled.
    if isinstance(exc, httpx.ConnectTimeout):
        return ProviderError(
            ProviderErrorCode.NETWORK,
            "Could not connect to provider",
            retryable=True,
            possibly_billed=False,
            transport_failure_kind=TransportFailureKind.CONNECT_TIMEOUT,
        )
    if isinstance(exc, httpx.ConnectError):
        return ProviderError(
            ProviderErrorCode.NETWORK,
            "Could not connect to provider",
            retryable=True,
            possibly_billed=False,
            transport_failure_kind=TransportFailureKind.CONNECT,
        )
    if isinstance(exc, httpx.ReadTimeout):
        return ProviderError(
            ProviderErrorCode.TIMEOUT,
            "Provider request timed out",
            retryable=True,
            possibly_billed=True,
            transport_failure_kind=TransportFailureKind.READ_TIMEOUT,
        )
    if isinstance(exc, httpx.WriteTimeout):
        return ProviderError(
            ProviderErrorCode.TIMEOUT,
            "Provider request timed out",
            retryable=True,
            possibly_billed=True,
            transport_failure_kind=TransportFailureKind.WRITE_TIMEOUT,
        )
    if isinstance(exc, httpx.PoolTimeout):
        return ProviderError(
            ProviderErrorCode.TIMEOUT,
            "Provider request timed out",
            retryable=True,
            possibly_billed=True,
            transport_failure_kind=TransportFailureKind.POOL_TIMEOUT,
        )
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError(
            ProviderErrorCode.TIMEOUT,
            "Provider request timed out",
            retryable=True,
            possibly_billed=True,
            transport_failure_kind=TransportFailureKind.TIMEOUT,
        )
    if isinstance(exc, TimeoutError):
        return ProviderError(
            ProviderErrorCode.TIMEOUT,
            "Provider request timed out",
            retryable=True,
            possibly_billed=True,
            transport_failure_kind=TransportFailureKind.TIMEOUT,
        )
    if isinstance(exc, httpx.RemoteProtocolError):
        return ProviderError(
            ProviderErrorCode.NETWORK,
            "Provider request failed",
            retryable=False,
            possibly_billed=None,
            transport_failure_kind=TransportFailureKind.REMOTE_PROTOCOL,
        )
    if isinstance(exc, httpx.NetworkError):
        return ProviderError(
            ProviderErrorCode.NETWORK,
            "Provider network request failed",
            retryable=True,
            possibly_billed=None,
            transport_failure_kind=TransportFailureKind.NETWORK,
        )
    if isinstance(exc, httpx.RequestError):
        return ProviderError(
            ProviderErrorCode.NETWORK,
            "Provider request failed",
            retryable=False,
            possibly_billed=None,
            transport_failure_kind=TransportFailureKind.REQUEST,
        )
    return ProviderError(ProviderErrorCode.PROTOCOL, "Provider response could not be processed", retryable=False, possibly_billed=None)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value

