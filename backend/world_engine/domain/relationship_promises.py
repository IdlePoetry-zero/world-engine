"""Private interpersonal promises and evidence-bound lifecycle transitions.

This module is independent of ``models`` so WorldState can use its DTOs without
an import cycle. A committed return is represented by a narrow adapter for the
dedicated ``core.promise_item_returned.v1`` event; rules and replay validation
must construct it from that registered typed event, never from model output.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .components import ExactDecimal, ResourceName, ScheduledEventRecord, Tick

MAX_WORLD_TICK = 2**63 - 1
PROMISE_DEADLINE_HANDLER = "relationship_promise.deadline"
PROMISE_DEADLINE_HANDLER_VERSION = "1.0.0"
PROMISE_TRUST_POLICY_ID = "core.promise_trust.v1"
PROMISE_TRUST_DEADLINE_HANDLER_VERSION = "2.0.0"
PROMISE_RETURN_EVENT_TYPE = "core.promise_item_returned.v1"
PROMISE_BREACH_EVENT_TYPE = "core.promise_breached.v1"
PROMISE_RELEASE_EVENT_TYPE = "core.promise_released.v1"


class PromiseModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class ReturnItemTerms(PromiseModel):
    """A pledge to return one exact resource quantity to a fixed holder account."""

    kind: Literal["return_item"] = "return_item"
    resource_id: ResourceName
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    promisee_account_id: UUID


PromiseStatus = Literal["active", "fulfilled", "breached", "released"]


def promise_deadline_schedule_id(promise_id: UUID) -> UUID:
    """Derive the sole deadline schedule ID from the stable promise ID."""

    return uuid5(promise_id, "core.relationship_promise.deadline.v1")


def promise_return_lot_id(event_id: UUID, source_lot_id: UUID) -> UUID:
    """Derive the returned physical lot identity from the committed event and source lot."""

    return uuid5(event_id, f"core.promise_item_returned.v1:lot:{source_lot_id}")


class PromiseReviewChoice(PromiseModel):
    promise_id: UUID
    choice: Literal["blocked", "refuse"]


class PromiseReviewReceipt(PromiseModel):
    choice: Literal["blocked", "refuse"]
    at_tick: Tick
    event_id: UUID


class RelationshipPromise(PromiseModel):
    """A participant-private pledge, separate from proof of its eventual outcome."""

    id: UUID
    promisor_id: UUID
    promisee_id: UUID
    private_text: str = Field(min_length=1, max_length=1000)
    terms: ReturnItemTerms
    status: PromiseStatus = "active"
    created_tick: Tick
    deadline_tick: Tick
    deadline_schedule_id: UUID
    resolved_tick: Tick | None = None
    revision: StrictInt = Field(default=1, ge=1)
    outcome_event_id: UUID | None = None
    review_receipt: PromiseReviewReceipt | None = Field(default=None, exclude_if=lambda receipt: receipt is None)
    trust_policy_id: Literal["core.promise_trust.v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @property
    def deadline_handler_version(self) -> str:
        if self.trust_policy_id is not None:
            return PROMISE_TRUST_DEADLINE_HANDLER_VERSION
        return PROMISE_DEADLINE_HANDLER_VERSION

    @model_validator(mode="after")
    def validate_promise_lifecycle(self) -> RelationshipPromise:
        if self.promisor_id == self.promisee_id:
            raise ValueError("a promise must have distinct promisor and promisee")
        if self.deadline_tick < self.created_tick:
            raise ValueError("a promise deadline cannot precede its creation")
        if self.deadline_tick >= MAX_WORLD_TICK:
            raise ValueError("a promise deadline must leave room for its due tick")
        if self.deadline_schedule_id != promise_deadline_schedule_id(self.id):
            raise ValueError("promise deadline schedule ID must be derived from its stable promise ID")
        if self.review_receipt is not None and (
            self.review_receipt.at_tick < self.created_tick or self.review_receipt.at_tick > self.deadline_tick
            or self.resolved_tick is not None and self.review_receipt.at_tick > self.resolved_tick
        ):
            raise ValueError("A promise review receipt must precede settlement and belong to its available window.")
        if self.status == "active":
            if self.resolved_tick is not None or self.outcome_event_id is not None:
                raise ValueError("an active promise cannot contain outcome fields")
            return self

        if self.resolved_tick is None or self.outcome_event_id is None:
            raise ValueError("a closed promise requires a resolved tick and outcome event")
        if self.resolved_tick < self.created_tick:
            raise ValueError("a promise cannot resolve before it was created")
        if self.status == "fulfilled" and self.resolved_tick > self.deadline_tick:
            raise ValueError("a fulfilled promise must be completed no later than its inclusive deadline")
        if self.status == "breached" and self.resolved_tick != self.deadline_tick + 1:
            raise ValueError("a breached promise resolves at its exact scheduled due tick")
        if self.status == "released" and self.resolved_tick > self.deadline_tick:
            raise ValueError("an active promise can only be released on or before its deadline")
        return self


def create_return_promise(
    *,
    promise_id: UUID,
    promisor_id: UUID,
    promisee_id: UUID,
    private_text: str,
    terms: ReturnItemTerms,
    created_tick: int,
    deadline_tick: int,
    trust_policy_id: Literal["core.promise_trust.v1"] | None = None,
) -> RelationshipPromise:
    """Create a recorded active pledge and its deterministic one-shot due schedule."""

    return RelationshipPromise(
        id=promise_id,
        promisor_id=promisor_id,
        promisee_id=promisee_id,
        private_text=private_text,
        terms=terms,
        status="active",
        created_tick=created_tick,
        deadline_tick=deadline_tick,
        deadline_schedule_id=promise_deadline_schedule_id(promise_id),
        revision=1,
        trust_policy_id=trust_policy_id,
    )


def build_promise_deadline_schedule(promise: RelationshipPromise) -> ScheduledEventRecord:
    """Build the maintenance schedule that breaches an unresolved promise after its deadline."""

    return ScheduledEventRecord(
        id=promise.deadline_schedule_id,
        due_tick=promise.deadline_tick + 1,
        phase="maintenance",
        handler_id=PROMISE_DEADLINE_HANDLER,
        handler_version=promise.deadline_handler_version,
        max_runs=1,
        payload={"promise_id": str(promise.id)},
    )


class ReturnLotTransfer(PromiseModel):
    """One exact source FIFO lot amount and its deterministic recipient lot ID."""

    source_lot_id: UUID
    received_lot_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class CommittedReturnTransfer(PromiseModel):
    """Narrow DTO adapted from one committed purpose-specific return event."""

    type: Literal["core.promise_item_returned.v1"] = PROMISE_RETURN_EVENT_TYPE
    event_id: UUID
    command_id: UUID
    promise_id: UUID
    expected_promise_revision: StrictInt = Field(ge=1)
    actor_id: UUID
    source_owner_id: UUID
    recipient_owner_id: UUID
    from_account_id: UUID
    to_account_id: UUID
    source_account_type: Literal["holder"]
    recipient_account_type: Literal["holder"]
    resource_id: ResourceName
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    transfer_tick: Tick
    scheduled_event_id: UUID
    expected_schedule_revision: StrictInt = Field(ge=1)
    expected_source_inventory_revision: StrictInt = Field(ge=1)
    expected_recipient_inventory_revision: StrictInt | None
    expected_source_account_balance: ExactDecimal = Field(ge=Decimal("0"))
    expected_recipient_account_balance: ExactDecimal = Field(ge=Decimal("0"))
    lots: tuple[ReturnLotTransfer, ...] = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def return_manifest_is_exact(self) -> CommittedReturnTransfer:
        source_ids = [item.source_lot_id for item in self.lots]
        received_ids = [item.received_lot_id for item in self.lots]
        if len(source_ids) != len(set(source_ids)) or len(received_ids) != len(set(received_ids)):
            raise ValueError("return manifest source and received lot IDs must be unique")
        if self.from_account_id == self.to_account_id or self.source_owner_id == self.recipient_owner_id:
            raise ValueError("a promise return requires distinct holder accounts and owners")
        if self.actor_id != self.source_owner_id:
            raise ValueError("the return actor must own the source holder account")
        if self.expected_source_account_balance < self.quantity:
            raise ValueError("the return source account must cover the exact promised quantity")
        if self.expected_recipient_inventory_revision is not None and self.expected_recipient_inventory_revision < 1:
            raise ValueError("an existing recipient inventory revision must be positive")
        if any(item.received_lot_id != promise_return_lot_id(self.event_id, item.source_lot_id) for item in self.lots):
            raise ValueError("received lot IDs must be deterministically bound to the return event and source lot")
        with localcontext() as context:
            context.prec = 80
            lot_total = sum((item.quantity for item in self.lots), Decimal("0"))
        if lot_total != self.quantity:
            raise ValueError("return manifest quantities must exactly equal the promised quantity")
        return self


class PromiseReleaseEvidence(PromiseModel):
    """A committed promisee-authorized release, which also cancels the due schedule."""

    type: Literal["core.promise_released.v1"] = PROMISE_RELEASE_EVENT_TYPE
    event_id: UUID
    command_id: UUID
    promise_id: UUID
    expected_promise_revision: StrictInt = Field(ge=1)
    actor_id: UUID
    resolved_tick: Tick
    scheduled_event_id: UUID
    expected_schedule_revision: StrictInt = Field(ge=1)


class PromiseDeadlineEvidence(PromiseModel):
    """The exact due-schedule event that breaches one still-active revision."""

    type: Literal["core.promise_breached.v1"] = PROMISE_BREACH_EVENT_TYPE
    event_id: UUID
    command_id: UUID
    promise_id: UUID
    expected_promise_revision: StrictInt = Field(ge=1)
    resolved_tick: Tick
    scheduled_event_id: UUID
    expected_schedule_revision: StrictInt = Field(ge=1)


def can_view_promise(promise: RelationshipPromise, viewer_id: UUID) -> bool:
    """The pledge text and terms are visible only to its two participants."""

    return viewer_id in {promise.promisor_id, promise.promisee_id}


def fulfill_return_promise(
    promise: RelationshipPromise,
    evidence: CommittedReturnTransfer,
) -> RelationshipPromise:
    """Close a promise as fulfilled from one exact, committed return event."""

    _require_active_revision(promise, evidence.promise_id, evidence.expected_promise_revision)
    if (
        evidence.actor_id != promise.promisor_id
        or evidence.source_owner_id != promise.promisor_id
        or evidence.recipient_owner_id != promise.promisee_id
        or evidence.to_account_id != promise.terms.promisee_account_id
        or evidence.resource_id != promise.terms.resource_id
        or evidence.quantity != promise.terms.quantity
        or evidence.scheduled_event_id != promise.deadline_schedule_id
        or not promise.created_tick <= evidence.transfer_tick <= promise.deadline_tick
    ):
        raise ValueError("committed return does not fulfill this exact promise")
    return _closed_promise(
        promise,
        status="fulfilled",
        resolved_tick=evidence.transfer_tick,
        outcome_event_id=evidence.event_id,
    )


def release_promise(
    promise: RelationshipPromise,
    evidence: PromiseReleaseEvidence,
) -> RelationshipPromise:
    """Release an active pledge only when the promisee explicitly authorizes it."""

    _require_active_revision(promise, evidence.promise_id, evidence.expected_promise_revision)
    if evidence.actor_id != promise.promisee_id:
        raise ValueError("only the promisee may release a promise")
    if (
        evidence.scheduled_event_id != promise.deadline_schedule_id
        or not promise.created_tick <= evidence.resolved_tick <= promise.deadline_tick
    ):
        raise ValueError("promise release must cancel its exact deadline schedule before it is due")
    return _closed_promise(
        promise,
        status="released",
        resolved_tick=evidence.resolved_tick,
        outcome_event_id=evidence.event_id,
    )


def breach_promise_at_deadline(
    promise: RelationshipPromise,
    evidence: PromiseDeadlineEvidence,
) -> RelationshipPromise:
    """Breach the active promise at its exact +1 tick schedule boundary."""

    _require_active_revision(promise, evidence.promise_id, evidence.expected_promise_revision)
    if (
        evidence.scheduled_event_id != promise.deadline_schedule_id
        or evidence.resolved_tick != promise.deadline_tick + 1
    ):
        raise ValueError("promise breach must match its exact due schedule and tick")
    return _closed_promise(
        promise,
        status="breached",
        resolved_tick=evidence.resolved_tick,
        outcome_event_id=evidence.event_id,
    )


def _require_active_revision(
    promise: RelationshipPromise,
    promise_id: UUID,
    expected_revision: int,
) -> None:
    if promise_id != promise.id:
        raise ValueError("promise evidence is bound to a different promise")
    if promise.status != "active":
        raise ValueError("only an active promise may be resolved")
    if expected_revision != promise.revision:
        raise ValueError("promise expected revision does not match the active revision")


def _closed_promise(
    promise: RelationshipPromise,
    *,
    status: Literal["fulfilled", "breached", "released"],
    resolved_tick: int,
    outcome_event_id: UUID,
) -> RelationshipPromise:
    raw = promise.model_dump(mode="python")
    raw.update(
        status=status,
        resolved_tick=resolved_tick,
        revision=promise.revision + 1,
        outcome_event_id=outcome_event_id,
    )
    return RelationshipPromise.model_validate(raw)
