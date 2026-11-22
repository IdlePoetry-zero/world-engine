from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from world_engine.domain.components import (
    CombatEncounterComponent,
    InventoryComponent,
    InventoryLot,
    LifeComponent,
    NeedsComponent,
    ProgressionComponent,
    SkillsComponent,
    SkillValue,
)
from world_engine.domain.cultivation_breakthrough import (
    CULTIVATION_BREAKTHROUGH_ROLL_LABEL,
    CULTIVATION_PRACTICE_SKILL_ID,
    plan_cultivation_breakthrough,
    registered_cultivation_breakthrough_policy,
)
from world_engine.domain.models import ResourceAccount, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.domain.travel_journeys import (
    TRAVEL_RESOLUTION_HANDLER_ID,
    TRAVEL_RESOLUTION_HANDLER_VERSION,
    TravelJourney,
    travel_resolution_schedule_id,
)
from world_engine.domain.components import ScheduledEventRecord
from world_engine.rules import RuleContext, RuleEngine, parse_action
from world_engine.rules.versions import LEGACY_RULESET_VERSION, MAGIC_GATE_RULESET_VERSION


WORLD_ID = UUID(int=1001)
BRANCH_ID = UUID(int=1002)
ACTOR_ID = UUID(int=1003)
PLACE_ID = UUID(int=1004)
QI_ACCOUNT_ID = UUID(int=1005)
QI_SINK_ID = UUID(int=1006)
LOT_ID = UUID(int=1007)
START_TICK = 100
RULESET = "1.0.0"
POLICY_ID = "cultivation.qi_gathering.v1"


def _state(*, health_current: int = 50, food_value: int = 80, critical_tick: int | None = None) -> WorldState:
    actor = WorldEntity(
        id=ACTOR_ID,
        kind="character",
        name="Aster",
        location_id=PLACE_ID,
        components={
            "capabilities": {"grants": ["cultivation.practice"], "restrictions": []},
            "health": {"current": health_current, "maximum": 100},
            "skills": SkillsComponent(skills={
                CULTIVATION_PRACTICE_SKILL_ID: SkillValue(level="40"),
            }).model_dump(mode="json"),
            "progression": ProgressionComponent(milestones=[]).model_dump(mode="json"),
            "inventory": InventoryComponent(lots=[InventoryLot(
                lot_id=LOT_ID,
                account_id=QI_ACCOUNT_ID,
                resource_id="cultivation.qi",
                quantity="2",
            )]).model_dump(mode="json"),
            "needs": NeedsComponent(
                food={"value": food_value, "critical_below": 20, "last_critical_damage_tick": critical_tick},
                water={"value": 80, "critical_below": 20},
                rest={"value": 80},
            ).model_dump(mode="json"),
        },
    )
    return WorldState(
        current_tick=START_TICK,
        entities={
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", name="Quiet Hall", components={"place": {"place_type": "building"}}),
            ACTOR_ID: actor,
        },
        resources={
            QI_ACCOUNT_ID: ResourceAccount(resource_id="cultivation.qi", balance="2", owner_id=ACTOR_ID),
            QI_SINK_ID: ResourceAccount(
                resource_id="cultivation.qi", balance="0", account_type="consumption_sink",
            ),
        },
        player_id=ACTOR_ID,
        metadata={"pack_id": "long_life_cultivation"},
    )


def _context(command_id: UUID, *, actor_id: UUID = ACTOR_ID) -> RuleContext:
    return RuleContext(
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=RULESET,
    )


def _command_for(state: WorldState, *, should_succeed: bool) -> UUID:
    policy = registered_cultivation_breakthrough_policy(state, POLICY_ID)
    assert policy is not None
    for candidate in range(1, 10_000):
        command_id = UUID(int=candidate)
        plan = plan_cultivation_breakthrough(
            state,
            actor_id=ACTOR_ID,
            policy=policy,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=command_id,
            ruleset_version=RULESET,
        )
        if (plan.outcome == "succeeded") == should_succeed:
            return command_id
    raise AssertionError("could not find a deterministic command seed for requested breakthrough result")


