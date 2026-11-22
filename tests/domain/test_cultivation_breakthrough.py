from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import (
    CapabilitiesComponent,
    HealthComponent,
    InventoryComponent,
    InventoryLot,
    ProgressionComponent,
    SkillsComponent,
    SkillValue,
)
from world_engine.domain.cultivation_breakthrough import (
    CULTIVATION_BREAKTHROUGH_POLICY,
    CULTIVATION_FAILURE_INJURY_TYPE,
    CultivationBreakthroughError,
    CultivationBreakthroughPolicy,
    cultivation_breakthrough_effect_id,
    cultivation_breakthrough_policy_digest,
    cultivation_breakthrough_roll_digest,
    plan_cultivation_breakthrough,
    realm_milestone,
)
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState


WORLD_ID = UUID(int=1001)
BRANCH_ID = UUID(int=1002)
ACTOR_ID = UUID(int=1003)
RULESET = "1.0.0"
START_TICK = 100


def _policy(**updates) -> CultivationBreakthroughPolicy:
    values = {
        "policy_id": "cultivation.breakthrough.practice",
        "initial_realm": "mortal",
        "source_realm": "mortal",
        "target_realm": "river",
        "qi_cost": "1.25",
        "duration_ticks": 600,
        "minimum_practice_skill_level": "10",
        "base_chance_percent": 25,
        "skill_bonus_per_point": 2,
        "maximum_skill_bonus": 30,
        "failure_damage": 7,
    }
    values.update(updates)
    return CultivationBreakthroughPolicy(**values)


def _state(
    *,
    current_tick: int = START_TICK,
    actor_status: str = "active",
    capability_grants: list[str] | None = None,
    capability_restrictions: list[str] | None = None,
    skill_level: str = "12.5",
    milestones: list[str] | None = None,
    lot_quantities: tuple[str, str] = ("0.75", "1"),
    lot_expiry_ticks: tuple[int | None, int | None] = (None, None),
    include_sink: bool = True,
    health_current: int = 50,
) -> WorldState:
    account_ids = (UUID(int=2001), UUID(int=2002))
    lot_ids = (UUID(int=3001), UUID(int=3002))
    sink_id = UUID(int=2003)
    inventory = InventoryComponent(
        revision=3,
        lots=[
            InventoryLot(
                lot_id=lot_ids[index],
                account_id=account_ids[index],
                resource_id="cultivation.qi",
                quantity=lot_quantities[index],
                expiry_tick=lot_expiry_ticks[index],
            )
            for index in range(2)
        ],
    )
    realm_history = ["training:practice"] if milestones is None else milestones
    actor = WorldEntity(
        id=ACTOR_ID,
        kind="character",
        name="Aster",
        status=actor_status,
        components={
            "capabilities": CapabilitiesComponent(
                grants=capability_grants or ["cultivation.practice", "cultivation.meditate"],
                restrictions=capability_restrictions or [],
            ).model_dump(mode="json"),
            "skills": SkillsComponent(skills={
                "skill.cultivation_practice": SkillValue(level=skill_level),
            }).model_dump(mode="json"),
            "progression": ProgressionComponent(
                experience=100,
                level=4,
                milestones=realm_history,
                next_threshold=200,
            ).model_dump(mode="json"),
            "health": HealthComponent(current=health_current, maximum=100).model_dump(mode="json"),
            "inventory": inventory.model_dump(mode="json"),
        },
    )
    resources = {
        account_ids[0]: ResourceAccount(
            resource_id="cultivation.qi", balance=lot_quantities[0], owner_id=ACTOR_ID,
        ),
        account_ids[1]: ResourceAccount(
            resource_id="cultivation.qi", balance=lot_quantities[1], owner_id=ACTOR_ID,
        ),
    }
    if include_sink:
        resources[sink_id] = ResourceAccount(
            resource_id="cultivation.qi", balance="10", account_type="consumption_sink",
        )
    return WorldState(current_tick=current_tick, entities={ACTOR_ID: actor}, resources=resources)


def _plan(state: WorldState, command_id: UUID, policy: CultivationBreakthroughPolicy | None = None):
    return plan_cultivation_breakthrough(
        state,
        actor_id=ACTOR_ID,
        policy=policy or _policy(),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        ruleset_version=RULESET,
    )


