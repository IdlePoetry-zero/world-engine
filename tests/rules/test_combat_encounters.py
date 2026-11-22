from __future__ import annotations

from uuid import UUID, uuid4, uuid5

import pytest

from world_engine.domain.combat_validation import validate_combat_batch
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import (
    CombatExchangeResolvedEvent,
    CombatRetreatResolvedEvent,
    DamageAppliedEvent,
    MovementCompletedEvent,
)
from world_engine.domain.models import TimeAdvancedEvent, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.rules import RuleContext, RuleEngine


def _state(
    *, destination_hidden: bool = False, route_risk: str = "0", opponent_health: int = 1000,
    protection: bool | None = None, opponent_is_player: bool = False, actor_lifespan: int | None = None,
):
    location_id, destination_id, route_id, actor_id, opponent_id = (uuid4() for _ in range(5))
    state = WorldState.model_validate({
        "player_id": str(opponent_id if opponent_is_player else actor_id),
        "entities": {
            str(location_id): {
                "id": str(location_id),
                "kind": "place",
                "components": {"place": {"place_type": "site", "routes": [{
                    "route_id": str(route_id),
                    "from_place_id": str(location_id),
                    "to_place_id": str(destination_id),
                    "travel_ticks": 3600,
                    "risk": route_risk,
                }] }},
            },
            str(destination_id): {
                "id": str(destination_id),
                "kind": "place",
                "name": "South Road",
                "components": {
                    "place": {"place_type": "site", "access_policy": "open"},
                    **({"hidden": True} if destination_hidden else {}),
                },
            },
            str(actor_id): {
                "id": str(actor_id),
                "kind": "character",
                "name": "Aster",
                "location_id": str(location_id),
                "components": {
                    "health": {"current": 80, "maximum": 100},
                    "capabilities": {"grants": ["world.travel"], "restrictions": []},
                    "skills": {"skills": {"skill.combat": {"level": "20"}}},
                    **({"life": {"birth_tick": 0, "lifespan_ticks": actor_lifespan}} if actor_lifespan else {}),
                },
            },
            str(opponent_id): {
                "id": str(opponent_id),
                "kind": "character",
                "name": "Bran",
                "location_id": str(location_id),
                "components": {
                    "health": {"current": opponent_health, "maximum": 1000},
                    **({"capabilities": {"combat_death_protection": protection}} if protection is not None else {}),
                },
            },
        },
    })
    return state, location_id, destination_id, route_id, actor_id, opponent_id


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


def test_attack_persists_the_seeded_exchange_and_symmetric_encounter_state():
    state, location_id, _, _, actor_id, opponent_id = _state()
    command_id = UUID(int=3)
    context = _context(actor_id, command_id)
    result = RuleEngine().evaluate(state, {"kind": "attack", "target_id": str(opponent_id)}, context)

    exchanges = [event for event in result.events if isinstance(event, CombatExchangeResolvedEvent)]
    assert len(exchanges) == 1
    exchange = exchanges[0]
    assert exchange.world_id == context.world_id
    assert exchange.branch_id == context.branch_id
    assert exchange.command_id == command_id
    assert exchange.location_id == location_id
    assert exchange.roll == result.rolls[0].value
    assert exchange.roll_digest == result.rolls[0].digest
    assert exchange.outcome == result.rolls[0].outcome
    assert exchange.encounter_status == "active"
    after = _replay(state, result.events)
    actor_encounter = after.entities[actor_id].components["combat_encounter"]
    opponent_encounter = after.entities[opponent_id].components["combat_encounter"]
    assert actor_encounter["encounter_id"] == opponent_encounter["encounter_id"] == str(exchange.encounter_id)
    assert actor_encounter["opponent_id"] == str(opponent_id)
    assert opponent_encounter["opponent_id"] == str(actor_id)
    assert actor_encounter["exchange_count"] == opponent_encounter["exchange_count"] == 1
    assert actor_encounter["status"] == opponent_encounter["status"] == "active"
    if exchange.outcome == "hit":
        damage = next(event for event in result.events if isinstance(event, DamageAppliedEvent))
        assert damage.event_id == exchange.damage_event_id
        assert damage.source_event_ids == [exchange.event_id]
        assert damage.target_id == opponent_id

    validate_combat_batch(
        result.events,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=command_id,
        ruleset_version=context.ruleset_version,
        actor_id=actor_id,
        command_type="action.attack",
        command_payload={"kind": "attack", "target_id": str(opponent_id), "weapon_resource_id": None},
        state=state,
    )


