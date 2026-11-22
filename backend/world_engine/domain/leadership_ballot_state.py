"""Apply a committed leadership ballot transition to its branch state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from .components import GovernanceComponent, is_leadership_office
from .errors import InvalidEvent
from .autonomous_vacancy_events import (
    LeadershipVacancyReviewRecordedEvent,
    leadership_vacancy_nomination_request_id,
    leadership_vacancy_review_ballot_id,
)
from .leadership_ballot_events import LeadershipBallotChangedEvent
from .leadership_ballots import (
    cancel_invalidated_leadership_ballot,
    cast_leadership_ballot_vote,
    close_leadership_ballot,
    open_leadership_ballot,
    open_vacant_leadership_ballot,
)

if TYPE_CHECKING:
    from .models import WorldState


BALLOT_COMMANDS = {
    "open": "action.open_leadership_ballot",
    "open_vacant": "action.open_vacant_leadership_ballot",
    "cast": "action.cast_leadership_ballot_vote",
    "close": "action.close_leadership_ballot",
    "cancel": "maintenance.cancel_invalidated_leadership_ballot",
}
NPC_BALLOT_VOTE_COMMAND = "action.request_leadership_ballot_vote"
AUTONOMOUS_VACANCY_COMMAND = "simulation.autonomous_vacancy"


def autonomous_vacancy_command_payload(events: Sequence[object]) -> dict[str, object]:
    """Return the exact envelope payload for one autonomous vacancy batch."""

    if (
        len(events) == 2
        and isinstance(events[0], LeadershipBallotChangedEvent)
        and isinstance(events[1], LeadershipVacancyReviewRecordedEvent)
    ):
        ballot_event, review = events
        _validate_vacancy_open_pair(ballot_event, review)
        return {
            "kind": "autonomous_vacancy",
            "organization_id": str(review.organization_id),
            "ballot_id": str(review.ballot_id),
            "operation": "open_vacant",
            "npc_actor_id": str(ballot_event.actor_id),
        }
    if len(events) == 1 and isinstance(events[0], LeadershipVacancyReviewRecordedEvent):
        review = events[0]
        if review.ballot_id is not None:
            raise InvalidEvent("A vacancy ballot opening must include its exact ballot transition.")
        return {
            "kind": "autonomous_vacancy",
            "organization_id": str(review.organization_id),
            "ballot_id": None,
            "operation": "defer",
            "npc_actor_id": str(review.proposer_id),
        }
    if len(events) == 1 and isinstance(events[0], LeadershipBallotChangedEvent):
        event = events[0]
        ballot = event.resulting_ballot
        if (
            event.operation not in {"cast", "close", "cancel"}
            or event.autonomous_review_request_id is None
            or ballot.incumbent_id is not None
        ):
            raise InvalidEvent("An autonomous vacancy transition must be a marked vacancy cast, close, or cancel.")
        return {
            "kind": "autonomous_vacancy",
            "organization_id": str(ballot.organization_id),
            "ballot_id": str(ballot.ballot_id),
            "operation": event.operation,
            "npc_actor_id": str(event.actor_id),
        }
    raise InvalidEvent("An autonomous vacancy command must contain one exact vacancy transition batch.")


def validate_leadership_ballot_batch(
    events: Sequence[object], *, world_id: UUID, branch_id: UUID, command_id: UUID, actor_id: UUID,
    ruleset_version: str, command_type: str | None = None, command_payload: Mapping[str, object] | None = None,
) -> None:
    review_events = [item for item in events if isinstance(item, LeadershipVacancyReviewRecordedEvent)]
    ballot_events = [item for item in events if isinstance(item, LeadershipBallotChangedEvent)]

    if command_type == AUTONOMOUS_VACANCY_COMMAND:
        payload = autonomous_vacancy_command_payload(events)
        if command_payload != payload:
            raise InvalidEvent("The autonomous vacancy payload must match its exact transition batch.")
        _validate_autonomous_batch_context(
            events, world_id=world_id, branch_id=branch_id, command_id=command_id,
            ruleset_version=ruleset_version,
        )
        return

    # Archive/backup replay intentionally omits command metadata. The review
    # event and durable marker still identify every autonomous transition.
    if command_type is None and review_events:
        if len(events) == 1 and len(review_events) == 1 and review_events[0].ballot_id is None:
            _validate_review_request(review_events[0], command_id, ruleset_version)
            return
        if (
            len(events) == 2 and len(ballot_events) == 1 and len(review_events) == 1
            and events[0] is ballot_events[0] and events[1] is review_events[0]
        ):
            autonomous_vacancy_command_payload(events)
            _validate_autonomous_batch_context(
                events, world_id=world_id, branch_id=branch_id, command_id=command_id,
                ruleset_version=ruleset_version,
            )
            return
        raise InvalidEvent("An archived vacancy review must be a defer or exact open-and-review batch.")

    if command_type is None and ballot_events and any(
        event.autonomous_review_request_id is not None for event in ballot_events
    ):
        if len(events) == 1:
            autonomous_vacancy_command_payload(events)
            _validate_autonomous_batch_context(
                events, world_id=world_id, branch_id=branch_id, command_id=command_id,
                ruleset_version=ruleset_version,
            )
            return
        raise InvalidEvent("A marked autonomous transition must be committed as its exact batch.")

    if review_events:
        raise InvalidEvent("Leadership vacancy review events require the autonomous vacancy command.")

    event = next((item for item in events if isinstance(item, LeadershipBallotChangedEvent)), None)
    if event is None:
        if command_type in BALLOT_COMMANDS.values() or command_type == NPC_BALLOT_VOTE_COMMAND:
            raise InvalidEvent("A leadership ballot command must include its committed transition.")
        return
    command_actor_id = event.requested_by or event.actor_id
    if len(events) != 1 or (event.world_id, event.branch_id, event.command_id, command_actor_id, event.ruleset_version) != (
        world_id, branch_id, command_id, actor_id, ruleset_version,
    ):
        raise InvalidEvent("A leadership ballot transition requires one exact command context.")
    if event.autonomous_review_request_id is not None:
        raise InvalidEvent("A marked autonomous vacancy transition requires its autonomous command.")
    if command_type is None:
        return
    ballot = event.resulting_ballot
    if event.requested_by is not None:
        if command_type != NPC_BALLOT_VOTE_COMMAND or command_payload != {
            "kind": "request_leadership_ballot_vote", "ballot_id": str(ballot.ballot_id),
            "voter_id": str(event.actor_id), "expected_ballot_revision": event.expected_revision,
        }:
            raise InvalidEvent("A hosted vote must match the requested voter and ballot.")
        return
    expected: dict[str, object] = {"kind": BALLOT_COMMANDS[event.operation].split(".", 1)[1]}
    if event.operation in {"open", "open_vacant"}:
        expected.update(organization_id=str(ballot.organization_id), nominee_id=str(ballot.nominee_id))
    else:
        expected.update(ballot_id=str(ballot.ballot_id), expected_ballot_revision=event.expected_revision)
        if event.operation == "cast":
            expected["choice"] = next(vote.choice for vote in ballot.votes if vote.voter_id == event.actor_id)
    if command_type != BALLOT_COMMANDS[event.operation] or command_payload != expected:
        raise InvalidEvent("The leadership ballot transition does not match the command payload.")


def _validate_autonomous_batch_context(
    events: Sequence[object], *, world_id: UUID, branch_id: UUID, command_id: UUID,
    ruleset_version: str,
) -> None:
    reviews = [item for item in events if isinstance(item, LeadershipVacancyReviewRecordedEvent)]
    ballots = [item for item in events if isinstance(item, LeadershipBallotChangedEvent)]
    for event in ballots:
        if (event.world_id, event.branch_id, event.command_id, event.ruleset_version) != (
            world_id, branch_id, command_id, ruleset_version,
        ):
            raise InvalidEvent("An autonomous ballot transition requires its exact world command context.")
    for review in reviews:
        _validate_review_request(review, command_id, ruleset_version)


def _validate_review_request(
    review: LeadershipVacancyReviewRecordedEvent, command_id: UUID, ruleset_version: str,
) -> None:
    try:
        expected_request_id = leadership_vacancy_nomination_request_id(
            command_id, ruleset_version, review.organization_id, review.period_index,
        )
        expected_ballot_id = leadership_vacancy_review_ballot_id(expected_request_id)
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("The vacancy review request identity is invalid.") from exc
    if review.decision_request_id != expected_request_id:
        raise InvalidEvent("A vacancy review must match its command, ruleset, organization, and period.")
    if review.ballot_id is not None and review.ballot_id != expected_ballot_id:
        raise InvalidEvent("A vacancy review ballot must derive from its request ID.")


def _validate_vacancy_open_pair(
    ballot_event: LeadershipBallotChangedEvent, review: LeadershipVacancyReviewRecordedEvent,
) -> None:
    ballot = ballot_event.resulting_ballot
    if (
        ballot_event.operation != "open_vacant"
        or ballot_event.autonomous_review_request_id != review.decision_request_id
        or review.ballot_id != leadership_vacancy_review_ballot_id(review.decision_request_id)
        or ballot.ballot_id != review.ballot_id
        or ballot.organization_id != review.organization_id
        or ballot.vacancy_proposer_id != review.proposer_id
        or ballot_event.actor_id != review.proposer_id
        or ballot.governance_revision != review.expected_governance_revision
    ):
        raise InvalidEvent("An autonomous vacancy opening must match its exact ballot and review receipt.")


def _validate_marked_vacancy_ballot_in_state(state: WorldState, event: LeadershipBallotChangedEvent) -> None:
    ballot = event.resulting_ballot
    request_id = event.autonomous_review_request_id
    if request_id is None or ballot.incumbent_id is not None:
        raise InvalidEvent("An autonomous ballot transition must identify its vacancy review.")
    review = state.leadership_vacancy_reviews.get(ballot.organization_id)
    if (
        review is None
        or review.decision_request_id != request_id
        or review.ballot_id != ballot.ballot_id
        or review.proposer_id != ballot.vacancy_proposer_id
        or review.expected_governance_revision != ballot.governance_revision
        or review.reviewed_tick != ballot.opened_tick
    ):
        raise InvalidEvent("An autonomous vacancy transition must match its stored review receipt.")


def apply_leadership_ballot(state: WorldState, event: LeadershipBallotChangedEvent) -> dict[str, object]:
    ballot = event.resulting_ballot
    existing = state.leadership_ballots
    try:
        if event.operation in {"open", "open_vacant"}:
            request_id = event.autonomous_review_request_id
            if event.operation == "open" and request_id is not None:
                raise InvalidEvent("Only a vacancy ballot may carry an autonomous review marker.")
            if event.operation == "open_vacant" and request_id is not None:
                year_ticks = state.calendar.year_days * state.calendar.day_seconds
                period_index = state.current_tick // year_ticks
                expected_request_id = leadership_vacancy_nomination_request_id(
                    event.command_id, event.ruleset_version, ballot.organization_id, period_index,
                )
                if (
                    request_id != expected_request_id
                    or ballot.ballot_id != leadership_vacancy_review_ballot_id(expected_request_id)
                ):
                    raise InvalidEvent("An autonomous vacancy ballot must derive from its current review request.")
            elif ballot.ballot_id != uuid5(event.command_id, "leadership-ballot"):
                raise InvalidEvent("A ballot must derive from its proposal command.")
            open_ballot = open_vacant_leadership_ballot if event.operation == "open_vacant" else open_leadership_ballot
            expected = open_ballot(
                state, ballot_id=ballot.ballot_id, organization_id=ballot.organization_id,
                nominee_id=ballot.nominee_id, actor_id=event.actor_id, existing_ballots=existing,
            )
            records = [*existing, expected]
        else:
            if event.autonomous_review_request_id is not None:
                _validate_marked_vacancy_ballot_in_state(state, event)
            prior = next((item for item in existing if item.ballot_id == ballot.ballot_id), None)
            if prior is None:
                raise InvalidEvent("Leadership ballot does not exist on this branch.")
            if event.operation == "cast":
                choice = next(vote.choice for vote in ballot.votes if vote.voter_id == event.actor_id)
                expected = cast_leadership_ballot_vote(
                    state, prior, voter_id=event.actor_id, choice=choice,
                    expected_ballot_revision=event.expected_revision,
                )
            elif event.operation == "close":
                expected = close_leadership_ballot(
                    state, prior, actor_id=event.actor_id, expected_ballot_revision=event.expected_revision,
                )
            else:
                expected = cancel_invalidated_leadership_ballot(
                    state, prior, expected_ballot_revision=event.expected_revision,
                )
            records = [expected if item.ballot_id == prior.ballot_id else item for item in existing]
    except (ValueError, StopIteration) as exc:
        raise InvalidEvent("Leadership ballot is incompatible with its branch state.") from exc
    if expected != ballot:
        raise InvalidEvent("Stored leadership ballot does not match the authoritative transition.")

    updates: dict[str, object] = {"leadership_ballots": records}
    if event.operation == "close" and ballot.status == "approved":
        organization = state.entities[ballot.organization_id]
        governance = GovernanceComponent.model_validate(organization.components["governance"])
        offices = [
            office.model_copy(update={"holder_id": ballot.nominee_id, "revision": office.revision + 1})
            if office.holder_id == ballot.incumbent_id and is_leadership_office(office) else office
            for office in governance.offices
        ]
        governance = governance.model_copy(update={
            "leader_id": ballot.nominee_id, "offices": offices, "revision": governance.revision + 1,
        })
        updates["entities"] = {**state.entities, organization.id: organization.model_copy(update={
            "components": {**organization.components, "governance": governance.model_dump(mode="json")},
        })}
    return updates
