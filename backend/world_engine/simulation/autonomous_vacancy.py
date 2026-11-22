"""Plan charter-authorized vacancy ballots at committed simulation boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator
from uuid import UUID, uuid5

from world_engine.domain.autonomous_vacancy import (
    AUTONOMOUS_VACANCY_BALLOT_AUTHORITY, VacancyOpportunity, vacancy_opportunity,
)
from world_engine.domain.autonomous_vacancy_events import (
    LeadershipVacancyReviewRecordedEvent,
    leadership_vacancy_nomination_request_id,
)
from world_engine.domain.leadership_ballot_events import LeadershipBallotChangedEvent, leadership_ballot_event_id
from world_engine.domain.leadership_ballots import (
    LeadershipBallot,
    ballot_resolvable_status,
    cancel_invalidated_leadership_ballot,
    cast_leadership_ballot_vote,
    close_leadership_ballot,
    leadership_ballot_is_invalidated,
    open_vacant_leadership_ballot,
)
from world_engine.domain.models import WorldState

from .contracts import DecisionOption, DecisionRequest, SimulationContext


NOMINATION_KIND = "organization.vacancy_nomination"
VOTE_KIND = "organization.vacancy_vote"


@dataclass(frozen=True, slots=True)
class VacancyNomination:
    opportunity: VacancyOpportunity
    request: DecisionRequest


@dataclass(frozen=True, slots=True)
class VacancyVote:
    ballot: LeadershipBallot
    voter_id: UUID
    request: DecisionRequest


def next_vacancy_year_boundary(state: WorldState, horizon: int) -> int | None:
    year_ticks = state.calendar.year_days * state.calendar.day_seconds
    next_year = max(1, state.current_tick // year_ticks + 1) * year_ticks
    if next_year <= horizon and any(_enabled_vacancies(state)):
        return next_year
    return None


def _enabled_vacancies(state: WorldState) -> Iterator[UUID]:
    return (
        entity.id for entity in state.entities.values()
        if entity.status == "active" and isinstance(governance := entity.components.get("governance"), dict)
        and governance.get("leader_id") is None
        and governance.get("succession_policy") in {"election", "council"}
        and AUTONOMOUS_VACANCY_BALLOT_AUTHORITY in governance.get("authority_tags", ())
    )


def next_autonomous_vacancy_step(
    state: WorldState,
    context: SimulationContext,
) -> VacancyNomination | VacancyVote | LeadershipBallotChangedEvent | None:
    controlled = context.externally_controlled_actor_ids | ({state.player_id} if state.player_id is not None else set())
    reviewed_ballots = {review.ballot_id for review in state.leadership_vacancy_reviews.values() if review.ballot_id}
    open_automatic = sorted(
        (ballot for ballot in state.leadership_ballots if ballot.ballot_id in reviewed_ballots and ballot.status == "open"),
        key=lambda ballot: (ballot.opened_tick, str(ballot.organization_id)),
    )
    for ballot in open_automatic:
        if controlled.intersection(ballot.eligible_voter_ids):
            continue
        if leadership_ballot_is_invalidated(state, ballot):
            cancelled = cancel_invalidated_leadership_ballot(state, ballot, expected_ballot_revision=ballot.revision)
            return _ballot_event(context, "cancel", ballot.procedural_actor_id, ballot.revision, cancelled,
                                 state.leadership_vacancy_reviews[ballot.organization_id].decision_request_id)
        if ballot_resolvable_status(ballot) is not None:
            closed = close_leadership_ballot(
                state, ballot, actor_id=ballot.procedural_actor_id, expected_ballot_revision=ballot.revision,
            )
            return _ballot_event(context, "close", ballot.procedural_actor_id, ballot.revision, closed,
                                 state.leadership_vacancy_reviews[ballot.organization_id].decision_request_id)
        voted = {vote.voter_id for vote in ballot.votes}
        voter_id = next(voter for voter in ballot.eligible_voter_ids if voter not in voted)
        name = state.entities[ballot.organization_id].name or "Organization"
        request = DecisionRequest(
            request_id=uuid5(context.command_id, f"{context.ruleset_version}:vacancy.vote:{ballot.ballot_id}:{voter_id}"),
            kind=VOTE_KIND, actor_id=voter_id, at_tick=state.current_tick, state_version=context.state_version,
            title=f"{name}: independent vote on the vacant leadership seat"[:240], reason_code="vacancy_ballot_vote_due",
            options=(DecisionOption(option_id="yes", label="Vote in favor"),
                     DecisionOption(option_id="no", label="Vote against"),
                     DecisionOption(option_id="abstain", label="Abstain from the vote")),
            evidence_ids=(ballot.organization_id, ballot.ballot_id),
        )
        return VacancyVote(ballot, voter_id, request)

    period = state.current_tick // (state.calendar.year_days * state.calendar.day_seconds)
    if period < 1:
        return None
    for organization_id in sorted(_enabled_vacancies(state), key=str):
        previous = state.leadership_vacancy_reviews.get(organization_id)
        if previous is not None and previous.period_index >= period:
            continue
        chance = vacancy_opportunity(state, organization_id, controlled_actor_ids=frozenset(controlled))
        if chance is None:
            continue
        organization_name = state.entities[organization_id].name or "Organization"
        proposer_name = state.entities[chance.proposer_id].name or "Member"
        nominations = tuple(
            DecisionOption(
                option_id=f"nominate_{member_id.hex}",
                label=f"Nominate {state.entities[member_id].name or 'registered member'}"[:240],
                effect_summary="Open a ballot; the electorate still decides whether the nominee takes office.",
            ) for member_id in chance.candidate_ids
        )
        request = DecisionRequest(
            request_id=leadership_vacancy_nomination_request_id(
                context.command_id, context.ruleset_version, organization_id, chance.period_index,
            ),
            kind=NOMINATION_KIND, actor_id=chance.proposer_id, at_tick=state.current_tick,
            state_version=context.state_version, title=f"{organization_name}: {proposer_name} may nominate a successor"[:240],
            reason_code="vacancy_nomination_due", evidence_ids=(organization_id,),
            options=(*nominations, DecisionOption(option_id="defer", label="Leave the seat vacant this year")),
        )
        return VacancyNomination(chance, request)
    return None


def resolve_autonomous_vacancy_step(
    state: WorldState, context: SimulationContext, step: VacancyNomination | VacancyVote, option_id: str,
) -> tuple[LeadershipBallotChangedEvent | LeadershipVacancyReviewRecordedEvent, ...]:
    if isinstance(step, VacancyVote):
        if option_id not in ("yes", "no", "abstain"):
            raise ValueError("The autonomous vote must be an offered choice.")
        ballot = step.ballot
        vote = cast_leadership_ballot_vote(
            state, ballot, voter_id=step.voter_id, choice=option_id,
            expected_ballot_revision=ballot.revision,
        )
        return (_ballot_event(context, "cast", step.voter_id, ballot.revision, vote,
                              state.leadership_vacancy_reviews[ballot.organization_id].decision_request_id),)

    chance = step.opportunity
    ballot_id = None if option_id == "defer" else uuid5(step.request.request_id, "ballot")
    review = LeadershipVacancyReviewRecordedEvent(
        event_id=uuid5(step.request.request_id, "review"),
        organization_id=chance.organization_id,
        period_index=chance.period_index,
        expected_governance_revision=chance.governance_revision,
        proposer_id=chance.proposer_id,
        ballot_id=ballot_id,
        decision_request_id=step.request.request_id,
    )
    if ballot_id is None:
        return (review,)
    nominee_id = next(candidate for candidate in chance.candidate_ids if option_id == f"nominate_{candidate.hex}")
    ballot = open_vacant_leadership_ballot(
        state, ballot_id=ballot_id, organization_id=chance.organization_id, nominee_id=nominee_id,
        actor_id=chance.proposer_id, existing_ballots=state.leadership_ballots,
    )
    return (_ballot_event(context, "open_vacant", chance.proposer_id, 0, ballot, step.request.request_id), review)


def _ballot_event(context, operation, actor_id, revision, ballot, review_request_id) -> LeadershipBallotChangedEvent:
    return LeadershipBallotChangedEvent(
        event_id=leadership_ballot_event_id(context.command_id, context.ruleset_version, operation, ballot.ballot_id),
        world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id, actor_id=actor_id,
        ruleset_version=context.ruleset_version, expected_revision=revision, operation=operation, resulting_ballot=ballot,
        autonomous_review_request_id=review_request_id,
    )
