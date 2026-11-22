"""Standalone typed event DTOs for scheduled market price reviews.

The shared ``DomainEvent`` registry imports this module from outside; this
module intentionally does not import ``domain.models`` so it remains safe to
register without introducing a model import cycle.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from .json_types import JsonObject
from .market_pricing import (
    MAX_PRICE_MINOR,
    PRICE_REVIEW_EVENT_TYPE,
    ExactDecimal,
    ResourceName,
    Tick,
    _Contract,
)


class MarketPriceChange(_Contract):
    """One changed offer and the compact CAS context needed to apply it."""

    market_id: UUID
    listing_id: UUID
    seller_id: UUID
    stock_account_id: UUID
    policy_id: ResourceName
    policy_revision: int = Field(ge=1, strict=True)
    expected_market_revision: int = Field(ge=1, strict=True)
    expected_seller_inventory_revision: int = Field(ge=1, strict=True)
    expected_stock_account_balance: ExactDecimal = Field(ge=Decimal("0"))
    previous_unit_price_minor: int = Field(ge=0, le=MAX_PRICE_MINOR, strict=True)
    new_unit_price_minor: int = Field(ge=1, le=MAX_PRICE_MINOR, strict=True)
    snapshot_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def price_must_change(self) -> MarketPriceChange:
        if self.previous_unit_price_minor == self.new_unit_price_minor:
            raise ValueError("a changed price row must have different prior and new prices")
        return self


class MarketPriceReviewResolvedEvent(_Contract):
    """One world-wide compact scheduled market pricing audit and update."""

    type: Literal["core.market_price_review_resolved.v1"] = PRICE_REVIEW_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    schedule_id: UUID
    expected_schedule_revision: int = Field(ge=1, strict=True)
    expected_completed_runs: int = Field(ge=0, strict=True)
    period_index: int = Field(ge=1, strict=True)
    due_tick: Tick
    config_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    assessment_count: int = Field(ge=0, le=10_000, strict=True)
    changed_count: int = Field(ge=0, le=10_000, strict=True)
    unchanged_count: int = Field(ge=0, le=10_000, strict=True)
    skipped_count: int = Field(ge=0, le=10_000, strict=True)
    assessment_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    changes: list[MarketPriceChange] = Field(default_factory=list, max_length=10_000)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @model_validator(mode="after")
    def stable_id_and_counts(self) -> MarketPriceReviewResolvedEvent:
        expected_id = uuid5(
            self.command_id,
            f"{self.ruleset_version}:market.price_review:{self.schedule_id}:{self.period_index}",
        )
        if self.event_id != expected_id:
            raise ValueError("market price review event ID must be bound to its persisted command and period")
        if self.period_index != self.expected_completed_runs + 1:
            raise ValueError("market price review period must follow the expected completed run")
        if self.changed_count != len(self.changes):
            raise ValueError("changed_count must equal the serialized changed rows")
        if self.assessment_count != self.changed_count + self.unchanged_count + self.skipped_count:
            raise ValueError("assessment_count must equal changed, unchanged, and skipped counts")
        if self.visibility != {"policy": "author_only"}:
            raise ValueError("market price review audit visibility is fixed to author_only")
        keys = [(row.market_id, row.listing_id) for row in self.changes]
        if len(keys) != len(set(keys)):
            raise ValueError("a review event cannot change the same listing twice")
        if keys != sorted(keys, key=lambda pair: (pair[0].hex, pair[1].hex)):
            raise ValueError("changed rows must be sorted by market and listing UUID")
        market_revisions: dict[UUID, int] = {}
        for row in self.changes:
            prior = market_revisions.setdefault(row.market_id, row.expected_market_revision)
            if prior != row.expected_market_revision:
                raise ValueError("all changes for a market must share its pre-review revision")
        return self
