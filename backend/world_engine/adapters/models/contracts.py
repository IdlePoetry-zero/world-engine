"""Typed contracts shared by model providers and the application gateway.

The objects in this module deliberately contain configuration and normalized
metadata only. Resolved secrets and provider request bodies never enter a
ModelResult or CallTrace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import ipaddress
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProviderKind(str, Enum):
    DEEPSEEK = "deepseek"
    OPENAI_COMPATIBLE = "openai_compatible"
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"

class RoleTask(str, Enum):
    INTENT = "intent"
    GM_ANSWER = "gm_answer"
    NPC_DECISION = "npc_decision"
    SOCIAL_ADJUDICATION = "social_adjudication"
    NARRATION = "narration"
    MEMORY_SUMMARY = "memory_summary"
    AUTHOR_ASSIST = "author_assist"
    EMBEDDING = "embedding"


class SpeedProfile(str, Enum):
    FAST = "fast"
    BALANCED = "balanced"
    PATIENT = "patient"


class DepthProfile(str, Enum):
    STANDARD = "standard"
    DEEP = "deep"
    EXHAUSTIVE = "exhaustive"


class DataPolicy(str, Enum):
    LOCAL_ONLY = "local_only"
    CLOUD_ALLOWED = "cloud_allowed"


class Capability(str, Enum):
    TEXT_GENERATION = "text_generation"
    STREAMING = "streaming"
    JSON_MODE = "json_mode"
    JSON_SCHEMA = "json_schema"
    TOOL_USE = "tool_use"
    EMBEDDINGS = "embeddings"
    IMAGE_INPUT = "image_input"
    THINKING = "thinking"
    EFFORT_CONTROL = "effort_control"
    USAGE_REPORTING = "usage_reporting"
    CANCELLATION = "cancellation"


class CapabilityState(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class UsageSource(str, Enum):
    REPORTED = "reported"
    UNKNOWN = "unknown"


class CostState(str, Enum):
    REPORTED = "reported"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class FinishReason(str, Enum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    FILTERED = "filtered"
    ABORTED = "aborted"
    OTHER = "other"


class StreamEventKind(str, Enum):
    STARTED = "started"
    TEXT_DELTA = "text_delta"
    USAGE = "usage"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "developer", "user", "assistant"}:
            raise ValueError(f"unsupported message role: {self.role}")
        if not isinstance(self.content, str):
            raise TypeError("message content must be text")


@dataclass(frozen=True, slots=True)
class TraceContext:
    request_id: str = field(default_factory=lambda: str(uuid4()))
    parent_trace: str | None = None
    world_id: str | None = None
    branch_id: str | None = None
    job_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class GenerationProfile:
    speed: SpeedProfile = SpeedProfile.BALANCED
    depth: DepthProfile = DepthProfile.STANDARD
    max_output_tokens: int | None = 2048

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")


@dataclass(frozen=True, slots=True)
class TimeoutPolicy:
    connect_seconds: float = 10.0
    read_seconds: float = 180.0
    write_seconds: float = 30.0
    pool_seconds: float = 10.0
    deadline_seconds: float = 600.0

    def __post_init__(self) -> None:
        bounded = {
            "connect_seconds": (self.connect_seconds, 60.0),
            "read_seconds": (self.read_seconds, 600.0),
            "write_seconds": (self.write_seconds, 600.0),
            "pool_seconds": (self.pool_seconds, 60.0),
            "deadline_seconds": (self.deadline_seconds, 600.0),
        }
        for name, (value, upper_bound) in bounded.items():
            if value <= 0 or value > upper_bound:
                raise ValueError(f"{name} must be in (0, {upper_bound:g}]")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    role_task: RoleTask
    messages: tuple[ChatMessage, ...] | Sequence[ChatMessage]
    schema_ref: str | None = None
    output_schema: Mapping[str, Any] | None = None
    capability_requirements: frozenset[Capability] = frozenset()
    profile: GenerationProfile = field(default_factory=GenerationProfile)
    timeout_policy: TimeoutPolicy = field(default_factory=TimeoutPolicy)
    trace_context: TraceContext = field(default_factory=TraceContext)
    data_policy: DataPolicy = DataPolicy.CLOUD_ALLOWED
    model: str | None = None
    temperature: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        if not self.messages:
            raise ValueError("messages must contain at least one message")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if self.output_schema is not None and self.schema_ref is None:
            object.__setattr__(self, "schema_ref", "inline")


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider_id: str
    kind: ProviderKind
    base_url: str
    default_model: str | None = None
    secret_ref: str | None = None
    max_retries: int = 2
    capability_overrides: Mapping[str, bool | None] = field(default_factory=dict)
    allow_insecure_http: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ProviderKind):
            try:
                object.__setattr__(self, "kind", ProviderKind(self.kind))
            except (ValueError, TypeError) as exc:
                raise ValueError("unsupported provider kind") from exc
        if not self.provider_id.strip():
            raise ValueError("provider_id is required")
        if self.secret_ref is not None and not (self.secret_ref.startswith("env:") or self.secret_ref.startswith("os:")):
            raise ValueError("secret_ref must reference an environment or OS credential entry")
        try:
            parsed = urlsplit(self.base_url)
            hostname = parsed.hostname
            port = parsed.port  # Parse eagerly so malformed ports fail here.
        except ValueError as exc:
            raise ValueError("base_url is malformed") from exc
        if parsed.scheme not in {"http", "https"} or not hostname:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain userinfo, query parameters, or a fragment")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("base_url port is out of range")
        host = hostname.rstrip(".").lower()
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        if parsed.scheme == "http" and not (is_loopback or self.allow_insecure_http):
            raise ValueError("non-loopback providers require HTTPS unless explicitly allowed")
        if not 0 <= self.max_retries <= 2:
            raise ValueError("max_retries must be between 0 and 2")
        for key, value in self.capability_overrides.items():
            try:
                Capability(key)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"unknown capability override: {key}") from exc
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"capability override {key} must be true, false, or null")
        # Freeze overrides so a live request cannot observe config mutation.
        object.__setattr__(self, "capability_overrides", MappingProxyType(dict(self.capability_overrides)))

    def public_dict(self) -> dict[str, Any]:
        """Return safe REST/config output; no resolved credential is stored here."""
        return {
            "provider_id": self.provider_id,
            "kind": self.kind.value,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "secret_ref": self.secret_ref,
            "max_retries": self.max_retries,
            "capability_overrides": dict(self.capability_overrides),
            "allow_insecure_http": self.allow_insecure_http,
        }

    @property
    def is_local_endpoint(self) -> bool:
        parsed = urlsplit(self.base_url)
        host = (parsed.hostname or "").rstrip(".").lower()
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return host == "localhost"


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    capability: Capability
    state: CapabilityState
    source: str
    probe_result: str
    discovered_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    facts: Mapping[Capability, CapabilityEvidence]
    max_context_tokens: int | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))

    def state(self, capability: Capability) -> CapabilityState:
        fact = self.facts.get(capability)
        return fact.state if fact else CapabilityState.UNKNOWN

    @classmethod
    def unknown(cls, *, protocol_text: bool = False) -> "ModelCapabilities":
        facts: dict[Capability, CapabilityEvidence] = {}
        for capability in Capability:
            state = CapabilityState.SUPPORTED if protocol_text and capability is Capability.TEXT_GENERATION else CapabilityState.UNKNOWN
            facts[capability] = CapabilityEvidence(
                capability=capability,
                state=state,
                source="protocol" if state is CapabilityState.SUPPORTED else "not_discovered",
                probe_result="text chat endpoint is available" if state is CapabilityState.SUPPORTED else "not probed or declared",
            )
        return cls(facts)


@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    model_id: str
    provider_id: str
    display_name: str | None = None
    owned_by: str | None = None
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities.unknown)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_public_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Provider-reported counters. Missing values remain None, never fabricated zeroes."""

    source: UsageSource = UsageSource.UNKNOWN
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    non_cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    other_items: Mapping[str, int | str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("input_tokens", "cached_input_tokens", "non_cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")
        object.__setattr__(self, "other_items", MappingProxyType(dict(self.other_items)))


@dataclass(frozen=True, slots=True)
class PriceVersion:
    provider_id: str
    model_id: str
    currency: str
    source_url: str
    effective_from: datetime
    input_per_million: Decimal | None = None
    cached_input_per_million: Decimal | None = None
    output_per_million: Decimal | None = None
    other_per_unit: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "other_per_unit", MappingProxyType(dict(self.other_per_unit)))


