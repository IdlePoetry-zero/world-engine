"""Versioned event contract for one formal autonomous organization review."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, StrictInt, model_validator

from .json_types import JsonObject
from .organization_strategy import (
    ORGANIZATION_STRATEGY_REVIEW_EVENT_TYPE,
    OrganizationalStrategyReviewRecord,
    StrategyModel,
    organization_strategy_review_event_id,
)


_AUTHOR_ONLY: JsonObject = {"policy": "author_only"}


class OrganizationStrategyReviewResolvedEvent(StrategyModel):
    """Commit current authority/condition evidence and an optional exact transfer."""

    type: Literal["core.organization_strategy_review_resolved.v1"] = ORGANIZATION_STRATEGY_REVIEW_EVENT_TYPE
    event_id: UUID
    schedule_id: UUID
    expected_schedule_revision: StrictInt = Field(ge=1, le=100_000)
    review: OrganizationalStrategyReviewRecord
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def review_is_bound_to_exact_schedule_occurrence(self) -> OrganizationStrategyReviewResolvedEvent:
        if (
            self.review.review_index != self.expected_schedule_revision
            or self.event_id != organization_strategy_review_event_id(self.schedule_id, self.review.review_index)
        ):
            raise ValueError("organization strategy review event must identify its exact scheduled occurrence")
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("organization strategy review audit visibility is fixed to author_only")
        return self
