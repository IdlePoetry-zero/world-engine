from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import GovernanceComponent, OfficeRecord
from world_engine.domain.leadership_ballots import (
    LeadershipBallot,
    LeadershipBallotError,
    ballot_resolvable_status,
    cancel_invalidated_leadership_ballot,
    cast_leadership_ballot_vote,
    close_leadership_ballot,
    leadership_ballot_is_invalidated,
    open_leadership_ballot,
    open_vacant_leadership_ballot,
    vacant_leadership_office_holder,
)
from world_engine.domain.models import WorldEntity, WorldState


def _state(
    *,
    policy: str = "election",
    incumbent: bool = False,
    occupied_leadership_office: bool = False,
    leadership_office_holder: str | None = None,
    has_leadership_office: bool = True,
    council_voters: tuple[str, ...] = ("proposer", "other"),
) -> tuple[WorldState, dict[str, object]]:
    ids = {name: uuid4() for name in ("proposer", "nominee", "other", "organization", "outsider")}
    members = [ids[name] for name in ("proposer", "nominee", "other")]
    leader_id = ids["proposer"] if incumbent else None
    offices = []
    if has_leadership_office:
        offices.append(OfficeRecord(
            office_id="office.leader",
            title="Leader",
            holder_id=(
                ids["proposer"] if incumbent else
                ids[leadership_office_holder] if leadership_office_holder is not None else
                ids["other"] if occupied_leadership_office else None
            ),
        ))
    if policy == "council":
        offices.extend(
            OfficeRecord(
                office_id=f"office.council.{name}",
                title=f"Council {name}",
                holder_id=ids[name],
                authority=["authority.succession_vote"],
            )
            for name in council_voters
        )
    governance = GovernanceComponent(
        revision=3,
        members=members,
        leader_id=leader_id,
        offices=offices,
        succession_policy=policy,
    )
    entities = {
        ids[name]: WorldEntity(id=ids[name], kind="character", name=name.title())
        for name in ("proposer", "nominee", "other", "outsider")
    }
    entities[ids["organization"]] = WorldEntity(
        id=ids["organization"],
        kind="organization",
        name="Council House",
        components={"governance": governance.model_dump(mode="json")},
    )
    return WorldState(current_tick=120, entities=entities), ids


def _open_vacancy(state: WorldState, ids: dict[str, object], *, nominee: str = "nominee", actor: str = "proposer", existing=()):
    return open_vacant_leadership_ballot(
        state,
        ballot_id=uuid4(),
        organization_id=ids["organization"],
        nominee_id=ids[nominee],
        actor_id=ids[actor],
        existing_ballots=existing,
    )


def _replace_governance(state: WorldState, organization_id, governance: GovernanceComponent) -> WorldState:
    entities = dict(state.entities)
    organization = entities[organization_id]
    entities[organization_id] = organization.model_copy(update={
        "components": {**organization.components, "governance": governance.model_dump(mode="json")},
    })
    return state.model_copy(update={"entities": entities})


def test_vacant_election_ballot_votes_and_closes_without_assigning_leadership():
    state, ids = _state()
    before = state.entities[ids["organization"]].components["governance"]
    ballot = _open_vacancy(state, ids)

    assert ballot.incumbent_id is None
    assert ballot.vacancy_proposer_id == ids["proposer"]
    assert ballot.procedural_actor_id == ids["proposer"]
    assert ballot.eligible_voter_ids == tuple(sorted(
        (ids["proposer"], ids["nominee"], ids["other"]), key=str,
    ))
    assert not leadership_ballot_is_invalidated(state, ballot)

    yes_from_nominee = cast_leadership_ballot_vote(
        state, ballot, voter_id=ids["nominee"], choice="yes", expected_ballot_revision=1,
    )
    approved_votes = cast_leadership_ballot_vote(
        state, yes_from_nominee, voter_id=ids["other"], choice="yes", expected_ballot_revision=2,
    )
    assert ballot_resolvable_status(approved_votes) == "approved"
    with pytest.raises(LeadershipBallotError, match="only the ballot's vacancy proposer"):
        close_leadership_ballot(
            state, approved_votes, actor_id=ids["other"], expected_ballot_revision=3,
        )
    closed = close_leadership_ballot(
        state, approved_votes, actor_id=ids["proposer"], expected_ballot_revision=3,
    )
    assert closed.status == "approved"
    assert closed.revision == 4
    assert state.entities[ids["organization"]].components["governance"] == before


