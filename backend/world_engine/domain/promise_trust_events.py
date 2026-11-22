"""Private v2 promise settlement wrappers carrying an atomic trust plan.

The established v1 settlement DTO remains embedded verbatim so transfer and
deadline semantics stay owned by the existing event contracts. These wrappers
only bind that settlement to its corresponding host-authored trust plan.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .json_types import JsonObject
from .promise_events import PromiseEventModel
from .promise_events import PromiseDeadlineBreachedEvent, PromiseItemReturnedEvent
from .promise_trust import PromiseTrustPlan


_AUTHOR_ONLY: JsonObject = {"policy": "author_only"}


class PromiseItemReturnedWithTrustEvent(PromiseEventModel):
    """Atomic on-time return plus its exact directed trust effect."""

    type: Literal["core.promise_item_returned.v2"] = "core.promise_item_returned.v2"
    event_id: UUID
    settlement: PromiseItemReturnedEvent
    trust: PromiseTrustPlan
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def settlement_and_trust_are_bound(self) -> PromiseItemReturnedWithTrustEvent:
        _validate_wrapper(
            self,
            settlement=self.settlement,
            expected_outcome="on_time_fulfilled",
            effective_tick=self.settlement.transfer_tick,
            from_id=self.settlement.recipient_owner_id,
            to_id=self.settlement.source_owner_id,
        )
        return self

    @property
    def world_id(self) -> UUID:
        return self.settlement.world_id

    @property
    def branch_id(self) -> UUID:
        return self.settlement.branch_id

    @property
    def command_id(self) -> UUID:
        return self.settlement.command_id

    @property
    def actor_id(self) -> UUID:
        return self.settlement.actor_id

    @property
    def ruleset_version(self) -> str:
        return self.settlement.ruleset_version

    @property
    def promise_id(self) -> UUID:
        return self.settlement.promise_id


class PromiseDeadlineBreachedWithTrustEvent(PromiseEventModel):
    """Atomic scheduled breach plus its exact directed trust effect."""

    type: Literal["core.promise_breached.v2"] = "core.promise_breached.v2"
    event_id: UUID
    settlement: PromiseDeadlineBreachedEvent
    trust: PromiseTrustPlan
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def settlement_and_trust_are_bound(self) -> PromiseDeadlineBreachedWithTrustEvent:
        _validate_wrapper(
            self,
            settlement=self.settlement,
            expected_outcome="breached",
            effective_tick=self.settlement.resolved_tick,
            from_id=self.settlement.promisee_id,
            to_id=self.settlement.promisor_id,
        )
        return self

    @property
    def world_id(self) -> UUID:
        return self.settlement.world_id

    @property
    def branch_id(self) -> UUID:
        return self.settlement.branch_id

    @property
    def command_id(self) -> UUID:
        return self.settlement.command_id

    @property
    def actor_id(self) -> UUID:
        return self.settlement.actor_id

    @property
    def ruleset_version(self) -> str:
        return self.settlement.ruleset_version

    @property
    def promise_id(self) -> UUID:
        return self.settlement.promise_id


def _validate_wrapper(
    wrapper: PromiseItemReturnedWithTrustEvent | PromiseDeadlineBreachedWithTrustEvent,
    *,
    settlement: PromiseItemReturnedEvent | PromiseDeadlineBreachedEvent,
    expected_outcome: Literal["on_time_fulfilled", "breached"],
    effective_tick: int,
    from_id: UUID,
    to_id: UUID,
) -> None:
    plan = wrapper.trust
    if wrapper.event_id != settlement.event_id:
        raise ValueError("v2 promise event ID must equal its nested v1 settlement ID")
    if wrapper.visibility != _AUTHOR_ONLY:
        raise ValueError("v2 promise settlement and trust details must be author-only")
    if (
        plan.world_id != settlement.world_id
        or plan.branch_id != settlement.branch_id
        or plan.command_id != settlement.command_id
        or plan.actor_id != settlement.actor_id
        or plan.ruleset_version != settlement.ruleset_version
    ):
        raise ValueError("promise trust plan must match its nested settlement command context")
    if (
        plan.promise_id != settlement.promise_id
        or plan.outcome != expected_outcome
        or plan.effective_tick != effective_tick
        or plan.from_id != from_id
        or plan.to_id != to_id
        or plan.outcome_event_id != settlement.event_id
    ):
        raise ValueError("promise trust plan must match its nested settlement outcome and audit identity")
    receipt = plan.receipt
    if (
        receipt.policy_id != plan.policy_id
        or receipt.promise_id != settlement.promise_id
        or receipt.outcome != expected_outcome
        or receipt.outcome_event_id != settlement.event_id
        or plan.evidence_refs_to_append != (settlement.event_id,)
    ):
        raise ValueError("promise trust receipt must match its nested settlement audit identity")