def _replay(state: WorldState, events) -> WorldState:
    current = state
    for event in events:
        current = apply_event(current, event)
    return current


def test_attempt_intent_requires_registered_server_policy_id():
    with pytest.raises(ValueError, match="registered schema"):
        parse_action({"kind": "attempt_cultivation_breakthrough"})
    parsed = parse_action({
        "kind": "attempt_cultivation_breakthrough",
        "policy_id": POLICY_ID,
    })
    assert parsed.policy_id == POLICY_ID


def test_success_commits_exact_duration_fifo_cost_and_registered_realm_result():
    state = _state()
    policy = registered_cultivation_breakthrough_policy(state, POLICY_ID)
    assert policy is not None
    command_id = _command_for(state, should_succeed=True)

    result = RuleEngine().evaluate(
        state,
        {"kind": "attempt_cultivation_breakthrough", "policy_id": POLICY_ID},
        _context(command_id),
    )

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "cultivation_breakthrough_succeeded"
    assert result.outcome.details["breakthrough_outcome"] == "succeeded"
    assert result.elapsed_ticks == policy.duration_ticks
    assert result.events[0] == TimeAdvancedEvent(to_tick=START_TICK + policy.duration_ticks,
                                                 event_id=result.events[0].event_id)
    assert result.rolls[0].label == CULTIVATION_BREAKTHROUGH_ROLL_LABEL
    after = _replay(state, result.events)
    assert after.current_tick == START_TICK + policy.duration_ticks
    assert after.resources[QI_ACCOUNT_ID].balance == Decimal("1")
    assert after.resources[QI_SINK_ID].balance == Decimal("1")
    progression = ProgressionComponent.model_validate(after.entities[ACTOR_ID].components["progression"])
    assert progression.milestones == ["cultivation.realm:qi_gathering"]
    assert any(event.type == "core.cultivation_breakthrough_resolved.v1" for event in result.events)


def test_failed_attempt_commits_cost_and_damage_instead_of_becoming_rejected():
    state = _state()
    policy = registered_cultivation_breakthrough_policy(state, POLICY_ID)
    assert policy is not None
    command_id = _command_for(state, should_succeed=False)

    result = RuleEngine().evaluate(
        state,
        {"kind": "attempt_cultivation_breakthrough", "policy_id": POLICY_ID},
        _context(command_id),
    )

    assert result.outcome.status == "failed"
    assert result.outcome.code == "cultivation_breakthrough_failed"
    assert result.outcome.details["breakthrough_outcome"] == "failed"
    assert result.elapsed_ticks == policy.duration_ticks
    after = _replay(state, result.events)
    assert after.current_tick == START_TICK + policy.duration_ticks
    assert after.resources[QI_ACCOUNT_ID].balance == Decimal("1")
    assert after.resources[QI_SINK_ID].balance == Decimal("1")
    assert after.entities[ACTOR_ID].components["health"]["current"] == 46
    progression = ProgressionComponent.model_validate(after.entities[ACTOR_ID].components["progression"])
    assert progression.milestones == []


def test_failure_damage_precedes_deprivation_and_advances_health_compare_and_swap():
    old_critical_tick = START_TICK - (86_400 - 3_600)
    state = _state(food_value=0, critical_tick=old_critical_tick)
    command_id = _command_for(state, should_succeed=False)

    result = RuleEngine().evaluate(
        state,
        {"kind": "attempt_cultivation_breakthrough", "policy_id": POLICY_ID},
        _context(command_id),
    )

    failure_damage_index = next(
        index for index, event in enumerate(result.events)
        if event.type == "core.damage_applied.v1" and event.injury_type == "core.injury.strain"
    )
    deprivation_index = next(
        index for index, event in enumerate(result.events)
        if event.type == "core.damage_applied.v1" and event.injury_type == "core.injury.deprivation"
    )
    assert failure_damage_index < deprivation_index
    assert result.events[deprivation_index].expected_health_revision == 2
    after = _replay(state, result.events)
    assert after.entities[ACTOR_ID].components["health"]["current"] == 45