def _command_for(state: WorldState, *, should_succeed: bool, policy: CultivationBreakthroughPolicy | None = None) -> UUID:
    policy = policy or _policy()
    actor = state.entities[ACTOR_ID]
    skill = SkillsComponent.model_validate(actor.components["skills"]).skills["skill.cultivation_practice"]
    bonus_steps = int((skill.level - policy.minimum_practice_skill_level).to_integral_value(rounding=ROUND_FLOOR))
    chance = max(5, min(95, policy.base_chance_percent + min(
        policy.maximum_skill_bonus,
        bonus_steps * policy.skill_bonus_per_point,
    )))
    for value in range(1, 10_000):
        command_id = UUID(int=value)
        digest = cultivation_breakthrough_roll_digest(
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=command_id,
            ruleset_version=RULESET,
        )
        passed = int(digest, 16) % 100 + 1 <= chance
        if passed == should_succeed:
            return command_id
    raise AssertionError("test seed search did not find the requested deterministic result")


def _with_actor_components(state: WorldState, **changes) -> WorldState:
    actor = state.entities[ACTOR_ID]
    updated_actor = actor.model_copy(update={"components": {**actor.components, **changes}})
    return WorldState.model_validate({
        **state.model_dump(mode="python"),
        "entities": {**state.entities, ACTOR_ID: updated_actor},
    })


def test_success_plan_is_immutable_fifo_exact_and_persists_only_realm_milestone():
    state = _state()
    original = state.model_dump(mode="json")
    policy = _policy()
    command_id = _command_for(state, should_succeed=True, policy=policy)

    plan = _plan(state, command_id, policy)

    assert plan.outcome == "succeeded"
    assert plan.started_tick == START_TICK
    assert plan.resolved_tick == START_TICK + policy.duration_ticks
    assert plan.duration_ticks == policy.duration_ticks
    assert plan.policy_id == policy.policy_id
    assert plan.policy_version == CULTIVATION_BREAKTHROUGH_POLICY
    assert plan.policy_digest == cultivation_breakthrough_policy_digest(policy)
    assert plan.initial_realm == "mortal"
    assert plan.attempt_id == cultivation_breakthrough_effect_id(command_id, RULESET, "attempt")
    assert plan.qi_consumption_event_id == cultivation_breakthrough_effect_id(command_id, RULESET, "qi_consumption")
    assert plan.progression_event_id == cultivation_breakthrough_effect_id(command_id, RULESET, "success")
    assert plan.failure_damage_event_id is None
    assert plan.roll <= plan.chance_percent
    assert plan.practice_skill_bonus == 4
    assert plan.qi_cost == Decimal("1.25")
    assert [(item.lot_id, item.quantity) for item in plan.consumed_lots] == [
        (UUID(int=3001), Decimal("0.75")),
        (UUID(int=3002), Decimal("0.50")),
    ]
    assert [(flow.holder_account_id, flow.quantity) for flow in plan.resource_flows] == [
        (UUID(int=2001), Decimal("0.75")),
        (UUID(int=2002), Decimal("0.50")),
    ]
    assert plan.resulting_inventory.revision == 4
    assert [(lot.lot_id, lot.quantity) for lot in plan.resulting_inventory.lots] == [
        (UUID(int=3002), Decimal("0.50")),
    ]
    assert plan.resulting_progression is not None
    assert plan.resulting_progression.revision == plan.expected_progression_revision + 1
    assert plan.resulting_progression.milestones == [
        "training:practice",
        "cultivation.realm:river",
    ]
    assert plan.failure_damage is None and plan.failure_injury_type is None
    assert all(item.expected_balance >= item.resulting_balance for item in plan.account_expectations if item.account_type == "holder")
    sink = next(item for item in plan.account_expectations if item.account_id == plan.sink_account_id)
    assert sink.expected_balance == Decimal("10")
    assert sink.resulting_balance == Decimal("11.25")
    assert state.model_dump(mode="json") == original
    assert _plan(state, command_id, policy) == plan