def test_council_vacancy_ballot_uses_only_active_voting_office_holders():
    state, ids = _state(policy="council", leadership_office_holder="nominee")
    ballot = _open_vacancy(state, ids, actor="proposer")
    assert ballot.eligible_voter_ids == tuple(sorted((ids["proposer"], ids["other"]), key=str))
    assert ids["nominee"] not in ballot.eligible_voter_ids
    governance = GovernanceComponent.model_validate(
        state.entities[ids["organization"]].components["governance"],
    )
    assert ballot.threshold == governance.decision_threshold

    with pytest.raises(LeadershipBallotError, match="proposer must belong to the registered succession electorate"):
        _open_vacancy(state, ids, actor="nominee")

    no_authority_state, no_authority_ids = _state(policy="council", council_voters=())
    with pytest.raises(LeadershipBallotError, match="registered succession electorate is empty"):
        _open_vacancy(no_authority_state, no_authority_ids)


@pytest.mark.parametrize(
    ("state_kwargs", "actor", "nominee", "message"),
    [
        ({"incumbent": True}, "other", "nominee", "already has a leader"),
        ({"occupied_leadership_office": True}, "proposer", "nominee", "leadership office is occupied"),
        ({"has_leadership_office": False}, "proposer", "nominee", "no registered leadership office"),
        ({"policy": "appointment"}, "proposer", "nominee", "does not allow a ballot"),
        ({}, "proposer", "outsider", "nominee must be a living active organization member"),
    ],
)
def test_vacancy_open_requires_empty_authorized_leadership_and_member_nominee(
    state_kwargs, actor, nominee, message,
):
    state, ids = _state(**state_kwargs)
    with pytest.raises(LeadershipBallotError, match=message):
        _open_vacancy(state, ids, actor=actor, nominee=nominee)


def test_vacancy_marker_invariants_and_legacy_incumbent_dump_shape():
    state, ids = _state()
    vacancy = _open_vacancy(state, ids)
    vacancy_raw = vacancy.model_dump(mode="python")
    assert vacancy_raw["vacancy_proposer_id"] == ids["proposer"]

    missing_proposer = {**vacancy_raw}
    missing_proposer.pop("vacancy_proposer_id")
    with pytest.raises(ValidationError, match="requires a vacancy proposer"):
        LeadershipBallot.model_validate(missing_proposer)

    proposer_not_elector = {**vacancy_raw, "vacancy_proposer_id": uuid4()}
    with pytest.raises(ValidationError, match="proposer must belong to the fixed electorate"):
        LeadershipBallot.model_validate(proposer_not_elector)

    incumbent_state, incumbent_ids = _state(incumbent=True)
    incumbent_ballot = open_leadership_ballot(
        incumbent_state,
        ballot_id=uuid4(),
        organization_id=incumbent_ids["organization"],
        nominee_id=incumbent_ids["nominee"],
        actor_id=incumbent_ids["proposer"],
        existing_ballots=(),
    )
    legacy_dump = incumbent_ballot.model_dump(mode="json")
    assert "vacancy_proposer_id" not in legacy_dump
    assert LeadershipBallot.model_validate(legacy_dump) == incumbent_ballot
    assert incumbent_ballot.procedural_actor_id == incumbent_ids["proposer"]