def test_lethal_failure_does_not_emit_actor_deprivation_after_terminal_damage():
    state = _state(health_current=2)
    command_id = _command_for(state, should_succeed=False)

    result = RuleEngine().evaluate(
        state,
        {"kind": "attempt_cultivation_breakthrough", "policy_id": POLICY_ID},
        _context(command_id),
    )

    assert result.outcome.status == "failed"
    assert result.elapsed_ticks == 3_600
    assert not any(
        event.type == "core.damage_applied.v1" and event.injury_type == "core.injury.deprivation"
        for event in result.events
    )
    after = _replay(state, result.events)
    assert after.entities[ACTOR_ID].status == "dead"
    assert after.current_tick == START_TICK + 3_600
    assert after.resources[QI_ACCOUNT_ID].balance == Decimal("1")


def test_unknown_policy_rejects_without_consuming_resources_or_time():
    state = _state()

    result = RuleEngine().evaluate(
        state,
        {"kind": "attempt_cultivation_breakthrough", "policy_id": "cultivation.not_registered"},
        _context(UUID(int=9001)),
    )

    assert result.outcome.status == "rejected"
    assert result.outcome.code == "cultivation_breakthrough_policy_unavailable"
    assert result.events == []
    assert result.elapsed_ticks == 0


@pytest.mark.parametrize(
    ("ruleset_version", "content_rules", "is_disabled"),
    [
        (LEGACY_RULESET_VERSION, {"magic": {"enabled": False}}, False),
        (MAGIC_GATE_RULESET_VERSION, {"magic": {"enabled": False}}, True),
        (MAGIC_GATE_RULESET_VERSION, {"magic": {"enabled": True}}, False),
        (MAGIC_GATE_RULESET_VERSION, {"magic": {"enabled": 0}}, False),
        (MAGIC_GATE_RULESET_VERSION, {"magic": {}}, False),
        (MAGIC_GATE_RULESET_VERSION, {}, False),
    ],
)
def test_only_the_opt_in_ruleset_honors_an_explicit_magic_disable(
    ruleset_version, content_rules, is_disabled,
):
    state = _state()
    state = state.model_copy(update={"metadata": {**state.metadata, "content_rules": content_rules}})
    context = _context(UUID(int=9100)).model_copy(update={"ruleset_version": ruleset_version})

    result = RuleEngine().evaluate(
        state,
        {"kind": "attempt_cultivation_breakthrough", "policy_id": POLICY_ID},
        context,
    )

    if is_disabled:
        assert result.outcome.status == "rejected"
        assert result.outcome.code == "cultivation_disabled_by_world_rules"
        assert result.events == []
        assert result.rolls == []
        assert result.elapsed_ticks == 0
    else:
        assert result.outcome.status in {"succeeded", "failed"}
        assert any(event.type == "core.cultivation_breakthrough_resolved.v1" for event in result.events)


def test_magic_gate_still_allows_ordinary_training_in_the_cultivation_skill():
    state = _state()
    state = state.model_copy(update={"metadata": {
        **state.metadata, "content_rules": {"magic": {"enabled": False}},
    }})
    context = _context(UUID(int=9101)).model_copy(update={"ruleset_version": MAGIC_GATE_RULESET_VERSION})

    result = RuleEngine().evaluate(
        state,
        {"kind": "train", "skill_id": CULTIVATION_PRACTICE_SKILL_ID, "duration_ticks": 300},
        context,
    )

    assert result.outcome.code == "training_completed"
    assert result.elapsed_ticks == 300


def test_duration_uses_normal_scheduled_boundary_gating_without_partial_costs():
    state = _state()
    state_payload = state.model_dump(mode="python")
    state_payload["scheduled_events"] = [ScheduledEventRecord(
        id=UUID(int=9010),
        due_tick=START_TICK + 1_800,
        phase="maintenance",
        handler_id="core.test",
        handler_version="1.0.0",
    )]
    blocked_state = WorldState.model_validate(state_payload)

    result = RuleEngine().evaluate(
        blocked_state,
        {"kind": "attempt_cultivation_breakthrough", "policy_id": POLICY_ID},
        _context(UUID(int=9011)),
    )

    assert result.outcome.code == "scheduled_boundary_required"
    assert result.outcome.status == "rejected"
    assert result.events == [] and result.elapsed_ticks == 0


