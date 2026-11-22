from __future__ import annotations

from uuid import UUID

import pytest

from world_engine.domain.components import LifeComponent
from world_engine.domain.models import TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.domain.relationship_disclosure_events import (
    RelationshipDisclosureRecordedEvent,
    validate_relationship_disclosure_batch,
)
from world_engine.domain.relationship_partnerships import RelationshipPartnership
from world_engine.rules import RuleContext, RuleEngine, parse_action


PLACE_ID = UUID("d1000000-0000-4000-8000-000000000001")
PROPOSER_ID = UUID("d2000000-0000-4000-8000-000000000001")
PARTNER_ID = UUID("d2000000-0000-4000-8000-000000000002")
RECIPIENT_ID = UUID("d2000000-0000-4000-8000-000000000003")
OUTSIDER_ID = UUID("d2000000-0000-4000-8000-000000000004")
PARTNERSHIP_ID = UUID("d3000000-0000-4000-8000-000000000001")


def _character(entity_id: UUID, name: str) -> WorldEntity:
    return WorldEntity(
        id=entity_id,
        kind="character",
        name=name,
        location_id=PLACE_ID,
        components={
            "life": LifeComponent(birth_tick=-100_000, life_stage="adult").model_dump(mode="json"),
        },
    )


def _partnership(status: str = "active", revision: int = 2) -> RelationshipPartnership:
    return RelationshipPartnership(
        partnership_id=PARTNERSHIP_ID,
        proposer_id=PROPOSER_ID,
        partner_id=PARTNER_ID,
        status=status,
        proposed_tick=10,
        updated_tick=10 if status == "pending" else 50,
        revision=revision,
    )


def _state(*, status: str = "active", revision: int = 2) -> WorldState:
    return WorldState(
        current_tick=100,
        entities={
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Garden"),
            PROPOSER_ID: _character(PROPOSER_ID, "Ari"),
            PARTNER_ID: _character(PARTNER_ID, "Bea"),
            RECIPIENT_ID: _character(RECIPIENT_ID, "Casey"),
            OUTSIDER_ID: _character(OUTSIDER_ID, "Dara"),
        },
        relationship_partnerships=[_partnership(status, revision)],
    )


def _context(actor_id: UUID = PROPOSER_ID) -> RuleContext:
    return RuleContext(
        world_id=UUID("d4000000-0000-4000-8000-000000000001"),
        branch_id=UUID("d4000000-0000-4000-8000-000000000002"),
        command_id=UUID("d4000000-0000-4000-8000-000000000003"),
        actor_id=actor_id,
        ruleset_version="disclosure-rules.v1",
    )


def _intent(*, partnership_id: UUID = PARTNERSHIP_ID, revision: object = 2,
            recipient_id: UUID = RECIPIENT_ID) -> dict[str, object]:
    return {
        "kind": "disclose_relationship_partnership",
        "partnership_id": str(partnership_id),
        "expected_revision": revision,
        "recipient_id": str(recipient_id),
    }


def test_disclose_partnership_intent_is_closed_and_revisioned() -> None:
    parsed = parse_action(_intent())
    assert parsed.kind == "disclose_relationship_partnership"
    assert parsed.partnership_id == PARTNERSHIP_ID
    assert parsed.expected_revision == 2
    assert parsed.recipient_id == RECIPIENT_ID

    with pytest.raises(ValueError, match="registered schema"):
        parse_action(_intent() | {"operation": "accept"})
    with pytest.raises(ValueError, match="registered schema"):
        parse_action(_intent(revision=True))
    with pytest.raises(ValueError, match="registered schema"):
        parse_action(_intent(revision="2"))


def test_disclosure_rule_emits_one_recipient_only_event_without_time_or_source_details() -> None:
    state = _state()
    context = _context()

    result = RuleEngine().evaluate(state, _intent(), context)

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "relationship_disclosure_recorded"
    assert result.outcome.details == {}
    assert result.elapsed_ticks == 0
    assert len(result.events) == 1
    assert not any(isinstance(event, TimeAdvancedEvent) for event in result.events)

    event = result.events[0]
    assert isinstance(event, RelationshipDisclosureRecordedEvent)
    assert event.type == "core.relationship_disclosure_recorded.v1"
    assert (event.world_id, event.branch_id, event.command_id, event.actor_id) == (
        context.world_id, context.branch_id, context.command_id, PROPOSER_ID,
    )
    assert event.record.holder_id == RECIPIENT_ID
    assert event.record.subject_id == PROPOSER_ID
    assert event.record.relationship_disclosure is not None
    receipt = event.record.relationship_disclosure
    assert receipt.source_partnership_id == PARTNERSHIP_ID
    assert receipt.source_revision == 2
    assert receipt.discloser_id == PROPOSER_ID
    assert receipt.opposite_participant_id == PARTNER_ID
    assert receipt.recipient_id == RECIPIENT_ID
    assert receipt.location_id == PLACE_ID
    assert receipt.disclosed_tick == state.current_tick
    assert event.visibility == {"policy": "subject_ids", "subject_ids": [str(RECIPIENT_ID)]}
    validate_relationship_disclosure_batch(
        result.events,
        state=state,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        actor_id=context.actor_id,
        ruleset_version=context.ruleset_version,
        command_type="action.disclose_relationship_partnership",
        command_payload=_intent(),
    )

    after = apply_event(state, event)
    assert after.current_tick == state.current_tick
    assert after.relationship_partnerships == state.relationship_partnerships
    assert after.knowledge == [event.record]


@pytest.mark.parametrize(
    ("state", "actor_id", "action"),
    [
        (_state(status="pending", revision=1), PROPOSER_ID, _intent()),
        (_state(), PROPOSER_ID, _intent(revision=1)),
        (_state(), OUTSIDER_ID, _intent()),
        (_state(), PROPOSER_ID, _intent(recipient_id=PARTNER_ID)),
    ],
)
def test_unavailable_disclosure_rejections_are_generic_without_events(
    state: WorldState, actor_id: UUID, action: dict[str, object],
) -> None:
    result = RuleEngine().evaluate(state, action, _context(actor_id))

    assert result.outcome.status == "rejected"
    assert result.outcome.code == "relationship_disclosure_unavailable"
    assert result.outcome.details == {}
    assert result.events == []
    assert result.elapsed_ticks == 0

