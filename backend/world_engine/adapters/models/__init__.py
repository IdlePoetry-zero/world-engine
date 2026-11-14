"""Typed model provider adapters and the normalized model gateway."""

from .anthropic import AnthropicMessagesAdapter
from .chat_completions import ChatCompletionsAdapter, DeepSeekAdapter
from .contracts import (
    AttemptRecord,
    CallTrace,
    Capability,
    CapabilityEvidence,
    CapabilityState,
    ChatMessage,
    CostRecord,
    CostState,
    DataPolicy,
    DepthProfile,
    DiscoveredModel,
    FinishReason,
    GenerationProfile,
    ModelCapabilities,
    ModelRequest,
    ModelResult,
    NormalizedEvent,
    PriceVersion,
    ProviderConfig,
    ProviderKind,
    ProviderUsage,
    RoleTask,
    SpeedProfile,
    StreamEventKind,
    TimeoutPolicy,
    TraceContext,
    UsageSource,
    execution_profile,
    to_jsonable,
)
from .errors import ProviderError, ProviderErrorCode
from .gateway import ModelGateway
from .ollama import OllamaAdapter, local_ollama_config
from .routing import ProviderTarget, RoleRoutingConfig, default_deepseek_routing
from .secrets import CompositeSecretResolver, EnvironmentSecretResolver, OSCredentialSecretResolver, SecretResolver

__all__ = [
    "AnthropicMessagesAdapter", "AttemptRecord", "CallTrace", "Capability", "CapabilityEvidence",
    "CapabilityState", "ChatCompletionsAdapter", "ChatMessage", "CompositeSecretResolver",
    "CostRecord", "CostState", "DataPolicy", "DeepSeekAdapter", "DepthProfile", "DiscoveredModel",
    "EnvironmentSecretResolver", "FinishReason", "GenerationProfile", "ModelCapabilities",
    "ModelGateway", "ModelRequest", "ModelResult", "NormalizedEvent", "OSCredentialSecretResolver",
    "OllamaAdapter", "PriceVersion", "ProviderConfig", "ProviderError", "ProviderErrorCode",
    "ProviderKind", "ProviderTarget", "ProviderUsage", "RoleRoutingConfig", "RoleTask", "SecretResolver",
    "SpeedProfile", "StreamEventKind", "TimeoutPolicy", "TraceContext", "UsageSource", "default_deepseek_routing",
    "execution_profile", "local_ollama_config", "to_jsonable",
]
