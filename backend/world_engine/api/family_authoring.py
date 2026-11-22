"""HTTP routes for private, author-recorded family links."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import Field, field_validator

from .contracts import RequestModel, Version


WorldTick = Annotated[str, Field(pattern=r"^(0|-?[1-9][0-9]{0,18})$")]


class KinshipLinkCreateRequest(RequestModel):
    branch_id: UUID
    expected_state_version: Version
    parent_id: UUID
    child_id: UUID
    relationship: Literal["birth", "adoptive"]


class KinshipLinkEndRequest(RequestModel):
    branch_id: UUID
    expected_state_version: Version
    expected_created_tick: WorldTick
    expected_relationship: Literal["birth", "adoptive"] | None = None

    @field_validator("expected_created_tick")
    @classmethod
    def tick_fits_signed_world_clock(cls, value: str) -> str:
        tick = int(value)
        if not -(2**63) <= tick <= 2**63 - 1:
            raise ValueError("expected_created_tick is outside the signed world clock range")
        return value


def family_authoring_router(service, scope):
    """Build the router around a ``FamilyAuthoringApplication`` and scope resolver.

    Runtime composition can include the returned router with the same
    ``scope(request) -> AccessScope`` callback used by other API routers.
    """

    router = APIRouter(prefix="/api/v1/worlds")

    @router.post("/{world_id}/author/kinship-links", status_code=201)
    def record_kinship_link(
        request: Request,
        world_id: UUID,
        body: KinshipLinkCreateRequest,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200, alias="Idempotency-Key")],
    ):
        return service.record_link(
            scope(request), world_id, body.branch_id, int(body.expected_state_version),
            parent_id=body.parent_id, child_id=body.child_id, relationship=body.relationship,
            idempotency_key=idempotency_key,
        )

    @router.post("/{world_id}/author/kinship-links/{link_id}/end")
    def end_kinship_link(
        request: Request,
        world_id: UUID,
        link_id: UUID,
        body: KinshipLinkEndRequest,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200, alias="Idempotency-Key")],
    ):
        return service.end_link(
            scope(request), world_id, body.branch_id, int(body.expected_state_version),
            link_id=link_id, expected_created_tick=int(body.expected_created_tick),
            expected_relationship=body.expected_relationship, idempotency_key=idempotency_key,
        )

    return router
