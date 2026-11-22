"""Typed, author-only event contract for leadership ballot lifecycle changes.

The host validates the previous ballot snapshot and trusted administrative
scope. This DTO binds the resulting ballot and operation to one command and
checks the transition details that are visible from the result alone.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from .json_types import JsonObject
from .leadership_ballots import LeadershipBallot


LEADERSHIP_BALLOT_CHANGED_EVENT_TYPE = "core.leadership_ballot_changed.v1"
LeadershipBallotOperation = Literal["open", "open_vacant", "cast", "close", "cancel"]
_AUTHOR_ONLY: JsonObject = {"policy": "author_only"}
_MAX_REVISION = 2**63 - 1


def leadership_ballot_event_id(
    command_id: UUID,
    ruleset_version: str,
    operation: LeadershipBallotOperation,
    ballot_id: UUID,
) -> UUID:
    """Derive deterministic event identity from command, operation and ballot.

    The UUID5 name is ``{ruleset}:leadership_ballot.{operation}:{ballot_id}``.
    """

    if not isinstance(command_id, UUID) or not isinstance(ballot_id, UUID):
        raise ValueError("leadership ballot event IDs require UUID command and ballot IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("leadership ballot event IDs require a nonempty ruleset version")
    if operation not in {"open", "open_vacant", "cast", "close", "cancel"}:
        raise ValueError("leadership ballot operation is unsupported")
    return uuid5(
        command_id,
        f"{ruleset_version}:leadership_ballot.{operation}:{ballot_id}",
    )


class LeadershipBallotEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class LeadershipBallotChangedEvent(LeadershipBallotEventModel):
    """Record one command-bound open, vote, close or cancel ballot transition."""

    type: Literal["core.leadership_ballot_changed.v1"] = LEADERSHIP_BALLOT_CHANGED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    expected_revision: StrictInt = Field(ge=0, le=_MAX_REVISION)
    operation: LeadershipBallotOperation
    resulting_ballot: LeadershipBallot
    requested_by: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    autonomous_review_request_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def transition_is_command_bound_and_author_only(self) -> LeadershipBallotChangedEvent:
        ballot = self.resulting_ballot
        vacancy_proposer_id = ballot.vacancy_proposer_id
        if self.event_id != leadership_ballot_event_id(
            self.command_id, self.ruleset_version, self.operation, ballot.ballot_id,
        ):
            raise ValueError("leadership ballot event ID must be bound to its command, operation and ballot")
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("leadership ballot audit visibility is fixed to author_only")
        if self.autonomous_review_request_id is not None and (
            ballot.incumbent_id is not None or self.requested_by is not None
            or ballot.ballot_id != uuid5(self.autonomous_review_request_id, "ballot")
        ):
            raise ValueError("An autonomous transition must name its request-bound vacancy ballot.")
        if self.requested_by is not None and (
            self.operation != "cast" or self.requested_by != ballot.procedural_actor_id
        ):
            raise ValueError("only the ballot's authorized initiator may request a hosted vote")

        if self.operation in {"open", "open_vacant"}:
            if self.expected_revision != 0 or ballot.revision != 1 or ballot.status != "open":
                raise ValueError("opening a leadership ballot must expect revision zero and create open revision one")
            if (self.operation == "open_vacant") != (vacancy_proposer_id is not None):
                raise ValueError("the opening operation must match the ballot's leadership context")
            if self.operation == "open" and self.actor_id != ballot.incumbent_id:
                raise ValueError("only the incumbent may open a leadership ballot")
            if self.operation == "open_vacant" and self.actor_id != vacancy_proposer_id:
                raise ValueError("only the vacancy proposer may open this leadership ballot")
            if ballot.votes:
                raise ValueError("an opened leadership ballot must not contain votes")
            return self

        if self.expected_revision < 1 or ballot.revision != self.expected_revision + 1:
            raise ValueError("a ballot transition must advance exactly one existing revision")

        if self.operation == "cast":
            if ballot.status != "open":
                raise ValueError("a vote may only be cast on an open ballot")
            if self.actor_id not in ballot.eligible_voter_ids:
                raise ValueError("only an eligible voter may cast a leadership ballot vote")
            if sum(vote.voter_id == self.actor_id for vote in ballot.votes) != 1:
                raise ValueError("a cast vote event must contain exactly one resulting vote by its actor")
        elif self.operation == "close":
            if ballot.incumbent_id is not None and self.actor_id != ballot.incumbent_id:
                raise ValueError("only the incumbent may close a leadership ballot")
            if vacancy_proposer_id is not None and self.actor_id != vacancy_proposer_id:
                raise ValueError("only the vacancy proposer may close this leadership ballot")
            if ballot.status not in {"approved", "rejected"}:
                raise ValueError("a closed leadership ballot must be approved or rejected")
            approved = _approval_is_met(ballot, _yes_count(ballot))
            if ballot.status == "approved" and not approved:
                raise ValueError("a ballot can be approved only when its yes votes meet the registered threshold")
            if ballot.status == "rejected" and (
                approved or (len(ballot.votes) < len(ballot.eligible_voter_ids)
                             and _approval_is_possible(ballot, _yes_count(ballot)))
            ):
                raise ValueError("a rejected ballot must be fully voted or have an irreversible outcome")
        elif ballot.status != "cancelled":
            raise ValueError("a cancelled leadership ballot must have cancelled status")
        return self


def _yes_count(ballot: LeadershipBallot) -> int:
    return sum(vote.choice == "yes" for vote in ballot.votes)


def _approval_is_met(ballot: LeadershipBallot, yes_votes: int) -> bool:
    eligible_count = len(ballot.eligible_voter_ids)
    if eligible_count == 0 or yes_votes < 1:
        return False
    share = Decimal(yes_votes) / Decimal(eligible_count)
    if ballot.threshold == Decimal("0.5"):
        return share > Decimal("0.5")
    return share >= ballot.threshold


def _approval_is_possible(ballot: LeadershipBallot, yes_votes: int) -> bool:
    uncast = len(ballot.eligible_voter_ids) - len(ballot.votes)
    return _approval_is_met(ballot, yes_votes + uncast)


LEADERSHIP_BALLOT_EVENT_ADAPTER = TypeAdapter(LeadershipBallotChangedEvent)


def parse_leadership_ballot_event(
    value: LeadershipBallotChangedEvent | dict[str, object],
) -> LeadershipBallotChangedEvent:
    """Parse the standalone leadership ballot change event DTO."""

    return LEADERSHIP_BALLOT_EVENT_ADAPTER.validate_python(value)
