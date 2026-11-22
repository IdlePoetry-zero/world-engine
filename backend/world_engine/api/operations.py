"""Model administration routes with safe errors and explicit discovery."""

from datetime import datetime
from typing import Callable

from fastapi import APIRouter, Request

from world_engine.application.operations import ModelOperations
from world_engine.domain.errors import InvalidEvent
from .contracts import ProviderRequest, SettingsRequest


def operations_router(operations: ModelOperations, scope: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/providers")
    def providers(request: Request):
        return {"items": operations.store.list_providers(scope(request))}

    @router.post("/providers", status_code=201)
    def create_provider(request: Request, body: ProviderRequest):
        try:
            return operations.store.save_provider(
                scope(request), body.model_dump(mode="json"), public_config=True,
            )
        except ValueError as error:
            raise InvalidEvent("Invalid provider configuration") from error

    @router.put("/providers/{provider_id}")
    def update_provider(request: Request, provider_id: str, body: ProviderRequest):
        identity = scope(request)
        operations.store.provider(identity, provider_id)
        try:
            return operations.store.save_provider(
                identity, body.model_dump(mode="json"), provider_id, public_config=True,
            )
        except ValueError as error:
            raise InvalidEvent("Invalid provider configuration") from error

    @router.get("/settings")
    def settings(request: Request):
        return operations.store.settings(scope(request))

    @router.put("/settings")
    def save_settings(request: Request, body: SettingsRequest):
        try:
            return operations.store.save_settings(scope(request), body.model_dump())
        except ValueError as error:
            raise InvalidEvent("Invalid role route") from error

    @router.get("/models")
    async def models(request: Request, provider_id: str):
        discovered = await operations.discover(scope(request), provider_id)
        return {"items": [_model_view(model) for model in discovered]}

    @router.post("/providers/{provider_id}/probe")
    async def probe(request: Request, provider_id: str):
        discovered = await operations.discover(scope(request), provider_id)
        return {"status": "discovered", "models": [_model_view(model) for model in discovered],
                "detail": "已读取供应商模型目录；这不代表生成能力已验证。"}

    @router.get("/operations/calls")
    def calls(request: Request):
        return {"items": [_call_view(item) for item in operations.store.list_calls(scope(request))]}

    return router


def _model_view(model):
    return {"id": model["model_id"], "name": model.get("display_name") or model["model_id"],
            "capability_evidence": model.get("capabilities"), "provider_id": model.get("provider_id")}


def _call_view(trace):
    provider_seconds = (datetime.fromisoformat(trace["ended_at"]) - datetime.fromisoformat(trace["started_at"])).total_seconds()
    queue_seconds = trace.get("queue_seconds") or 0
    duration_ms = (max(0, provider_seconds) + queue_seconds) * 1000
    return {
        "request_id": trace["request_id"], "role": trace["role_task"], "provider": trace["provider_id"],
        "model": trace["reported_model"] or trace["requested_model"], "started_at": trace["started_at"],
        "duration_ms": duration_ms, "usage": trace["usage"], "cost": trace["cost"],
        "status": trace["end_reason"], "trace": trace,
    }
