from __future__ import annotations

from uuid import UUID, uuid5

import pytest

from world_engine.domain.components import (
    CapabilitiesComponent,
    HealthComponent,
    InventoryComponent,
    InventoryLot,
    LifeComponent,
    NeedsComponent,
    PopulationComponent,
    ProgressionComponent,
    SkillsComponent,
    SkillValue,
)
from world_engine.domain.cultivation_breakthrough import (
    CULTIVATION_FAILURE_INJURY_TYPE,
    cultivation_breakthrough_policies,
    plan_cultivation_breakthrough,
)
from world_engine.domain.cultivation_breakthrough_events import (
    CultivationBreakthroughResolvedEvent,
    breakthrough_effect_events,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import parse_event
from world_engine.domain.gameplay_events import DamageAppliedEvent, LifeEndedEvent
from world_engine.domain.models import ResourceAccount, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.domain.treatment_validation import validate_treatment_batch
from world_engine.rules import RuleContext, RuleEngine


WORLD_ID = UUID(int=71)
BRANCH_ID = UUID(int=72)
ACTOR_ID = UUID(int=73)
COHORT_ID = UUID(int=74)
HOLDER_ID = UUID(int=75)
SINK_ID = UUID(int=76)
LOT_ID = UUID(int=77)
START_TICK = 10_000
RULESET = "1.0.0"
POLICY_ID = "cultivation.qi_gathering.v1"


def _state(*, health: int = 50, population: bool = False, lifespan: bool = False) -> WorldState:
    life = None
    if population or lifespan:
        life = LifeComponent(
            birth_tick=START_TICK,
            lifespan_ticks=3600 if lifespan else None,
            population_group_id=COHORT_ID if population else None,
        )
    inventory = InventoryComponent(lots=[InventoryLot(
        lot_id=LOT_ID,
        account_id=HOLDER_ID,
        resource_id="cultivation.qi",
        quantity="2",
    )])
    components = {
        "capabilities": CapabilitiesComponent(grants=["cultivation.practice"]).model_dump(mode="json"),
        "skills": SkillsComponent(skills={
            "skill.cultivation_practice": SkillValue(level="100"),
        }).model_dump(mode="json"),
        "progression": ProgressionComponent(milestones=["training:practice"]).model_dump(mode="json"),
        "health": HealthComponent(current=health, maximum=100).model_dump(mode="json"),
        "inventory": inventory.model_dump(mode="json"),
    }
    if life is not None:
        components["life"] = life.model_dump(mode="json")
    entities = {
        ACTOR_ID: WorldEntity(
            id=ACTOR_ID,
            kind="character",
            components=components,
        ),
    }
    if population:
        entities[COHORT_ID] = WorldEntity(
            id=COHORT_ID,
            kind="cohort",
            components={"population": PopulationComponent(
                species_id="core.human", headcount=4, unmodeled_headcount=3,
            ).model_dump(mode="json")},
        )
    return WorldState(
        current_tick=START_TICK,
        entities=entities,
        resources={
            HOLDER_ID: ResourceAccount(resource_id="cultivation.qi", balance="2", owner_id=ACTOR_ID),
            SINK_ID: ResourceAccount(resource_id="cultivation.qi", balance="0", account_type="consumption_sink"),
        },
        metadata={"pack_id": "long_life_cultivation"},
    )


def _plan(state: WorldState, *, outcome: str, command_id: UUID | None = None):
    policy = next(row for row in cultivation_breakthrough_policies(state) if row.policy_id == POLICY_ID)
    for value in range(1, 1000):
        candidate_id = command_id or UUID(int=10_000 + value)
        candidate = plan_cultivation_breakthrough(
            state,
            actor_id=ACTOR_ID,
            policy=policy,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=candidate_id,
            ruleset_version=RULESET,
        )
        if candidate.outcome == outcome:
            return candidate
        if command_id is not None:
            break
    raise AssertionError(f"could not find a deterministic {outcome} seed")


def _events(state: WorldState, *, outcome: str, health: int = 50, population: bool = False):
    plan = _plan(state, outcome=outcome)
    return plan, [
        TimeAdvancedEvent(to_tick=plan.resolved_tick),
        *breakthrough_effect_events(state, plan),
    ]


def _validate(state: WorldState, plan, events) -> None:
    validate_treatment_batch(
        events,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=plan.command_id,
        ruleset_version=RULESET,
        actor_id=ACTOR_ID,
        command_type="action.attempt_cultivation_breakthrough",
        command_payload={"kind": "attempt_cultivation_breakthrough", "policy_id": POLICY_ID},
        state=state,
    )


def test_success_event_is_registered_private_and_reducer_applies_progression_after_exact_cost():
    state = _state()
    plan, events = _events(state, outcome="succeeded")
    outcome = events[3]

    assert isinstance(outcome, CultivationBreakthroughResolvedEvent)
    assert outcome.plan == plan
    assert outcome.visibility == {"policy": "subject_ids", "subject_ids": [str(ACTOR_ID)]}
    assert parse_event(outcome.model_dump(mode="json")) == outcome
    _validate(state, plan, events)

    resolved = state
    for event in events:
        resolved = apply_event(resolved, event)
    progression = ProgressionComponent.model_validate(resolved.entities[ACTOR_ID].components["progression"])
    assert progression.revision == 2
    assert progression.milestones[-1] == "cultivation.realm:qi_gathering"
    assert resolved.resources[SINK_ID].balance == 1


def test_failure_emits_exact_private_strain_and_correct_population_revision_for_lethal_damage():
    state = _state(health=3, population=True)
    plan, events = _events(state, outcome="failed")
    damage = events[-1]

    assert isinstance(damage, DamageAppliedEvent)
    assert damage.damage == plan.failure_damage
    assert damage.injury_type == CULTIVATION_FAILURE_INJURY_TYPE
    assert damage.source_event_ids == []
    assert damage.expected_population_revision == 1
    assert damage.expected_health_revision == plan.expected_health_revision
    assert damage.visibility == {"policy": "subject_ids", "subject_ids": [str(ACTOR_ID)]}
    _validate(state, plan, events)

    resolved = state
    for event in events:
        resolved = apply_event(resolved, event)
    assert resolved.entities[ACTOR_ID].status == "dead"
    assert PopulationComponent.model_validate(resolved.entities[COHORT_ID].components["population"]).headcount == 3


def test_failure_batch_accepts_the_registered_actor_lifespan_companion():
    state = _state(lifespan=True)
    plan, events = _events(state, outcome="failed")
    events.append(LifeEndedEvent(
        event_id=uuid5(plan.command_id, f"{RULESET}:life.expired"),
        entity_id=ACTOR_ID,
        expected_life_revision=1,
    ))

    _validate(state, plan, events)


def test_rule_engine_timed_needs_companion_passes_replay_validation():
    state = _state()
    actor = state.entities[ACTOR_ID]
    actor_with_needs = actor.model_copy(update={
        "components": {
            **actor.components,
            "needs": NeedsComponent(
                food={"value": 80}, water={"value": 80}, rest={"value": 80},
            ).model_dump(mode="json"),
        },
    })
    state = WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": {**state.entities, ACTOR_ID: actor_with_needs},
    })
    plan = _plan(state, outcome="succeeded")
    result = RuleEngine().evaluate(
        state,
        {"kind": "attempt_cultivation_breakthrough", "policy_id": POLICY_ID},
        RuleContext(
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=plan.command_id,
            actor_id=ACTOR_ID,
            ruleset_version=RULESET,
        ),
    )

    assert result.outcome.status == "succeeded"
    _validate(state, plan, result.events)


