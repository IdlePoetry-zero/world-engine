"""Versioned host events for the R06 relationship-promise lifecycle.

The module does not import ``models`` so the event registry can import these
contracts while WorldState is being declared.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .json_types import JsonObject
from .relationship_promises import (
    CommittedReturnTransfer,
    RelationshipPromise,
    promise_deadline_schedule_id,
)


class PromiseEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


def promise_event_id(command_id: UUID, ruleset_version: str, operation: str) -> UUID:
    return uuid5(command_id, f"{ruleset_version}:relationship_promise.{operation}")


def promise_id_for_command(command_id: UUID, ruleset_version: str) -> UUID:
    return uuid5(command_id, f"{ruleset_version}:relationship_promise.record")


def participant_visibility(first_id: UUID, second_id: UUID) -> JsonObject:
    return {"policy": "subject_ids", "subject_ids": sorted((str(first_id), str(second_id)))}


class RelationshipPromiseRecordedEvent(PromiseEventModel):
    """Record one active private pledge and create its exact deadline schedule."""

    type: Literal["core.relationship_promise_recorded.v1"] = "core.relationship_promise_recorded.v1"
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    recorded_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    promise: RelationshipPromise
    visibility: JsonObject

    @model_validator(mode="after")
    def recorded_promise_is_command_bound(self) -> RelationshipPromiseRecordedEvent:
        if self.event_id != promise_event_id(self.command_id, self.ruleset_version, "recorded"):
            raise ValueError("promise-recorded event ID must be bound to its command")
        if self.promise.id != promise_id_for_command(self.command_id, self.ruleset_version):
            raise ValueError("promise ID must be deterministically bound to the recording command")
        if (
            self.actor_id != self.promise.promisor_id
            or self.promise.status != "active"
            or self.promise.revision != 1
            or self.promise.created_tick != self.recorded_tick
            or self.promise.deadline_tick <= self.recorded_tick
            or self.promise.deadline_schedule_id != promise_deadline_schedule_id(self.promise.id)
            or self.promise.resolved_tick is not None
            or self.promise.outcome_event_id is not None
        ):
            raise ValueError("new promise must be an active future pledge made by its promisor")
        if self.visibility != participant_visibility(self.promise.promisor_id, self.promise.promisee_id):
            raise ValueError("promise details must be visible only to its two participants")
        return self


class PromiseItemReturnedEvent(CommittedReturnTransfer):
    """Move an exact FIFO quantity and fulfill the named promise atomically."""

    world_id: UUID
    branch_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    visibility: JsonObject

    @model_validator(mode="after")
    def return_event_is_command_bound_and_private(self) -> PromiseItemReturnedEvent:
        if self.event_id != promise_event_id(self.command_id, self.ruleset_version, "item_returned"):
            raise ValueError("promise return event ID must be bound to its command")
        if self.visibility != participant_visibility(self.source_owner_id, self.recipient_owner_id):
            raise ValueError("promise return details must be visible only to its two participants")
        return self


class PromiseReleasedEvent(PromiseEventModel):
    """Record the promisee's explicit release and cancel the due schedule."""

    type: Literal["core.promise_released.v1"] = "core.promise_released.v1"
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    promise_id: UUID
    expected_promise_revision: int = Field(ge=1)
    promisor_id: UUID
    promisee_id: UUID
    resolved_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    scheduled_event_id: UUID
    expected_schedule_revision: int = Field(ge=1)
    visibility: JsonObject

    @model_validator(mode="after")
    def release_event_is_command_bound_and_private(self) -> PromiseReleasedEvent:
        if self.event_id != promise_event_id(self.command_id, self.ruleset_version, "released"):
            raise ValueError("promise-release event ID must be bound to its command")
        if self.actor_id != self.promisee_id:
            raise ValueError("only the promisee may release a promise")
        if self.visibility != participant_visibility(self.promisor_id, self.promisee_id):
            raise ValueError("promise release details must be visible only to its two participants")
        return self


class PromiseDeadlineBreachedEvent(PromiseEventModel):
    """Resolve the exact queued one-shot due schedule as a breach."""

    type: Literal["core.promise_breached.v1"] = "core.promise_breached.v1"
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    actor_id: UUID
    promise_id: UUID
    expected_promise_revision: int = Field(ge=1)
    promisor_id: UUID
    promisee_id: UUID
    resolved_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    scheduled_event_id: UUID
    expected_schedule_revision: int = Field(ge=1)
    visibility: JsonObject

    @model_validator(mode="after")
    def breach_event_is_command_bound_and_private(self) -> PromiseDeadlineBreachedEvent:
        if self.event_id != promise_event_id(self.command_id, self.ruleset_version, "breached"):
            raise ValueError("promise-breach event ID must be bound to its command")
        if self.visibility != participant_visibility(self.promisor_id, self.promisee_id):
            raise ValueError("promise breach details must be visible only to its two participants")
        return self


PromiseEvent = Annotated[
    RelationshipPromiseRecordedEvent
    | PromiseItemReturnedEvent
    | PromiseReleasedEvent
    | PromiseDeadlineBreachedEvent,
    Field(discriminator="type"),
]

PROMISE_EVENT_ADAPTER = TypeAdapter(PromiseEvent)


def parse_promise_event(value: PromiseEvent | dict[str, object]) -> PromiseEvent:
    """Parse only the four registered R06 promise event types."""

    return PROMISE_EVENT_ADAPTER.validate_python(value)
