from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from world_engine.domain.combat_truce import (
    CombatTruceChangedEvent,
    CombatTruceOffer,
    combat_truce_event_id,
    combat_truce_visibility,
    current_combat_truce_offer,
    is_current_combat_truce_offer,
    parse_combat_truce_event,
    plan_combat_truce,
)
from world_engine.domain.components import CombatEncounterComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import WorldEntity, WorldState


WORLD_ID = UUID(int=1001)
BRANCH_ID = UUID(int=1002)
LOCATION_ID = UUID(int=1003)
RETREAT_LOCATION_ID = UUID(int=1004)
ACTOR_ID = UUID(int=1005)
OPPONENT_ID = UUID(int=1006)
ENCOUNTER_ID = UUID(int=1007)
EXCHANGE_EVENT_ID = UUID(int=1008)
OTHER_EVENT_ID = UUID(int=1009)
RULESET = "1.0.0"
TICK = 120


def _state(
    *,
    current_tick: int = TICK,
    exchange_count: int = 1,
    last_event_id: UUID = EXCHANGE_EVENT_ID,
    status: str = "active",
    last_outcome: str = "miss",
    actor_status: str = "active",
    opponent_status: str = "active",
    actor_location_id: UUID = LOCATION_ID,
    opponent_location_id: UUID = LOCATION_ID,
):
    encounter_fields = {
        "encounter_id": ENCOUNTER_ID,
        "location_id": LOCATION_ID,
        "started_tick": 100,
        "status": status,
        "exchange_count": exchange_count,
        "last_event_id": last_event_id,
        "last_damage_event_id": None,
        "last_outcome": last_outcome,
        "resolved_tick": current_tick if status != "active" else None,
    }
    actor_encounter = CombatEncounterComponent(opponent_id=OPPONENT_ID, **encounter_fields)
    opponent_encounter = CombatEncounterComponent(opponent_id=ACTOR_ID, **encounter_fields)
    return WorldState(
        current_tick=current_tick,
        entities={
            LOCATION_ID: WorldEntity(id=LOCATION_ID, kind="place", name="Arena"),
            RETREAT_LOCATION_ID: WorldEntity(id=RETREAT_LOCATION_ID, kind="place", name="Road"),
            ACTOR_ID: WorldEntity(
                id=ACTOR_ID,
                kind="character",
                name="Aster",
                status=actor_status,
                location_id=actor_location_id,
                components={"combat_encounter": actor_encounter.model_dump(mode="json")},
            ),
            OPPONENT_ID: WorldEntity(
                id=OPPONENT_ID,
                kind="character",
                name="Bran",
                status=opponent_status,
                location_id=opponent_location_id,
                components={"combat_encounter": opponent_encounter.model_dump(mode="json")},
            ),
        },
    )


def _plan(
    state: WorldState,
    operation: str,
    actor_id: UUID,
    *,
    command_id: UUID,
    existing_offers=(),
    offer_id: UUID | None = None,
    requested_by: UUID | None = None,
):
    return plan_combat_truce(
        state,
        operation=operation,  # type: ignore[arg-type]
        actor_id=actor_id,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        ruleset_version=RULESET,
        existing_offers=existing_offers,
        offer_id=offer_id,
        requested_by=requested_by,
    )


def _offer(state: WorldState, *, actor_id: UUID = ACTOR_ID, command_id: UUID = UUID(int=1101)):
    return _plan(state, "offer", actor_id, command_id=command_id)


def test_offer_is_bound_to_the_live_exchange_and_visible_only_to_both_participants():
    state = _state()
    before = state.model_dump(mode="json")

    event = _offer(state)

    offer = event.resulting_offer
    assert isinstance(event, CombatTruceChangedEvent)
    assert event.type == "core.combat_truce_changed.v1"
    assert event.operation == "offer"
    assert event.event_id == offer.offer_id == combat_truce_event_id(
        UUID(int=1101), RULESET, "offer",
    )
    assert (
        offer.encounter_id,
        offer.proposer_id,
        offer.recipient_id,
        offer.location_id,
        offer.exchange_count,
        offer.last_event_id,
        offer.offered_tick,
        offer.status,
        offer.responded_tick,
    ) == (
        ENCOUNTER_ID,
        ACTOR_ID,
        OPPONENT_ID,
        LOCATION_ID,
        1,
        EXCHANGE_EVENT_ID,
        TICK,
        "pending",
        None,
    )
    assert event.visibility == combat_truce_visibility(ACTOR_ID, OPPONENT_ID) == {
        "policy": "subject_ids",
        "subject_ids": sorted((str(ACTOR_ID), str(OPPONENT_ID))),
    }
    assert is_current_combat_truce_offer(state, offer)
    assert current_combat_truce_offer(state, ACTOR_ID, [offer]) == offer
    assert current_combat_truce_offer(state, OPPONENT_ID, [offer]) == offer
    assert state.model_dump(mode="json") == before


