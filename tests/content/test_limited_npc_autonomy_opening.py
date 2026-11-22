from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from world_engine.content.catalog import ContentCatalog
from world_engine.content.npc_autonomy_opening import (
    NPC_AUTONOMY_LIMITED_PROFILE,
    NPC_AUTONOMY_PROFILE_KEY,
    NPC_GOAL_COMPLETION_POLICY,
    NPC_SHARED_TRADE_POLICY,
    NPC_TRADE_TICKS,
    enable_limited_npc_autonomy,
)
from world_engine.domain.components import MarketComponent
from world_engine.domain.models import WorldState
from world_engine.domain.npc_plan_review import (
    NPC_PLAN_REVIEW_HANDLER_ID,
    build_npc_plan_review_schedule,
)
from world_engine.domain.npc_plans import NpcPlanStep
from world_engine.domain.reducers import apply_event
from world_engine.rules.contracts import RuleContext
from world_engine.rules.engine import RULES_VERSION
from world_engine.simulation import SimulationEngine, WorldAdvancePlan
from world_engine.simulation.contracts import SimulationContext
from world_engine.simulation.npc_capabilities import (
    build_default_npc_capability_registry,
    preflight_npc_plan_step,
)


@pytest.fixture(scope="module")
def opening_worlds():
    catalog = ContentCatalog()
    return {
        pack_id: catalog.build_world(pack_id, "Limited autonomy opening", "Opening Player")
        for pack_id in ("low_magic_feudal", "modern_city", "long_life_cultivation")
    }


def _review_schedules(state: WorldState):
    return sorted(
        (event for event in state.scheduled_events if event.handler_id == NPC_PLAN_REVIEW_HANDLER_ID),
        key=lambda event: (event.due_tick, str(event.id)),
    )


def _actor_and_reserve_goal(state: WorldState, schedule):
    actor_id = UUID(schedule.payload["actor_id"])
    actor = state.entities[actor_id]
    assert actor.status == "active"
    assert actor_id != state.player_id
    assert actor_id in {UUID(value) for value in state.metadata["content_index"]["other_resident_ids"]}
    goal, = [goal for goal in state.goals if goal.owner_id == actor_id and goal.status == "active"]
    condition, = goal.conditions
    assert condition.kind == "resource_balance_minimum"
    assert condition.subject_id == actor_id
    balance = sum(
        (
            account.balance for account in state.resources.values()
            if account.owner_id == actor_id
            and account.account_type == "holder"
            and account.resource_id == condition.resource_id
        ),
        start=0,
    )
    assert balance < condition.quantity
    return actor, goal


