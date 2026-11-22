from __future__ import annotations

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.content import ContentCatalog
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.npc_plan_events import (
    NpcPlanCreatedEvent,
    NpcPlanRevisedEvent,
    npc_plan_event_id,
)
from world_engine.domain.npc_plans import (
    MAX_NPC_PLAN_JSON_DEPTH,
    NpcPlan,
    NpcPlanStep,
    apply_npc_plan_created,
    apply_npc_plan_revised,
)


@pytest.fixture
def world():
    draft = ContentCatalog().build_world(
        "modern_city", "NPC Plan Tests", "Rin", seed=771,
    )
    state = draft.state
    goal = next(
        item for item in state.goals
        if item.owner_id != state.player_id
        and state.entities[item.owner_id].kind in {"character", "person"}
        and item.status == "active"
    )
    return draft, state, goal.owner_id, goal


def _plan(state, actor_id: UUID, goal_id: UUID, **updates) -> NpcPlan:
    payload = {
        "plan_id": uuid4(),
        "actor_id": actor_id,
        "goal_ids": [goal_id],
        "revision": 1,
        "status": "ready",
        "created_tick": state.current_tick,
        "updated_tick": state.current_tick,
        "next_review_tick": state.current_tick + 60,
        "steps": [
            NpcPlanStep(
                capability_id="core.travel.route",
                # Parameters are preserved as bounded JSON; capability schemas
                # and reference resolution belong to the execution registry.
                arguments={"destination_id": "opaque-here", "mode": "walk"},
            ),
        ],
        "current_step": 0,
        "attempts": 0,
        "last_failure": None,
        "reason": "Reach a place relevant to the actor's active goal.",
        "evidence_refs": [],
    }
    payload.update(updates)
    return NpcPlan.model_validate(payload)


def _created_event(draft, plan, *, command_id=None, visibility=None) -> NpcPlanCreatedEvent:
    command_id = command_id or uuid4()
    payload = {
        "event_id": npc_plan_event_id(
            command_id, draft.ruleset_version, "created", plan.plan_id, plan.revision,
        ),
        "world_id": draft.world_id,
        "branch_id": draft.branch_id,
        "command_id": command_id,
        "actor_id": plan.actor_id,
        "ruleset_version": draft.ruleset_version,
        "plan": plan,
    }
    if visibility is not None:
        payload["visibility"] = visibility
    return NpcPlanCreatedEvent.model_validate(payload)


def _revised_event(
    draft,
    plan,
    expected_revision: int,
    *,
    command_id=None,
    visibility=None,
) -> NpcPlanRevisedEvent:
    command_id = command_id or uuid4()
    payload = {
        "event_id": npc_plan_event_id(
            command_id, draft.ruleset_version, "revised", plan.plan_id, plan.revision,
        ),
        "world_id": draft.world_id,
        "branch_id": draft.branch_id,
        "command_id": command_id,
        "actor_id": plan.actor_id,
        "ruleset_version": draft.ruleset_version,
        "plan": plan,
        "expected_revision": expected_revision,
    }
    if visibility is not None:
        payload["visibility"] = visibility
    return NpcPlanRevisedEvent.model_validate(payload)


def _rebuild(plan: NpcPlan, **updates) -> NpcPlan:
    payload = plan.model_dump(mode="python")
    payload.update(updates)
    return NpcPlan.model_validate(payload)


def test_plan_created_running_and_completed_lifecycle_validates_full_world_state(world) -> None:
    draft, state, actor_id, goal = world
    initial = _plan(state, actor_id, goal.id)
    created = apply_npc_plan_created(state, _created_event(draft, initial))
    assert created.npc_plans == [initial]

    running = _rebuild(
        initial,
        revision=2,
        status="running",
        updated_tick=state.current_tick,
        next_review_tick=state.current_tick + 120,
        attempts=1,
    )
    running_state = apply_npc_plan_revised(created, _revised_event(draft, running, 1))
    assert running_state.npc_plans[0] == running

    succeeded_goal = goal.model_copy(update={
        "status": "succeeded",
        "progress": 100,
        "evidence_refs": [uuid4()],
        "updated_tick": state.current_tick,
    })
    succeeded_goals_state = running_state.model_copy(update={
        "goals": [succeeded_goal if item.id == goal.id else item for item in running_state.goals],
    })

    completed = _rebuild(
        running,
        revision=3,
        status="completed",
        updated_tick=state.current_tick,
        next_review_tick=None,
        current_step=len(running.steps),
        last_failure=None,
        evidence_refs=[uuid4()],
    )
    completed_state = apply_npc_plan_revised(
        succeeded_goals_state,
        _revised_event(draft, completed, 2),
    )
    assert completed_state.npc_plans[0] == completed
    assert completed_state.current_tick == state.current_tick

    revived = _rebuild(
        completed,
        revision=4,
        status="ready",
        updated_tick=state.current_tick,
        next_review_tick=state.current_tick + 30,
        current_step=0,
        evidence_refs=[],
    )
    with pytest.raises(InvalidEvent, match="completed NPC plan cannot be revised"):
        apply_npc_plan_revised(completed_state, _revised_event(draft, revived, 3))


