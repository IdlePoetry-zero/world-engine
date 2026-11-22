from __future__ import annotations

import hashlib
import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from world_engine.domain.components import LifeComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, canonical_state_payload, state_hash
from world_engine.domain.relationship_partnership_events import (
    RelationshipPartnershipChangedEvent,
    partnership_event_id,
    partnership_visibility,
)
from world_engine.domain.relationship_partnerships import (
    RelationshipPartnership,
    eligible_partnership_proposal_ids,
    plan_relationship_partnership,
)


PLACE_A = UUID("f1000000-0000-4000-8000-000000000001")
PLACE_B = UUID("f1000000-0000-4000-8000-000000000002")
ALICE = UUID("f2000000-0000-4000-8000-000000000001")
BOB = UUID("f2000000-0000-4000-8000-000000000002")
CASEY = UUID("f2000000-0000-4000-8000-000000000003")
OTHER = UUID("f2000000-0000-4000-8000-000000000004")
PARTNERSHIP = UUID("f3000000-0000-4000-8000-000000000001")
SECOND_PARTNERSHIP = UUID("f3000000-0000-4000-8000-000000000002")
YEAR_TICKS = 86_400 * 360


def _character(
    actor_id: UUID,
    name: str,
    *,
    tick: int,
    age_years: int = 20,
    life_stage: str = "adult",
    location_id: UUID = PLACE_A,
    status: str = "active",
) -> WorldEntity:
    return WorldEntity(
        id=actor_id,
        kind="character",
        name=name,
        location_id=location_id,
        status=status,
        components={
            "life": LifeComponent(
                birth_tick=tick - age_years * YEAR_TICKS,
                life_stage=life_stage,
            ).model_dump(mode="json"),
        },
    )


def _state(
    *,
    tick: int = 100,
    alice=None,
    bob=None,
    casey=None,
    partnerships: list[RelationshipPartnership] | None = None,
) -> WorldState:
    return WorldState(
        current_tick=tick,
        entities={
            PLACE_A: WorldEntity(id=PLACE_A, kind="place", name="Garden"),
            PLACE_B: WorldEntity(id=PLACE_B, kind="place", name="Library"),
            ALICE: alice or _character(ALICE, "Alice", tick=tick),
            BOB: bob or _character(BOB, "Bob", tick=tick),
            CASEY: casey or _character(CASEY, "Casey", tick=tick),
            OTHER: _character(OTHER, "Other", tick=tick),
        },
        calendar={"day_seconds": 86_400, "year_days": 360},
        relationship_partnerships=[] if partnerships is None else partnerships,
    )


def _propose(
    state: WorldState,
    *,
    partnership_id: UUID = PARTNERSHIP,
    proposer_id: UUID = ALICE,
    partner_id: UUID = BOB,
    expected_revision: int = 0,
    proposal_terms=(),
) -> RelationshipPartnership:
    return plan_relationship_partnership(
        state,
        operation="propose",
        partnership_id=partnership_id,
        actor_id=proposer_id,
        partner_id=partner_id,
        expected_revision=expected_revision,
        proposal_terms=proposal_terms,
    )


def _transition(
    state: WorldState,
    partnership: RelationshipPartnership,
    operation: str,
    actor_id: UUID,
    *,
    expected_revision: int | None = None,
    proposal_terms=(),
) -> RelationshipPartnership:
    return plan_relationship_partnership(
        state,
        operation=operation,
        partnership_id=partnership.partnership_id,
        actor_id=actor_id,
        expected_revision=partnership.revision if expected_revision is None else expected_revision,
        proposal_terms=proposal_terms,
    )


