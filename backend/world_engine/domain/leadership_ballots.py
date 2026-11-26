"""Pure, revisioned ballots for a living incumbent's voluntary transfer.

These planners only update a private ballot projection. They never mutate an
organization's governance, appoint the nominee, or infer votes from dialogue.
The host/reducer must persist the returned DTO in an authorized event.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Annotated, Literal, Protocol, TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .components import GovernanceComponent, is_leadership_office

if TYPE_CHECKING:
    from .models import WorldState


BallotTick = Annotated[int, Field(ge=-(2**63), le=2**63 - 1)]
BallotVoteChoice = Literal["yes", "no", "abstain"]
BallotPolicy = Literal["election", "council"]
BallotStatus = Literal["open", "approved", "rejected", "cancelled"]
BallotResolution = Literal["approved", "rejected"]


class BallotModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class LeadershipBallotVote(BallotModel):
    """One immutable vote from one member of the fixed electorate."""

    voter_id: UUID
    choice: BallotVoteChoice


class LeadershipBallot(BallotModel):
    """Private ballot projection pinned to a leader and governance revision.

    ``incumbent_id`` identifies the incumbent for the established voluntary
    transfer flow. Vacancy ballots leave it empty and record their explicit
    proposer in ``vacancy_proposer_id``. The latter is omitted from legacy
    incumbent-ballot dumps when unset.
    """

    ballot_id: UUID
    organization_id: UUID
    incumbent_id: UUID | None
    vacancy_proposer_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    nominee_id: UUID
    opened_tick: BallotTick
    updated_tick: BallotTick
    governance_revision: int = Field(ge=1)
    policy: BallotPolicy
    eligible_voter_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100_000)
    threshold: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    votes: tuple[LeadershipBallotVote, ...] = Field(default=(), max_length=100_000)
    revision: int = Field(default=1, ge=1, le=2**63 - 1)
    status: BallotStatus = "open"

    @model_validator(mode="after")
    def validate_order_uniqueness_and_resolution(self) -> LeadershipBallot:
        voter_ids = tuple(str(item) for item in self.eligible_voter_ids)
        electorate = set(voter_ids)
        if voter_ids != tuple(sorted(voter_ids)) or len(voter_ids) != len(electorate):
            raise ValueError("eligible_voter_ids must be sorted and unique")
        vote_ids = tuple(str(item.voter_id) for item in self.votes)
        if vote_ids != tuple(sorted(vote_ids)) or len(vote_ids) != len(set(vote_ids)):
            raise ValueError("votes must be sorted by voter_id and unique")
        if not set(vote_ids).issubset(electorate):
            raise ValueError("every ballot vote must come from the fixed electorate")
        if self.incumbent_id is None:
            if self.vacancy_proposer_id is None:
                raise ValueError("a vacancy ballot requires a vacancy proposer")
            if str(self.vacancy_proposer_id) not in electorate:
                raise ValueError("the vacancy proposer must belong to the fixed electorate")
        elif self.vacancy_proposer_id is not None:
            raise ValueError("an incumbent ballot cannot record a vacancy proposer")
        if self.updated_tick < self.opened_tick:
            raise ValueError("updated_tick cannot precede opened_tick")
        if self.status in {"approved", "rejected"}:
            if _resolvable_status(self) != self.status:
                raise ValueError("terminal ballot status does not match the recorded votes")
        return self

    @property
    def procedural_actor_id(self) -> UUID:
        """Return the actor authorized to perform proposer/incumbent steps."""
        actor_id = self.incumbent_id if self.incumbent_id is not None else self.vacancy_proposer_id
        assert actor_id is not None  # Enforced by validate_order_uniqueness_and_resolution.
        return actor_id


class _Entity(Protocol):
    id: UUID
    kind: str
    status: str
    components: Mapping[str, object]


class _State(Protocol):
    current_tick: int
    entities: Mapping[UUID, _Entity]


class LeadershipBallotError(ValueError):
    """The requested ballot transition is not valid for the supplied state."""


def open_leadership_ballot(
    state: WorldState | _State,
    *,
    ballot_id: UUID,
    organization_id: UUID,
    nominee_id: UUID,
    actor_id: UUID,
    existing_ballots: Iterable[LeadershipBallot],
) -> LeadershipBallot:
    """Open a ballot on behalf of the living incumbent using current governance.

    Electorate, threshold, policy, and governance revision are always derived
    from the state. `existing_ballots` must be the authoritative ballot list
    for the world; it is used only to prevent duplicate IDs and concurrent open
    ballots for the same organization.
    """
    _validate_opening_slot(ballot_id, organization_id, existing_ballots)

    _organization, governance = _governance_for(state, organization_id)
    incumbent_id = governance.leader_id
    if incumbent_id is None or actor_id != incumbent_id:
        raise LeadershipBallotError("only the current incumbent may open a leadership ballot")
    _require_live_leadership(state, governance, incumbent_id)
    _require_active_member(state, governance, nominee_id, "nominee")
    if nominee_id == incumbent_id:
        raise LeadershipBallotError("the incumbent cannot nominate themself")
    policy = governance.succession_policy
    if policy not in {"election", "council"}:
        raise LeadershipBallotError("the organization's succession policy does not allow a ballot")

    electorate = _electorate(state, governance, incumbent_id, policy)
    if not electorate:
        raise LeadershipBallotError("the registered succession electorate is empty")
    return LeadershipBallot(
        ballot_id=ballot_id,
        organization_id=organization_id,
        incumbent_id=incumbent_id,
        nominee_id=nominee_id,
        opened_tick=state.current_tick,
        updated_tick=state.current_tick,
        governance_revision=governance.revision,
        policy=policy,
        eligible_voter_ids=electorate,
        threshold=governance.decision_threshold,
        votes=(),
        revision=1,
        status="open",
    )


def open_vacant_leadership_ballot(
    state: WorldState | _State,
    *,
    ballot_id: UUID,
    organization_id: UUID,
    nominee_id: UUID,
    actor_id: UUID,
    existing_ballots: Iterable[LeadershipBallot],
) -> LeadershipBallot:
    """Open an explicit election/council ballot for a vacant leader slot.

    The organization must have no leader, and registered leadership offices
    must be empty or held only by the active nominee. The proposer must be an
    active member in the policy-derived fixed electorate. As with incumbent
    ballots, this only creates a ballot projection; it never appoints the
    nominee.
    """
    _validate_opening_slot(ballot_id, organization_id, existing_ballots)
    _organization, governance = _governance_for(state, organization_id)
    _require_vacant_leadership(governance, nominee_id)
    _require_active_member(state, governance, actor_id, "vacancy proposer")
    _require_active_member(state, governance, nominee_id, "nominee")
    policy = governance.succession_policy
    if policy not in {"election", "council"}:
        raise LeadershipBallotError("the organization's succession policy does not allow a ballot")

    electorate = _electorate(state, governance, None, policy)
    if not electorate:
        raise LeadershipBallotError("the registered succession electorate is empty")
    if actor_id not in electorate:
        raise LeadershipBallotError("the vacancy proposer must belong to the registered succession electorate")
    return LeadershipBallot(
        ballot_id=ballot_id,
        organization_id=organization_id,
        incumbent_id=None,
        vacancy_proposer_id=actor_id,
        nominee_id=nominee_id,
        opened_tick=state.current_tick,
        updated_tick=state.current_tick,
        governance_revision=governance.revision,
        policy=policy,
        eligible_voter_ids=electorate,
        threshold=governance.decision_threshold,
        votes=(),
        revision=1,
        status="open",
    )


def cast_leadership_ballot_vote(
    state: WorldState | _State,
    ballot: LeadershipBallot,
    *,
    voter_id: UUID,
    choice: BallotVoteChoice,
    expected_ballot_revision: int,
) -> LeadershipBallot:
    """Record one electorate member's vote with ballot and governance CAS."""
    _require_open_ballot(state, ballot, expected_ballot_revision)
    if voter_id not in ballot.eligible_voter_ids:
        raise LeadershipBallotError("voter is not in the ballot's fixed electorate")
    if any(item.voter_id == voter_id for item in ballot.votes):
        raise LeadershipBallotError("each eligible voter may cast only one vote")
    if _resolvable_status(ballot) is not None:
        required_closer = "incumbent" if ballot.incumbent_id is not None else "vacancy proposer"
        raise LeadershipBallotError(
            f"the ballot outcome is already mathematically determined; {required_closer} must close it"
        )

    vote = LeadershipBallotVote(voter_id=voter_id, choice=choice)
    votes = tuple(sorted((*ballot.votes, vote), key=lambda item: str(item.voter_id)))
    return LeadershipBallot.model_validate({
        **ballot.model_dump(mode="python"),
        "votes": votes,
        "revision": ballot.revision + 1,
        "updated_tick": state.current_tick,
        # Voting never appoints or closes. The incumbent's/proposer's close is
        # separately auditable, even when this vote makes the result irreversible.
        "status": "open",
    })


def close_leadership_ballot(
    state: WorldState | _State,
    ballot: LeadershipBallot,
    *,
    actor_id: UUID,
    expected_ballot_revision: int,
) -> LeadershipBallot:
    """Resolve a completed or mathematically irreversible ballot.

    Closing is an explicit act by the same living incumbent or vacancy
    proposer who opened it. This function only changes ballot status; it never
    changes governance or transfers an office.
    """
    _require_open_ballot(state, ballot, expected_ballot_revision)
    if actor_id != ballot.procedural_actor_id:
        if ballot.incumbent_id is None:
            raise LeadershipBallotError("only the ballot's vacancy proposer may close it")
        raise LeadershipBallotError("only the ballot's current incumbent may close it")
    result = _resolvable_status(ballot)
    if result is None:
        raise LeadershipBallotError("the ballot cannot close before all votes or an irreversible result")
    return LeadershipBallot.model_validate({
        **ballot.model_dump(mode="python"),
        "status": result,
        "revision": ballot.revision + 1,
        "updated_tick": state.current_tick,
    })


def cancel_invalidated_leadership_ballot(
    state: WorldState | _State,
    ballot: LeadershipBallot,
    *,
    expected_ballot_revision: int,
) -> LeadershipBallot:
    """Cancel an open ballot whose pinned governance/electorate is no longer live.

    This is a system cleanup planner, not an actor action. A still-valid ballot
    cannot be cancelled through this helper.
    """
    _require_ballot_revision(state, ballot, expected_ballot_revision)
    if ballot.status != "open":
        raise LeadershipBallotError("only an open ballot can be cancelled")
    issue = _snapshot_issue(state, ballot)
    if issue is None:
        raise LeadershipBallotError("the ballot is still valid and cannot be cancelled as invalidated")
    return LeadershipBallot.model_validate({
        **ballot.model_dump(mode="python"),
        "status": "cancelled",
        "revision": ballot.revision + 1,
        "updated_tick": state.current_tick,
    })


def leadership_ballot_is_invalidated(state: WorldState | _State, ballot: LeadershipBallot) -> bool:
    return ballot.status == "open" and _snapshot_issue(state, ballot) is not None


def ballot_resolvable_status(ballot: LeadershipBallot) -> BallotResolution | None:
    """Return the determined outcome without changing ballot state."""
    return _resolvable_status(ballot)


def _require_open_ballot(
    state: WorldState | _State,
    ballot: LeadershipBallot,
    expected_ballot_revision: int,
) -> None:
    _require_ballot_revision(state, ballot, expected_ballot_revision)
    if ballot.status != "open":
        raise LeadershipBallotError("only an open leadership ballot can be changed")
    issue = _snapshot_issue(state, ballot)
    if issue is not None:
        raise LeadershipBallotError(f"leadership ballot is invalidated: {issue}")


def _require_ballot_revision(
    state: WorldState | _State,
    ballot: LeadershipBallot,
    expected_ballot_revision: int,
) -> None:
    if expected_ballot_revision != ballot.revision:
        raise LeadershipBallotError("expected_ballot_revision does not match the authoritative ballot")
    if state.current_tick < ballot.updated_tick:
        raise LeadershipBallotError("state tick precedes the ballot's latest update")


def _snapshot_issue(state: WorldState | _State, ballot: LeadershipBallot) -> str | None:
    if state.current_tick < ballot.opened_tick:
        return "state tick precedes ballot opening"
    organization = state.entities.get(ballot.organization_id)
    if organization is None or organization.status != "active":
        return "organization is not active"
    try:
        governance = GovernanceComponent.model_validate(organization.components.get("governance"))
    except Exception:
        return "organization governance is unavailable"
    if governance.revision != ballot.governance_revision:
        return "governance revision changed"
    if governance.succession_policy != ballot.policy or ballot.policy not in {"election", "council"}:
        return "succession policy changed"
    if ballot.incumbent_id is None:
        try:
            _require_vacant_leadership(governance, ballot.nominee_id)
            _require_active_member(state, governance, ballot.procedural_actor_id, "vacancy proposer")
            _require_active_member(state, governance, ballot.nominee_id, "nominee")
            electorate = _electorate(state, governance, None, ballot.policy)
        except LeadershipBallotError as exc:
            return str(exc)
    else:
        if governance.leader_id != ballot.incumbent_id:
            return "incumbent changed"
        try:
            _require_live_leadership(state, governance, ballot.incumbent_id)
            _require_active_member(state, governance, ballot.nominee_id, "nominee")
            electorate = _electorate(state, governance, ballot.incumbent_id, ballot.policy)
        except LeadershipBallotError as exc:
            return str(exc)
        if ballot.nominee_id == ballot.incumbent_id:
            return "nominee is the incumbent"
    if electorate != ballot.eligible_voter_ids:
        return "fixed electorate changed"
    if governance.decision_threshold != ballot.threshold:
        return "decision threshold changed"
    return None


def _governance_for(
    state: WorldState | _State,
    organization_id: UUID,
):
    entity = state.entities.get(organization_id)
    if (
        entity is None
        or entity.status != "active"
        or entity.kind not in {"organization", "polity", "faction", "company", "government"}
    ):
        raise LeadershipBallotError("the ballot organization is not active")
    try:
        governance = GovernanceComponent.model_validate(entity.components.get("governance"))
    except Exception as exc:
        raise LeadershipBallotError("the ballot organization has no valid governance component") from exc
    return entity, governance


def _validate_opening_slot(
    ballot_id: UUID,
    organization_id: UUID,
    existing_ballots: Iterable[LeadershipBallot],
) -> None:
    stored = tuple(existing_ballots)
    if any(ballot.ballot_id == ballot_id for ballot in stored):
        raise LeadershipBallotError("ballot_id already exists")
    if any(
        ballot.organization_id == organization_id and ballot.status == "open"
        for ballot in stored
    ):
        raise LeadershipBallotError("an open leadership ballot already exists for this organization")


def _require_live_leadership(state, governance, incumbent_id: UUID) -> None:
    incumbent = state.entities.get(incumbent_id)
    if not _is_active_character(incumbent) or incumbent_id not in governance.members:
        raise LeadershipBallotError("the incumbent must be a living active organization member")
    offices = [office for office in governance.offices if is_leadership_office(office)]
    if not offices or any(office.holder_id != incumbent_id for office in offices):
        raise LeadershipBallotError("the incumbent must hold every registered leadership office")


def vacant_leadership_office_holder(governance: GovernanceComponent) -> UUID | None:
    """Return the sole holder of registered leadership offices, if any.

    An organization-level leader, a missing registered leadership office, or
    offices split across multiple holders is not a compatible vacancy state.
    Non-leadership council offices do not participate in this check.
    """
    if governance.leader_id is not None:
        raise LeadershipBallotError("the organization already has a leader")
    offices = [office for office in governance.offices if is_leadership_office(office)]
    if not offices:
        raise LeadershipBallotError("the organization has no registered leadership office")
    holders = {office.holder_id for office in offices if office.holder_id is not None}
    if len(holders) > 1:
        raise LeadershipBallotError("a registered leadership office is occupied by conflicting holders")
    return next(iter(holders), None)


def _require_vacant_leadership(governance: GovernanceComponent, nominee_id: UUID) -> None:
    office_holder_id = vacant_leadership_office_holder(governance)
    if office_holder_id is not None and office_holder_id != nominee_id:
        raise LeadershipBallotError("a registered leadership office is occupied by a different holder")


def _require_active_member(state, governance: GovernanceComponent, person_id: UUID, label: str) -> None:
    person = state.entities.get(person_id)
    if person_id not in governance.members or not _is_active_character(person):
        raise LeadershipBallotError(f"the {label} must be a living active organization member")


def _is_active_character(entity) -> bool:
    return (
        entity is not None
        and entity.kind in {"character", "person"}
        and entity.status == "active"
    )


def _electorate(
    state: WorldState | _State,
    governance: GovernanceComponent,
    incumbent_id: UUID | None,
    policy: BallotPolicy,
) -> tuple[UUID, ...]:
    members = set(governance.members)
    if policy == "election":
        eligible = {
            member_id for member_id in members
            if member_id != incumbent_id and _is_active_character(state.entities.get(member_id))
        }
    else:
        eligible = {
            office.holder_id
            for office in governance.offices
            if "authority.succession_vote" in office.authority
            and office.holder_id is not None
            and office.holder_id != incumbent_id
            and office.holder_id in members
            and _is_active_character(state.entities.get(office.holder_id))
        }
    return tuple(sorted(eligible, key=str))


def _resolvable_status(ballot: LeadershipBallot) -> BallotResolution | None:
    electorate_count = len(ballot.eligible_voter_ids)
    if electorate_count == 0:
        return "rejected"
    yes_count = sum(vote.choice == "yes" for vote in ballot.votes)
    remaining = electorate_count - len(ballot.votes)
    yes_share = Decimal(yes_count) / Decimal(electorate_count)
    maximum_yes_share = Decimal(yes_count + remaining) / Decimal(electorate_count)
    approved = yes_count > 0 and _meets_threshold(yes_share, ballot.threshold)
    if approved:
        return "approved"
    if not _can_still_approve(maximum_yes_share, yes_count + remaining, ballot.threshold):
        return "rejected"
    if not remaining:
        return "rejected"
    return None


def _meets_threshold(share: Decimal, threshold: Decimal) -> bool:
    if threshold == Decimal("0.5"):
        return share > threshold
    return share >= threshold


def _can_still_approve(maximum_share: Decimal, maximum_yes: int, threshold: Decimal) -> bool:
    if maximum_yes == 0:
        return False
    if threshold == Decimal("0.5"):
        return maximum_share > threshold
    return maximum_share >= threshold