def test_plan_revision_rejects_stale_revision_and_immutable_history_changes(world) -> None:
    draft, state, actor_id, goal = world
    initial = _plan(state, actor_id, goal.id)
    created = apply_npc_plan_created(state, _created_event(draft, initial))
    running = _rebuild(
        initial,
        revision=2,
        status="running",
        updated_tick=state.current_tick,
        next_review_tick=state.current_tick + 120,
        attempts=1,
    )
    revised_state = apply_npc_plan_revised(created, _revised_event(draft, running, 1))

    stale_copy = _rebuild(running, updated_tick=state.current_tick)
    with pytest.raises(InvalidEvent, match="expected_revision does not match stored state"):
        apply_npc_plan_revised(revised_state, _revised_event(draft, stale_copy, 1))

    other_goal = next(
        item for item in state.goals
        if item.owner_id != actor_id and item.status == "active"
    )
    changed_actor = _rebuild(
        running,
        revision=3,
        actor_id=other_goal.owner_id,
        goal_ids=[other_goal.id],
    )
    with pytest.raises(InvalidEvent, match="plan ID and actor are immutable"):
        apply_npc_plan_revised(revised_state, _revised_event(draft, changed_actor, 2))

    changed_creation = _rebuild(running, revision=3, created_tick=running.created_tick - 1)
    with pytest.raises(InvalidEvent, match="created_tick is immutable"):
        apply_npc_plan_revised(revised_state, _revised_event(draft, changed_creation, 2))


def test_plan_rejects_player_dead_actor_and_cross_owner_goal(world) -> None:
    draft, state, actor_id, goal = world
    player_plan = _plan(state, state.player_id, goal.id)
    with pytest.raises(InvalidEvent, match="active non-player character"):
        apply_npc_plan_created(state, _created_event(draft, player_plan))

    other_goal = next(
        item for item in state.goals
        if item.owner_id not in {actor_id, state.player_id} and item.status == "active"
    )
    cross_owner = _plan(state, actor_id, other_goal.id)
    with pytest.raises(InvalidEvent, match="goals must belong to its actor"):
        apply_npc_plan_created(state, _created_event(draft, cross_owner))

    entities = dict(state.entities)
    actor = entities[actor_id]
    entities[actor_id] = actor.model_copy(update={"status": "dead"})
    dead_state = state.model_copy(update={"entities": entities})
    dead_plan = _plan(dead_state, actor_id, goal.id)
    with pytest.raises(InvalidEvent, match="active non-player character"):
        apply_npc_plan_created(dead_state, _created_event(draft, dead_plan))


def test_terminal_cleanup_allows_dead_actor_and_expired_or_failed_goals(world) -> None:
    draft, state, actor_id, goal = world
    initial = _plan(state, actor_id, goal.id)
    created = apply_npc_plan_created(state, _created_event(draft, initial))

    entities = dict(created.entities)
    actor = entities[actor_id]
    components = deepcopy(actor.components)
    components["health"]["current"] = 0
    entities[actor_id] = actor.model_copy(update={"status": "dead", "components": components})
    group_id = UUID(actor.components["life"]["population_group_id"])
    group = entities[group_id]
    group_components = deepcopy(group.components)
    group_components["population"]["unmodeled_headcount"] += 1
    group_components["population"]["age_cohorts"][0]["headcount"] += 1
    group_components["population"]["revision"] += 1
    entities[group_id] = group.model_copy(update={"components": group_components})
    dead_state = created.model_copy(update={"entities": entities})
    cancelled = _rebuild(
        initial,
        revision=2,
        status="cancelled",
        updated_tick=state.current_tick,
        next_review_tick=None,
    )
    cancelled_state = apply_npc_plan_revised(dead_state, _revised_event(draft, cancelled, 1))
    assert cancelled_state.npc_plans[0].status == "cancelled"
    assert cancelled_state.entities[actor_id].status == "dead"

    expired_goal = goal.model_copy(update={"deadline": state.current_tick - 1})
    expired_state = created.model_copy(update={
        "goals": [expired_goal if item.id == goal.id else item for item in created.goals],
    })
    abandoned = _rebuild(
        initial,
        revision=2,
        status="abandoned",
        updated_tick=state.current_tick,
        next_review_tick=None,
        last_failure="The goal deadline passed before the next review.",
    )
    abandoned_state = apply_npc_plan_revised(expired_state, _revised_event(draft, abandoned, 1))
    assert abandoned_state.npc_plans[0].status == "abandoned"

    failed_goal = goal.model_copy(update={"status": "failed"})
    failed_state = created.model_copy(update={
        "goals": [failed_goal if item.id == goal.id else item for item in created.goals],
    })
    failed_abandonment = _rebuild(
        initial,
        revision=2,
        status="abandoned",
        updated_tick=state.current_tick,
        next_review_tick=None,
        last_failure="The referenced goal failed.",
    )
    assert apply_npc_plan_revised(
        failed_state,
        _revised_event(draft, failed_abandonment, 1),
    ).npc_plans[0].status == "abandoned"