def test_offer_can_be_made_by_either_participant_but_only_one_can_be_pending():
    state = _state()
    first = _offer(state)
    assert first.resulting_offer.proposer_id == ACTOR_ID

    with pytest.raises(InvalidEvent, match="already has a current pending"):
        _plan(
            state,
            "offer",
            OPPONENT_ID,
            command_id=UUID(int=1102),
            existing_offers=[first.resulting_offer],
        )

    # A declined offer is no longer pending, so the other participant may offer.
    declined = _plan(
        state,
        "decline",
        OPPONENT_ID,
        command_id=UUID(int=1103),
        existing_offers=[first.resulting_offer],
        offer_id=first.resulting_offer.offer_id,
    )
    second = _plan(
        state,
        "offer",
        OPPONENT_ID,
        command_id=UUID(int=1104),
        existing_offers=[declined.resulting_offer],
    )
    assert second.resulting_offer.proposer_id == OPPONENT_ID
    assert second.resulting_offer.recipient_id == ACTOR_ID


@pytest.mark.parametrize(
    ("operation", "status"),
    [("accept", "accepted"), ("decline", "declined")],
)
def test_only_recipient_can_respond_without_advancing_combat_or_world_time(operation, status):
    state = _state()
    offer_event = _offer(state)
    offer = offer_event.resulting_offer
    before = state.model_dump(mode="json")

    event = _plan(
        state,
        operation,
        OPPONENT_ID,
        command_id=UUID(int=1201 if operation == "accept" else 1202),
        existing_offers=[offer],
        offer_id=offer.offer_id,
    )

    result = event.resulting_offer
    assert event.operation == operation
    assert result.status == status
    assert result.responded_tick == state.current_tick == TICK
    assert (
        result.offer_id,
        result.encounter_id,
        result.proposer_id,
        result.recipient_id,
        result.location_id,
        result.exchange_count,
        result.last_event_id,
        result.offered_tick,
    ) == (
        offer.offer_id,
        offer.encounter_id,
        offer.proposer_id,
        offer.recipient_id,
        offer.location_id,
        offer.exchange_count,
        offer.last_event_id,
        offer.offered_tick,
    )
    assert event.visibility == offer_event.visibility
    assert event.event_id == combat_truce_event_id(
        event.command_id, RULESET, operation, offer.offer_id,
    )
    assert state.model_dump(mode="json") == before
    assert not is_current_combat_truce_offer(state, result)


def test_hosted_response_binds_requester_and_optional_field_is_omitted_for_direct_response():
    state = _state()
    offer = _offer(state).resulting_offer

    hosted = _plan(
        state,
        "accept",
        OPPONENT_ID,
        command_id=UUID(int=1301),
        existing_offers=[offer],
        offer_id=offer.offer_id,
        requested_by=ACTOR_ID,
    )
    assert hosted.requested_by == ACTOR_ID
    assert parse_combat_truce_event(hosted.model_dump(mode="json")) == hosted

    direct = _plan(
        state,
        "decline",
        OPPONENT_ID,
        command_id=UUID(int=1302),
        existing_offers=[offer],
        offer_id=offer.offer_id,
    )
    assert direct.requested_by is None
    assert "requested_by" not in direct.model_dump(mode="json")


def test_rejects_self_response_wrong_requester_unknown_offer_and_duplicate_offer_records():
    state = _state()
    offer = _offer(state).resulting_offer

    with pytest.raises(InvalidEvent, match="other combat participant"):
        _plan(
            state, "accept", ACTOR_ID, command_id=UUID(int=1401),
            existing_offers=[offer], offer_id=offer.offer_id,
        )
    with pytest.raises(InvalidEvent, match="requested by the offer's proposer"):
        _plan(
            state, "accept", OPPONENT_ID, command_id=UUID(int=1402),
            existing_offers=[offer], offer_id=offer.offer_id, requested_by=OPPONENT_ID,
        )
    with pytest.raises(InvalidEvent, match="does not exist exactly once"):
        _plan(
            state, "decline", OPPONENT_ID, command_id=UUID(int=1403),
            existing_offers=[], offer_id=offer.offer_id,
        )
    with pytest.raises(InvalidEvent, match="IDs must be unique"):
        _plan(
            state, "decline", OPPONENT_ID, command_id=UUID(int=1404),
            existing_offers=[offer, offer], offer_id=offer.offer_id,
        )


