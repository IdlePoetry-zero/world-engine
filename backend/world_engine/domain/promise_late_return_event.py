"""Author-only event wrapper for a late return and its bounded trust repair."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .json_types import JsonObject
from .promise_events import PromiseEventModel, PromiseItemReturnedEvent
from .promise_trust import PromiseTrustPlan


_AUTHOR_ONLY: JsonObject = {"policy": "author_only"}


class PromiseItemReturnedLateEvent(PromiseEventModel):
    """A late physical return linked to its breach and one trust repair receipt."""

    type: Literal["core.promise_item_returned_late.v1"] = "core.promise_item_returned_late.v1"
    event_id: UUID
    settlement: PromiseItemReturnedEvent
    source_breach_event_id: UUID
    trust: PromiseTrustPlan
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def late_return_is_bound_to_breach_and_plan(self) -> PromiseItemReturnedLateEvent:
        settlement = self.settlement
        plan = self.trust
        receipt = plan.receipt
        if self.event_id != settlement.event_id:
            raise ValueError("late-return event ID must equal its nested settlement ID")
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("late-return settlement and trust details must be author-only")
        if self.source_breach_event_id == self.event_id:
            raise ValueError("late-return and source breach event IDs must differ")
        if (
            plan.world_id != settlement.world_id
            or plan.branch_id != settlement.branch_id
            or plan.command_id != settlement.command_id
            or plan.actor_id != settlement.actor_id
            or plan.ruleset_version != settlement.ruleset_version
        ):
            raise ValueError("late-return trust plan must match its nested settlement command context")
        if (
            plan.promise_id != settlement.promise_id
            or plan.outcome != "late_return"
            or plan.effective_tick != settlement.transfer_tick
            or plan.from_id != settlement.recipient_owner_id
            or plan.to_id != settlement.source_owner_id
            or plan.outcome_event_id != self.event_id
        ):
            raise ValueError("late-return trust plan must match the nested settlement outcome and identity")
        if (
            receipt.policy_id != plan.policy_id
            or receipt.promise_id != settlement.promise_id
            or receipt.outcome != "breached"
            or receipt.outcome_event_id != self.source_breach_event_id
            or receipt.late_return_event_id != self.event_id
            or receipt.late_return_delta != plan.applied_delta
            or plan.evidence_refs_to_append != (self.source_breach_event_id, self.event_id)
        ):
            raise ValueError("late-return receipt must extend the exact original breach with this repair")
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
