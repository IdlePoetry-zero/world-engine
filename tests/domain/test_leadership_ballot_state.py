from __future__ import annotations

from uuid import UUID, uuid4, uuid5

import pytest

from world_engine.domain.autonomous_vacancy import (
    AUTONOMOUS_VACANCY_BALLOT_AUTHORITY,
    vacancy_opportunity,
)
from world_engine.domain.autonomous_vacancy_events import (
    LeadershipVacancyReviewRecordedEvent,
    leadership_vacancy_nomination_request_id,
    leadership_vacancy_review_ballot_id,
)
from world_engine.domain.components import GovernanceComponent, OfficeRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.leadership_ballot_events import (
    LeadershipBallotChangedEvent,
    leadership_ballot_event_id,
)
from world_engine.domain.leadership_ballot_state import (
    AUTONOMOUS_VACANCY_COMMAND,
    autonomous_vacancy_command_payload,
    validate_leadership_ballot_batch,
)
from world_engine.domain.leadership_ballots import (
    cast_leadership_ballot_vote,
    open_vacant_leadership_ballot,
)
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event


WORLD_ID = UUID("11000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("22000000-0000-4000-8000-000000000002")
OPEN_COMMAND_ID = UUID("33000000-0000-4000-8000-000000000003")
VOTE_COMMAND_ID = UUID("44000000-0000-4000-8000-000000000004")
OWNER_ID = UUID("55000000-0000-4000-8000-000000000005")
RULESET = "autonomous-vacancy-batch-test.v1"


def _state() -> tuple[WorldState, dict[str, UUID]]:
    ids = {
        name: UUID(f"{number:08x}-0000-4000-8000-00000000000{number}")
        for name, number in (("org", 1), ("voter_a", 2), ("voter_b", 3), ("outsider", 4))
    }
    governance = GovernanceComponent(
        revision=3,
        members=[ids["voter_a"], ids["voter_b"]],
        offices=[OfficeRecord(office_id="office.leader", title="Leader")],
        succession_policy="election",
        authority_tags=[AUTONOMOUS_VACANCY_BALLOT_AUTHORITY],
    )
    entities = {
        actor_id: WorldEntity(id=actor_id, kind="character", name=name)
        for name, actor_id in ids.items() if name != "org"
    }
    entities[ids["org"]] = WorldEntity(
        id=ids["org"], kind="organization", name="House",
        components={"governance": governance.model_dump(mode="json")},
    )
    year_ticks = 360 * 86_400
    return WorldState(current_tick=year_ticks + 123, entities=entities), ids


def _opening_batch(
    state: WorldState, ids: dict[str, UUID], *, command_id: UUID = OPEN_COMMAND_ID,
) -> tuple[LeadershipBallotChangedEvent, LeadershipVacancyReviewRecordedEvent, UUID]:
    opportunity = vacancy_opportunity(state, ids["org"])
    assert opportunity is not None
    request_id = leadership_vacancy_nomination_request_id(
        command_id, RULESET, ids["org"], opportunity.period_index,
    )
    ballot_id = leadership_vacancy_review_ballot_id(request_id)
    ballot = open_vacant_leadership_ballot(
        state,
        ballot_id=ballot_id,
        organization_id=ids["org"],
        nominee_id=opportunity.candidate_ids[-1],
        actor_id=opportunity.proposer_id,
        existing_ballots=state.leadership_ballots,
    )
    ballot_event = LeadershipBallotChangedEvent(
        event_id=leadership_ballot_event_id(command_id, RULESET, "open_vacant", ballot_id),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        actor_id=opportunity.proposer_id,
        ruleset_version=RULESET,
        expected_revision=0,
        operation="open_vacant",
        resulting_ballot=ballot,
        autonomous_review_request_id=request_id,
    )
    review = LeadershipVacancyReviewRecordedEvent(
        event_id=uuid5(request_id, "review"),
        organization_id=ids["org"],
        period_index=opportunity.period_index,
        expected_governance_revision=opportunity.governance_revision,
        proposer_id=opportunity.proposer_id,
        decision_request_id=request_id,
        ballot_id=ballot_id,
    )
    return ballot_event, review, request_id


def _defer_event(state: WorldState, ids: dict[str, UUID]) -> LeadershipVacancyReviewRecordedEvent:
    opportunity = vacancy_opportunity(state, ids["org"])
    assert opportunity is not None
    request_id = leadership_vacancy_nomination_request_id(
        OPEN_COMMAND_ID, RULESET, ids["org"], opportunity.period_index,
    )
    return LeadershipVacancyReviewRecordedEvent(
        event_id=uuid5(request_id, "review"),
        organization_id=ids["org"],
        period_index=opportunity.period_index,
        expected_governance_revision=opportunity.governance_revision,
        proposer_id=opportunity.proposer_id,
        decision_request_id=request_id,
        ballot_id=None,
    )


def test_autonomous_open_batch_binds_payload_and_allows_owner_envelope_actor():
    state, ids = _state()
    ballot_event, review, request_id = _opening_batch(state, ids)
    events = (ballot_event, review)
    payload = {
        "kind": "autonomous_vacancy",
        "organization_id": str(ids["org"]),
        "ballot_id": str(leadership_vacancy_review_ballot_id(request_id)),
        "operation": "open_vacant",
        "npc_actor_id": str(ballot_event.actor_id),
    }

    assert AUTONOMOUS_VACANCY_COMMAND == "simulation.autonomous_vacancy"
    assert autonomous_vacancy_command_payload(events) == payload
    # The command actor is the world owner; the exact NPC is carried by the event.
    assert OWNER_ID != ballot_event.actor_id
    validate_leadership_ballot_batch(
        events,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=OPEN_COMMAND_ID,
        actor_id=OWNER_ID,
        ruleset_version=RULESET,
        command_type=AUTONOMOUS_VACANCY_COMMAND,
        command_payload=payload,
    )


def test_open_batch_rejects_wrong_payload_order_extra_events_and_forged_review():
    state, ids = _state()
    ballot_event, review, _request_id = _opening_batch(state, ids)
    events = (ballot_event, review)
    good_payload = autonomous_vacancy_command_payload(events)

    with pytest.raises(InvalidEvent, match="exact vacancy transition batch"):
        autonomous_vacancy_command_payload((review, ballot_event))
    with pytest.raises(InvalidEvent, match="exact vacancy transition batch"):
        autonomous_vacancy_command_payload((ballot_event, review, review))
    with pytest.raises(InvalidEvent, match="marked vacancy cast, close, or cancel"):
        autonomous_vacancy_command_payload((ballot_event,))

    bad_payload = {**good_payload, "npc_actor_id": str(OWNER_ID)}
    with pytest.raises(InvalidEvent, match="exact transition batch"):
        validate_leadership_ballot_batch(
            events,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=OPEN_COMMAND_ID,
            actor_id=OWNER_ID,
            ruleset_version=RULESET,
            command_type=AUTONOMOUS_VACANCY_COMMAND,
            command_payload=bad_payload,
        )

    for forged_review in (
        review.model_copy(update={"organization_id": ids["outsider"]}),
        review.model_copy(update={"proposer_id": ids["outsider"]}),
        review.model_copy(update={"expected_governance_revision": review.expected_governance_revision + 1}),
        review.model_copy(update={"decision_request_id": uuid4()}),
    ):
        with pytest.raises(InvalidEvent):
            autonomous_vacancy_command_payload((ballot_event, forged_review))

    wrong_branch = ballot_event.model_copy(update={"branch_id": ids["outsider"]})
    with pytest.raises(InvalidEvent, match="world command context"):
        validate_leadership_ballot_batch(
            (wrong_branch, review),
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=OPEN_COMMAND_ID,
            actor_id=OWNER_ID,
            ruleset_version=RULESET,
            command_type=AUTONOMOUS_VACANCY_COMMAND,
            command_payload=good_payload,
        )


def test_defer_payload_and_archive_replay_are_request_bound():
    state, ids = _state()
    review = _defer_event(state, ids)
    events = (review,)
    payload = {
        "kind": "autonomous_vacancy",
        "organization_id": str(ids["org"]),
        "ballot_id": None,
        "operation": "defer",
        "npc_actor_id": str(review.proposer_id),
    }

    assert autonomous_vacancy_command_payload(events) == payload
    validate_leadership_ballot_batch(
        events,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=OPEN_COMMAND_ID,
        actor_id=OWNER_ID,
        ruleset_version=RULESET,
        command_type=AUTONOMOUS_VACANCY_COMMAND,
        command_payload=payload,
    )
    validate_leadership_ballot_batch(
        events,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=OPEN_COMMAND_ID,
        actor_id=OWNER_ID,
        ruleset_version=RULESET,
    )

    with pytest.raises(InvalidEvent, match="include its exact ballot transition"):
        autonomous_vacancy_command_payload((review.model_copy(update={"ballot_id": uuid4()}),))
    with pytest.raises(InvalidEvent, match="command, ruleset, organization, and period"):
        validate_leadership_ballot_batch(
            events,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=uuid4(),
            actor_id=OWNER_ID,
            ruleset_version=RULESET,
        )


def test_marked_later_vacancy_transition_uses_new_command_and_durable_request_marker():
    state, ids = _state()
    opening, review, request_id = _opening_batch(state, ids)
    state = apply_event(state, opening)
    state = apply_event(state, review)
    prior = state.leadership_ballots[0]
    voter_id = next(voter for voter in prior.eligible_voter_ids if voter != opening.actor_id)
    resulting = cast_leadership_ballot_vote(
        state, prior, voter_id=voter_id, choice="abstain", expected_ballot_revision=prior.revision,
    )
    cast_event = LeadershipBallotChangedEvent(
        event_id=leadership_ballot_event_id(VOTE_COMMAND_ID, RULESET, "cast", prior.ballot_id),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=VOTE_COMMAND_ID,
        actor_id=voter_id,
        ruleset_version=RULESET,
        expected_revision=prior.revision,
        operation="cast",
        resulting_ballot=resulting,
        autonomous_review_request_id=request_id,
    )
    payload = {
        "kind": "autonomous_vacancy",
        "organization_id": str(ids["org"]),
        "ballot_id": str(prior.ballot_id),
        "operation": "cast",
        "npc_actor_id": str(voter_id),
    }

    validate_leadership_ballot_batch(
        (cast_event,),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=VOTE_COMMAND_ID,
        actor_id=OWNER_ID,
        ruleset_version=RULESET,
        command_type=AUTONOMOUS_VACANCY_COMMAND,
        command_payload=payload,
    )
    validate_leadership_ballot_batch(
        (cast_event,),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=VOTE_COMMAND_ID,
        actor_id=OWNER_ID,
        ruleset_version=RULESET,
    )
    updated = apply_event(state, cast_event)
    assert updated.leadership_ballots[0] == resulting

    with pytest.raises(InvalidEvent, match="stored review receipt"):
        apply_event(state.model_copy(update={"leadership_vacancy_reviews": {}}), cast_event)