def test_invalidated_vacancy_ballot_can_be_cancelled_when_proposer_leaves_electorate():
    state, ids = _state()
    ballot = _open_vacancy(state, ids)
    dead_proposer = state.entities[ids["proposer"]].model_copy(update={"status": "dead"})
    entities = dict(state.entities)
    entities[ids["proposer"]] = dead_proposer
    invalidated = state.model_copy(update={"entities": entities})

    assert leadership_ballot_is_invalidated(invalidated, ballot)
    cancelled = cancel_invalidated_leadership_ballot(
        invalidated, ballot, expected_ballot_revision=1,
    )
    assert cancelled.status == "cancelled"
    assert cancelled.revision == 2
    with pytest.raises(LeadershipBallotError, match="invalidated"):
        cast_leadership_ballot_vote(
            invalidated, ballot, voter_id=ids["nominee"], choice="yes", expected_ballot_revision=1,
        )
    with pytest.raises(LeadershipBallotError, match="invalidated"):
        close_leadership_ballot(
            invalidated, ballot, actor_id=ids["proposer"], expected_ballot_revision=1,
        )


def test_vacancy_must_remain_empty_and_rejected_ballot_can_be_reopened():
    state, ids = _state()
    ballot = _open_vacancy(state, ids)
    organization = state.entities[ids["organization"]]
    governance = GovernanceComponent.model_validate(organization.components["governance"])
    # Keep the revision pinned here to prove that empty leadership is checked
    # directly as well as indirectly through normal revisioned transitions.
    occupied_leader = governance.model_copy(update={"leader_id": ids["other"]})
    occupied = _replace_governance(state, ids["organization"], occupied_leader)
    assert leadership_ballot_is_invalidated(occupied, ballot)
    with pytest.raises(LeadershipBallotError, match="invalidated: the organization already has a leader"):
        close_leadership_ballot(
            occupied, ballot, actor_id=ids["proposer"], expected_ballot_revision=1,
        )

    occupied_office = governance.model_copy(update={
        "offices": [office.model_copy(update={"holder_id": ids["other"]}) for office in governance.offices],
    })
    occupied_office_state = _replace_governance(state, ids["organization"], occupied_office)
    assert leadership_ballot_is_invalidated(occupied_office_state, ballot)
    with pytest.raises(LeadershipBallotError, match="invalidated: a registered leadership office is occupied"):
        cast_leadership_ballot_vote(
            occupied_office_state, ballot, voter_id=ids["nominee"], choice="yes", expected_ballot_revision=1,
        )

    first_no = cast_leadership_ballot_vote(
        state, ballot, voter_id=ids["nominee"], choice="no", expected_ballot_revision=1,
    )
    second_no = cast_leadership_ballot_vote(
        state, first_no, voter_id=ids["other"], choice="no", expected_ballot_revision=2,
    )
    assert ballot_resolvable_status(second_no) == "rejected"
    rejected = close_leadership_ballot(
        state, second_no, actor_id=ids["proposer"], expected_ballot_revision=3,
    )
    assert rejected.status == "rejected"

    reopened = _open_vacancy(state, ids, existing=(rejected,))
    assert reopened.status == "open"
    assert reopened.ballot_id != rejected.ballot_id