@pytest.mark.parametrize(("protection", "opponent_is_player", "expected_status", "expected_code"), [
    (None, False, "dead", "attack_hit"),
    (False, False, "dead", "attack_hit"),
    (True, True, "dead", "attack_hit"),
    (True, False, "incapacitated", "combat_target_incapacitated"),
])
def test_lethal_attack_ends_encounter_with_opt_in_protection_for_npcs_only(
    protection, opponent_is_player, expected_status, expected_code,
):
    state, _, _, route_id, actor_id, opponent_id = _state(
        opponent_health=6, protection=protection, opponent_is_player=opponent_is_player,
    )
    engine = RuleEngine()
    context = _context(actor_id, UUID(int=400))
    intent = {"kind": "attack", "target_id": str(opponent_id)}
    result = engine.evaluate(state, intent, context)

    assert result.outcome.code == expected_code
    exchange = next(event for event in result.events if isinstance(event, CombatExchangeResolvedEvent))
    assert exchange.damage == 6
    assert exchange.encounter_status == "resolved"
    validate_combat_batch(
        result.events,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        ruleset_version=context.ruleset_version,
        actor_id=actor_id,
        command_type="action.attack",
        command_payload=intent,
        state=state,
    )
    after = _replay(state, result.events)
    opponent = after.entities[opponent_id]
    assert opponent.status == expected_status
    assert opponent.components["combat_encounter"]["status"] == "resolved"
    assert after.entities[actor_id].components["combat_encounter"]["status"] == "resolved"
    health = opponent.components["health"]
    if expected_status == "dead":
        assert health["current"] == 0
        assert "target_incapacitated" not in result.outcome.details
        return

    assert result.outcome.details["target_incapacitated"] is True
    assert health["current"] == 1
    assert health["incapacitating_injury_id"] == health["injuries"][0]["injury_id"]
    assert engine.evaluate(after, intent, context).outcome.code == "target_unavailable"
    blocked = engine.evaluate(after, {"kind": "wait", "duration_ticks": 100}, _context(opponent_id, UUID(int=402)))
    assert blocked.outcome.code == "actor_incapacitated"
    assert blocked.events == []
    departure = engine.evaluate_travel_departure(after, route_id, _context(opponent_id, UUID(int=403)))
    assert departure.outcome.code == "actor_incapacitated"
    assert departure.events == []


@pytest.mark.parametrize(("opponent_health", "command_number", "expected_code", "expected_health"), [
    (6, 401, "attack_missed", 6),
    (7, 400, "attack_hit", 1),
])
def test_protected_active_npc_keeps_active_encounter_after_miss_or_nonlethal_hit(
    opponent_health, command_number, expected_code, expected_health,
):
    state, _, _, _, actor_id, opponent_id = _state(opponent_health=opponent_health, protection=True)
    result = RuleEngine().evaluate(
        state, {"kind": "attack", "target_id": str(opponent_id)}, _context(actor_id, UUID(int=command_number)),
    )
    assert result.outcome.code == expected_code
    assert "target_incapacitated" not in result.outcome.details
    opponent = _replay(state, result.events).entities[opponent_id]
    assert opponent.status == "active"
    assert opponent.components["combat_encounter"]["status"] == "active"
    assert opponent.components["health"]["current"] == expected_health


def test_attack_reports_actor_lifespan_expiration_alongside_target_incapacitation():
    state, _, _, _, actor_id, opponent_id = _state(opponent_health=6, protection=True, actor_lifespan=10)
    result = RuleEngine().evaluate(
        state, {"kind": "attack", "target_id": str(opponent_id)}, _context(actor_id, UUID(int=400)),
    )

    assert result.outcome.status == "partial"
    assert result.outcome.code == "lifespan_expired"
    assert result.outcome.details["target_incapacitated"] is True
    after = _replay(state, result.events)
    assert after.entities[actor_id].status == "dead"
    assert after.entities[opponent_id].status == "incapacitated"


