from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request

from world_engine.application.conversation import ConversationApplication
from world_engine.domain.errors import InvalidEvent
from .contracts import MessageRequest, NarrativeRewriteRequest
from .serialization import wire


def conversation_router(service: ConversationApplication, scope):
    router = APIRouter(prefix="/api/v1")

    @router.get("/worlds/{world_id}/conversation")
    def conversation(request: Request, world_id: UUID, branch_id: UUID | None = None):
        return service.default_conversation(scope(request), world_id, branch_id)

    @router.post("/worlds/{world_id}/messages")
    async def send(request: Request, world_id: UUID, body: MessageRequest,
                   idempotency_key: Annotated[str | None, Header()] = None):
        if body.world_id != world_id or (idempotency_key is not None and idempotency_key != str(body.request_id)):
            raise InvalidEvent("Request identity or world path does not match its body")
        return wire(await service.send(scope(request), body))

    @router.post("/worlds/{world_id}/narratives/{narrative_id}/rewrite")
    async def rewrite_narrative(request: Request, world_id: UUID, narrative_id: UUID, body: NarrativeRewriteRequest):
        return await service.rewrite_narrative(scope(request), world_id, narrative_id, body.style)

    @router.get("/commands/{command_id}")
    def status(request: Request, command_id: UUID):
        return wire(service.status(scope(request), command_id))

    return router