def test_vacant_office_holder_must_match_nominee_and_all_occupied_offices_must_agree():
    state, ids = _state(leadership_office_holder="nominee")
    organization = state.entities[ids["organization"]]
    governance = GovernanceComponent.model_validate(organization.components["governance"])
    assert vacant_leadership_office_holder(governance) == ids["nominee"]

    # Multiple registered leadership offices are compatible when they all
    # point to the one active nominee; empty slots may coexist with them.
    compatible_governance = governance.model_copy(update={
        "offices": [
            *governance.offices,
            OfficeRecord(
                office_id="office.deputy_leader",
                title="Deputy Leader",
                holder_id=ids["nominee"],
                authority=["organization.lead"],
            ),
            OfficeRecord(
                office_id="office.regent",
                title="Regent",
                authority=["organization.lead"],
            ),
        ],
    })
    assert vacant_leadership_office_holder(compatible_governance) == ids["nominee"]
    compatible_state = _replace_governance(state, ids["organization"], compatible_governance)
    ballot = _open_vacancy(compatible_state, ids)
    assert ballot.nominee_id == ids["nominee"]
    assert not leadership_ballot_is_invalidated(compatible_state, ballot)

    first_yes = cast_leadership_ballot_vote(
        compatible_state, ballot, voter_id=ids["nominee"], choice="yes", expected_ballot_revision=1,
    )
    second_yes = cast_leadership_ballot_vote(
        compatible_state, first_yes, voter_id=ids["other"], choice="yes", expected_ballot_revision=2,
    )
    closed = close_leadership_ballot(
        compatible_state, second_yes, actor_id=ids["proposer"], expected_ballot_revision=3,
    )
    assert closed.status == "approved"
    assert GovernanceComponent.model_validate(
        compatible_state.entities[ids["organization"]].components["governance"],
    ).leader_id is None

    wrong_nominee = _replace_governance(
        compatible_state,
        ids["organization"],
        compatible_governance.model_copy(update={
            "offices": [
                office.model_copy(update={"holder_id": ids["other"]})
                if office.office_id == "office.leader" else office
                for office in compatible_governance.offices
            ],
        }),
    )
    with pytest.raises(LeadershipBallotError, match="leadership office is occupied"):
        _open_vacancy(wrong_nominee, ids)

    conflicting_governance = compatible_governance.model_copy(update={
        "offices": [
            office.model_copy(update={"holder_id": ids["other"]})
            if office.office_id == "office.deputy_leader" else office
            for office in compatible_governance.offices
        ],
    })
    conflicting_state = _replace_governance(compatible_state, ids["organization"], conflicting_governance)
    assert leadership_ballot_is_invalidated(conflicting_state, ballot)
    with pytest.raises(LeadershipBallotError, match="invalidated: a registered leadership office is occupied"):
        cast_leadership_ballot_vote(
            conflicting_state, ballot, voter_id=ids["nominee"], choice="yes", expected_ballot_revision=1,
        )
    with pytest.raises(LeadershipBallotError, match="invalidated: a registered leadership office is occupied"):
        close_leadership_ballot(
            conflicting_state, ballot, actor_id=ids["proposer"], expected_ballot_revision=1,
        )
    with pytest.raises(LeadershipBallotError, match="leadership office is occupied"):
        _open_vacancy(conflicting_state, ids)


def test_vacant_leadership_office_holder_reports_empty_and_rejects_invalid_slot_sets():
    empty_state, empty_ids = _state()
    empty_governance = GovernanceComponent.model_validate(
        empty_state.entities[empty_ids["organization"]].components["governance"],
    )
    assert vacant_leadership_office_holder(empty_governance) is None

    leader_state, leader_ids = _state(incumbent=True)
    leader_governance = GovernanceComponent.model_validate(
        leader_state.entities[leader_ids["organization"]].components["governance"],
    )
    with pytest.raises(LeadershipBallotError, match="already has a leader"):
        vacant_leadership_office_holder(leader_governance)

    no_office_state, no_office_ids = _state(has_leadership_office=False)
    no_office_governance = GovernanceComponent.model_validate(
        no_office_state.entities[no_office_ids["organization"]].components["governance"],
    )
    with pytest.raises(LeadershipBallotError, match="no registered leadership office"):
        vacant_leadership_office_holder(no_office_governance)

    conflict_state, conflict_ids = _state(leadership_office_holder="nominee")
    organization = conflict_state.entities[conflict_ids["organization"]]
    conflict_governance = GovernanceComponent.model_validate(organization.components["governance"]).model_copy(update={
        "offices": [
            *GovernanceComponent.model_validate(organization.components["governance"]).offices,
            OfficeRecord(
                office_id="office.other_leader",
                title="Other Leader",
                holder_id=conflict_ids["other"],
                authority=["organization.lead"],
            ),
        ],
    })
    with pytest.raises(LeadershipBallotError, match="leadership office is occupied"):
        vacant_leadership_office_holder(conflict_governance)