def _apply_partnership_change(
    state: WorldState,
    result: RelationshipPartnership,
    *,
    operation: str,
    actor_id: UUID,
    expected_revision: int,
    command_id: UUID,
    requested_by: UUID | None = None,
) -> WorldState:
    event = RelationshipPartnershipChangedEvent(
        event_id=partnership_event_id(command_id, "partnership-test.v1", operation, result.partnership_id),
        world_id=UUID("10000000-0000-4000-8000-000000000001"),
        branch_id=UUID("20000000-0000-4000-8000-000000000002"),
        command_id=command_id,
        actor_id=actor_id,
        requested_by=requested_by,
        ruleset_version="partnership-test.v1",
        operation=operation,
        expected_revision=expected_revision,
        resulting_partnership=result,
        visibility=partnership_visibility(result.proposer_id, result.partner_id),
    )
    return apply_event(state, event)


def test_propose_creates_a_frozen_pending_record_without_mutating_state():
    state = _state()
    before = state.model_dump(mode="python")

    proposal = _propose(state)

    assert proposal == RelationshipPartnership(
        partnership_id=PARTNERSHIP,
        proposer_id=ALICE,
        partner_id=BOB,
        status="pending",
        proposed_tick=state.current_tick,
        updated_tick=state.current_tick,
        revision=1,
    )
    assert state.model_dump(mode="python") == before
    with pytest.raises(ValidationError):
        proposal.status = "active"
    with pytest.raises(ValidationError):
        RelationshipPartnership.model_validate({**proposal.model_dump(mode="python"), "extra": "forbidden"})
    with pytest.raises(InvalidEvent, match="expected_revision zero"):
        _propose(state, expected_revision=1)


def test_accept_requires_the_proposed_partner_and_exact_revision():
    proposal = _propose(_state())
    state = _state(tick=120, partnerships=[proposal])

    with pytest.raises(InvalidEvent, match="Only the current proposal recipient"):
        _transition(state, proposal, "accept", ALICE)
    with pytest.raises(InvalidEvent, match="expected_revision"):
        _transition(state, proposal, "accept", BOB, expected_revision=0)

    active = _transition(state, proposal, "accept", BOB)

    assert active.status == "active"
    assert active.revision == 2
    assert active.updated_tick == state.current_tick
    assert state.relationship_partnerships == [proposal]


def test_accept_requires_both_active_adults_at_the_same_place():
    proposal = _propose(_state())
    separated = _state(
        tick=120,
        alice=_character(ALICE, "Alice", tick=120, location_id=PLACE_B),
        partnerships=[proposal],
    )
    with pytest.raises(InvalidEvent, match="co-located"):
        _transition(separated, proposal, "accept", BOB)

    underage = _state(
        tick=120,
        alice=_character(ALICE, "Alice", tick=120, age_years=17),
        partnerships=[proposal],
    )
    with pytest.raises(InvalidEvent, match="at least 18 world years old"):
        _transition(underage, proposal, "accept", BOB)


@pytest.mark.parametrize(
    ("operation", "actor_id", "status", "revision"),
    [
        ("decline", BOB, "declined", 2),
        ("withdraw", ALICE, "withdrawn", 2),
    ],
)
def test_decline_and_withdraw_are_permitted_at_a_distance(operation, actor_id, status, revision):
    proposal = _propose(_state())
    remote = _state(
        tick=120,
        alice=_character(ALICE, "Alice", tick=120, location_id=PLACE_B),
        bob=_character(BOB, "Bob", tick=120),
        partnerships=[proposal],
    )

    result = _transition(remote, proposal, operation, actor_id)

    assert result.status == status
    assert result.revision == revision


def test_either_active_partner_can_end_long_distance_partnership():
    proposal = _propose(_state())
    active = _transition(_state(partnerships=[proposal]), proposal, "accept", BOB)
    remote = _state(
        tick=150,
        alice=_character(ALICE, "Alice", tick=150, location_id=PLACE_B),
        partnerships=[active],
    )

    ended = _transition(remote, active, "end", ALICE)

    assert ended.status == "ended"
    assert ended.revision == 3
    assert ended.updated_tick == remote.current_tick


