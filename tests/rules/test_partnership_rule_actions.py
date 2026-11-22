from __future__ import annotations

from uuid import UUID, uuid5

import pytest

from world_engine.domain.components import LifeComponent
from world_engine.domain.models import TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.domain.relationship_partnership_events import (
    RelationshipPartnershipChangedEvent,
    partnership_event_id,
    partnership_visibility,
)
from world_engine.domain.relationship_partnerships import RelationshipPartnership
from world_engine.rules import RuleContext, RuleEngine, UpdateRelationshipPartnershipProposalIntent, parse_action


PLACE_ID = UUID("c1000000-0000-4000-8000-000000000001")
PROPOSER_ID = UUID("c2000000-0000-4000-8000-000000000001")
PARTNER_ID = UUID("c2000000-0000-4000-8000-000000000002")
OTHER_ID = UUID("c2000000-0000-4000-8000-000000000003")
PARTNERSHIP_ID = UUID("c3000000-0000-4000-8000-000000000001")
YEAR_TICKS = 86_400 * 360


def _state(*, partnerships: list[RelationshipPartnership] | None = None) -> WorldState:
    def character(entity_id: UUID, name: str) -> WorldEntity:
        return WorldEntity(
            id=entity_id,
            kind="character",
            name=name,
            location_id=PLACE_ID,
            components={
                "life": LifeComponent(birth_tick=-20 * YEAR_TICKS, life_stage="adult").model_dump(mode="json"),
            },
        )

    return WorldState(
        current_tick=100,
        calendar={"day_seconds": 86_400, "year_days": 360},
        entities={
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Garden"),
            PROPOSER_ID: character(PROPOSER_ID, "Ari"),
            PARTNER_ID: character(PARTNER_ID, "Bea"),
            OTHER_ID: character(OTHER_ID, "Casey"),
        },
        player_id=PROPOSER_ID,
        relationship_partnerships=partnerships or [],
    )


def _context(actor_id: UUID = PROPOSER_ID, command_id: UUID | None = None) -> RuleContext:
    return RuleContext(
        world_id=UUID("c4000000-0000-4000-8000-000000000001"),
        branch_id=UUID("c4000000-0000-4000-8000-000000000002"),
        command_id=command_id or UUID("c4000000-0000-4000-8000-000000000003"),
        actor_id=actor_id,
        ruleset_version="partnership-rules.v1",
    )


def _partnership(status: str = "pending", revision: int = 1) -> RelationshipPartnership:
    return RelationshipPartnership(
        partnership_id=PARTNERSHIP_ID,
        proposer_id=PROPOSER_ID,
        partner_id=PARTNER_ID,
        status=status,
        proposed_tick=10,
        updated_tick=10 if revision == 1 else 50,
        revision=revision,
    )


def test_partnership_action_contracts_are_closed_and_revisioned() -> None:
    proposal = parse_action({
        "kind": "propose_relationship_partnership",
        "partner_id": str(PARTNER_ID),
    })
    assert proposal.kind == "propose_relationship_partnership"
    assert proposal.partner_id == PARTNER_ID

    resolution = parse_action({
        "kind": "resolve_relationship_partnership",
        "partnership_id": str(PARTNERSHIP_ID),
        "operation": "accept",
        "expected_revision": 1,
    })
    assert resolution.kind == "resolve_relationship_partnership"
    assert resolution.partnership_id == PARTNERSHIP_ID
    assert resolution.expected_revision == 1

    request = parse_action({
        "kind": "request_relationship_partnership_response",
        "partnership_id": str(PARTNERSHIP_ID),
        "expected_revision": 1,
    })
    assert request.kind == "request_relationship_partnership_response"

    with pytest.raises(ValueError, match="registered schema"):
        parse_action({"kind": "propose_relationship_partnership", "partner_id": str(PARTNER_ID), "status": "active"})
    with pytest.raises(ValueError, match="registered schema"):
        parse_action({
            "kind": "resolve_relationship_partnership",
            "partnership_id": str(PARTNERSHIP_ID),
            "operation": "accept",
            "expected_revision": "1",
        })
    with pytest.raises(ValueError, match="registered schema"):
        parse_action({
            "kind": "request_relationship_partnership_response",
            "partnership_id": str(PARTNERSHIP_ID),
            "expected_revision": 1,
            "operation": "accept",
        })