def test_follow_up_attack_advances_the_same_encounter_and_ordinary_move_is_rejected():
    state, location_id, destination_id, _route_id, actor_id, opponent_id = _state()
    engine = RuleEngine()
    first = engine.evaluate(state, {"kind": "attack", "target_id": str(opponent_id)}, _context(actor_id, UUID(int=4)))
    after_first = _replay(state, first.events)
    first_component = after_first.entities[actor_id].components["combat_encounter"]

    move = engine.evaluate(after_first, {"kind": "move", "destination_id": str(destination_id)}, _context(actor_id, UUID(int=5)))
    assert move.outcome.code == "combat_encounter_active"
    assert move.events == []
    assert move.elapsed_ticks == 0

    second = engine.evaluate(after_first, {"kind": "attack", "target_id": str(opponent_id)}, _context(actor_id, UUID(int=6)))
    assert second.outcome.status != "rejected"
    second_exchange = next(event for event in second.events if isinstance(event, CombatExchangeResolvedEvent))
    assert second_exchange.encounter_id == UUID(first_component["encounter_id"])
    assert second_exchange.exchange_number == 2
    after_second = _replay(after_first, second.events)
    assert after_second.entities[actor_id].components["combat_encounter"]["exchange_count"] == 2
    assert after_second.entities[opponent_id].components["combat_encounter"]["exchange_count"] == 2
    assert after_second.entities[actor_id].location_id == location_id


def test_retreat_uses_the_existing_route_and_closes_the_active_encounter():
    state, location_id, destination_id, route_id, actor_id, opponent_id = _state()
    engine = RuleEngine()
    attack = engine.evaluate(state, {"kind": "attack", "target_id": str(opponent_id)}, _context(actor_id, UUID(int=7)))
    engaged = _replay(state, attack.events)
    command_id = UUID(int=8)
    context = _context(actor_id, command_id)
    retreat = engine.evaluate(
        engaged,
        {"kind": "retreat", "destination_id": str(destination_id), "route_id": str(route_id)},
        context,
    )

    assert retreat.outcome.code == "retreated"
    assert retreat.elapsed_ticks == 3600
    retreat_event = next(event for event in retreat.events if isinstance(event, CombatRetreatResolvedEvent))
    movement = next(event for event in retreat.events if isinstance(event, MovementCompletedEvent))
    assert retreat.events.index(retreat_event) < retreat.events.index(movement)
    assert retreat_event.route_id == movement.route_id == route_id
    assert retreat_event.from_place_id == location_id
    assert retreat_event.to_place_id == destination_id
    after = _replay(engaged, retreat.events)
    assert after.entities[actor_id].location_id == destination_id
    assert after.entities[opponent_id].location_id == location_id
    assert after.entities[actor_id].components["combat_encounter"]["status"] == "retreated"
    assert after.entities[opponent_id].components["combat_encounter"]["status"] == "retreated"
    validate_combat_batch(
        retreat.events,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=command_id,
        ruleset_version=context.ruleset_version,
        actor_id=actor_id,
        command_type="action.retreat",
        command_payload={"kind": "retreat", "destination_id": str(destination_id), "route_id": str(route_id)},
        state=engaged,
    )


