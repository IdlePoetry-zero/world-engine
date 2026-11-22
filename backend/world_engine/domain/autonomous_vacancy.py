"""Read-only eligibility planning for explicitly authorized vacancy ballots."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .components import GovernanceComponent
from .leadership_ballots import (
    LeadershipBallotError,
    _electorate,
    _governance_for,
    vacant_leadership_office_holder,
)
from .models import WorldState


AUTONOMOUS_VACANCY_BALLOT_AUTHORITY = "authority.autonomous_vacancy_ballot"
_YEAR_INDEX_MAX = 2**63 - 1
_CANDIDATE_LIMIT = 32


class VacancyOpportunity(BaseModel):
    """Minimal, private-free inputs for proposing an autonomous vacancy ballot."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    organization_id: UUID
    governance_revision: StrictInt = Field(ge=1, le=_YEAR_INDEX_MAX)
    period_index: StrictInt = Field(ge=1, le=_YEAR_INDEX_MAX)
    proposer_id: UUID
    candidate_ids: tuple[UUID, ...] = Field(min_length=1, max_length=_CANDIDATE_LIMIT)
    eligible_voter_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def stable_unique_participants(self) -> VacancyOpportunity:
        candidate_keys = tuple(str(item) for item in self.candidate_ids)
        voter_keys = tuple(str(item) for item in self.eligible_voter_ids)
        if candidate_keys != tuple(sorted(candidate_keys)) or len(candidate_keys) != len(set(candidate_keys)):
            raise ValueError("vacancy opportunity candidates must be sorted and unique")
        if voter_keys != tuple(sorted(voter_keys)) or len(voter_keys) != len(set(voter_keys)):
            raise ValueError("vacancy opportunity voters must be sorted and unique")
        if self.proposer_id not in self.eligible_voter_ids:
            raise ValueError("the vacancy ballot proposer must belong to the registered electorate")
        return self


def vacancy_opportunity(
    state: WorldState,
    organization_id: UUID,
    *,
    controlled_actor_ids: frozenset[UUID] = frozenset(),
) -> VacancyOpportunity | None:
    """Return a bounded opening opportunity only when charter and roster allow it.

    The function is read-only and exposes IDs and revisions only. It does not
    inspect decision history, open a ballot, or infer authority from membership.
    """

    if not isinstance(state, WorldState) or not isinstance(organization_id, UUID):
        return None
    try:
        controlled = set(controlled_actor_ids)
    except TypeError:
        return None
    if any(not isinstance(actor_id, UUID) for actor_id in controlled):
        return None
    if state.player_id is not None:
        controlled.add(state.player_id)

    try:
        _organization, governance = _governance_for(state, organization_id)
        office_holder_id = vacant_leadership_office_holder(governance)
    except LeadershipBallotError:
        return None

    if AUTONOMOUS_VACANCY_BALLOT_AUTHORITY not in governance.authority_tags:
        return None
    if governance.succession_policy not in {"election", "council"}:
        return None
    if any(
        ballot.organization_id == organization_id and ballot.status == "open"
        for ballot in state.leadership_ballots
    ):
        return None

    year_ticks = state.calendar.year_days * state.calendar.day_seconds
    period_index = state.current_tick // year_ticks
    if period_index < 1:
        return None

    eligible_voter_ids = _electorate(
        state, governance, incumbent_id=None, policy=governance.succession_policy,
    )
    if not eligible_voter_ids or controlled.intersection(eligible_voter_ids):
        return None

    if office_holder_id is not None:
        candidates = (office_holder_id,) if _is_active_registered_member(
            state, governance, office_holder_id,
        ) and office_holder_id not in controlled else ()
    else:
        candidates = tuple(
            member_id for member_id in sorted(governance.members, key=str)
            if member_id not in controlled and _is_active_registered_member(state, governance, member_id)
        )[:_CANDIDATE_LIMIT]
    if not candidates:
        return None

    return VacancyOpportunity(
        organization_id=organization_id,
        governance_revision=governance.revision,
        period_index=period_index,
        proposer_id=eligible_voter_ids[0],
        candidate_ids=candidates,
        eligible_voter_ids=eligible_voter_ids,
    )


def _is_active_registered_member(
    state: WorldState,
    governance: GovernanceComponent,
    member_id: UUID,
) -> bool:
    person = state.entities.get(member_id)
    return (
        member_id in governance.members
        and person is not None
        and person.kind in {"character", "person"}
        and person.status == "active"
    )
