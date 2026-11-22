from decimal import Decimal
from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from world_engine.domain.leadership_ballots import LeadershipBallot, LeadershipBallotVote
from world_engine.domain.leadership_ballot_events import (
    LEADERSHIP_BALLOT_CHANGED_EVENT_TYPE,
    LeadershipBallotChangedEvent,
    leadership_ballot_event_id,
    parse_leadership_ballot_event,
)


WORLD_ID = UUID("11000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("22000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("33000000-0000-4000-8000-000000000003")
INCUMBENT_ID = UUID("44000000-0000-4000-8000-000000000004")
NOMINEE_ID = UUID("55000000-0000-4000-8000-000000000005")
VOTER_A_ID = UUID("66000000-0000-4000-8000-000000000006")
VOTER_B_ID = UUID("77000000-0000-4000-8000-000000000007")
BALLOT_ID = UUID("88000000-0000-4000-8000-000000000008")
RULESET = "leadership-ballot-test.v1"
ELIGIBLE = tuple(sorted((INCUMBENT_ID, NOMINEE_ID, VOTER_A_ID)))


def _ballot(*, status="open", revision=1, votes=(), threshold=Decimal("0.5")):
    return LeadershipBallot(
        ballot_id=BALLOT_ID,
        organization_id=UUID("99000000-0000-4000-8000-000000000009"),
        incumbent_id=INCUMBENT_ID,
        nominee_id=NOMINEE_ID,
        opened_tick=10,
        updated_tick=10 if revision == 1 else 11,
        governance_revision=6,
        policy="election",
        eligible_voter_ids=ELIGIBLE,
        threshold=threshold,
        votes=tuple(votes),
        revision=revision,
        status=status,
    )


def _event(operation, ballot, *, expected_revision, actor_id):
    return LeadershipBallotChangedEvent(
        event_id=leadership_ballot_event_id(COMMAND_ID, RULESET, operation, BALLOT_ID),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=actor_id,
        ruleset_version=RULESET,
        expected_revision=expected_revision,
        operation=operation,
        resulting_ballot=ballot,
    )


@pytest.mark.parametrize(
    ("operation", "ballot", "expected_revision", "actor_id"),
    [
        ("open", _ballot(), 0, INCUMBENT_ID),
        ("cast", _ballot(revision=2, votes=(LeadershipBallotVote(voter_id=VOTER_A_ID, choice="yes"),)), 1, VOTER_A_ID),
        ("close", _ballot(
            status="approved", revision=3, votes=(
                LeadershipBallotVote(voter_id=INCUMBENT_ID, choice="yes"),
                LeadershipBallotVote(voter_id=VOTER_A_ID, choice="yes"),
            ),
        ), 2, INCUMBENT_ID),
        ("close", _ballot(
            status="rejected", revision=4, votes=(
                LeadershipBallotVote(voter_id=INCUMBENT_ID, choice="yes"),
                LeadershipBallotVote(voter_id=NOMINEE_ID, choice="no"),
                LeadershipBallotVote(voter_id=VOTER_A_ID, choice="no"),
            ),
        ), 3, INCUMBENT_ID),
        ("cancel", _ballot(status="cancelled", revision=2), 1, uuid4()),
    ],
)
def test_event_binds_operation_revision_actor_and_author_only_visibility(
    operation, ballot, expected_revision, actor_id,
):
    event = _event(operation, ballot, expected_revision=expected_revision, actor_id=actor_id)

    assert event.type == LEADERSHIP_BALLOT_CHANGED_EVENT_TYPE
    assert event.event_id == uuid5(
        COMMAND_ID,
        f"{RULESET}:leadership_ballot.{operation}:{BALLOT_ID}",
    )
    assert event.expected_revision == expected_revision
    assert event.visibility == {"policy": "author_only"}
    assert parse_leadership_ballot_event(event.model_dump(mode="json")) == event


def test_event_ids_are_deterministic_and_bound_to_command_ruleset_operation_and_ballot():
    first = leadership_ballot_event_id(COMMAND_ID, RULESET, "cast", BALLOT_ID)
    assert first == leadership_ballot_event_id(COMMAND_ID, RULESET, "cast", BALLOT_ID)
    assert first != leadership_ballot_event_id(uuid4(), RULESET, "cast", BALLOT_ID)
    assert first != leadership_ballot_event_id(COMMAND_ID, "other.v1", "cast", BALLOT_ID)
    assert first != leadership_ballot_event_id(COMMAND_ID, RULESET, "close", BALLOT_ID)
    assert first != leadership_ballot_event_id(COMMAND_ID, RULESET, "cast", uuid4())


@pytest.mark.parametrize(
    ("operation", "ballot", "expected_revision", "actor_id", "error"),
    [
        ("open", _ballot(), 0, NOMINEE_ID, "only the incumbent may open"),
        ("open", _ballot(revision=2), 1, INCUMBENT_ID, "opening a leadership ballot"),
        ("cast", _ballot(revision=2), 1, VOTER_A_ID, "exactly one resulting vote"),
        ("cast", _ballot(revision=2, votes=(LeadershipBallotVote(voter_id=VOTER_A_ID, choice="yes"),)), 1, uuid4(), "eligible voter"),
        ("cast", _ballot(status="approved", revision=2, votes=(
            LeadershipBallotVote(voter_id=INCUMBENT_ID, choice="yes"),
            LeadershipBallotVote(voter_id=VOTER_A_ID, choice="yes"),
        )), 1, VOTER_A_ID, "only be cast on an open ballot"),
        ("close", _ballot(revision=3, votes=(LeadershipBallotVote(voter_id=VOTER_A_ID, choice="yes"),)).model_copy(update={"status": "approved"}), 2, INCUMBENT_ID, "terminal ballot status"),
        ("close", _ballot(status="approved", revision=3, votes=(
            LeadershipBallotVote(voter_id=INCUMBENT_ID, choice="yes"),
            LeadershipBallotVote(voter_id=VOTER_A_ID, choice="yes"),
        )), 2, NOMINEE_ID, "only the incumbent may close"),
        ("close", _ballot(revision=2).model_copy(update={"status": "rejected"}), 1, INCUMBENT_ID, "terminal ballot status"),
        ("cancel", _ballot(revision=2), 1, INCUMBENT_ID, "cancelled status"),
    ],
)
def test_event_rejects_invalid_actor_status_or_snapshot(
    operation, ballot, expected_revision, actor_id, error,
):
    with pytest.raises(ValidationError, match=error):
        _event(operation, ballot, expected_revision=expected_revision, actor_id=actor_id)


def test_event_rejects_wrong_identity_visibility_revision_and_extra_fields():
    valid = _event("open", _ballot(), expected_revision=0, actor_id=INCUMBENT_ID)
    values = valid.model_dump(mode="python")

    with pytest.raises(ValidationError, match="bound to its command"):
        LeadershipBallotChangedEvent.model_validate({**values, "event_id": uuid4()})
    with pytest.raises(ValidationError, match="author_only"):
        LeadershipBallotChangedEvent.model_validate({**values, "visibility": {"policy": "public"}})
    with pytest.raises(ValidationError):
        LeadershipBallotChangedEvent.model_validate({**values, "unexpected": True})
    with pytest.raises(ValidationError):
        LeadershipBallotChangedEvent.model_validate({**values, "expected_revision": -1})


def test_event_id_helper_rejects_invalid_scope():
    with pytest.raises(ValueError, match="UUID command and ballot"):
        leadership_ballot_event_id("bad", RULESET, "open", BALLOT_ID)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nonempty ruleset"):
        leadership_ballot_event_id(COMMAND_ID, " ", "open", BALLOT_ID)
    with pytest.raises(ValueError, match="unsupported"):
        leadership_ballot_event_id(COMMAND_ID, RULESET, "vote", BALLOT_ID)  # type: ignore[arg-type]
