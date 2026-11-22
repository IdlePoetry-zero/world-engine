from __future__ import annotations

from uuid import UUID, uuid4, uuid5

import pytest

from world_engine.content.catalog import ContentCatalog
from world_engine.content.npc_autonomy_opening import (
    NPC_AUTONOMY_DIVERSE_PROFILE,
    NPC_AUTONOMY_PROFILE_KEY,
    NPC_GOAL_COMPLETION_POLICY,
    NPC_SHARED_TRADE_POLICY,
    NPC_TRADE_TICKS,
    enable_diverse_npc_autonomy,
)
from world_engine.domain.components import GoalCondition, MarketComponent, PlaceComponent
from world_engine.domain.models import WorldState
from world_engine.domain.npc_plan_review import (
    NPC_PLAN_REVIEW_HANDLER_ID,
    build_npc_plan_review_schedule,
)
from world_engine.domain.npc_plans import NPC_SHARED_TRAINING_POLICY, NpcPlanStep
from world_engine.domain.reducers import state_hash
from world_engine.rules.contracts import RuleContext
from world_engine.rules.engine import RULES_VERSION
from world_engine.simulation.npc_capabilities import (
    build_default_npc_capability_registry,
    preflight_npc_plan_step,
)


_PACK_CASES = (
    ("low_magic_feudal", "2.0.0"),
    ("modern_city", "2.0.0"),
    ("long_life_cultivation", "2.0.0"),
    ("low_magic_feudal", "2.8.0"),
    ("modern_city", "2.8.0"),
    ("long_life_cultivation", "2.8.0"),
    ("low_magic_feudal", "2.9.0"),
)


@pytest.fixture(scope="module")
def source_worlds():
    catalog = ContentCatalog()
    return {
        key: catalog.build_world(
            pack_id,
            f"Diverse autonomy {pack_id} {version}",
            "Opening Player",
            seed=20261121,
            population_mode="sustained",
            pack_version=version,
        )
        for key, (pack_id, version) in enumerate(_PACK_CASES)
    }


def _review_schedules(state: WorldState):
    return sorted(
        (item for item in state.scheduled_events if item.handler_id == NPC_PLAN_REVIEW_HANDLER_ID),
        key=lambda item: (item.due_tick, str(item.id)),
    )


def _preflight_trade(state: WorldState, world_id, actor_id: UUID):
    actor = state.entities[actor_id]
    place = state.entities[actor.location_id]
    market = MarketComponent.model_validate(place.components.get("market"))
    goals = [
        goal for goal in state.goals
        if goal.owner_id == actor_id and goal.status == "active"
        and len(goal.conditions) == 1 and goal.conditions[0].kind == "resource_balance_minimum"
    ]
    assert len(goals) == 1
    condition = goals[0].conditions[0]
    offer = next(
        item for item in sorted(market.offers, key=lambda row: str(row.listing_id))
        if item.active and item.resource_id == condition.resource_id and item.seller_id != actor_id
        and item.minimum_quantity <= condition.quantity
    )
    result = preflight_npc_plan_step(
        state,
        actor_id,
        NpcPlanStep(
            capability_id="world.action.trade.v1",
            arguments={"listing_id": str(offer.listing_id), "quantity": format(offer.minimum_quantity, "f")},
        ),
        RuleContext(
            world_id=world_id,
            branch_id=uuid5(world_id, "diverse-profile-test:branch"),
            command_id=uuid4(),
            actor_id=actor_id,
            ruleset_version=RULES_VERSION,
        ),
        registry=build_default_npc_capability_registry(),
    )
    assert result.status == "interval_required"
    assert result.required_ticks == NPC_TRADE_TICKS
    assert result.resolution.outcome.status == "succeeded"