def test_retreat_does_not_bypass_hidden_destination_or_route_hazards():
    hidden_state, _, destination_id, route_id, actor_id, opponent_id = _state(destination_hidden=True)
    engine = RuleEngine()
    attack = engine.evaluate(hidden_state, {"kind": "attack", "target_id": str(opponent_id)}, _context(actor_id, UUID(int=9)))
    engaged = _replay(hidden_state, attack.events)
    hidden_retreat = engine.evaluate(
        engaged,
        {"kind": "retreat", "destination_id": str(destination_id), "route_id": str(route_id)},
        _context(actor_id, UUID(int=10)),
    )
    assert hidden_retreat.outcome.code == "destination_unseen"
    assert hidden_retreat.events == []

    # A forged event batch cannot use the same hidden destination via the
    # reducer path either; only the existing observed/open-route rules apply.
    active = next(event for event in attack.events if isinstance(event, CombatExchangeResolvedEvent))
    actor_encounter = engaged.entities[actor_id].components["combat_encounter"]
    opponent_encounter = engaged.entities[opponent_id].components["combat_encounter"]
    command_id = UUID(int=101)
    arrival_tick = engaged.current_tick + 3600
    event = CombatRetreatResolvedEvent(
        event_id=uuid5(command_id, "1.0.0:combat.retreat"),
        world_id=active.world_id,
        branch_id=active.branch_id,
        command_id=command_id,
        ruleset_version="1.0.0",
        encounter_id=active.encounter_id,
        actor_id=actor_id,
        opponent_id=opponent_id,
        route_id=route_id,
        from_place_id=engaged.entities[actor_id].location_id,
        to_place_id=destination_id,
        expected_actor_encounter_revision=actor_encounter["revision"],
        expected_opponent_encounter_revision=opponent_encounter["revision"],
        started_tick=engaged.current_tick,
        arrived_tick=arrival_tick,
        visibility={"policy": "subject_ids", "subject_ids": sorted((str(actor_id), str(opponent_id)))},
    )
    advanced = apply_event(engaged, TimeAdvancedEvent(to_tick=arrival_tick))
    with pytest.raises(InvalidEvent, match="undiscovered destination"):
        apply_event(advanced, event)

    risky_state, location_id, destination_id, route_id, actor_id, opponent_id = _state(route_risk="1")
    attack = engine.evaluate(risky_state, {"kind": "attack", "target_id": str(opponent_id)}, _context(actor_id, UUID(int=11)))
    engaged = _replay(risky_state, attack.events)
    retreat = engine.evaluate(
        engaged,
        {"kind": "retreat", "destination_id": str(destination_id), "route_id": str(route_id)},
        _context(actor_id, UUID(int=12)),
    )
    assert retreat.outcome.code == "retreat_interrupted"
    after = _replay(engaged, retreat.events)
    assert after.entities[actor_id].location_id == location_id
    assert after.entities[actor_id].components["health"]["current"] < 80
    assert after.entities[actor_id].components["health"]["injuries"]
    assert after.entities[actor_id].components["combat_encounter"]["status"] == "active"
    assert after.entities[opponent_id].components["combat_encounter"]["status"] == "active"


def test_commit_batch_hook_requires_attack_command_and_matching_hit_damage():
    state, _, _, _, actor_id, opponent_id = _state()
    engine = RuleEngine()
    result = None
    context = None
    for command_number in range(200, 400):
        context = _context(actor_id, UUID(int=command_number))
        candidate = engine.evaluate(state, {"kind": "attack", "target_id": str(opponent_id)}, context)
        exchange = next(event for event in candidate.events if isinstance(event, CombatExchangeResolvedEvent))
        if exchange.outcome == "hit":
            result = candidate
            break
    assert result is not None and context is not None
    with pytest.raises(InvalidEvent, match="attack action"):
        validate_combat_batch(
            result.events,
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            ruleset_version=context.ruleset_version,
            actor_id=actor_id,
            command_type="action.move",
            command_payload={"kind": "attack", "target_id": str(opponent_id), "weapon_resource_id": None},
            state=state,
        )
    without_damage = [event for event in result.events if not isinstance(event, DamageAppliedEvent)]
    with pytest.raises(InvalidEvent, match="matching damage event"):
        validate_combat_batch(
            without_damage,
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            ruleset_version=context.ruleset_version,
            actor_id=actor_id,
            command_type="action.attack",
            command_payload={"kind": "attack", "target_id": str(opponent_id), "weapon_resource_id": None},
            state=state,
        )


def test_combat_event_hash_and_encounter_transition_are_checked_during_replay():
    state, _, _, _, actor_id, opponent_id = _state()
    attack = RuleEngine().evaluate(state, {"kind": "attack", "target_id": str(opponent_id)}, _context(actor_id, UUID(int=13)))
    event = next(event for event in attack.events if isinstance(event, CombatExchangeResolvedEvent))
    tampered_roll = event.model_copy(update={"roll": event.roll % 100 + 1})
    with pytest.raises(InvalidEvent, match="deterministic command seed|roll and chance"):
        apply_event(state, tampered_roll)

    after = _replay(state, attack.events)
    stale = event.model_copy(update={"expected_actor_encounter_revision": 7})
    with pytest.raises(InvalidEvent, match="revision is stale"):
        apply_event(after, stale)