def test_partnership_proposal_update_contract_requires_complete_nonempty_terms() -> None:
    action = parse_action({
        "kind": "update_relationship_partnership_proposal",
        "partnership_id": str(PARTNERSHIP_ID),
        "expected_revision": 1,
        "operation": "counter",
        "proposal_terms": ["A shared home", "No other commitments"],
    })

    assert isinstance(action, UpdateRelationshipPartnershipProposalIntent)
    assert action.proposal_terms == ("A shared home", "No other commitments")

    for malformed in (
        {
            "kind": "update_relationship_partnership_proposal",
            "partnership_id": str(PARTNERSHIP_ID),
            "expected_revision": 1,
            "operation": "revise",
        },
        {
            "kind": "update_relationship_partnership_proposal",
            "partnership_id": str(PARTNERSHIP_ID),
            "expected_revision": 1,
            "operation": "revise",
            "proposal_terms": [],
        },
        {
            "kind": "update_relationship_partnership_proposal",
            "partnership_id": str(PARTNERSHIP_ID),
            "expected_revision": 1,
            "operation": "accept",
            "proposal_terms": ["A term"],
        },
        {
            "kind": "update_relationship_partnership_proposal",
            "partnership_id": str(PARTNERSHIP_ID),
            "expected_revision": 1,
            "operation": "counter",
            "proposal_terms": ["A term"],
            "status": "active",
        },
        {
            "kind": "update_relationship_partnership_proposal",
            "partnership_id": str(PARTNERSHIP_ID),
            "expected_revision": 1,
            "operation": "counter",
            "proposal_terms": [f"Term {index}" for index in range(7)],
        },
        {
            "kind": "update_relationship_partnership_proposal",
            "partnership_id": str(PARTNERSHIP_ID),
            "expected_revision": 1,
            "operation": "counter",
            "proposal_terms": ["x" * 401],
        },
    ):
        with pytest.raises(ValueError, match="registered schema"):
            parse_action(malformed)


@pytest.mark.parametrize(
    ("operation", "actor_id"),
    [("revise", PROPOSER_ID), ("counter", PARTNER_ID)],
)
def test_partnership_proposal_update_uses_exact_native_transition_and_one_zero_time_event(
    operation: str,
    actor_id: UUID,
) -> None:
    state = _state(partnerships=[_partnership()])
    context = _context(actor_id, UUID(int=720 + (1 if operation == "revise" else 2)))
    terms = ["We will share the household", "Each person keeps their own work"]

    result = RuleEngine().evaluate(
        state,
        {
            "kind": "update_relationship_partnership_proposal",
            "partnership_id": str(PARTNERSHIP_ID),
            "expected_revision": 1,
            "operation": operation,
            "proposal_terms": terms,
        },
        context,
    )

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == f"relationship_partnership_{operation}"
    assert result.elapsed_ticks == 0
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, RelationshipPartnershipChangedEvent)
    assert event.operation == operation
    assert event.expected_revision == 1
    assert event.resulting_partnership.revision == 2
    assert event.resulting_partnership.status == "pending"
    assert event.resulting_partnership.proposal_terms == tuple(terms)
    assert (event.world_id, event.branch_id, event.command_id, event.actor_id) == (
        context.world_id, context.branch_id, context.command_id, actor_id,
    )
    after = apply_event(state, event)
    assert after.current_tick == state.current_tick
    assert after.relationship_partnerships[0] == event.resulting_partnership


def test_partnership_proposal_update_rejects_stale_revision_and_wrong_party() -> None:
    state = _state(partnerships=[_partnership()])
    for actor_id, revision, operation in (
        (PROPOSER_ID, 2, "revise"),
        (PARTNER_ID, 1, "revise"),
        (PROPOSER_ID, 1, "counter"),
    ):
        result = RuleEngine().evaluate(
            state,
            {
                "kind": "update_relationship_partnership_proposal",
                "partnership_id": str(PARTNERSHIP_ID),
                "expected_revision": revision,
                "operation": operation,
                "proposal_terms": ["A replacement term"],
            },
            _context(actor_id, UUID(int=740 + revision)),
        )
        assert result.outcome.status == "rejected"
        assert result.outcome.code == "relationship_partnership_unavailable"
        assert result.events == []
        assert result.elapsed_ticks == 0


def test_counterproposal_changes_who_may_revise_or_counter_next() -> None:
    state = _state(partnerships=[_partnership()])

    counter = RuleEngine().evaluate(
        state,
        {
            "kind": "update_relationship_partnership_proposal",
            "partnership_id": str(PARTNERSHIP_ID),
            "expected_revision": 1,
            "operation": "counter",
            "proposal_terms": ["Shared home"],
        },
        _context(PARTNER_ID, UUID(int=751)),
    )
    assert counter.outcome.status == "succeeded"
    counter_event = counter.events[0]
    assert isinstance(counter_event, RelationshipPartnershipChangedEvent)
    assert counter_event.resulting_partnership.current_proposal_author_id == PARTNER_ID
    assert counter_event.resulting_partnership.current_proposal_recipient_id == PROPOSER_ID
    state_after_counter = apply_event(state, counter_event)

    revised_counter = RuleEngine().evaluate(
        state_after_counter,
        {
            "kind": "update_relationship_partnership_proposal",
            "partnership_id": str(PARTNERSHIP_ID),
            "expected_revision": 2,
            "operation": "revise",
            "proposal_terms": ["Shared home", "Separate finances"],
        },
        _context(PARTNER_ID, UUID(int=752)),
    )
    assert revised_counter.outcome.status == "succeeded"
    revised_event = revised_counter.events[0]
    assert isinstance(revised_event, RelationshipPartnershipChangedEvent)
    assert revised_event.resulting_partnership.revision == 3
    state_after_revision = apply_event(state_after_counter, revised_event)

    next_counter = RuleEngine().evaluate(
        state_after_revision,
        {
            "kind": "update_relationship_partnership_proposal",
            "partnership_id": str(PARTNERSHIP_ID),
            "expected_revision": 3,
            "operation": "counter",
            "proposal_terms": ["Shared home", "Separate finances", "One yearly visit"],
        },
        _context(PROPOSER_ID, UUID(int=753)),
    )
    assert next_counter.outcome.status == "succeeded"
    next_event = next_counter.events[0]
    assert isinstance(next_event, RelationshipPartnershipChangedEvent)
    assert next_event.resulting_partnership.revision == 4
    assert next_event.resulting_partnership.current_proposal_author_id == PROPOSER_ID
    assert next_event.resulting_partnership.current_proposal_recipient_id == PARTNER_ID