def test_failure_consumes_the_same_fifo_qi_and_exposes_bounded_damage_without_realm_update():
    state = _state(health_current=3)
    policy = _policy(failure_damage=15)
    command_id = _command_for(state, should_succeed=False, policy=policy)

    plan = _plan(state, command_id, policy)

    assert plan.outcome == "failed"
    assert plan.roll > plan.chance_percent
    assert plan.qi_cost == Decimal("1.25")
    assert [(item.lot_id, item.quantity) for item in plan.consumed_lots] == [
        (UUID(int=3001), Decimal("0.75")),
        (UUID(int=3002), Decimal("0.50")),
    ]
    assert sum((flow.quantity for flow in plan.resource_flows), Decimal("0")) == Decimal("1.25")
    assert plan.resulting_inventory.lots[0].quantity == Decimal("0.50")
    assert plan.resulting_progression is None and plan.progression_event_id is None
    assert plan.failure_damage_event_id == cultivation_breakthrough_effect_id(
        command_id, RULESET, "failure_damage",
    )
    assert plan.failure_damage == 15
    assert plan.failure_injury_type == CULTIVATION_FAILURE_INJURY_TYPE
    assert plan.expected_health_revision == 1
    assert _plan(state, command_id, policy) == plan


def test_latest_realm_marker_selects_source_and_absent_marker_uses_policy_initial_realm():
    initial = _state(milestones=["training:practice"])
    assert _plan(initial, _command_for(initial, should_succeed=True)).source_realm == "mortal"

    later_realm = _state(milestones=["cultivation.realm:old", "cultivation.realm:mortal"])
    assert _plan(later_realm, _command_for(later_realm, should_succeed=True)).source_realm == "mortal"


def test_unmarked_actor_cannot_skip_from_explicit_initial_realm_to_an_upper_stage():
    state = _state(milestones=["training:practice"])
    upper_stage_policy = _policy(
        initial_realm="mortal",
        source_realm="river",
        target_realm="peak",
    )

    with pytest.raises(CultivationBreakthroughError) as caught:
        _plan(state, UUID(int=8001), upper_stage_policy)

    assert caught.value.code == "source_realm_mismatch"


def test_qi_expiring_at_completion_is_skipped_but_a_lot_valid_through_completion_is_fifo_eligible():
    state = _state(
        lot_quantities=("0.75", "2"),
        lot_expiry_ticks=(START_TICK + 600, START_TICK + 601),
    )
    plan = _plan(state, _command_for(state, should_succeed=True))

    assert [(item.lot_id, item.quantity) for item in plan.consumed_lots] == [
        (UUID(int=3002), Decimal("1.25")),
    ]
    assert [(lot.lot_id, lot.quantity) for lot in plan.resulting_inventory.lots] == [
        (UUID(int=3001), Decimal("0.75")),
        (UUID(int=3002), Decimal("0.75")),
    ]


@pytest.mark.parametrize(
    ("state_changes", "policy_changes", "expected_code"),
    [
        ({"actor_status": "dead", "health_current": 0}, {}, "actor_unavailable"),
        ({"capability_grants": ["cultivation.meditate"]}, {}, "cultivation_capability_missing"),
        ({"capability_restrictions": ["cultivation.practice"]}, {}, "cultivation_capability_missing"),
        ({"skill_level": "9.99"}, {}, "practice_skill_insufficient"),
        ({"milestones": ["cultivation.realm:older"]}, {}, "source_realm_mismatch"),
        ({"milestones": ["cultivation.realm:river", "cultivation.realm:mortal"]}, {}, "target_realm_already_reached"),
        ({"lot_quantities": ("0.5", "0.5")}, {}, "qi_unavailable_at_completion"),
        ({"lot_expiry_ticks": (START_TICK + 600, START_TICK + 600)}, {}, "qi_unavailable_at_completion"),
        ({"include_sink": False}, {}, "qi_sink_missing"),
        ({}, {"minimum_practice_skill_level": "13"}, "practice_skill_insufficient"),
    ],
)
def test_strict_prerequisites_reject_without_planning(
    state_changes: dict[str, object], policy_changes: dict[str, object], expected_code: str,
):
    state = _state(**state_changes)
    with pytest.raises(CultivationBreakthroughError) as caught:
        _plan(state, UUID(int=9001), _policy(**policy_changes))

    assert caught.value.code == expected_code


def test_policy_and_milestone_lengths_are_bounded():
    with pytest.raises(ValidationError):
        _policy(target_realm="r" * 143)
    with pytest.raises(ValidationError):
        _policy(duration_ticks=2**31)
    with pytest.raises(ValidationError):
        _policy(qi_resource_id="cultivation.spirit_stone")
    with pytest.raises(ValueError, match="length cap"):
        realm_milestone("r" * 143)
