"""Provider-neutral, secret-safe model adapter errors."""

from __future__ import annotations

import re
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contracts import CallTrace


class ProviderErrorCode(str, Enum):
    AUTHENTICATION = "authentication"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK = "network"
    SERVER = "server"
    CLIENT = "client"
    CONTEXT_LIMIT = "context_limit"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    INVALID_OUTPUT = "invalid_output"
    TRUNCATED = "truncated"
    PROTOCOL = "protocol"
    CANCELLED = "cancelled"
    SECRET = "secret_resolution"


class TransportFailureKind(str, Enum):
    """A fixed, provider-neutral classification for transport failures."""

    CONNECT_TIMEOUT = "connect_timeout"
    CONNECT = "connect"
    READ_TIMEOUT = "read_timeout"
    WRITE_TIMEOUT = "write_timeout"
    POOL_TIMEOUT = "pool_timeout"
    TIMEOUT = "timeout"
    REMOTE_PROTOCOL = "remote_protocol"
    NETWORK = "network"
    REQUEST = "request"


_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_KEY_RE = re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{12,})\b")


def redact_text(value: str, secret: str | None = None) -> str:
    """Redact known key shapes and an active credential before surfacing text."""
    safe = _BEARER_RE.sub("Bearer [REDACTED]", value)
    safe = _KEY_RE.sub("[REDACTED]", safe)
    if secret:
        safe = safe.replace(secret, "[REDACTED]")
    return safe


class ProviderError(RuntimeError):
    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        provider_request_id: str | None = None,
        possibly_billed: bool | None = None,
        transport_failure_kind: TransportFailureKind | str | None = None,
        call_trace: "CallTrace | None" = None,
    ) -> None:
        self.code = code
        self.safe_message = redact_text(message)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.provider_request_id = provider_request_id
        self.possibly_billed = possibly_billed
        try:
            self.transport_failure_kind = (
                TransportFailureKind(transport_failure_kind)
                if transport_failure_kind is not None else None
            )
        except (TypeError, ValueError):
            # Never persist caller-controlled text as a diagnostic category.
            self.transport_failure_kind = None
        self.call_trace = call_trace
        super().__init__(self.safe_message)

