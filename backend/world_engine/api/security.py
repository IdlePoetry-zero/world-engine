"""Local browser boundary; authentication can later be replaced by an identity adapter.

Loopback is not sufficient protection against requests sent by an unrelated web
page. Require an exact host/origin and an HttpOnly session cookie. This boundary
does not claim to protect against software already controlling the local user.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from hmac import compare_digest
from typing import Protocol
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


@dataclass(frozen=True)
class LocalSession:
    principal_id: str
    tenant_id: str
    allowed_origins: frozenset[str]
    token: str = field(default_factory=lambda: secrets.token_urlsafe(48), repr=False)
    cookie_name: str = "world_engine_session"

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(urlsplit(origin).netloc.lower() for origin in self.allowed_origins)


class IdentityProvider(Protocol):
    """Resolve a session using server-controlled authentication context."""

    async def authenticate(self, request: Request) -> AuthenticatedIdentity | None: ...


@dataclass(frozen=True)
class AuthenticatedIdentity:
    principal_id: str
    tenant_id: str


class LocalSessionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, session: LocalSession, identity_provider: IdentityProvider | None = None):
        super().__init__(app)
        self.session = session
        self.identity_provider = identity_provider

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        session = self.session
        host = request.headers.get("host", "").lower()
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site", "")
        if host not in session.allowed_hosts:
            return self._error("INVALID_HOST", "不允许的访问地址。", 400)
        if origin is not None and origin not in session.allowed_origins:
            return self._error("FORBIDDEN_ORIGIN", "此来源不能访问本地世界。", 403)
        if fetch_site == "cross-site":
            return self._error("FORBIDDEN_ORIGIN", "拒绝跨站访问。", 403)

        is_api = request.url.path.startswith("/api/")
        bootstrap = request.url.path == "/api/v1/session" and request.method == "GET"
        provided = request.cookies.get(session.cookie_name, "")
        authenticated = compare_digest(provided.encode(), session.token.encode())
        if self.identity_provider is None:
            identity = AuthenticatedIdentity(session.principal_id, session.tenant_id) if authenticated or bootstrap else None
        else:
            identity = await self.identity_provider.authenticate(request) if is_api else None
        if is_api and identity is None:
            return self._error("AUTH_REQUIRED", "请重新连接本地会话。", 401)

        # Only the local session identity is authoritative. Client-supplied user
        # and tenant fields must never replace these values in the application.
        if identity is not None:
            request.state.principal_id = identity.principal_id
            request.state.tenant_id = identity.tenant_id
        response = await call_next(request)
        if self.identity_provider is None and bootstrap and response.status_code < 400:
            response.set_cookie(
                session.cookie_name,
                session.token,
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
                path="/api",
            )
        if is_api:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @staticmethod
    def _error(code: str, message: str, status: int) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": code, "message": message, "retryable": False}},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )
