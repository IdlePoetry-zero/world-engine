from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from world_engine.domain.components import GovernanceComponent, OfficeRecord
from world_engine.domain.leadership_ballots import (
    LeadershipBallot,
    LeadershipBallotError,
    ballot_resolvable_status,
    cancel_invalidated_leadership_ballot,
    cast_leadership_ballot_vote,
    close_leadership_ballot,
    open_leadership_ballot,
)
from world_engine.domain.models import WorldEntity, WorldState


def _election_state(*, threshold: str = "0.5", extra_member_status: str = "active"):
    incumbent_id, nominee_id, voter_id, extra_id, organization_id = (uuid4() for _ in range(5))
    governance = GovernanceComponent(
        revision=4,
        members=[incumbent_id, nominee_id, voter_id, extra_id],
        leader_id=incumbent_id,
        succession_policy="election",
        decision_threshold=Decimal(threshold),
        offices=[OfficeRecord(office_id="office.leader", title="Leader", holder_id=incumbent_id)],
    )
    entities = {
        incumbent_id: WorldEntity(id=incumbent_id, kind="character", name="Incumbent"),
        nominee_id: WorldEntity(id=nominee_id, kind="character", name="Nominee"),
        voter_id: WorldEntity(id=voter_id, kind="character", name="Voter"),
        extra_id: WorldEntity(id=extra_id, kind="character", name="Extra", status=extra_member_status),
        organization_id: WorldEntity(
            id=organization_id,
            kind="organization",
            name="Council House",
            components={"governance": governance.model_dump(mode="json")},
        ),
    }
    state = WorldState(current_tick=120, entities=entities)
    return state, organization_id, incumbent_id, nominee_id, voter_id, extra_id


def _open(state, organization_id, incumbent_id, nominee_id, *, existing=()):
    return open_leadership_ballot(
        state,
        ballot_id=uuid4(),
        organization_id=organization_id,
        nominee_id=nominee_id,
        actor_id=incumbent_id,
        existing_ballots=existing,
    )


def test_election_snapshots_active_members_and_requires_incumbent_close():
    state, organization_id, incumbent_id, nominee_id, voter_id, extra_id = _election_state(
        extra_member_status="dead",
    )
    governance_before = state.entities[organization_id].components["governance"]
    ballot = _open(state, organization_id, incumbent_id, nominee_id)
    assert ballot.incumbent_id == incumbent_id
    assert ballot.policy == "election"
    assert ballot.governance_revision == 4
    assert ballot.opened_tick == ballot.updated_tick == 120
    assert ballot.threshold == Decimal("0.5")
    assert ballot.eligible_voter_ids == tuple(sorted((nominee_id, voter_id), key=str))
    assert incumbent_id not in ballot.eligible_voter_ids
    assert extra_id not in ballot.eligible_voter_ids

    first = cast_leadership_ballot_vote(
        state, ballot, voter_id=nominee_id, choice="yes", expected_ballot_revision=1,
    )
    assert first.revision == 2 and first.status == "open"
    assert ballot_resolvable_status(first) is None
    second = cast_leadership_ballot_vote(
        state, first, voter_id=voter_id, choice="yes", expected_ballot_revision=2,
    )
    assert second.status == "open"  # a vote never impersonates incumbent closure
    assert ballot_resolvable_status(second) == "approved"
    with pytest.raises(LeadershipBallotError, match="only the ballot's current incumbent"):
        close_leadership_ballot(
            state, second, actor_id=nominee_id, expected_ballot_revision=3,
        )
    closed = close_leadership_ballot(
        state, second, actor_id=incumbent_id, expected_ballot_revision=3,
    )
    assert closed.status == "approved" and closed.revision == 4
    assert closed.updated_tick == state.current_tick
    assert state.entities[organization_id].components["governance"] == governance_before


def test_strict_half_threshold_tie_rejects_and_early_impossibility_is_resolvable():
    state, organization_id, incumbent_id, nominee_id, voter_id, extra_id = _election_state(
        extra_member_status="dead",
    )
    ballot = _open(state, organization_id, incumbent_id, nominee_id)
    one_yes = cast_leadership_ballot_vote(
        state, ballot, voter_id=nominee_id, choice="yes", expected_ballot_revision=1,
    )
    tie = cast_leadership_ballot_vote(
        state, one_yes, voter_id=voter_id, choice="no", expected_ballot_revision=2,
    )
    assert ballot_resolvable_status(tie) == "rejected"
    assert close_leadership_ballot(
        state, tie, actor_id=incumbent_id, expected_ballot_revision=3,
    ).status == "rejected"

    state, organization_id, incumbent_id, nominee_id, voter_id, extra_id = _election_state(
        extra_member_status="dead",
    )
    # Add a second active non-incumbent elector, producing three voters.
    governance = GovernanceComponent.model_validate(
        state.entities[organization_id].components["governance"]
    )
    active_fourth = uuid4()
    expanded_governance = governance.model_copy(update={
        "revision": 5,
        "members": [*governance.members, active_fourth],
    })
    expanded_entities = dict(state.entities)
    expanded_entities[active_fourth] = WorldEntity(
        id=active_fourth, kind="character", name="Another voter",
    )
    org = expanded_entities[organization_id]
    expanded_entities[organization_id] = org.model_copy(update={
        "components": {"governance": expanded_governance.model_dump(mode="json")}
    })
    state = WorldState.model_validate({
        **state.model_dump(mode="json"),
        "entities": {str(key): value.model_dump(mode="json") for key, value in expanded_entities.items()},
    })
    ballot = _open(state, organization_id, incumbent_id, nominee_id)
    first_no = cast_leadership_ballot_vote(
        state, ballot, voter_id=nominee_id, choice="no", expected_ballot_revision=1,
    )
    second_no = cast_leadership_ballot_vote(
        state, first_no, voter_id=voter_id, choice="no", expected_ballot_revision=2,
    )
    assert ballot_resolvable_status(second_no) == "rejected"
    with pytest.raises(LeadershipBallotError, match="mathematically determined"):
        cast_leadership_ballot_vote(
            state, second_no, voter_id=active_fourth, choice="yes", expected_ballot_revision=3,
        )
    assert close_leadership_ballot(
        state, second_no, actor_id=incumbent_id, expected_ballot_revision=3,
    ).status == "rejected"


def test_council_electorate_uses_only_active_member_holders_of_voting_offices():
    incumbent_id, nominee_id, voter_id, dead_voter_id, other_id, organization_id = (uuid4() for _ in range(6))
    governance = GovernanceComponent(
        revision=9,
        members=[incumbent_id, nominee_id, voter_id, dead_voter_id, other_id],
        leader_id=incumbent_id,
        succession_policy="council",
        offices=[
            OfficeRecord(office_id="office.leader", title="Leader", holder_id=incumbent_id),
            OfficeRecord(office_id="office.council.a", title="Council", holder_id=voter_id,
                         authority=["authority.succession_vote"]),
            OfficeRecord(office_id="office.council.b", title="Second council seat", holder_id=voter_id,
                         authority=["authority.succession_vote"]),
            OfficeRecord(office_id="office.dead", title="Vacant by death", holder_id=dead_voter_id,
                         authority=["authority.succession_vote"]),
            OfficeRecord(office_id="office.treasurer", title="Treasurer", holder_id=other_id,
                         authority=["authority.finance"]),
        ],
    )
    state = WorldState(entities={
        incumbent_id: WorldEntity(id=incumbent_id, kind="character", name="Incumbent"),
        nominee_id: WorldEntity(id=nominee_id, kind="character", name="Nominee"),
        voter_id: WorldEntity(id=voter_id, kind="character", name="Councillor"),
        dead_voter_id: WorldEntity(id=dead_voter_id, kind="character", name="Dead councillor", status="dead"),
        other_id: WorldEntity(id=other_id, kind="character", name="Treasurer"),
        organization_id: WorldEntity(id=organization_id, kind="organization", components={
            "governance": governance.model_dump(mode="json"),
        }),
    })
    ballot = _open(state, organization_id, incumbent_id, nominee_id)
    assert ballot.policy == "council"
    assert ballot.eligible_voter_ids == (voter_id,)
    yes = cast_leadership_ballot_vote(
        state, ballot, voter_id=voter_id, choice="yes", expected_ballot_revision=1,
    )
    assert ballot_resolvable_status(yes) == "approved"
    assert close_leadership_ballot(
        state, yes, actor_id=incumbent_id, expected_ballot_revision=2,
    ).status == "approved"


def test_ballot_cas_duplicate_votes_and_invalidated_governance_cancellation():
    state, organization_id, incumbent_id, nominee_id, voter_id, _extra_id = _election_state()
    ballot = _open(state, organization_id, incumbent_id, nominee_id)
    with pytest.raises(LeadershipBallotError, match="expected_ballot_revision"):
        cast_leadership_ballot_vote(
            state, ballot, voter_id=nominee_id, choice="yes", expected_ballot_revision=0,
        )
    with pytest.raises(LeadershipBallotError, match="cannot close before"):
        close_leadership_ballot(
            state, ballot, actor_id=incumbent_id, expected_ballot_revision=1,
        )
    yes = cast_leadership_ballot_vote(
        state, ballot, voter_id=nominee_id, choice="yes", expected_ballot_revision=1,
    )
    with pytest.raises(LeadershipBallotError, match="only one vote"):
        cast_leadership_ballot_vote(
            state, yes, voter_id=nominee_id, choice="no", expected_ballot_revision=2,
        )

    organization = state.entities[organization_id]
    governance = GovernanceComponent.model_validate(organization.components["governance"])
    revised = governance.model_copy(update={"revision": governance.revision + 1})
    entities = dict(state.entities)
    entities[organization_id] = organization.model_copy(update={
        "components": {"governance": revised.model_dump(mode="json")}
    })
    invalidated_state = WorldState.model_validate({
        **state.model_dump(mode="json"),
        "entities": {str(key): value.model_dump(mode="json") for key, value in entities.items()},
    })
    cancelled = cancel_invalidated_leadership_ballot(
        invalidated_state, ballot, expected_ballot_revision=1,
    )
    assert cancelled.status == "cancelled" and cancelled.revision == 2
    with pytest.raises(LeadershipBallotError, match="governance revision changed"):
        cast_leadership_ballot_vote(
            invalidated_state, ballot, voter_id=nominee_id, choice="yes", expected_ballot_revision=1,
        )
    with pytest.raises(LeadershipBallotError, match="still valid"):
        cancel_invalidated_leadership_ballot(state, ballot, expected_ballot_revision=1)


def test_open_rejects_nonincumbent_unknown_policy_no_electorate_and_parallel_ballot():
    state, organization_id, incumbent_id, nominee_id, voter_id, _extra_id = _election_state()
    with pytest.raises(LeadershipBallotError, match="only the current incumbent"):
        open_leadership_ballot(
            state, ballot_id=uuid4(), organization_id=organization_id,
            nominee_id=nominee_id, actor_id=nominee_id, existing_ballots=(),
        )
    ballot = _open(state, organization_id, incumbent_id, nominee_id)
    with pytest.raises(LeadershipBallotError, match="open leadership ballot already exists"):
        _open(state, organization_id, voter_id, nominee_id, existing=(ballot,))

    organization = state.entities[organization_id]
    governance = GovernanceComponent.model_validate(organization.components["governance"])
    appointment = governance.model_copy(update={"succession_policy": "appointment"})
    entities = dict(state.entities)
    entities[organization_id] = organization.model_copy(update={
        "components": {"governance": appointment.model_dump(mode="json")}
    })
    appointment_state = WorldState.model_validate({
        **state.model_dump(mode="json"),
        "entities": {str(key): value.model_dump(mode="json") for key, value in entities.items()},
    })
    with pytest.raises(LeadershipBallotError, match="does not allow a ballot"):
        _open(appointment_state, organization_id, incumbent_id, nominee_id)


def test_ballot_model_rejects_forged_terminal_status_and_unsorted_votes():
    state, organization_id, incumbent_id, nominee_id, voter_id, extra_id = _election_state()
    ballot = _open(state, organization_id, incumbent_id, nominee_id)
    raw = ballot.model_dump(mode="python")
    raw["status"] = "approved"
    with pytest.raises(ValueError, match="terminal ballot status"):
        LeadershipBallot.model_validate(raw)

    vote_rows = [
        {"voter_id": nominee_id, "choice": "yes"},
        {"voter_id": voter_id, "choice": "no"},
    ]
    vote_rows.sort(key=lambda row: str(row["voter_id"]))
    raw = {
        **ballot.model_dump(mode="python"),
        "votes": tuple(reversed(vote_rows)),
        "revision": 3,
    }
    with pytest.raises(ValueError, match="votes must be sorted"):
        LeadershipBallot.model_validate(raw)


def test_exact_nonhalf_threshold_uses_registered_governance_threshold():
    state, organization_id, incumbent_id, nominee_id, voter_id, extra_id = _election_state(threshold="0.6")
    ballot = _open(state, organization_id, incumbent_id, nominee_id)
    assert ballot.threshold == Decimal("0.6")
    yes_one = cast_leadership_ballot_vote(
        state, ballot, voter_id=nominee_id, choice="yes", expected_ballot_revision=1,
    )
    assert ballot_resolvable_status(yes_one) is None
    yes_two = cast_leadership_ballot_vote(
        state, yes_one, voter_id=voter_id, choice="yes", expected_ballot_revision=2,
    )
    assert ballot_resolvable_status(yes_two) == "approved"
    assert close_leadership_ballot(
        state, yes_two, actor_id=incumbent_id, expected_ballot_revision=3,
    ).status == "approved"