@dataclass(frozen=True, slots=True)
class CostRecord:
    state: CostState = CostState.UNKNOWN
    amount: Decimal | None = None
    currency: str | None = None
    source_url: str | None = None
    price_effective_from: datetime | None = None
    components: Mapping[str, Decimal | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", MappingProxyType(dict(self.components)))

    def to_public_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt_number: int
    started_at: datetime
    ended_at: datetime
    elapsed_seconds: float
    outcome: str
    status_code: int | None = None
    provider_request_id: str | None = None
    retry_after_seconds: float | None = None
    retryable: bool = False
    possibly_billed: bool | None = None
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class CallTrace:
    request_id: str
    parent_trace: str | None
    world_id: str | None
    branch_id: str | None
    job_id: str | None
    role_task: RoleTask
    speed_profile: SpeedProfile
    depth_profile: DepthProfile
    data_policy: DataPolicy
    provider_id: str
    provider_kind: ProviderKind
    requested_model: str
    reported_model: str | None
    started_at: datetime
    ended_at: datetime
    queue_seconds: float | None
    first_token_seconds: float | None
    validation_seconds: float | None
    attempts: tuple[AttemptRecord, ...]
    finish_reason: FinishReason | None
    usage: ProviderUsage
    estimated_usage: ProviderUsage | None = None
    cost: CostRecord = field(default_factory=CostRecord)
    error_code: str | None = None
    error_message: str | None = None
    end_reason: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ModelResult:
    normalized_output: str | Any
    provider_usage: ProviderUsage
    estimated_usage: ProviderUsage | None
    cost: CostRecord
    finish_reason: FinishReason
    provider_request_id: str | None
    requested_model: str
    reported_model: str | None
    timings: Mapping[str, float | None]
    validation: Mapping[str, Any]
    call_trace: CallTrace

    def __post_init__(self) -> None:
        object.__setattr__(self, "timings", MappingProxyType(dict(self.timings)))
        object.__setattr__(self, "validation", MappingProxyType(dict(self.validation)))

    def to_public_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    kind: StreamEventKind
    request_id: str
    provider_id: str
    model: str
    text: str | None = None
    usage: ProviderUsage | None = None
    provider_request_id: str | None = None
    finish_reason: FinishReason | None = None
    result: ModelResult | None = None
    provisional: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    """Depth drives simulation work; speed controls scheduling, never temperature."""

    speed: SpeedProfile
    depth: DepthProfile
    candidate_count: int
    memory_limit: int
    causal_expansion_layers: int
    semantic_review_rounds: int
    max_steady_macro_days: int
    reflection_mode: str
    concurrency_mode: str
    output_mode: str


def execution_profile(speed: SpeedProfile, depth: DepthProfile) -> RuntimeProfile:
    depth_values = {
        DepthProfile.STANDARD: (2, 12, 1, 1, 30, "salient_events"),
        DepthProfile.DEEP: (4, 24, 2, 1, 14, "salient_events_and_plans"),
        DepthProfile.EXHAUSTIVE: (6, 40, 3, 2, 7, "salient_events_plans_and_conflicts"),
    }[depth]
    speed_values = {
        SpeedProfile.FAST: ("bounded_parallel", "progressive"),
        SpeedProfile.BALANCED: ("fair_queue", "normal_streaming"),
        SpeedProfile.PATIENT: ("resource_conservative", "validate_before_presenting"),
    }[speed]
    return RuntimeProfile(speed, depth, *depth_values, *speed_values)


def to_jsonable(value: Any) -> Any:
    """Convert typed operation records to JSON-safe data without `asdict`.

    `MappingProxyType` is used for immutable mappings and is not deepcopy-able,
    which makes `dataclasses.asdict()` unsuitable for API or persistence output.
    """
    from dataclasses import fields, is_dataclass

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k.value if isinstance(k, Enum) else k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [to_jsonable(item) for item in value]
    if is_dataclass(value):
        return {field_.name: to_jsonable(getattr(value, field_.name)) for field_ in fields(value)}
    return value

