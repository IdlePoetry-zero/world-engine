"""Opt-in, bounded NPC autonomy settings for a newly seeded full world."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid5

from world_engine.domain.components import GoalCondition, GoalRecord, MarketComponent, PlaceComponent
from world_engine.domain.investigation import entity_visible_to_actor
from world_engine.domain.models import WorldState
from world_engine.domain.npc_goal_completion import NPC_GOAL_COMPLETION_POLICY
from world_engine.domain.npc_plan_review import (
    NPC_PLAN_REVIEW_HANDLER_ID,
    NPC_STANDING_REVIEW_POLICY,
    build_npc_plan_review_schedule,
)
from world_engine.domain.npc_plans import (
    NPC_SHARED_TRADE_POLICY,
    NPC_TRADE_TICKS,
    NpcPlanStep,
)
from world_engine.rules.contracts import RuleContext
from world_engine.rules.engine import RULES_VERSION
from world_engine.simulation.npc_capabilities import (
    build_default_npc_capability_registry,
    preflight_npc_plan_step,
)


NPC_AUTONOMY_PROFILE_KEY = "npc_autonomy_profile"
NPC_AUTONOMY_LIMITED_PROFILE = "limited_v1"
NPC_AUTONOMY_DIVERSE_PROFILE = "diverse_v1"
NPC_AUTONOMY_LIMITED_COHORT_SIZE = 3
NPC_AUTONOMY_LIMITED_REVIEW_DAY_OFFSETS = (1, 3, 7)
NPC_AUTONOMY_DIVERSE_REVIEW_DAY_OFFSETS = (1, 3, 7)
NPC_AUTONOMY_DIVERSE_RESERVE_COUNT = 2
_CHARACTER_KINDS = frozenset({"character", "person"})
_PLACE_KINDS = frozenset({"place", "location"})
_ACTIVE_PLAN_STATUSES = frozenset({"ready", "running", "blocked"})
_DIVERSE_TRAVEL_PROFILES = {
    "low_magic_feudal": {
        "role": "courier",
        "source_goal_title": "Deliver an overdue letter",
        "milestone_title": "Reach {destination} to deliver the overdue letter",
        "purpose": "Reach a delivery stop on an open route",
    },
    "modern_city": {
        "role": "trail steward",
        "source_goal_title": "Reopen a public path",
        "milestone_title": "Reach {destination} to inspect the public path",
        "purpose": "Reach a route checkpoint for the public path review",
    },
    "long_life_cultivation": {
        "role": "mountain courier",
        "source_goal_title": "Confirm an arrival",
        "milestone_title": "Reach {destination} to confirm the courier handoff",
        "purpose": "Reach a courier handoff point on an open route",
    },
}


def add_standing_npc_reviews(state: WorldState, *, world_id: UUID) -> WorldState:
    """Stagger normal reviews for every named NPC with a standing goal."""
    if state.metadata.get(NPC_STANDING_REVIEW_POLICY) is not True:
        return state
    index = state.metadata.get("content_index", {})
    leading = {UUID(value) for value in index.get("key_npc_ids", ())}
    scheduled_actors = {
        item.payload["actor_id"] for item in state.scheduled_events if item.handler_id == NPC_PLAN_REVIEW_HANDLER_ID
    }
    new_reviews = []
    day = state.calendar.day_seconds
    candidates = {
        goal.owner_id for goal in state.goals if goal.status == "active" and goal.owner_id != state.player_id
    }
    for actor_id in sorted(candidates, key=str):
        actor = state.entities.get(actor_id)
        if (
            actor is None or actor.kind not in _CHARACTER_KINDS or not actor.name
            or actor.status != "active" or str(actor_id) in scheduled_actors
        ):
            continue
        phase = uuid5(actor_id, NPC_STANDING_REVIEW_POLICY).int
        key_npc = actor_id in leading
        cadence = (24 + phase % 19) if key_npc else (72 + phase % 47)
        first = (1 + phase % cadence) * day + (8 + phase % 11) * day // 24
        new_reviews.append(build_npc_plan_review_schedule(
            state, world_id, actor_id, state.current_tick + first, repeat_ticks=cadence * day, max_runs=10_000,
        ))
    return state.model_copy(update={"scheduled_events": [*state.scheduled_events, *new_reviews]})


def enable_limited_npc_autonomy(state: WorldState, *, world_id: UUID) -> WorldState:
    """Enable three bounded, staggered NPC trade reviews on a fresh content seed.

    Only named non-player residents with an active, unmet resource-reserve goal
    and a legal purchase at their current market are enrolled. The schedule is
    finite: each selected character receives one review. Existing NPC review
    schedules and plans are left untouched; this helper is for genesis only.
    """
    if not isinstance(state, WorldState):
        raise TypeError("limited NPC autonomy requires a WorldState")
    if not isinstance(world_id, UUID):
        raise TypeError("limited NPC autonomy requires the authoritative world UUID")
    if state.current_tick != 0:
        raise ValueError("limited NPC autonomy can only be enabled at new-world genesis")
    if state.metadata.get(NPC_AUTONOMY_PROFILE_KEY) is not None:
        raise ValueError("an NPC autonomy profile has already been applied to this world")

    content_index = state.metadata.get("content_index")
    if not isinstance(content_index, dict):
        raise ValueError("limited NPC autonomy requires a shipped full-content seed")
    resident_ids = content_index.get("other_resident_ids")
    if not isinstance(resident_ids, list):
        raise ValueError("limited NPC autonomy requires named resident IDs in the content index")

    metadata = dict(state.metadata)
    metadata.update({
        NPC_AUTONOMY_PROFILE_KEY: NPC_AUTONOMY_LIMITED_PROFILE,
        NPC_SHARED_TRADE_POLICY: True,
        NPC_GOAL_COMPLETION_POLICY: True,
    })
    planning_state = WorldState.model_validate(
        state.model_dump(mode="python") | {"metadata": metadata},
    )

    registry = build_default_npc_capability_registry()
    candidates: list[UUID] = []
    for actor_id in sorted({UUID(str(value)) for value in resident_ids}, key=str):
        actor = planning_state.entities.get(actor_id)
        if (
            actor is None
            or actor.kind not in _CHARACTER_KINDS
            or actor.status != "active"
            or actor_id == planning_state.player_id
            or _has_in_progress_plan(planning_state, actor_id)
            or _has_any_review_schedule(planning_state, actor_id)
        ):
            continue
        if _has_actionable_reserve_goal(planning_state, world_id, actor_id, registry):
            candidates.append(actor_id)
            if len(candidates) == NPC_AUTONOMY_LIMITED_COHORT_SIZE:
                break

    if not candidates:
        raise ValueError("limited NPC autonomy found no resident with an actionable local reserve goal")

    schedules = []
    for actor_id, offset_days in zip(candidates, NPC_AUTONOMY_LIMITED_REVIEW_DAY_OFFSETS, strict=False):
        due_tick = state.current_tick + offset_days * state.calendar.day_seconds
        schedules.append(build_npc_plan_review_schedule(
            planning_state,
            world_id,
            actor_id,
            due_tick,
            max_runs=1,
        ))

    return WorldState.model_validate(
        planning_state.model_dump(mode="python")
        | {"scheduled_events": [*planning_state.scheduled_events, *schedules]},
    )


def enable_diverse_npc_autonomy(state: WorldState, *, world_id: UUID) -> WorldState:
    """Enroll one route milestone and two reachable reserve goals at genesis.

    The key person's authored prose goal remains intact. A separate typed
    reach milestone is bound to a route that is currently visible and passes
    the registered move capability preflight. Two distinct resident reviews
    are admitted only when their existing reserve goals have legal local
    purchases. Every review schedule runs once at a fixed day boundary.
    """
    if not isinstance(state, WorldState):
        raise TypeError("diverse NPC autonomy requires a WorldState")
    if not isinstance(world_id, UUID):
        raise TypeError("diverse NPC autonomy requires the authoritative world UUID")
    if state.current_tick != 0:
        raise ValueError("diverse NPC autonomy can only be enabled at new-world genesis")
    if state.metadata.get(NPC_AUTONOMY_PROFILE_KEY) is not None:
        raise ValueError("an NPC autonomy profile has already been applied to this world")
    if state.npc_plans or any(
        schedule.handler_id == NPC_PLAN_REVIEW_HANDLER_ID for schedule in state.scheduled_events
    ):
        raise ValueError("diverse NPC autonomy requires a fresh genesis without existing NPC plans or reviews")

    content_index = state.metadata.get("content_index")
    pack_id = state.metadata.get("pack_id")
    travel_profile = _DIVERSE_TRAVEL_PROFILES.get(pack_id) if isinstance(pack_id, str) else None
    if travel_profile is None or not isinstance(content_index, dict):
        raise ValueError("diverse NPC autonomy is available only for the three shipped full-content backgrounds")
    key_npc_ids = content_index.get("key_npc_ids")
    resident_ids = content_index.get("other_resident_ids")
    if not isinstance(key_npc_ids, list) or not isinstance(resident_ids, list):
        raise ValueError("diverse NPC autonomy requires key NPC and resident IDs in the content index")

    metadata = dict(state.metadata)
    metadata.update({
        NPC_AUTONOMY_PROFILE_KEY: NPC_AUTONOMY_DIVERSE_PROFILE,
        NPC_SHARED_TRADE_POLICY: True,
        NPC_GOAL_COMPLETION_POLICY: True,
    })
    planning_state = WorldState.model_validate(
        state.model_dump(mode="python") | {"metadata": metadata},
    )
    registry = build_default_npc_capability_registry()

    travel_actor, authored_goal = _diverse_travel_actor_and_goal(
        planning_state, key_npc_ids, travel_profile,
    )
    route, destination = _diverse_reachable_route(
        planning_state, world_id, travel_actor.id, registry,
    )
    milestone = GoalRecord(
        id=uuid5(
            world_id,
            f"npc-autonomy:diverse_v1:reach-goal:{pack_id}:{travel_actor.id}:{destination.id}",
        ),
        owner_id=travel_actor.id,
        title=travel_profile["milestone_title"].format(destination=destination.name),
        description=(
            f"{travel_profile['purpose']} at {destination.name} via the currently reachable route "
            f"from {planning_state.entities[travel_actor.location_id].name} "
            f"(route ID {route.route_id}). This is a travel milestone "
            f"toward the existing authored goal '{authored_goal.title}': {authored_goal.description} "
            "Arrival alone does not prove the letter was delivered, path reopened, or handoff confirmed."
        ),
        status="active",
        priority=90,
        progress=0,
        conditions=[GoalCondition(
            kind="reach_location",
            subject_id=travel_actor.id,
            target_id=destination.id,
        )],
        created_tick=state.current_tick,
        updated_tick=state.current_tick,
    )
    planning_state = WorldState.model_validate(
        planning_state.model_dump(mode="python")
        | {"goals": [*planning_state.goals, milestone]},
    )

    reserve_actor_ids: list[UUID] = []
    for raw_id in sorted({str(value) for value in resident_ids}):
        actor_id = UUID(raw_id)
        actor = planning_state.entities.get(actor_id)
        if (
            actor is None
            or actor.kind not in _CHARACTER_KINDS
            or actor.status != "active"
            or actor_id in {planning_state.player_id, travel_actor.id}
            or _has_in_progress_plan(planning_state, actor_id)
            or _has_any_review_schedule(planning_state, actor_id)
        ):
            continue
        if _has_actionable_reserve_goal(planning_state, world_id, actor_id, registry):
            reserve_actor_ids.append(actor_id)
            if len(reserve_actor_ids) == NPC_AUTONOMY_DIVERSE_RESERVE_COUNT:
                break

    if len(reserve_actor_ids) != NPC_AUTONOMY_DIVERSE_RESERVE_COUNT:
        raise ValueError(
            "diverse NPC autonomy could not find two residents with actionable local reserve goals"
        )

    selected_actors = [travel_actor.id, *reserve_actor_ids]
    schedules = [
        build_npc_plan_review_schedule(
            planning_state,
            world_id,
            actor_id,
            state.current_tick + offset_days * state.calendar.day_seconds,
            max_runs=1,
        )
        for actor_id, offset_days in zip(
            selected_actors,
            NPC_AUTONOMY_DIVERSE_REVIEW_DAY_OFFSETS,
            strict=True,
        )
    ]
    return WorldState.model_validate(
        planning_state.model_dump(mode="python")
        | {"scheduled_events": [*planning_state.scheduled_events, *schedules]},
    )


def _diverse_travel_actor_and_goal(state: WorldState, key_npc_ids: list[object], profile):
    expected_role = profile["role"]
    matching_actors = []
    for raw_id in key_npc_ids:
        try:
            actor_id = UUID(str(raw_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("diverse NPC autonomy found an invalid key NPC ID") from exc
        actor = state.entities.get(actor_id)
        if (
            actor is not None
            and actor.kind in _CHARACTER_KINDS
            and actor.status == "active"
            and actor_id != state.player_id
            and actor.components.get("public_role") == expected_role
        ):
            matching_actors.append(actor)
    if len(matching_actors) != 1:
        raise ValueError(
            f"diverse NPC autonomy requires exactly one authored key NPC with role {expected_role!r}"
        )
    actor = matching_actors[0]
    authored_goals = [
        goal for goal in state.goals
        if goal.owner_id == actor.id
        and goal.status == "active"
        and goal.title == profile["source_goal_title"]
        and goal.created_tick <= state.current_tick
    ]
    if len(authored_goals) != 1:
        raise ValueError(
            f"diverse NPC autonomy requires the active authored goal {profile['source_goal_title']!r}"
        )
    return actor, authored_goals[0]


def _diverse_reachable_route(state: WorldState, world_id: UUID, actor_id: UUID, registry):
    actor = state.entities[actor_id]
    origin = state.entities.get(actor.location_id) if actor.location_id else None
    if origin is None or origin.kind not in _PLACE_KINDS or origin.status != "active":
        raise ValueError("diverse NPC autonomy found no active origin place for its authored key NPC")
    try:
        place = PlaceComponent.model_validate(origin.components.get("place"))
    except Exception as exc:
        raise ValueError("diverse NPC autonomy found no registered outgoing routes for its authored key NPC") from exc

    candidates = []
    branch_id = uuid5(world_id, "npc-autonomy:diverse_v1:preflight-branch")
    for route in place.routes:
        destination = state.entities.get(route.to_place_id)
        if (
            destination is None
            or destination.kind not in _PLACE_KINDS
            or destination.status != "active"
            or destination.components.get("hidden") is True
            and not entity_visible_to_actor(state, actor.id, destination.id)
        ):
            continue
        try:
            destination_place = PlaceComponent.model_validate(destination.components.get("place"))
        except Exception:
            continue
        step = NpcPlanStep(
            capability_id="world.action.move.v1",
            arguments={"destination_id": str(destination.id), "route_id": str(route.route_id)},
        )
        preflight = preflight_npc_plan_step(
            state,
            actor.id,
            step,
            RuleContext(
                world_id=world_id,
                branch_id=branch_id,
                command_id=uuid5(world_id, f"npc-autonomy:diverse_v1:preflight:{actor.id}:{route.route_id}"),
                actor_id=actor.id,
                ruleset_version=RULES_VERSION,
            ),
            registry=registry,
        )
        if preflight.status != "journey_start" or preflight.action_kind != "move":
            continue
        candidates.append((
            0 if destination_place.place_type == "region" else 1,
            route.travel_ticks,
            (destination.name or "").casefold(),
            str(destination.id),
            str(route.route_id),
            route,
            destination,
        ))
    if not candidates:
        raise ValueError("diverse NPC autonomy found no reachable, registered outgoing route for its authored key NPC")
    candidates.sort(key=lambda item: item[:5])
    selected = candidates[0]
    return selected[5], selected[6]


def _has_in_progress_plan(state: WorldState, actor_id: UUID) -> bool:
    return any(plan.actor_id == actor_id and plan.status in _ACTIVE_PLAN_STATUSES for plan in state.npc_plans)


def _has_any_review_schedule(state: WorldState, actor_id: UUID) -> bool:
    actor_key = str(actor_id)
    return any(
        schedule.handler_id == NPC_PLAN_REVIEW_HANDLER_ID
        and schedule.payload.get("actor_id") == actor_key
        for schedule in state.scheduled_events
    )


def _has_actionable_reserve_goal(state: WorldState, world_id: UUID, actor_id: UUID, registry) -> bool:
    actor = state.entities[actor_id]
    due_limit = state.current_tick + NPC_AUTONOMY_LIMITED_REVIEW_DAY_OFFSETS[-1] * state.calendar.day_seconds
    goals = sorted(
        (
            goal for goal in state.goals
            if goal.owner_id == actor_id
            and goal.status == "active"
            and goal.created_tick <= state.current_tick
            and (goal.deadline is None or goal.deadline >= state.current_tick)
            and len(goal.conditions) == 1
            and goal.conditions[0].kind == "resource_balance_minimum"
            and goal.conditions[0].subject_id == actor_id
            and goal.conditions[0].resource_id is not None
            and goal.conditions[0].quantity is not None
            and (goal.deadline is None or goal.deadline >= due_limit)
        ),
        key=lambda goal: (-goal.priority, str(goal.id)),
    )
    place = state.entities.get(actor.location_id) if actor.location_id else None
    if (
        place is None
        or place.kind not in {"place", "location"}
        or place.status != "active"
    ):
        return False
    try:
        market = MarketComponent.model_validate(place.components.get("market"))
    except Exception:
        return False

    for goal in goals:
        condition = goal.conditions[0]
        current_balance = sum(
            (
                account.balance for account in state.resources.values()
                if account.owner_id == actor_id
                and account.account_type == "holder"
                and account.resource_id == condition.resource_id
            ),
            Decimal("0"),
        )
        shortage = condition.quantity - current_balance
        if shortage <= 0:
            continue
        for offer in sorted(market.offers, key=lambda item: str(item.listing_id)):
            if (
                not offer.active
                or offer.resource_id != condition.resource_id
                or offer.seller_id == actor_id
                or offer.minimum_quantity > shortage
            ):
                continue
            step = NpcPlanStep(
                capability_id="world.action.trade.v1",
                arguments={
                    "listing_id": str(offer.listing_id),
                    "quantity": _decimal_text(offer.minimum_quantity),
                },
            )
            context = RuleContext(
                world_id=world_id,
                branch_id=uuid5(world_id, "npc-autonomy:limited_v1:preflight-branch"),
                command_id=uuid5(world_id, f"npc-autonomy:limited_v1:preflight:{actor_id}:{offer.listing_id}"),
                actor_id=actor_id,
                ruleset_version=RULES_VERSION,
            )
            preflight = preflight_npc_plan_step(
                state,
                actor_id,
                step,
                context,
                registry=registry,
            )
            if preflight.status == "interval_required" and preflight.required_ticks == NPC_TRADE_TICKS:
                return True
    return False


def _decimal_text(value: Decimal) -> str:
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result