def test_same_tick_exchange_change_stales_offer_but_allows_new_offer_for_new_version():
    state = _state()
    offer_event = _offer(state)
    offer = offer_event.resulting_offer
    next_exchange = _state(exchange_count=2, last_event_id=OTHER_EVENT_ID, current_tick=TICK)

    assert not is_current_combat_truce_offer(next_exchange, offer)
    assert current_combat_truce_offer(next_exchange, ACTOR_ID, [offer]) is None
    with pytest.raises(InvalidEvent, match="stale"):
        _plan(
            next_exchange,
            "accept",
            OPPONENT_ID,
            command_id=UUID(int=1501),
            existing_offers=[offer],
            offer_id=offer.offer_id,
        )

    next_offer = _plan(
        next_exchange,
        "offer",
        ACTOR_ID,
        command_id=UUID(int=1502),
        existing_offers=[offer],
    )
    assert next_offer.resulting_offer.offer_id != offer.offer_id
    assert next_offer.resulting_offer.exchange_count == 2
    assert next_offer.resulting_offer.last_event_id == OTHER_EVENT_ID


def test_retreat_or_lost_colocation_stales_offer():
    state = _state()
    offer = _offer(state).resulting_offer
    retreated = _state(
        status="retreated",
        last_outcome="retreated",
        actor_location_id=RETREAT_LOCATION_ID,
        opponent_location_id=LOCATION_ID,
    )
    assert not is_current_combat_truce_offer(retreated, offer)
    assert current_combat_truce_offer(retreated, ACTOR_ID, [offer]) is None
    with pytest.raises(InvalidEvent, match="stale"):
        _plan(
            retreated,
            "decline",
            OPPONENT_ID,
            command_id=UUID(int=1601),
            existing_offers=[offer],
            offer_id=offer.offer_id,
        )

    moved = _state(actor_location_id=RETREAT_LOCATION_ID)
    assert not is_current_combat_truce_offer(moved, offer)


def test_current_offer_predicate_treats_inactive_participants_as_stale():
    offer = _offer(_state()).resulting_offer
    deceased_proposer = _state(actor_status="dead")
    deceased_recipient = _state(opponent_status="dead")

    assert not is_current_combat_truce_offer(deceased_proposer, offer)
    assert not is_current_combat_truce_offer(deceased_recipient, offer)


def test_event_contract_rejects_wrong_id_actor_requester_or_visibility():
    state = _state()
    offer = _offer(state).resulting_offer
    response = _plan(
        state,
        "accept",
        OPPONENT_ID,
        command_id=UUID(int=1701),
        existing_offers=[offer],
        offer_id=offer.offer_id,
    )
    values = response.model_dump(mode="python")

    with pytest.raises(ValidationError, match="event ID must be bound"):
        CombatTruceChangedEvent.model_validate({**values, "event_id": UUID(int=1702)})
    with pytest.raises(ValidationError, match="only the offer recipient"):
        CombatTruceChangedEvent.model_validate({**values, "actor_id": ACTOR_ID})
    with pytest.raises(ValidationError, match="visible only to its two participants"):
        CombatTruceChangedEvent.model_validate({**values, "visibility": {"policy": "public"}})
    with pytest.raises(ValidationError, match="requested by its proposer"):
        CombatTruceChangedEvent.model_validate({**values, "requested_by": OPPONENT_ID})
    assert parse_combat_truce_event(response.model_dump(mode="json")) == response


def test_event_id_helper_requires_offer_id_for_responses_and_rejects_unsupported_operations():
    with pytest.raises(ValueError, match="requires its UUID offer ID"):
        combat_truce_event_id(UUID(int=1801), RULESET, "accept")
    with pytest.raises(ValueError, match="unsupported"):
        combat_truce_event_id(UUID(int=1801), RULESET, "cancel")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="derives from its offer command"):
        combat_truce_event_id(UUID(int=1801), RULESET, "offer", UUID(int=1802))


def test_offer_record_rejects_inconsistent_pending_and_resolved_timestamps():
    base = {
        "offer_id": UUID(int=1901),
        "encounter_id": ENCOUNTER_ID,
        "proposer_id": ACTOR_ID,
        "recipient_id": OPPONENT_ID,
        "location_id": LOCATION_ID,
        "exchange_count": 1,
        "last_event_id": EXCHANGE_EVENT_ID,
        "offered_tick": TICK,
    }
    with pytest.raises(ValidationError, match="pending combat truce offer cannot have a response tick"):
        CombatTruceOffer(**{**base, "responded_tick": TICK})
    with pytest.raises(ValidationError, match="requires a response tick"):
        CombatTruceOffer(**{**base, "status": "accepted"})
    with pytest.raises(ValidationError, match="at or after its offer"):
        CombatTruceOffer(**{**base, "status": "declined", "responded_tick": TICK - 1})