@pytest.mark.parametrize("case_index", range(len(_PACK_CASES)))
def test_diverse_profile_adds_one_reachable_goal_and_two_reserve_reviews(source_worlds, case_index):
    draft = source_worlds[case_index]
    source = draft.state
    world_id = uuid5(draft.world_id, "diverse-profile-authoritative-world")
    original_hash = state_hash(source)
    original_goal_ids = {goal.id for goal in source.goals}
    original_review_schedules = _review_schedules(source)

    opted_in = enable_diverse_npc_autonomy(source, world_id=world_id)
    repeated = enable_diverse_npc_autonomy(source, world_id=world_id)

    assert state_hash(source) == original_hash
    assert opted_in == repeated
    assert opted_in.current_tick == source.current_tick == 0
    assert opted_in.metadata[NPC_AUTONOMY_PROFILE_KEY] == NPC_AUTONOMY_DIVERSE_PROFILE
    assert opted_in.metadata[NPC_SHARED_TRADE_POLICY] is True
    assert opted_in.metadata[NPC_GOAL_COMPLETION_POLICY] is True
    assert opted_in.metadata.get(NPC_SHARED_TRAINING_POLICY) is None
    assert opted_in.resources == source.resources
    assert len(opted_in.entities) == len(source.entities)
    assert len(opted_in.goals) == len(source.goals) + 1
    assert all(goal.id in {item.id for item in opted_in.goals} for goal in source.goals)

    schedules = _review_schedules(opted_in)
    assert len(original_review_schedules) == 0
    assert len(schedules) == 3
    assert [item.due_tick for item in schedules] == [
        source.calendar.day_seconds,
        3 * source.calendar.day_seconds,
        7 * source.calendar.day_seconds,
    ]
    assert all(item.max_runs == 1 and item.repeat_ticks is None for item in schedules)

    actor_ids = [UUID(item.payload["actor_id"]) for item in schedules]
    assert len(actor_ids) == len(set(actor_ids))
    assert source.player_id not in actor_ids
    travel_actor_id = actor_ids[0]
    key_ids = {UUID(value) for value in source.metadata["content_index"]["key_npc_ids"]}
    resident_ids = {UUID(value) for value in source.metadata["content_index"]["other_resident_ids"]}
    assert travel_actor_id in key_ids
    reserve_actor_ids = set(actor_ids[1:])
    assert reserve_actor_ids <= resident_ids
    assert travel_actor_id not in reserve_actor_ids

    travel_actor = opted_in.entities[travel_actor_id]
    source_travel_actor = source.entities[travel_actor_id]
    assert travel_actor.components["capabilities"] == source_travel_actor.components["capabilities"]
    travel_goals = [
        goal for goal in opted_in.goals
        if goal.owner_id == travel_actor_id
        and goal.id not in original_goal_ids
        and goal.status == "active"
        and len(goal.conditions) == 1
        and goal.conditions[0].kind == "reach_location"
    ]
    milestone, = travel_goals
    condition = milestone.conditions[0]
    assert condition == GoalCondition(
        kind="reach_location", subject_id=travel_actor_id, target_id=condition.target_id,
    )
    assert milestone.priority == 90
    original_prose_goals = [goal for goal in source.goals if goal.owner_id == travel_actor_id]
    assert len(original_prose_goals) == 1
    assert next(goal for goal in opted_in.goals if goal.id == original_prose_goals[0].id) == original_prose_goals[0]

    origin = opted_in.entities[travel_actor.location_id]
    origin_place = PlaceComponent.model_validate(origin.components.get("place"))
    route_id_text = f"route ID "
    route_id = UUID(milestone.description.split(route_id_text, 1)[1].split(")", 1)[0])
    route = next(item for item in origin_place.routes if item.route_id == route_id)
    destination = opted_in.entities[condition.target_id]
    assert route.to_place_id == destination.id
    assert destination.components.get("hidden") is not True or destination.id in {
        UUID(place_id) for place_id in travel_actor.components.get("known_places", [])
    }
    preflight = preflight_npc_plan_step(
        opted_in,
        travel_actor_id,
        NpcPlanStep(
            capability_id="world.action.move.v1",
            arguments={"destination_id": str(destination.id), "route_id": str(route.route_id)},
        ),
        RuleContext(
            world_id=world_id,
            branch_id=uuid5(world_id, "diverse-profile-test:branch"),
            command_id=uuid4(),
            actor_id=travel_actor_id,
            ruleset_version=RULES_VERSION,
        ),
        registry=build_default_npc_capability_registry(),
    )
    assert preflight.status == "journey_start"
    assert preflight.action_kind == "move"

    for reserve_actor_id in reserve_actor_ids:
        actor = opted_in.entities[reserve_actor_id]
        assert actor.components["capabilities"] == source.entities[reserve_actor_id].components["capabilities"]
        _preflight_trade(opted_in, world_id, reserve_actor_id)


def test_diverse_profile_rejects_existing_world_profiles_and_reviews(source_worlds):
    source = source_worlds[0].state
    world_id = uuid4()

    with pytest.raises(ValueError, match="new-world genesis"):
        enable_diverse_npc_autonomy(source.model_copy(update={"current_tick": 1}), world_id=world_id)

    limited_profiled = WorldState.model_validate(
        source.model_dump(mode="python")
        | {"metadata": {**source.metadata, NPC_AUTONOMY_PROFILE_KEY: "limited_v1"}},
    )
    with pytest.raises(ValueError, match="already been applied"):
        enable_diverse_npc_autonomy(limited_profiled, world_id=world_id)

    key_actor = source.entities[UUID(source.metadata["content_index"]["key_npc_ids"][0])]
    author_review = build_npc_plan_review_schedule(
        source, world_id, key_actor.id, source.calendar.day_seconds, max_runs=1,
    )
    source_with_review = WorldState.model_validate(
        source.model_dump(mode="python")
        | {"scheduled_events": [*source.scheduled_events, author_review]},
    )
    with pytest.raises(ValueError, match="fresh genesis without existing NPC plans or reviews"):
        enable_diverse_npc_autonomy(source_with_review, world_id=world_id)


def test_diverse_profile_rejects_unsupported_custom_background(source_worlds):
    source = source_worlds[0].state
    custom = WorldState.model_validate(
        source.model_dump(mode="python")
        | {"metadata": {**source.metadata, "pack_id": "custom_pack"}},
    )
    with pytest.raises(ValueError, match="three shipped full-content backgrounds"):
        enable_diverse_npc_autonomy(custom, world_id=uuid4())