def test_remote_decline_withdraw_and_end_still_require_an_active_authorized_actor():
    proposal = _propose(_state())
    remote = _state(
        tick=120,
        bob=_character(BOB, "Bob", tick=120, location_id=PLACE_B, status="dead"),
        partnerships=[proposal],
    )
    with pytest.raises(InvalidEvent, match="active character"):
        _transition(remote, proposal, "decline", BOB)
    with pytest.raises(InvalidEvent, match="Only the current proposal author"):
        _transition(_state(partnerships=[proposal]), proposal, "withdraw", BOB)
    with pytest.raises(InvalidEvent, match="Only a partnership participant"):
        _transition(_state(partnerships=[proposal]), proposal, "decline", CASEY)


def test_one_open_record_per_unordered_pair_but_parallel_partners_are_allowed():
    proposal = _propose(_state())
    state_with_proposal = _state(partnerships=[proposal])
    with pytest.raises(InvalidEvent, match="pending or active partnership"):
        _propose(state_with_proposal, partnership_id=SECOND_PARTNERSHIP, proposer_id=BOB, partner_id=ALICE)

    active = _transition(state_with_proposal, proposal, "accept", BOB)
    parallel = _propose(
        _state(partnerships=[active]),
        partnership_id=SECOND_PARTNERSHIP,
        partner_id=CASEY,
    )
    assert parallel.status == "pending"
    assert parallel.proposer_id == ALICE
    assert parallel.partner_id == CASEY


def test_authorized_proposal_picker_respects_visibility_age_and_existing_pairs():
    proposal = _propose(_state())
    child = _character(CASEY, "Casey", tick=100, age_years=17, life_stage="adult")
    state = _state(casey=child, partnerships=[proposal])
    assert eligible_partnership_proposal_ids(state, ALICE, (ALICE, BOB, CASEY, OTHER)) == [OTHER]
    assert eligible_partnership_proposal_ids(state, ALICE, (ALICE, BOB, CASEY)) == []


@pytest.mark.parametrize("historical_status", ["declined", "withdrawn", "ended"])
def test_closed_historical_records_do_not_block_a_new_proposal(historical_status):
    if historical_status == "declined":
        historical = RelationshipPartnership(
            partnership_id=PARTNERSHIP,
            proposer_id=ALICE,
            partner_id=BOB,
            status="declined",
            proposed_tick=50,
            updated_tick=60,
            revision=2,
        )
    elif historical_status == "withdrawn":
        historical = RelationshipPartnership(
            partnership_id=PARTNERSHIP,
            proposer_id=ALICE,
            partner_id=BOB,
            status="withdrawn",
            proposed_tick=50,
            updated_tick=60,
            revision=2,
        )
    else:
        historical = RelationshipPartnership(
            partnership_id=PARTNERSHIP,
            proposer_id=ALICE,
            partner_id=BOB,
            status="ended",
            proposed_tick=50,
            updated_tick=90,
            revision=3,
        )
    state = _state(partnerships=[historical])

    new_proposal = _propose(state, partnership_id=SECOND_PARTNERSHIP)

    assert new_proposal.status == "pending"


def test_proposal_requires_a_new_id_two_distinct_active_adults_and_shared_location():
    proposal = _propose(_state())
    state_with_proposal = _state(partnerships=[proposal])
    with pytest.raises(InvalidEvent, match="ID already exists"):
        _propose(state_with_proposal, partnership_id=PARTNERSHIP)

    with pytest.raises(InvalidEvent, match="distinct participants"):
        _propose(_state(), partnership_id=SECOND_PARTNERSHIP, partner_id=ALICE)

    separated = _state(bob=_character(BOB, "Bob", tick=100, location_id=PLACE_B))
    with pytest.raises(InvalidEvent, match="co-located"):
        _propose(separated, partnership_id=SECOND_PARTNERSHIP)

    deceased = _state(bob=_character(BOB, "Bob", tick=100, status="dead"))
    with pytest.raises(InvalidEvent, match="active characters"):
        _propose(deceased, partnership_id=SECOND_PARTNERSHIP)

    minor = _state(bob=_character(BOB, "Bob", tick=100, age_years=17))
    with pytest.raises(InvalidEvent, match="at least 18 world years old"):
        _propose(minor, partnership_id=SECOND_PARTNERSHIP)


