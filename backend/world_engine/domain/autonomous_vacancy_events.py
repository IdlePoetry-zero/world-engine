"""Typed audit contract for one autonomous leadership-vacancy review."""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .json_types import JsonObject


LEADERSHIP_VACANCY_REVIEW_RECORDED_EVENT_TYPE = "core.leadership_vacancy_review_recorded.v1"
_AUTHOR_ONLY: JsonObject = {"policy": "author_only"}
_MAX_INDEX = 2**63 - 1


def leadership_vacancy_nomination_request_id(
    command_id: UUID,
    ruleset_version: str,
    organization_id: UUID,
    period_index: int,
) -> UUID:
    """Derive a retry-stable request ID for an organization's annual review."""

    if not isinstance(command_id, UUID) or not isinstance(organization_id, UUID):
        raise ValueError("vacancy nomination request IDs require UUID command and organization IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip() or len(ruleset_version) > 100:
        raise ValueError("vacancy nomination request IDs require a nonempty ruleset version")
    if type(period_index) is not int or not 1 <= period_index <= _MAX_INDEX:
        raise ValueError("vacancy nomination request IDs require a positive bounded period index")
    return uuid5(
        command_id,
        f"{ruleset_version}:leadership_vacancy_nomination:{organization_id}:{period_index}",
    )


class _VacancyReviewModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class LeadershipVacancyReviewRecord(_VacancyReviewModel):
    """Latest per-organization audit receipt, retaining the exact review tick."""

    organization_id: UUID
    period_index: StrictInt = Field(ge=1, le=_MAX_INDEX)
    expected_governance_revision: StrictInt = Field(ge=1, le=_MAX_INDEX)
    proposer_id: UUID
    decision_request_id: UUID
    ballot_id: UUID | None
    reviewed_tick: StrictInt = Field(ge=0, le=_MAX_INDEX)


class LeadershipVacancyReviewRecordedEvent(_VacancyReviewModel):
    """Record one eligible autonomous review and its defer/open outcome."""

    type: Literal["core.leadership_vacancy_review_recorded.v1"] = LEADERSHIP_VACANCY_REVIEW_RECORDED_EVENT_TYPE
    event_id: UUID
    organization_id: UUID
    period_index: StrictInt = Field(ge=1, le=_MAX_INDEX)
    expected_governance_revision: StrictInt = Field(ge=1, le=_MAX_INDEX)
    proposer_id: UUID
    decision_request_id: UUID
    ballot_id: UUID | None
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def identity_is_request_bound_and_author_only(self) -> LeadershipVacancyReviewRecordedEvent:
        if self.event_id != uuid5(self.decision_request_id, "review"):
            raise ValueError("vacancy review event ID must be bound to its decision request")
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("vacancy review audit visibility is fixed to author_only")
        return self


def leadership_vacancy_review_ballot_id(decision_request_id: UUID) -> UUID:
    """Return the stable ballot identity associated with a review request."""

    if not isinstance(decision_request_id, UUID):
        raise ValueError("vacancy ballot IDs require a UUID decision request")
    return uuid5(decision_request_id, "ballot")