def test_proposal_returns_one_private_deterministic_event_without_time_advance() -> None:
    state = _state()
    context = _context()
    partnership_id = uuid5(context.command_id, "relationship-partnership")

    result = RuleEngine().evaluate(
        state,
        {"kind": "propose_relationship_partnership", "partner_id": str(PARTNER_ID)},
        context,
    )

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "relationship_partnership_propose"
    assert result.elapsed_ticks == 0
    assert not any(isinstance(event, TimeAdvancedEvent) for event in result.events)
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, RelationshipPartnershipChangedEvent)
    assert event.event_id == partnership_event_id(
        context.command_id, context.ruleset_version, "propose", partnership_id,
    )
    assert (event.world_id, event.branch_id, event.command_id, event.actor_id) == (
        context.world_id, context.branch_id, context.command_id, PROPOSER_ID,
    )
    assert event.operation == "propose"
    assert event.expected_revision == 0
    assert event.resulting_partnership.partnership_id == partnership_id
    assert event.resulting_partnership.status == "pending"
    assert event.visibility == partnership_visibility(PROPOSER_ID, PARTNER_ID)

    after = apply_event(state, event)
    assert after.current_tick == state.current_tick
    assert after.relationship_partnerships == [event.resulting_partnership]


@pytest.mark.parametrize(
    ("operation", "status", "revision", "actor_id", "result_status", "result_revision"),
    [
        ("accept", "pending", 1, PARTNER_ID, "active", 2),
        ("decline", "pending", 1, PARTNER_ID, "declined", 2),
        ("withdraw", "pending", 1, PROPOSER_ID, "withdrawn", 2),
        ("end", "active", 2, PARTNER_ID, "ended", 3),
    ],
)
def test_partnership_transition_emits_the_exact_private_revision(
    operation: str,
    status: str,
    revision: int,
    actor_id: UUID,
    result_status: str,
    result_revision: int,
) -> None:
    state = _state(partnerships=[_partnership(status=status, revision=revision)])
    context = _context(actor_id, UUID(f"c5000000-0000-4000-8000-00000000000{revision}"))

    result = RuleEngine().evaluate(
        state,
        {
            "kind": "resolve_relationship_partnership",
            "partnership_id": str(PARTNERSHIP_ID),
            "operation": operation,
            "expected_revision": revision,
        },
        context,
    )

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == f"relationship_partnership_{operation}"
    assert result.elapsed_ticks == 0
    assert len(result.events) == 1
    event = result.events[0]
    assert isinstance(event, RelationshipPartnershipChangedEvent)
    assert event.operation == operation
    assert event.expected_revision == revision
    assert event.resulting_partnership.status == result_status
    assert event.resulting_partnership.revision == result_revision
    assert event.visibility == partnership_visibility(PROPOSER_ID, PARTNER_ID)
    assert event.event_id == partnership_event_id(
        context.command_id, context.ruleset_version, operation, PARTNERSHIP_ID,
    )
    assert apply_event(state, event).relationship_partnerships == [event.resulting_partnership]


@pytest.mark.parametrize(
    ("state", "action", "actor_id"),
    [
        (
            _state(partnerships=[_partnership()]),
            {"kind": "resolve_relationship_partnership", "partnership_id": str(PARTNERSHIP_ID), "operation": "accept", "expected_revision": 2},
            PARTNER_ID,
        ),
        (
            _state(partnerships=[_partnership()]),
            {"kind": "resolve_relationship_partnership", "partnership_id": str(PARTNERSHIP_ID), "operation": "accept", "expected_revision": 1},
            OTHER_ID,
        ),
    ],
)
def test_unavailable_partnership_transition_is_rejected_without_an_event(
    state: WorldState,
    action: dict[str, object],
    actor_id: UUID,
) -> None:
    result = RuleEngine().evaluate(state, action, _context(actor_id))

    assert result.outcome.status == "rejected"
    assert result.outcome.code == "relationship_partnership_unavailable"
    assert result.outcome.details == {}
    assert result.events == []
