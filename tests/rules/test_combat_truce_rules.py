from __future__ import annotations

from uuid import UUID, uuid4

from world_engine.domain.combat_truce import CombatTruceChangedEvent
from world_engine.domain.gameplay_events import CombatExchangeResolvedEvent
from world_engine.domain.models import TimeAdvancedEvent, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.rules import RuleContext, RuleEngine
from world_engine.rules.contracts import RequestCombatTruceResponseIntent, parse_action


def _state():
    location_id, actor_id, opponent_id = (uuid4() for _ in range(3))
    state = WorldState.model_validate({
        "player_id": str(actor_id),
        "entities": {
            str(location_id): {
                "id": str(location_id),
                "kind": "place",
                "components": {"place": {"place_type": "site"}},
            },
            str(actor_id): {
                "id": str(actor_id),
                "kind": "character",
                "name": "Aster",
                "location_id": str(location_id),
                "components": {
                    "health": {"current": 80, "maximum": 100},
                    "skills": {"skills": {"skill.combat": {"level": "20"}}},
                },
            },
            str(opponent_id): {
                "id": str(opponent_id),
                "kind": "character",
                "name": "Bran",
                "location_id": str(location_id),
                "components": {"health": {"current": 1000, "maximum": 1000}},
            },
        },
    })
    return state, actor_id, opponent_id


def _context(actor_id: UUID, command_id: UUID) -> RuleContext:
    return RuleContext(
        world_id=UUID(int=1),
        branch_id=UUID(int=2),
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version="1.0.0",
    )


def _replay(state: WorldState, events):
    current = state
    for event in events:
        current = apply_event(current, event)
    return current


def _engaged_state(state: WorldState, actor_id: UUID, opponent_id: UUID) -> WorldState:
    exchange = RuleEngine().evaluate(
        state,
        {"kind": "attack", "target_id": str(opponent_id)},
        _context(actor_id, UUID(int=3)),
    )
    assert exchange.outcome.status != "rejected"
    assert any(isinstance(event, CombatExchangeResolvedEvent) for event in exchange.events)
    return _replay(state, exchange.events)


def test_offer_records_one_private_event_without_advancing_time_or_resolving_combat():
    state, actor_id, opponent_id = _state()
    engaged = _engaged_state(state, actor_id, opponent_id)
    last_event_id = UUID(engaged.entities[actor_id].components["combat_encounter"]["last_event_id"])
    current_tick = engaged.current_tick

    result = RuleEngine().evaluate(
        engaged,
        {"kind": "offer_combat_truce", "expected_last_event_id": str(last_event_id)},
        _context(actor_id, UUID(int=4)),
    )

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "combat_truce_offered"
    assert result.elapsed_ticks == 0
    assert len(result.events) == 1
    assert not any(isinstance(event, TimeAdvancedEvent) for event in result.events)
    event = result.events[0]
    assert isinstance(event, CombatTruceChangedEvent)
    assert event.operation == "offer"
    assert event.resulting_offer.proposer_id == actor_id
    assert event.resulting_offer.recipient_id == opponent_id
    assert event.resulting_offer.last_event_id == last_event_id

    after = _replay(engaged, result.events)
    assert after.current_tick == current_tick
    assert after.entities[actor_id].components["combat_encounter"]["status"] == "active"
    assert after.entities[opponent_id].components["combat_encounter"]["status"] == "active"
    assert after.combat_truce_offers == [event.resulting_offer]


def test_truce_response_can_be_accepted_or_declined_only_by_the_recipient():
    for operation, expected_status, expected_code in (
        ("accept", "accepted", "combat_truce_accepted"),
        ("decline", "declined", "combat_truce_declined"),
    ):
        state, actor_id, opponent_id = _state()
        engaged = _engaged_state(state, actor_id, opponent_id)
        last_event_id = UUID(engaged.entities[actor_id].components["combat_encounter"]["last_event_id"])
        offered = RuleEngine().evaluate(
            engaged,
            {"kind": "offer_combat_truce", "expected_last_event_id": str(last_event_id)},
            _context(actor_id, UUID(int=4)),
        )
        offered_state = _replay(engaged, offered.events)
        offer_id = offered.events[0].resulting_offer.offer_id

        response = RuleEngine().evaluate(
            offered_state,
            {"kind": "resolve_combat_truce", "offer_id": str(offer_id), "operation": operation},
            _context(opponent_id, UUID(int=5)),
        )

        assert response.outcome.status == "succeeded"
        assert response.outcome.code == expected_code
        assert response.elapsed_ticks == 0
        assert len(response.events) == 1
        event = response.events[0]
        assert isinstance(event, CombatTruceChangedEvent)
        assert event.operation == operation
        assert event.resulting_offer.status == expected_status
        after = _replay(offered_state, response.events)
        assert after.combat_truce_offers[0].status == expected_status
        if operation == "accept":
            for participant_id in (actor_id, opponent_id):
                encounter = after.entities[participant_id].components["combat_encounter"]
                assert encounter["status"] == "resolved"
                assert encounter["last_outcome"] == "truce"
        else:
            assert after.entities[actor_id].components["combat_encounter"]["status"] == "active"

        wrong_actor = RuleEngine().evaluate(
            offered_state,
            {"kind": "resolve_combat_truce", "offer_id": str(offer_id), "operation": operation},
            _context(actor_id, UUID(int=6)),
        )
        assert wrong_actor.outcome.status == "rejected"
        assert wrong_actor.outcome.code == "combat_truce_unavailable"
        assert wrong_actor.events == []


def test_offer_rejects_a_stale_encounter_event_and_host_response_request_is_not_a_rule_mutation():
    state, actor_id, opponent_id = _state()
    engaged = _engaged_state(state, actor_id, opponent_id)

    stale = RuleEngine().evaluate(
        engaged,
        {"kind": "offer_combat_truce", "expected_last_event_id": str(UUID(int=99))},
        _context(actor_id, UUID(int=4)),
    )
    assert stale.outcome.status == "rejected"
    assert stale.outcome.code == "combat_truce_encounter_changed"
    assert stale.events == []
    assert stale.elapsed_ticks == 0

    fresh_last_event_id = UUID(engaged.entities[actor_id].components["combat_encounter"]["last_event_id"])
    offered = RuleEngine().evaluate(
        engaged,
        {"kind": "offer_combat_truce", "expected_last_event_id": str(fresh_last_event_id)},
        _context(actor_id, UUID(int=5)),
    )
    offered_state = _replay(engaged, offered.events)
    offer_id = offered.events[0].resulting_offer.offer_id
    next_exchange = RuleEngine().evaluate(
        offered_state,
        {"kind": "attack", "target_id": str(opponent_id)},
        _context(actor_id, UUID(int=6)),
    )
    changed_encounter = _replay(offered_state, next_exchange.events)
    stale_response = RuleEngine().evaluate(
        changed_encounter,
        {"kind": "resolve_combat_truce", "offer_id": str(offer_id), "operation": "accept"},
        _context(opponent_id, UUID(int=7)),
    )
    assert stale_response.outcome.status == "rejected"
    assert stale_response.outcome.code == "combat_truce_unavailable"
    assert stale_response.events == []
    assert stale_response.elapsed_ticks == 0

    request = parse_action({"kind": "request_combat_truce_response", "offer_id": str(UUID(int=98))})
    assert isinstance(request, RequestCombatTruceResponseIntent)
    hosted_request = RuleEngine().evaluate(engaged, request, _context(actor_id, UUID(int=8)))
    assert hosted_request.outcome.status == "rejected"
    assert hosted_request.events == []
    assert hosted_request.elapsed_ticks == 0