@pytest.mark.parametrize("pack_id", ["low_magic_feudal", "modern_city", "long_life_cultivation"])
def test_limited_opening_seeds_only_unmet_reachable_resident_goals(opening_worlds, pack_id):
    draft = opening_worlds[pack_id]
    source = draft.state
    opted_in = enable_limited_npc_autonomy(source, world_id=draft.world_id)

    assert source.metadata.get(NPC_AUTONOMY_PROFILE_KEY) is None
    assert source.metadata.get(NPC_SHARED_TRADE_POLICY) is None
    assert source.metadata.get(NPC_GOAL_COMPLETION_POLICY) is None
    assert source.scheduled_events == [event for event in source.scheduled_events
                                       if event.handler_id != NPC_PLAN_REVIEW_HANDLER_ID]
    assert opted_in.current_tick == 0
    assert opted_in.metadata[NPC_AUTONOMY_PROFILE_KEY] == NPC_AUTONOMY_LIMITED_PROFILE
    assert opted_in.metadata[NPC_SHARED_TRADE_POLICY] is True
    assert opted_in.metadata[NPC_GOAL_COMPLETION_POLICY] is True
    assert len(opted_in.entities) == len(source.entities)
    assert opted_in.goals == source.goals
    assert opted_in.resources == source.resources

    schedules = _review_schedules(opted_in)
    assert len(schedules) == 3
    assert [item.due_tick for item in schedules] == [
        source.calendar.day_seconds,
        3 * source.calendar.day_seconds,
        7 * source.calendar.day_seconds,
    ]
    assert all(item.max_runs == 1 and item.repeat_ticks is None for item in schedules)

    registry = build_default_npc_capability_registry()
    assert "world.action.trade.v1" in registry
    for schedule in schedules:
        actor, goal = _actor_and_reserve_goal(opted_in, schedule)
        place = opted_in.entities[actor.location_id]
        market = MarketComponent.model_validate(place.components.get("market"))
        condition = goal.conditions[0]
        offer = next(
            offer for offer in market.offers
            if offer.active
            and offer.resource_id == condition.resource_id
            and offer.seller_id != actor.id
        )
        step = NpcPlanStep(
            capability_id="world.action.trade.v1",
            arguments={
                "listing_id": str(offer.listing_id),
                "quantity": format(offer.minimum_quantity, "f"),
            },
        )
        preflight = preflight_npc_plan_step(
            opted_in,
            actor.id,
            step,
            RuleContext(
                world_id=draft.world_id,
                branch_id=draft.branch_id,
                command_id=uuid4(),
                actor_id=actor.id,
                ruleset_version=RULES_VERSION,
            ),
            registry=registry,
        )
        assert preflight.status == "interval_required"
        assert preflight.required_ticks == NPC_TRADE_TICKS
        assert preflight.resolution.outcome.status == "succeeded"

    repeated = enable_limited_npc_autonomy(source, world_id=draft.world_id)
    assert repeated == opted_in
    with pytest.raises(ValueError, match="already been applied"):
        enable_limited_npc_autonomy(opted_in, world_id=draft.world_id)


def test_existing_author_review_is_preserved_and_actor_is_not_reenrolled(opening_worlds):
    draft = opening_worlds["modern_city"]
    source = draft.state
    candidate_id = UUID(source.metadata["content_index"]["other_resident_ids"][0])
    authored = build_npc_plan_review_schedule(
        source,
        draft.world_id,
        candidate_id,
        12 * source.calendar.day_seconds,
        max_runs=1,
    )
    source_with_author_schedule = WorldState.model_validate(
        source.model_dump(mode="python")
        | {"scheduled_events": [*source.scheduled_events, authored]},
    )

    opted_in = enable_limited_npc_autonomy(source_with_author_schedule, world_id=draft.world_id)
    schedules = _review_schedules(opted_in)
    assert authored in schedules
    assert sum(item.id == authored.id for item in schedules) == 1
    assert candidate_id not in {UUID(item.payload["actor_id"]) for item in schedules if item.id != authored.id}
    assert [item.due_tick for item in schedules if item.id != authored.id] == [
        source.calendar.day_seconds,
        3 * source.calendar.day_seconds,
        7 * source.calendar.day_seconds,
    ]


def test_simulation_engine_surfaces_opt_in_review_at_its_staggered_boundary(opening_worlds):
    draft = opening_worlds["modern_city"]
    state = enable_limited_npc_autonomy(draft.state, world_id=draft.world_id)
    schedule = _review_schedules(state)[0]
    context = SimulationContext(
        world_id=draft.world_id,
        branch_id=draft.branch_id,
        command_id=uuid4(),
        command_actor_id=uuid4(),
        state_version=0,
        world_seed="limited-v1-boundary",
    )
    engine = SimulationEngine()
    plan = WorldAdvancePlan(target_tick=schedule.due_tick)

    first = engine.plan_window(state, plan, context)
    assert first.outcome == "advanced"
    assert first.next_tick == schedule.due_tick
    assert first.metrics.to_tick == schedule.due_tick
    assert first.metrics.integration_boundary == "scheduled_decision_requires_resolution"
    advanced = state
    for event in first.events:
        advanced = apply_event(advanced, event)
    assert advanced.current_tick == schedule.due_tick

    pending = engine.plan_window(advanced, plan, context)
    request, = pending.decision_requests
    assert pending.outcome == "decision_required"
    assert request.kind == "npc.plan_review"
    assert request.actor_id == UUID(schedule.payload["actor_id"])
    assert request.at_tick == schedule.due_tick
    assert pending.events == ()
    assert advanced.current_tick == schedule.due_tick