def test_plan_arguments_and_time_are_bounded_and_typed(world) -> None:
    _, state, actor_id, goal = world
    with pytest.raises(ValidationError, match="JSON object"):
        NpcPlanStep(capability_id="core.test", arguments=["not", "an", "object"])

    deep_value: object = "leaf"
    for _ in range(MAX_NPC_PLAN_JSON_DEPTH):
        deep_value = {"nested": deep_value}
    with pytest.raises(ValidationError, match="nested too deeply"):
        NpcPlanStep(capability_id="core.test", arguments={"nested": deep_value})

    with pytest.raises(ValidationError, match="length limit|byte limit"):
        NpcPlanStep(capability_id="core.test", arguments={"value": "x" * 20_000})

    template = _plan(state, actor_id, goal.id).model_dump(mode="python")
    with pytest.raises(ValidationError, match="less than or equal"):
        NpcPlan.model_validate({**template, "created_tick": 2**63, "updated_tick": 2**63})
    with pytest.raises(ValidationError, match="updated_tick cannot precede created_tick"):
        NpcPlan.model_validate({**template, "created_tick": 1, "updated_tick": 0})

    draft, _, _, _ = world
    future_plan = _plan(
        state,
        actor_id,
        goal.id,
        created_tick=state.current_tick + 1,
        updated_tick=state.current_tick + 1,
        next_review_tick=state.current_tick + 10,
    )
    with pytest.raises(InvalidEvent, match="updated_tick must equal the current world tick"):
        apply_npc_plan_created(state, _created_event(draft, future_plan))


def test_plan_completion_requires_every_step_evidence_and_cleared_failure(world) -> None:
    _, state, actor_id, goal = world
    template = _plan(state, actor_id, goal.id).model_dump(mode="python")
    with pytest.raises(ValidationError, match="consumed every step"):
        NpcPlan.model_validate({
            **template,
            "status": "completed",
            "next_review_tick": None,
            "current_step": 0,
            "evidence_refs": [uuid4()],
        })
    with pytest.raises(ValidationError, match="requires at least one evidence reference"):
        NpcPlan.model_validate({
            **template,
            "status": "completed",
            "next_review_tick": None,
            "current_step": len(template["steps"]),
        })
    with pytest.raises(ValidationError, match="explicitly clear last_failure"):
        NpcPlan.model_validate({
            **template,
            "status": "completed",
            "next_review_tick": None,
            "current_step": len(template["steps"]),
            "evidence_refs": [uuid4()],
            "last_failure": "still blocked",
        })


def test_plan_cannot_complete_a_failed_goal(world) -> None:
    draft, state, actor_id, goal = world
    initial = _plan(state, actor_id, goal.id)
    created = apply_npc_plan_created(state, _created_event(draft, initial))
    running = _rebuild(
        initial,
        revision=2,
        status="running",
        updated_tick=state.current_tick,
        next_review_tick=state.current_tick + 90,
    )
    running_state = apply_npc_plan_revised(created, _revised_event(draft, running, 1))
    failed_goal = goal.model_copy(update={"status": "failed"})
    failed_state = running_state.model_copy(update={
        "goals": [failed_goal if item.id == goal.id else item for item in running_state.goals],
    })
    completed = _rebuild(
        running,
        revision=3,
        status="completed",
        updated_tick=state.current_tick,
        next_review_tick=None,
        current_step=len(initial.steps),
        evidence_refs=[uuid4()],
    )
    with pytest.raises(InvalidEvent, match="cannot claim a failed, abandoned, or expired goal"):
        apply_npc_plan_revised(failed_state, _revised_event(draft, completed, 2))


def test_plan_events_are_command_revision_bound_and_author_only(world) -> None:
    draft, state, actor_id, goal = world
    plan = _plan(state, actor_id, goal.id)
    command_id = uuid4()
    created = _created_event(draft, plan, command_id=command_id)
    assert created.event_id == npc_plan_event_id(
        command_id, draft.ruleset_version, "created", plan.plan_id, 1,
    )
    assert created.visibility == {"policy": "author_only"}

    public_event = created.model_dump(mode="python")
    public_event["visibility"] = {"policy": "public"}
    with pytest.raises(ValidationError, match="author_only"):
        NpcPlanCreatedEvent.model_validate(public_event)
    forged_public = NpcPlanCreatedEvent.model_construct(**{**public_event, "plan": plan})
    with pytest.raises(InvalidEvent, match="author_only"):
        apply_npc_plan_created(state, forged_public)

    changed_event_id = created.model_dump(mode="python")
    changed_event_id["event_id"] = uuid4()
    with pytest.raises(ValidationError, match="bound to its command"):
        NpcPlanCreatedEvent.model_validate(changed_event_id)

    revised = _rebuild(
        plan,
        revision=2,
        status="running",
        updated_tick=state.current_tick,
        next_review_tick=state.current_tick + 90,
    )
    private_revision = _revised_event(draft, revised, 1)
    public_revision = private_revision.model_dump(mode="python")
    public_revision["visibility"] = {"policy": "public"}
    with pytest.raises(ValidationError, match="author_only"):
        NpcPlanRevisedEvent.model_validate(public_revision)