def test_lifespan_expiry_resolves_only_at_the_exact_registered_completion_tick():
    state = _state()
    actor = state.entities[ACTOR_ID]
    updated_actor = actor.model_copy(update={"components": {
        **actor.components,
        "life": LifeComponent(birth_tick=START_TICK, lifespan_ticks=3_600).model_dump(mode="json"),
    }})
    expiry_state = WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": {**state.entities, ACTOR_ID: updated_actor},
    })
    command_id = _command_for(expiry_state, should_succeed=True)

    result = RuleEngine().evaluate(
        expiry_state,
        {"kind": "attempt_cultivation_breakthrough", "policy_id": POLICY_ID},
        _context(command_id),
    )

    assert result.elapsed_ticks == 3_600
    assert result.outcome.code == "lifespan_expired"
    assert any(event.type == "core.life_ended.v1" for event in result.events)
    after = _replay(expiry_state, result.events)
    assert after.current_tick == START_TICK + 3_600
    assert after.entities[ACTOR_ID].status == "dead"
    assert after.resources[QI_ACCOUNT_ID].balance == Decimal("1")


def test_active_combat_and_travel_block_breakthrough_before_time_or_costs():
    state = _state()
    actor = state.entities[ACTOR_ID]
    opponent_id = UUID(int=1010)
    encounter_id = UUID(int=1011)
    encounter_event_id = UUID(int=1012)
    actor_encounter = CombatEncounterComponent(
        encounter_id=encounter_id,
        opponent_id=opponent_id,
        location_id=PLACE_ID,
        started_tick=START_TICK,
        exchange_count=1,
        last_event_id=encounter_event_id,
        last_outcome="miss",
    )
    opponent_encounter = actor_encounter.model_copy(update={"opponent_id": ACTOR_ID})
    combat_state = WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": {
            **state.entities,
            ACTOR_ID: actor.model_copy(update={
                "components": {**actor.components, "combat_encounter": actor_encounter.model_dump(mode="json")},
            }),
            opponent_id: WorldEntity(
                id=opponent_id,
                kind="character",
                location_id=PLACE_ID,
                components={"combat_encounter": opponent_encounter.model_dump(mode="json")},
            ),
        },
    })
    blocked_combat = RuleEngine().evaluate(
        combat_state,
        {"kind": "attempt_cultivation_breakthrough", "policy_id": POLICY_ID},
        _context(UUID(int=9002)),
    )
    assert blocked_combat.outcome.code == "cultivation_breakthrough_combat_blocked"
    assert blocked_combat.events == [] and blocked_combat.elapsed_ticks == 0

    destination_id = UUID(int=1013)
    journey_id = UUID(int=1014)
    journey = TravelJourney(
        journey_id=journey_id,
        actor_id=ACTOR_ID,
        route_id=UUID(int=1015),
        from_place_id=PLACE_ID,
        to_place_id=destination_id,
        departed_tick=START_TICK,
        arrival_tick=10_000,
    )
    schedule = ScheduledEventRecord(
        id=travel_resolution_schedule_id(journey_id),
        due_tick=journey.arrival_tick,
        phase="resolution",
        handler_id=TRAVEL_RESOLUTION_HANDLER_ID,
        handler_version=TRAVEL_RESOLUTION_HANDLER_VERSION,
        payload={"journey_id": str(journey_id)},
    )
    traveling_actor = actor.model_copy(update={"location_id": None})
    travel_state = WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": {
            PLACE_ID: state.entities[PLACE_ID],
            ACTOR_ID: traveling_actor,
            destination_id: WorldEntity(id=destination_id, kind="place", components={"place": {"place_type": "site"}}),
        },
        "travel_journeys": [journey],
        "scheduled_events": [schedule],
    })
    blocked_travel = RuleEngine().evaluate(
        travel_state,
        {"kind": "attempt_cultivation_breakthrough", "policy_id": POLICY_ID},
        _context(UUID(int=9003)),
    )
    assert blocked_travel.outcome.code == "cultivation_breakthrough_travel_blocked"
    assert blocked_travel.events == [] and blocked_travel.elapsed_ticks == 0
