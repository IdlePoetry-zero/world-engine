from __future__ import annotations

from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from world_engine.domain.autonomous_vacancy import (
    AUTONOMOUS_VACANCY_BALLOT_AUTHORITY,
    vacancy_opportunity,
)
from world_engine.domain.autonomous_vacancy_events import (
    LEADERSHIP_VACANCY_REVIEW_RECORDED_EVENT_TYPE,
    LeadershipVacancyReviewRecordedEvent,
    leadership_vacancy_nomination_request_id,
    leadership_vacancy_review_ballot_id,
)
from world_engine.domain.components import GovernanceComponent, OfficeRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import event_payload, parse_event
from world_engine.domain.leadership_ballot_events import (
    LeadershipBallotChangedEvent,
    leadership_ballot_event_id,
)
from world_engine.domain.leadership_ballots import open_vacant_leadership_ballot
from world_engine.domain.leadership_ballots import LeadershipBallotVote
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, state_hash


COMMAND_ID = UUID("33000000-0000-4000-8000-000000000003")
WORLD_ID = UUID("11000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("22000000-0000-4000-8000-000000000002")
RULESET = "autonomous-vacancy-test.v1"


def _state(*, policy: str = "election", authorized: bool = True) -> tuple[WorldState, dict[str, UUID]]:
    ids = {name: uuid4() for name in ("org", "voter_a", "voter_b", "outsider")}
    offices = [OfficeRecord(office_id="office.leader", title="Leader")]
    if policy == "council":
        offices.append(OfficeRecord(
            office_id="office.council.a", title="Councilor A", holder_id=ids["voter_a"],
            authority=["authority.succession_vote"],
        ))
    governance = GovernanceComponent(
        revision=3,
        members=[ids["voter_a"], ids["voter_b"]],
        offices=offices,
        succession_policy=policy,
        authority_tags=[AUTONOMOUS_VACANCY_BALLOT_AUTHORITY] if authorized else [],
    )
    entities = {
        ids[name]: WorldEntity(id=ids[name], kind="character", name=name)
        for name in ("voter_a", "voter_b", "outsider")
    }
    entities[ids["org"]] = WorldEntity(
        id=ids["org"], kind="organization", name="House",
        components={"governance": governance.model_dump(mode="json")},
    )
    year_ticks = 360 * 86_400
    return WorldState(current_tick=year_ticks + 123, entities=entities), ids


def _event(state: WorldState, organization_id: UUID, *, ballot_id: UUID | None = None, **overrides):
    opportunity = vacancy_opportunity(state, organization_id)
    assert opportunity is not None
    request_id = leadership_vacancy_nomination_request_id(
        COMMAND_ID, RULESET, organization_id, opportunity.period_index,
    )
    values = {
        "event_id": uuid5(request_id, "review"),
        "organization_id": organization_id,
        "period_index": opportunity.period_index,
        "expected_governance_revision": opportunity.governance_revision,
        "proposer_id": opportunity.proposer_id,
        "decision_request_id": request_id,
        "ballot_id": ballot_id,
    }
    return LeadershipVacancyReviewRecordedEvent(**(values | overrides))


def _open_ballot_event(state: WorldState, ids: dict[str, UUID], request_id: UUID):
    opportunity = vacancy_opportunity(state, ids["org"])
    assert opportunity is not None
    ballot_id = leadership_vacancy_review_ballot_id(request_id)
    ballot = open_vacant_leadership_ballot(
        state,
        ballot_id=ballot_id,
        organization_id=ids["org"],
        nominee_id=opportunity.candidate_ids[-1],
        actor_id=opportunity.proposer_id,
        existing_ballots=state.leadership_ballots,
    )
    return LeadershipBallotChangedEvent(
        event_id=leadership_ballot_event_id(COMMAND_ID, RULESET, "open_vacant", ballot_id),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=opportunity.proposer_id,
        ruleset_version=RULESET,
        expected_revision=0,
        operation="open_vacant",
        resulting_ballot=ballot,
        autonomous_review_request_id=request_id,
    )


def test_event_contract_is_request_bound_author_only_and_registered():
    state, ids = _state()
    event = _event(state, ids["org"])

    assert event.type == LEADERSHIP_VACANCY_REVIEW_RECORDED_EVENT_TYPE
    assert event.event_id == uuid5(event.decision_request_id, "review")
    assert event.visibility == {"policy": "author_only"}
    assert parse_event(event.model_dump(mode="json")) == event
    assert "event_id" not in event_payload(event)

    with pytest.raises(ValidationError, match="bound to its decision request"):
        LeadershipVacancyReviewRecordedEvent.model_validate({
            **event.model_dump(mode="python"), "event_id": uuid4(),
        })
    with pytest.raises(ValidationError, match="author_only"):
        LeadershipVacancyReviewRecordedEvent.model_validate({
            **event.model_dump(mode="python"), "visibility": {"policy": "public"},
        })


def test_request_ids_are_deterministic_and_reject_unbounded_inputs():
    organization_id = uuid4()
    first = leadership_vacancy_nomination_request_id(COMMAND_ID, RULESET, organization_id, 5)
    assert first == leadership_vacancy_nomination_request_id(COMMAND_ID, RULESET, organization_id, 5)
    assert first != leadership_vacancy_nomination_request_id(COMMAND_ID, "other.v1", organization_id, 5)
    assert first != leadership_vacancy_nomination_request_id(COMMAND_ID, RULESET, organization_id, 6)
    assert leadership_vacancy_review_ballot_id(first) == uuid5(first, "ballot")
    with pytest.raises(ValueError, match="positive bounded"):
        leadership_vacancy_nomination_request_id(COMMAND_ID, RULESET, organization_id, True)


def test_defer_records_an_independently_eligible_opportunity_and_preserves_legacy_empty_hash():
    state, ids = _state()
    opportunity = vacancy_opportunity(state, ids["org"])
    assert opportunity is not None
    event = _event(state, ids["org"])

    assert "leadership_vacancy_reviews" not in state.model_dump(mode="json")
    updated = apply_event(state, event)

    assert updated.leadership_vacancy_reviews[ids["org"]].period_index == opportunity.period_index
    assert updated.leadership_vacancy_reviews[ids["org"]].reviewed_tick == state.current_tick
    assert updated.leadership_vacancy_reviews[ids["org"]].ballot_id is None
    assert not updated.leadership_ballots
    assert updated.model_copy(update={"leadership_vacancy_reviews": {}}) == state
    with pytest.raises(InvalidEvent, match="at most one monotonic"):
        apply_event(updated, event)


def test_council_review_uses_only_its_registered_succession_voters():
    state, ids = _state(policy="council")
    opportunity = vacancy_opportunity(state, ids["org"])
    assert opportunity is not None
    assert opportunity.eligible_voter_ids == (ids["voter_a"],)

    event = _event(state, ids["org"])
    updated = apply_event(state, event)

    assert updated.leadership_vacancy_reviews[ids["org"]].proposer_id == ids["voter_a"]


@pytest.mark.parametrize("field", ["proposer_id", "expected_governance_revision", "period_index"])
def test_defer_must_match_the_exact_current_opportunity(field):
    state, ids = _state()
    event = _event(state, ids["org"])
    wrong = {
        "proposer_id": ids["outsider"],
        "expected_governance_revision": event.expected_governance_revision + 1,
        "period_index": event.period_index + 1,
    }[field]
    tampered = LeadershipVacancyReviewRecordedEvent.model_validate({
        **event.model_dump(mode="python"), field: wrong,
    })

    error = "current positive calendar year" if field == "period_index" else "independently eligible opportunity"
    with pytest.raises(InvalidEvent, match=error):
        apply_event(state, tampered)


def test_defer_requires_authority_no_player_control_and_a_positive_current_period():
    unauthorized, ids = _state(authorized=False)
    unauthorized_request = uuid4()
    unauthorized_event = LeadershipVacancyReviewRecordedEvent(
        event_id=uuid5(unauthorized_request, "review"),
        organization_id=ids["org"], period_index=1, expected_governance_revision=3,
        proposer_id=ids["voter_a"], decision_request_id=unauthorized_request, ballot_id=None,
    )
    with pytest.raises(InvalidEvent, match="independently eligible opportunity"):
        apply_event(unauthorized, unauthorized_event)

    authorized, controlled_ids = _state()
    controlled = authorized.model_copy(update={"player_id": controlled_ids["voter_a"]})
    controlled_request = uuid4()
    controlled_event = LeadershipVacancyReviewRecordedEvent(
        event_id=uuid5(controlled_request, "review"), organization_id=controlled_ids["org"], period_index=1,
        expected_governance_revision=3, proposer_id=controlled_ids["voter_b"],
        decision_request_id=controlled_request, ballot_id=None,
    )
    with pytest.raises(InvalidEvent, match="independently eligible opportunity"):
        apply_event(controlled, controlled_event)

    before_first_year = unauthorized.model_copy(update={"current_tick": 0})
    request_id = uuid4()
    before_event = LeadershipVacancyReviewRecordedEvent(
        event_id=uuid5(request_id, "review"), organization_id=ids["org"], period_index=1,
        expected_governance_revision=3, proposer_id=ids["voter_a"],
        decision_request_id=request_id, ballot_id=None,
    )
    with pytest.raises(InvalidEvent, match="current positive calendar year"):
        apply_event(before_first_year, before_event)


def test_open_record_must_match_the_same_tick_request_bound_vacancy_ballot():
    state, ids = _state()
    opportunity = vacancy_opportunity(state, ids["org"])
    assert opportunity is not None
    request_id = leadership_vacancy_nomination_request_id(
        COMMAND_ID, RULESET, ids["org"], opportunity.period_index,
    )
    ballot_event = _open_ballot_event(state, ids, request_id)
    assert ballot_event.autonomous_review_request_id == request_id
    with_ballot = WorldState.model_validate({
        **state.model_dump(mode="json"),
        "leadership_ballots": [ballot_event.resulting_ballot.model_dump(mode="json")],
    })
    review = _event(
        state,
        ids["org"],
        ballot_id=leadership_vacancy_review_ballot_id(request_id),
        decision_request_id=request_id,
    )

    updated = apply_event(with_ballot, review)
    assert updated.leadership_vacancy_reviews[ids["org"]].ballot_id == ballot_event.resulting_ballot.ballot_id
    assert updated.leadership_ballots == with_ballot.leadership_ballots

    without_ballot = _event(
        state, ids["org"], ballot_id=leadership_vacancy_review_ballot_id(request_id),
        decision_request_id=request_id,
    )
    with pytest.raises(InvalidEvent, match="same-tick, newly opened ballot"):
        apply_event(state, without_ballot)

    wrong_ballot_link = LeadershipVacancyReviewRecordedEvent.model_validate({
        **review.model_dump(mode="python"), "ballot_id": uuid4(),
    })
    with pytest.raises(InvalidEvent, match="bound to its decision request"):
        apply_event(with_ballot, wrong_ballot_link)


def test_review_rejects_a_ballot_that_already_has_a_vote():
    state, ids = _state()
    opportunity = vacancy_opportunity(state, ids["org"])
    assert opportunity is not None
    request_id = leadership_vacancy_nomination_request_id(
        COMMAND_ID, RULESET, ids["org"], opportunity.period_index,
    )
    ballot_event = _open_ballot_event(state, ids, request_id)
    voted_ballot = ballot_event.resulting_ballot.model_copy(update={
        "votes": (LeadershipBallotVote(voter_id=opportunity.proposer_id, choice="yes"),),
        "revision": 2,
    })
    with_vote = WorldState.model_validate({
        **state.model_dump(mode="json"),
        "leadership_ballots": [voted_ballot.model_dump(mode="json")],
    })
    review = _event(
        state, ids["org"], ballot_id=leadership_vacancy_review_ballot_id(request_id),
        decision_request_id=request_id,
    )

    with pytest.raises(InvalidEvent, match="newly opened ballot with no votes"):
        apply_event(with_vote, review)


def test_review_is_monotonic_across_years_and_state_rejects_corrupt_receipts():
    state, ids = _state()
    first_event = _event(state, ids["org"])
    first_state = apply_event(state, first_event)
    year_ticks = first_state.calendar.year_days * first_state.calendar.day_seconds
    next_state = first_state.model_copy(update={"current_tick": year_ticks * 2 + 17})
    next_opportunity = vacancy_opportunity(next_state, ids["org"])
    assert next_opportunity is not None
    next_request = leadership_vacancy_nomination_request_id(
        COMMAND_ID, RULESET, ids["org"], next_opportunity.period_index,
    )
    next_event = LeadershipVacancyReviewRecordedEvent(
        event_id=uuid5(next_request, "review"), organization_id=ids["org"],
        period_index=next_opportunity.period_index,
        expected_governance_revision=next_opportunity.governance_revision,
        proposer_id=next_opportunity.proposer_id, decision_request_id=next_request, ballot_id=None,
    )
    updated = apply_event(next_state, next_event)
    assert updated.leadership_vacancy_reviews[ids["org"]].period_index == 2

    with pytest.raises(ValidationError, match="matching organization keys"):
        WorldState.model_validate({
            **updated.model_dump(mode="python"),
            "leadership_vacancy_reviews": {uuid4(): updated.leadership_vacancy_reviews[ids["org"]]},
        })

    # Empty ledger state preserves the canonical bytes of pre-feature worlds.
    legacy = state.model_copy(update={"leadership_vacancy_reviews": {}})
    assert state_hash(legacy) == state_hash(state)
