"""Validated public data envelopes shared by domain, storage, and API code."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import to_jsonable_python


JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
ResourceId = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")]
EntityKind = Annotated[str, Field(min_length=1, max_length=96, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")]


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class MembershipRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    PLAYER = "player"
    OBSERVER = "observer"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AccessScope(ContractModel):
    tenant_id: UUID
    user_id: UUID
    correlation_id: UUID | None = None


class CalendarConfig(ContractModel):
    day_seconds: int = Field(default=86_400, ge=1, le=2**31 - 1)
    year_days: int = Field(default=360, ge=1, le=2**31 - 1)
    calendar_id: str = Field(default="core.standard", min_length=1, max_length=160)


class ResourceAccount(ContractModel):
    resource_id: ResourceId
    balance: Decimal = Field(ge=Decimal("0"))
    owner_id: UUID | None = None

    @field_validator("balance", mode="before")
    @classmethod
    def parse_exact_decimal(cls, value: object) -> Decimal:
        if isinstance(value, float):
            raise ValueError("resource balances must use decimal strings or integers, not binary floats")
        if not isinstance(value, (str, int, Decimal)):
            raise ValueError("resource balances must be decimal strings or integers")
        try:
            amount = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("resource balance is not a valid decimal") from exc
        if not amount.is_finite():
            raise ValueError("resource balance must be finite")
        scale, integer_digits = _decimal_shape(amount)
        if scale > 6:
            raise ValueError("resource balance supports at most 6 fractional digits")
        if integer_digits > 30:
            raise ValueError("resource balance supports at most 30 integer digits")
        return amount


class WorldEntity(ContractModel):
    id: UUID
    kind: EntityKind
    name: str | None = Field(default=None, max_length=240)
    location_id: UUID | None = None
    status: str = Field(default="active", min_length=1, max_length=64)
    components: JsonObject = Field(default_factory=dict)


class WorldState(ContractModel):
    """A typed state envelope retaining the v1 world projection shape."""

    current_tick: int = Field(default=0, ge=-(2**63), le=2**63 - 1)
    entities: dict[UUID, WorldEntity] = Field(default_factory=dict)
    resources: dict[UUID, ResourceAccount] = Field(default_factory=dict)
    relations: list[JsonObject] = Field(default_factory=list)
    knowledge: list[JsonObject] = Field(default_factory=list)
    goals: list[JsonObject] = Field(default_factory=list)
    scheduled_events: list[JsonObject] = Field(default_factory=list)
    player_id: UUID | None = None
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_state_invariants(self) -> WorldState:
        for entity_id, entity in self.entities.items():
            if entity_id != entity.id:
                raise ValueError(f"entity key {entity_id} does not match entity id {entity.id}")
            if entity.location_id is not None:
                location = self.entities.get(entity.location_id)
                if location is None or location.kind not in {"location", "place"}:
                    raise ValueError(f"entity {entity_id} references a missing or non-location location")
                if location.status in {"dead", "retired"}:
                    raise ValueError(f"entity {entity_id} references a retired location")
                if entity_id == entity.location_id:
                    raise ValueError(f"entity {entity_id} cannot be located inside itself")
        if self.player_id is not None:
            player = self.entities.get(self.player_id)
            if player is None or player.kind not in {"character", "person"}:
                raise ValueError("player_id must reference an existing character entity")
        for account_id, account in self.resources.items():
            if not isinstance(account_id, UUID):
                raise ValueError("resource account keys must be UUIDs")
            if account.balance < 0:
                raise ValueError(f"resource account {account_id} has a negative balance")
            if account.owner_id is not None and account.owner_id not in self.entities:
                raise ValueError(f"resource account {account_id} references a missing owner entity")
        _validate_reference_graph(self.entities)
        return self


class NewWorld(ContractModel):
    world_id: UUID = Field(default_factory=uuid4)
    branch_id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=240)
    ruleset_version: str = Field(default="1.0.0", min_length=1, max_length=100)
    pack_version: str = Field(default="1.0.0", min_length=1, max_length=100)
    state: WorldState = Field(default_factory=WorldState)


class WorldRecord(ContractModel):
    world_id: UUID
    tenant_id: UUID
    owner_id: UUID
    title: str
    active_branch_id: UUID
    ruleset_version: str
    pack_version: str
    created_at: datetime


class BranchRecord(ContractModel):
    branch_id: UUID
    world_id: UUID
    name: str
    parent_branch_id: UUID | None
    fork_seq: int = Field(ge=0)
    head_seq: int = Field(ge=0)
    state_version: int = Field(ge=0)
    current_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime


class WorldCreated(ContractModel):
    world: WorldRecord
    branch: BranchRecord


class WorldView(ContractModel):
    world: WorldRecord
    branch: BranchRecord
    state: WorldState
    last_seq: int = Field(ge=0)
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class EntityCreatedEvent(ContractModel):
    type: Literal["core.entity_created.v1"] = "core.entity_created.v1"
    event_id: UUID = Field(default_factory=uuid4)
    entity: WorldEntity
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class EntityFieldChangedEvent(ContractModel):
    type: Literal["core.entity_field_changed.v1"] = "core.entity_field_changed.v1"
    event_id: UUID = Field(default_factory=uuid4)
    entity_id: UUID
    field_name: Literal["name", "location_id", "status"]
    value: str | UUID | None
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class ResourceTransferredEvent(ContractModel):
    type: Literal["core.resource_transferred.v1"] = "core.resource_transferred.v1"
    event_id: UUID = Field(default_factory=uuid4)
    from_account_id: UUID
    to_account_id: UUID
    resource_id: ResourceId
    quantity: Decimal = Field(gt=Decimal("0"))
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @field_validator("quantity", mode="before")
    @classmethod
    def parse_exact_quantity(cls, value: object) -> Decimal:
        if isinstance(value, float) or not isinstance(value, (str, int, Decimal)):
            raise ValueError("resource quantities must use decimal strings or integers, not binary floats")
        try:
            quantity = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("resource quantity is not a valid decimal") from exc
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("resource quantity must be finite and greater than zero")
        scale, integer_digits = _decimal_shape(quantity)
        if scale > 6:
            raise ValueError("resource quantity supports at most 6 fractional digits")
        if integer_digits > 30:
            raise ValueError("resource quantity supports at most 30 integer digits")
        return quantity


class TimeAdvancedEvent(ContractModel):
    type: Literal["core.time_advanced.v1"] = "core.time_advanced.v1"
    event_id: UUID = Field(default_factory=uuid4)
    to_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


type DomainEvent = Annotated[
    EntityCreatedEvent | EntityFieldChangedEvent | ResourceTransferredEvent | TimeAdvancedEvent,
    Field(discriminator="type"),
]


class CommandEnvelope(ContractModel):
    command_id: UUID = Field(default_factory=uuid4)
    actor_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_state_version: int = Field(ge=0)
    command_type: str = Field(min_length=1, max_length=160)
    payload: JsonObject = Field(default_factory=dict)
    causation_ids: list[UUID] = Field(default_factory=list)
    correlation_id: UUID | None = None

    @field_validator("idempotency_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("idempotency key cannot be blank")
        return value

    def payload_hash(self) -> str:
        request = {
            "actor_id": str(self.actor_id),
            "expected_state_version": self.expected_state_version,
            "command_type": self.command_type,
            "payload": self.payload,
            "causation_ids": [str(item) for item in self.causation_ids],
        }
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EventRecord(ContractModel):
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    sequence: int = Field(ge=1)
    tick: int = Field(ge=-(2**63), le=2**63 - 1)
    type: str
    command_id: UUID
    actor_id: UUID
    causation_ids: list[UUID] = Field(default_factory=list)
    correlation_id: UUID | None = None
    ruleset_version: str
    payload: JsonObject
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})
    state_version_after: int = Field(ge=1)
    commit_boundary: bool = False
    committed_at: datetime


class EventPage(ContractModel):
    branch_id: UUID
    events: list[EventRecord]
    next_cursor: int = Field(ge=0)
    has_more: bool


class CommitResult(ContractModel):
    command_id: UUID
    branch_id: UUID
    duplicate: bool = False
    state_version: int = Field(ge=0)
    first_seq: int | None = Field(default=None, ge=1)
    last_seq: int | None = Field(default=None, ge=1)
    events: list[EventRecord] = Field(default_factory=list)
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class SnapshotRecord(ContractModel):
    snapshot_id: UUID
    world_id: UUID
    branch_id: UUID
    last_seq: int = Field(ge=0)
    state_version: int = Field(ge=0)
    current_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: WorldState
    created_at: datetime


class NewJob(ContractModel):
    job_id: UUID = Field(default_factory=uuid4)
    world_id: UUID
    branch_id: UUID
    job_type: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=200)
    payload: JsonObject = Field(default_factory=dict)


class JobRecord(ContractModel):
    job_id: UUID
    tenant_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID | None = None
    job_type: str
    status: JobStatus
    payload: JsonObject
    attempt_count: int = Field(ge=0)
    fencing_token: int = Field(ge=0)
    committed_through_tick: int | None = Field(default=None, ge=-(2**63), le=2**63 - 1)
    result: JsonObject | None = None
    created_at: datetime
    updated_at: datetime


class JobLease(ContractModel):
    job_id: UUID
    world_id: UUID
    branch_id: UUID
    worker_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)
    expires_at: datetime


class OutboxRecord(ContractModel):
    outbox_id: UUID
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    topic: str
    payload: JsonObject
    attempts: int = Field(ge=0)
    created_at: datetime
    published_at: datetime | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _decimal_shape(value: Decimal) -> tuple[int, int]:
    """Return scale and integer digits without context-sensitive normalization."""
    digits = list(value.as_tuple().digits)
    exponent = value.as_tuple().exponent
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    scale = max(0, -exponent)
    integer_digits = max(1, len(digits) + exponent)
    return scale, integer_digits


def _validate_reference_graph(entities: dict[UUID, WorldEntity]) -> None:
    """Validate optional location and containment references form an acyclic graph."""
    parents: dict[UUID, set[UUID]] = {}
    for entity_id, entity in entities.items():
        if entity.location_id is not None:
            parents.setdefault(entity_id, set()).add(entity.location_id)
        parent_id = entity.components.get("parent_id")
        if parent_id is None:
            continue
        if not isinstance(parent_id, str):
            raise ValueError(f"entity {entity_id} parent_id must be a UUID string")
        try:
            parent = UUID(parent_id)
        except ValueError as exc:
            raise ValueError(f"entity {entity_id} parent_id is not a UUID") from exc
        if parent == entity_id:
            raise ValueError(f"entity {entity_id} cannot contain itself")
        if parent not in entities:
            raise ValueError(f"entity {entity_id} references a missing parent entity")
        parents.setdefault(entity_id, set()).add(parent)

    visiting: set[UUID] = set()
    visited: set[UUID] = set()

    def visit(node: UUID) -> None:
        if node in visiting:
            raise ValueError("entity location or containment graph contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for parent in parents.get(node, set()):
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    for origin in entities:
        visit(origin)