def test_invalid_operations_irrelevant_ids_stale_records_and_closed_transitions_are_rejected():
    proposal = _propose(_state())
    state = _state(partnerships=[proposal])

    with pytest.raises(InvalidEvent, match="operation is invalid"):
        plan_relationship_partnership(
            state,
            operation="renew",
            partnership_id=PARTNERSHIP,
            actor_id=ALICE,
        )
    with pytest.raises(InvalidEvent, match="only supplied when proposing"):
        plan_relationship_partnership(
            state,
            operation="accept",
            partnership_id=PARTNERSHIP,
            actor_id=BOB,
            partner_id=ALICE,
            expected_revision=1,
        )

    active = _transition(state, proposal, "accept", BOB)
    active_state = _state(partnerships=[active])
    with pytest.raises(InvalidEvent, match="Only a pending partnership"):
        _transition(active_state, active, "decline", BOB)
    ended = _transition(active_state, active, "end", ALICE)
    ended_state = _state(partnerships=[ended])
    with pytest.raises(InvalidEvent, match="Only an active partnership"):
        _transition(ended_state, ended, "end", BOB)


def test_legacy_author_is_original_proposer_and_remains_omitted_from_serialization():
    proposal = _propose(_state(), proposal_terms=("I will tell you before major travel.",))
    serialized = proposal.model_dump(mode="json")

    assert proposal.proposal_author_id is None
    assert proposal.current_proposal_author_id == ALICE
    assert proposal.current_proposal_recipient_id == BOB
    assert "proposal_author_id" not in serialized
    restored = RelationshipPartnership.model_validate(serialized)
    assert restored == proposal
    assert restored.current_proposal_author_id == ALICE

    state = _state(partnerships=[proposal])
    legacy_payload = state.model_dump(mode="json")
    serialized_partnership = legacy_payload["relationship_partnerships"][0]
    assert "proposal_author_id" not in serialized_partnership
    reopened = WorldState.model_validate(legacy_payload)
    canonical = canonical_state_payload(state)
    legacy_digest = hashlib.sha256(
        json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert state_hash(state) == legacy_digest
    assert state_hash(reopened) == legacy_digest


def test_revise_and_counter_replace_full_terms_and_swap_authority_without_replacing_participants():
    proposal = _propose(_state(), proposal_terms=("initial term", "second initial term"))
    proposal_state = _state(tick=120, partnerships=[proposal])

    revised = _transition(
        proposal_state,
        proposal,
        "revise",
        ALICE,
        proposal_terms=("complete replacement terms",),
    )
    assert revised.proposer_id == ALICE
    assert revised.partner_id == BOB
    assert revised.proposal_terms == ("complete replacement terms",)
    assert revised.proposal_author_id == ALICE
    assert revised.current_proposal_author_id == ALICE
    assert revised.current_proposal_recipient_id == BOB
    assert revised.proposed_tick == proposal.proposed_tick
    assert revised.updated_tick == proposal_state.current_tick
    assert revised.revision == 2

    revised_state = _state(tick=140, partnerships=[revised])
    counter = _transition(
        revised_state,
        revised,
        "counter",
        BOB,
        proposal_terms=("counterparty's full replacement", "new second term"),
    )
    assert counter.proposer_id == ALICE
    assert counter.partner_id == BOB
    assert counter.proposal_terms == ("counterparty's full replacement", "new second term")
    assert counter.proposal_author_id == BOB
    assert counter.current_proposal_author_id == BOB
    assert counter.current_proposal_recipient_id == ALICE
    assert counter.proposed_tick == proposal.proposed_tick
    assert counter.revision == 3

    counter_state = _state(tick=140, partnerships=[counter])
    with pytest.raises(InvalidEvent, match="current proposal recipient"):
        _transition(counter_state, counter, "accept", BOB)
    accepted = _transition(counter_state, counter, "accept", ALICE)
    assert accepted.status == "active"
    assert accepted.proposal_author_id == BOB
    assert accepted.proposal_terms == counter.proposal_terms
    assert accepted.current_proposal_author_id == BOB


@pytest.mark.parametrize("operation", ["revise", "counter"])
def test_pending_terms_updates_require_current_authority_nonempty_changed_terms_and_exact_revision(operation):
    proposal = _propose(_state(), proposal_terms=("existing term",))
    state = _state(partnerships=[proposal])
    actor_id = ALICE if operation == "revise" else BOB

    with pytest.raises(InvalidEvent, match="change the current terms"):
        _transition(state, proposal, operation, actor_id, proposal_terms=("existing term",))
    with pytest.raises(InvalidEvent, match="full nonempty terms"):
        _transition(state, proposal, operation, actor_id, proposal_terms=())
    with pytest.raises(InvalidEvent, match="current proposal author" if operation == "revise" else "current proposal recipient"):
        _transition(state, proposal, operation, BOB if operation == "revise" else ALICE,
                     proposal_terms=("changed term",))
    with pytest.raises(InvalidEvent, match="expected_revision"):
        plan_relationship_partnership(
            state,
            operation=operation,
            partnership_id=proposal.partnership_id,
            actor_id=actor_id,
            expected_revision=0,
            proposal_terms=("changed term",),
        )


def test_pending_revisions_require_both_active_adults_and_withdrawal_follows_current_author():
    proposal = _propose(_state(), proposal_terms=("first",))
    separated = _state(
        bob=_character(BOB, "Bob", tick=100, location_id=PLACE_B),
        partnerships=[proposal],
    )
    with pytest.raises(InvalidEvent, match="co-located"):
        _transition(separated, proposal, "counter", BOB, proposal_terms=("counter",))
    minor = _state(
        alice=_character(ALICE, "Alice", tick=100, age_years=17),
        partnerships=[proposal],
    )
    with pytest.raises(InvalidEvent, match="at least 18 world years old"):
        _transition(minor, proposal, "revise", ALICE, proposal_terms=("revised",))

    counter = _transition(_state(partnerships=[proposal]), proposal, "counter", BOB,
                          proposal_terms=("counterproposal",))
    counter_state = _state(partnerships=[counter])
    with pytest.raises(InvalidEvent, match="current proposal author"):
        _transition(counter_state, counter, "withdraw", ALICE)
    withdrawn = _transition(counter_state, counter, "withdraw", BOB)
    assert withdrawn.status == "withdrawn"
    assert withdrawn.proposal_author_id == BOB
    assert withdrawn.proposal_terms == ("counterproposal",)


def test_reducer_replays_revise_counter_and_hosted_accept_against_prior_state():
    proposal = _propose(_state(), proposal_terms=("initial",))
    proposal_state = _state(partnerships=[proposal])
    revised = _transition(proposal_state, proposal, "revise", ALICE, proposal_terms=("revised",))
    revised_state = _apply_partnership_change(
        proposal_state,
        revised,
        operation="revise",
        actor_id=ALICE,
        expected_revision=1,
        command_id=UUID("f4000000-0000-4000-8000-000000000001"),
    )
    assert revised_state.relationship_partnerships == [revised]

    counter = _transition(revised_state, revised, "counter", BOB, proposal_terms=("countered",))
    counter_state = _apply_partnership_change(
        revised_state,
        counter,
        operation="counter",
        actor_id=BOB,
        expected_revision=2,
        command_id=UUID("f4000000-0000-4000-8000-000000000002"),
        requested_by=ALICE,
    )
    assert counter_state.relationship_partnerships == [counter]

    accepted = _transition(counter_state, counter, "accept", ALICE)
    accepted_state = _apply_partnership_change(
        counter_state,
        accepted,
        operation="accept",
        actor_id=ALICE,
        expected_revision=3,
        command_id=UUID("f4000000-0000-4000-8000-000000000003"),
        requested_by=BOB,
    )
    assert accepted_state.relationship_partnerships == [accepted]
    assert accepted.proposal_author_id == BOB
    assert accepted.proposal_terms == ("countered",)
