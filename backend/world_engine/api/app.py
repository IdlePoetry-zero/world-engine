"""Application composition and HTTP world routes.

The factory takes real application services; tests can inject isolated database
instances. Startup/bootstrap is separate from import to avoid creating files or
running migrations as an import side effect.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from world_engine.application.worlds import WorldApplication
from world_engine.application.operations import ModelOperations
from world_engine.application.credentials import CredentialService
from world_engine.adapters.models.errors import ProviderError
from world_engine.domain.errors import AccessDenied, Conflict, DomainError, NotFound, StaleVersion
from world_engine.domain.models import AccessScope

from .contracts import CreateWorldRequest
from .security import LocalSession, LocalSessionMiddleware
from .serialization import wire


def create_app(worlds: WorldApplication, session: LocalSession, *, operations: ModelOperations | None = None, credentials: CredentialService | None = None, static_directory: Path | None = None) -> FastAPI:
    app = FastAPI(title="万象世界引擎", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalSessionMiddleware, session=session)
    app.state.worlds = worlds

    def scope(request: Request) -> AccessScope:
        return AccessScope(
            tenant_id=UUID(request.state.tenant_id),
            user_id=UUID(request.state.principal_id),
            correlation_id=uuid4(),
        )

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError):
        status, code, message = 422, "RULE_REJECTED", "当前条件下不能完成这项操作。"
        if isinstance(error, AccessDenied):
            status, code, message = 403, "FORBIDDEN", "当前身份没有操作权限。"
        elif isinstance(error, NotFound):
            status, code, message = 404, "NOT_FOUND", "找不到该资源。"
        elif isinstance(error, StaleVersion):
            status, code, message = 409, "STALE_VERSION", "世界已发生变化，请刷新后重试。"
        elif isinstance(error, Conflict):
            status, code, message = 409, "CONFLICT", "请求与已有记录冲突，请检查操作状态。"
        return JSONResponse({"error": {"code": code, "message": message, "retryable": False}}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        # Do not echo input values: provider configuration may contain a value
        # accidentally pasted into the wrong field.
        fields = [{"path": ".".join(str(p) for p in item["loc"]), "type": item["type"]} for item in error.errors()]
        return JSONResponse(
            {"error": {"code": "INVALID_REQUEST", "message": "输入字段不符合要求。", "field_errors": fields, "retryable": False}},
            status_code=422,
        )

    @app.exception_handler(ProviderError)
    async def provider_error(request: Request, error: ProviderError):
        return JSONResponse({"error": {"code": error.code.value,
            "message": "模型请求未完成，请检查供应商配置与调用记录。", "retryable": error.retryable}}, status_code=502)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "world-engine"}

    @app.get("/api/v1/session")
    def get_session(request: Request):
        return {
            "principal": {"id": request.state.principal_id, "display_name": "本地玩家"},
            "permissions": ["world.create", "world.own.manage"],
        }

    @app.get("/api/v1/worlds")
    def list_worlds(request: Request):
        return wire(worlds.list_worlds(scope(request)))

    @app.get("/api/v1/packs")
    def list_packs(request: Request):
        return wire(worlds.list_packs())

    @app.post("/api/v1/worlds", status_code=201)
    def create_world(
        request: Request,
        body: CreateWorldRequest,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        return wire(worlds.create_world(scope(request), body.pack_id, body.title, body.player_name, idempotency_key))

    @app.get("/api/v1/worlds/{world_id}/view")
    def world_view(
        request: Request, world_id: UUID, branch_id: UUID | None = None,
        mode: Literal["player", "author"] = "player",
    ):
        return wire(worlds.view_world(scope(request), world_id, branch_id, author=mode == "author"))

    @app.get("/api/v1/worlds/{world_id}/branches")
    def list_branches(request: Request, world_id: UUID):
        return wire(worlds.list_branches(scope(request), world_id))

    if operations is not None:
        from .operations import operations_router
        app.include_router(operations_router(operations, scope))

    if credentials is not None:
        from .credentials import credential_router
        app.include_router(credential_router(credentials, scope))

    if static_directory is not None and static_directory.is_dir():
        app.mount("/", StaticFiles(directory=static_directory, html=True), name="frontend")
    return app
