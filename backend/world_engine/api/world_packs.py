"""Bounded, authenticated world template ZIP operations."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request
from fastapi.responses import Response
from pydantic import StringConstraints
from starlette.concurrency import run_in_threadpool

from world_engine.application.errors import ApplicationFailure
from world_engine.application.template_migration import TemplateMigrationApplication
from world_engine.application.world_pack_import import WorldPackApplication
from world_engine.application.world_packs import MAX_WORLD_PACK_BYTES, STORY_OPENING_ID_PATTERN
from world_engine.domain.models import MembershipRole

from .contracts import RequestModel
from .serialization import wire


async def _archive_body(request: Request) -> bytes:
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/zip":
        raise ApplicationFailure("WORLD_PACK_FORMAT_REQUIRED", "请上传世界包 ZIP。", status_code=415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_WORLD_PACK_BYTES:
            raise ApplicationFailure("WORLD_PACK_TOO_LARGE", "世界包超出 ZIP 上传大小限制。", status_code=413)
        body.extend(chunk)
    return bytes(body)


class TemplateUpdateCommit(RequestModel):
    preview_id: UUID
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


def world_pack_router(service: WorldPackApplication, scope, authoring=None):
    router = APIRouter(prefix="/api/v1")

    @router.post("/packs/import/preview")
    async def preview_world_pack(request: Request):
        data = await _archive_body(request)
        return wire(await run_in_threadpool(service.preview, data))

    @router.post("/packs/import", status_code=201)
    async def import_world_pack(
        request: Request,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
        x_world_pack_sha256: Annotated[str, Header(pattern=r"^[a-f0-9]{64}$")],
        x_story_opening: Annotated[str | None, Header(pattern=STORY_OPENING_ID_PATTERN)] = None,
    ):
        identity = scope(request)
        data = await _archive_body(request)
        return wire(await run_in_threadpool(service.import_world, identity, data, x_world_pack_sha256, idempotency_key,
                                            x_story_opening))

    @router.get("/worlds/{world_id}/source-world-pack")
    def download_author_source_package(request: Request, world_id: UUID):
        return Response(
            service.author_source(scope(request), world_id), media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="author-world-pack-{world_id}.zip"',
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            },
        )

    if authoring is not None:
        migration = TemplateMigrationApplication(service, authoring)

        @router.post("/worlds/{world_id}/template-updates/preview")
        async def preview_template_update(request: Request, world_id: UUID, branch_id: UUID):
            identity = scope(request)
            await run_in_threadpool(service.worlds.repository.authorize_role, identity, world_id,
                                    {MembershipRole.OWNER, MembershipRole.EDITOR})
            data = await _archive_body(request)
            return wire(await run_in_threadpool(migration.preview, identity, world_id, branch_id, data))

        @router.post("/worlds/{world_id}/template-updates", status_code=201)
        def apply_template_update(request: Request, world_id: UUID, body: TemplateUpdateCommit):
            return wire(migration.commit(scope(request), world_id, body.preview_id, body.label))

    return router