@pytest.mark.parametrize("mutation", ["missing_outcome", "wrong_clock", "wrong_inventory", "wrong_damage", "extra_qi"])
def test_breakthrough_batch_rejects_missing_or_substituted_registered_effects(mutation: str):
    state = _state(health=3)
    plan, events = _events(state, outcome="failed")
    changed = list(events)
    if mutation == "missing_outcome":
        changed.pop(3)
    elif mutation == "wrong_clock":
        changed[0] = changed[0].model_copy(update={"to_tick": plan.resolved_tick + 1})
    elif mutation == "wrong_inventory":
        changed[1] = changed[1].model_copy(update={"expected_revision": plan.expected_inventory_revision + 1})
    elif mutation == "wrong_damage":
        changed[-1] = changed[-1].model_copy(update={"damage": plan.failure_damage + 1})
    else:
        from world_engine.domain.gameplay_events import ResourceConsumedEvent

        changed.append(ResourceConsumedEvent(
            consumer_id=ACTOR_ID,
            purpose="cultivation_breakthrough",
            flows=[{"holder_account_id": HOLDER_ID, "sink_account_id": SINK_ID,
                    "resource_id": "cultivation.qi", "quantity": "1"}],
        ))

    with pytest.raises(InvalidEvent):
        _validate(state, plan, changed)
