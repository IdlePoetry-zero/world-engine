from uuid import uuid4, uuid5

import pytest

from world_engine.content import ContentCatalog
from world_engine.domain.models import WorldState
from world_engine.domain.npc_plan_review import build_npc_plan_review_schedule
from world_engine.domain.npc_plans import NpcPlan, NpcPlanStep
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.rules.contracts import RuleContext
from world_engine.simulation import SimulationEngine, WorldAdvancePlan
from world_engine.simulation.contracts import DecisionResponse, SimulationContext
from world_engine.simulation.engine import StaleDecisionResponse
from world_engine.simulation.npc_capabilities import preflight_npc_plan_step


@pytest.fixture(scope="module")
def review_world():
    draft = ContentCatalog().build_world("modern_city", "NPC review engine", "Player", seed=23)
    state = draft.state
    for goal in state.goals:
        actor = state.entities[goal.owner_id]
        if actor.kind != "character" or actor.id == state.player_id or actor.location_id is None:
            continue
        place = state.entities[actor.location_id].components.get("place", {})
        for route in place.get("routes", []):
            step = NpcPlanStep(capability_id="world.action.move.v1", arguments={
                "destination_id": route["to_place_id"], "route_id": route["route_id"],
            })
            checked = preflight_npc_plan_step(state, actor.id, step, RuleContext(
                world_id=draft.world_id, branch_id=draft.branch_id, command_id=uuid4(), actor_id=actor.id,
                ruleset_version=draft.ruleset_version,
            ))
            if checked.status == "journey_start":
                schedule = build_npc_plan_review_schedule(state, draft.world_id, actor.id, 0,
                                                         repeat_ticks=3600, max_runs=2)
                payload = state.model_dump(mode="python")
                # This fixture isolates successful completion and exact clock
                # accounting. Hazard/interruption and retry behavior have
                # separate tests; a random interrupted trip must not be
                # mistaken for a broken arrival reducer.
                for authored_route in payload["entities"][actor.location_id]["components"]["place"]["routes"]:
                    if authored_route["route_id"] == route["route_id"]:
                        authored_route["risk"] = "0"
                payload["scheduled_events"] = [schedule, *state.scheduled_events]
                return draft, WorldState.model_validate(payload), actor.id, goal.id, step
    raise AssertionError("The full world must provide an NPC with a legal route.")


def _context(draft, **changes):
    return SimulationContext(
        world_id=draft.world_id, branch_id=draft.branch_id, command_id=uuid4(),
        command_actor_id=uuid4(), state_version=0, world_seed="23", **changes,
    )


def _apply_all(state, events):
    for event in events:
        state = apply_event(state, event)
    return state


def test_review_accepts_a_bound_registered_policy_plan_without_advancing_time(review_world):
    draft, state, actor_id, goal_id, step = review_world
    context = _context(draft)
    engine = SimulationEngine()
    advance = WorldAdvancePlan(started_tick=0, target_tick=7200)
    pending = engine.plan_window(state, advance, context)
    assert pending.outcome == "decision_required" and not pending.events
    request, = pending.decision_requests
    assert request.kind == "npc.plan_review" and request.actor_id == actor_id
    plan = NpcPlan(
        plan_id=uuid5(request.request_id, f"npc.plan.v1:{actor_id}:0"), actor_id=actor_id,
        goal_ids=[goal_id], revision=1, status="ready", created_tick=0, updated_tick=0,
        next_review_tick=0, steps=[step], current_step=0, reason="Deterministic policy fixture.",
    )
    response = DecisionResponse(
        request_id=request.request_id, state_version=0, at_tick=0, source="policy", option_id="plan",
        npc_planning_status="ready", npc_plan=plan, npc_plan_state_hash=state_hash(state),
    )
    before = state_hash(state)
    resolution = engine.plan_window(state, advance, context.model_copy(update={"decision_responses": (response,)}))
    assert [event.type for event in resolution.events] == [
        "core.npc_plan_created.v1", "core.npc_plan_review_resolved.v1",
    ]
    after = _apply_all(state, resolution.events)
    assert after.current_tick == state.current_tick == 0
    assert state_hash(state) == before
    assert after.npc_plans[0] == plan
    assert after.scheduled_events[0].due_tick == 3600
    assert resolution.events[-1].source == "policy" and resolution.events[-1].model_call_id is None


def test_user_controlled_actor_review_is_skipped_without_a_model_request(review_world):
    draft, state, actor_id, _, _ = review_world
    context = _context(draft, externally_controlled_actor_ids=frozenset({actor_id}))
    resolution = SimulationEngine().plan_window(state, WorldAdvancePlan(target_tick=100), context)
    assert resolution.outcome == "advanced" and not resolution.decision_requests
    event, = resolution.events
    assert event.outcome == "skipped" and event.source == "policy"
    assert event.reason == "npc_review_actor_user_controlled"
    assert not _apply_all(state, resolution.events).npc_plans


def test_defer_records_a_review_but_stale_snapshot_cannot_replace_it(review_world):
    draft, state, _, _, _ = review_world
    context = _context(draft)
    engine = SimulationEngine()
    advance = WorldAdvancePlan(target_tick=100)
    request, = engine.plan_window(state, advance, context).decision_requests
    response = DecisionResponse(
        request_id=request.request_id, state_version=0, at_tick=0, source="model", option_id="plan",
        npc_planning_status="defer", npc_plan_state_hash=state_hash(state), npc_model_call_id="fixture-call",
    )
    bad = response.model_copy(update={"npc_plan_state_hash": "0" * 64})
    with pytest.raises(StaleDecisionResponse, match="snapshot"):
        engine.plan_window(state, advance, context.model_copy(update={"decision_responses": (bad,)}))
    result = engine.plan_window(state, advance, context.model_copy(update={"decision_responses": (response,)}))
    after = _apply_all(state, result.events)
    assert not after.npc_plans and after.current_tick == 0
    assert result.events[0].outcome == "defer" and result.events[0].model_call_id == "fixture-call"
