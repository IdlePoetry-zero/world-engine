"""HTTP contracts for previewed place and organization creation."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator

from world_engine.domain.components import (
    BoundedName,
    ExactDecimal,
    GovernanceComponent,
    MembershipRecruitmentPolicy,
    ResourceName,
    Tick,
    UnitInterval,
)

from .contracts import RequestModel, Version
from .serialization import wire

class PlaceRouteDraft(RequestModel):
    """Route data supplied by the author; the new origin and route ID are server-bound."""

    to_place_id: UUID
    travel_ticks: StrictInt = Field(gt=0, le=2**63 - 1)
    capacity: StrictInt | None = Field(default=None, gt=0)
    risk: UnitInterval
    access_tags: list[ResourceName] = Field(default_factory=list, max_length=128)
    travel_modes: list[ResourceName] = Field(default_factory=list, max_length=64)
    is_open: StrictBool


class RenewableHarvestRecipientDraft(RequestModel):
    entity_id: UUID
    account_id: UUID
    per_period: ExactDecimal = Field(gt=Decimal("0"))


class RenewableHarvestDraft(RequestModel):
    """A finite harvest plan without source account or past-issuance fields."""

    policy_id: ResourceName
    resource_id: ResourceName
    period_ticks: StrictInt = Field(gt=0, le=2**63 - 1)
    maximum_total: ExactDecimal = Field(gt=Decimal("0"))
    recipients: list[RenewableHarvestRecipientDraft] = Field(min_length=1, max_length=100_000)


class OfficeDraftRequest(RequestModel):
    office_id: ResourceName
    title: BoundedName
    holder_id: UUID | None = None
    authority: list[ResourceName] = Field(default_factory=list, max_length=256)
    term_end_tick: Tick | None = None


class GovernanceDraftRequest(RequestModel):
    """Author-editable governance fields; component revision remains server-owned."""

    charter: str = Field(default="", max_length=8000)
    members: list[UUID] = Field(default_factory=list, max_length=100_000)
    leader_id: UUID | None = None
    offices: list[OfficeDraftRequest] = Field(default_factory=list, max_length=4096)
    succession_policy: Literal["election", "appointment", "inheritance", "council", "none"] = "none"
    decision_threshold: UnitInterval = Decimal("0.5")
    authority_tags: list[ResourceName] = Field(default_factory=list, max_length=256)
    charter_clause_ids: list[ResourceName] = Field(default_factory=list, max_length=4096)
    membership_recruitment: list[MembershipRecruitmentPolicy] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def matches_registered_component(self) -> GovernanceDraftRequest:
        GovernanceComponent.model_validate(self.model_dump(mode="python"))
        return self


class PlaceDraftRequest(RequestModel):
    branch_id: UUID
    expected_state_version: Version
    name: str = Field(min_length=1, max_length=240)
    place_type: Literal["region", "settlement", "building", "wilderness", "site", "other"]
    access_policy: Literal["open", "restricted", "private"]
    region_id: UUID | None = None
    parent_id: UUID | None = None
    capacity: StrictInt | None = Field(default=None, gt=0)
    terrain: ResourceName | None = None
    environment: dict[ResourceName, StrictStr | StrictInt | StrictBool] = Field(default_factory=dict, max_length=128)
    routes: list[PlaceRouteDraft] = Field(default_factory=list, max_length=2048)
    renewable_harvests: list[RenewableHarvestDraft] = Field(default_factory=list, max_length=4096)
    description: str = Field(default="", max_length=4000)

    @field_validator("name")
    @classmethod
    def name_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must contain non-whitespace text")
        return value

    @model_validator(mode="after")
    def harvest_entries_are_unambiguous(self) -> PlaceDraftRequest:
        policy_ids = [item.policy_id.casefold() for item in self.renewable_harvests]
        resource_ids = [item.resource_id.casefold() for item in self.renewable_harvests]
        if len(policy_ids) != len(set(policy_ids)):
            raise ValueError("renewable harvest policy IDs must be unique ignoring case")
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("a place can have only one harvest policy per resource")
        return self


class OrganizationDraftRequest(RequestModel):
    branch_id: UUID
    expected_state_version: Version
    name: str = Field(min_length=1, max_length=240)
    kind: Literal["polity", "organization", "faction", "company", "government"]
    location_id: UUID | None = None
    public_role: str = Field(default="", max_length=1000)
    description: str = Field(default="", max_length=4000)
    governance: GovernanceDraftRequest | None = None
    treasury_resources: list[ResourceName] = Field(default_factory=list, max_length=128)

    @field_validator("name")
    @classmethod
    def name_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must contain non-whitespace text")
        return value

    @field_validator("treasury_resources")
    @classmethod
    def treasury_resource_ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("treasury resource IDs must be unique")
        return value


class PlacePreviewRequest(PlaceDraftRequest):
    pass


class OrganizationPreviewRequest(OrganizationDraftRequest):
    pass


class EntityCommitRequest(RequestModel):
    preview_id: UUID


def entity_authoring_router(service, scope):
    router = APIRouter(prefix="/api/v1/worlds")

    @router.post("/{world_id}/author/places/preview")
    def place_preview(request: Request, world_id: UUID, body: PlacePreviewRequest):
        return wire(service.preview_place(
            scope(request),
            world_id,
            body.branch_id,
            int(body.expected_state_version),
            body.model_dump(mode="json", exclude={"branch_id", "expected_state_version"}),
        ))

    @router.post("/{world_id}/author/places/commit", status_code=201)
    def place_commit(request: Request, world_id: UUID, body: EntityCommitRequest):
        return wire(service.commit_place(scope(request), world_id, body.preview_id))

    @router.post("/{world_id}/author/organizations/preview")
    def organization_preview(request: Request, world_id: UUID, body: OrganizationPreviewRequest):
        return wire(service.preview_organization(
            scope(request),
            world_id,
            body.branch_id,
            int(body.expected_state_version),
            body.model_dump(mode="json", exclude={"branch_id", "expected_state_version"}),
        ))

    @router.post("/{world_id}/author/organizations/commit", status_code=201)
    def organization_commit(request: Request, world_id: UUID, body: EntityCommitRequest):
        return wire(service.commit_organization(scope(request), world_id, body.preview_id))

    return router
