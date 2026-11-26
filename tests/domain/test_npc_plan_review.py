from __future__ import annotations

from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from world_engine.application.npc_planning import _safe_model_call_id
from world_engine.domain.components import GoalRecord, ScheduledEventRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.npc_plan_review import (
    NPC_FAILURE_REVIEW_POLICY,
    NPC_MODEL_OUTPUT_DEFER_REASONS,
    NPC_MODEL_UNAVAILABLE_REASON,
    NPC_PLAN_REVIEW_HANDLER_ID,
    NPC_PLAN_REVIEW_HANDLER_VERSION,
    build_npc_failure_review,
    build_npc_plan_review_schedule,
    build_npc_provider_unavailable_review,
    npc_plan_review_request_id,
    npc_plan_review_schedule_id,
    queued_npc_plan_reviews,
    validate_npc_plan_review_schedule,
)
from world_engine.domain.npc_plan_review_events import (
    NpcPlanReviewResolvedEvent,
    apply_npc_plan_review_resolved,
    npc_plan_review_event_id,
)
from world_engine.domain.npc_plans import NpcPlan, NpcPlanStep
from world_engine.domain.events import parse_event
from world_engine.domain.reducers import apply_event, state_hash


def _world(*, tick: int = 40, actor_status: str = "active", actor_id: UUID | None = None):
    world_id = uuid4()
    actor_id = actor_id or uuid4()
    goal_id = uuid4()
    actor = WorldEntity(id=actor_id, kind="character", name="Ari", status=actor_status)
    goal = GoalRecord(
        id=goal_id,
        owner_id=actor_id,
        title="Find shelter",
        created_tick=max(0, tick - 10),
        updated_tick=max(0, tick - 10),
    )
    state = WorldState(
        current_tick=tick,
        entities={actor_id: actor},
        goals=[goal],
    )
    return world_id, actor_id, goal_id, state


def _schedule(state: WorldState, world_id: UUID, actor_id: UUID, *, repeat_ticks: int | None = None, max_runs: int = 1):
    return build_npc_plan_review_schedule(
        state,
        world_id,
        actor_id,
        state.current_tick,
        repeat_ticks=repeat_ticks,
        max_runs=max_runs,
    )


def _event(
    world_id: UUID,
    actor_id: UUID,
    schedule: ScheduledEventRecord,
    *,
    outcome: str = "defer",
    source: str = "policy",
    plan_id: UUID | None = None,
    model_call_id: str | None = None,
    at_tick: int | None = None,
    reason: str | None = None,
    based_on_state_version: int = 8,
    command_id: UUID | None = None,
    visibility: dict[str, str] | None = None,
) -> NpcPlanReviewResolvedEvent:
    command_id = command_id or uuid4()
    ruleset_version = "test.rules.1"
    decision_request_id = npc_plan_review_request_id(command_id, ruleset_version, schedule)
    return NpcPlanReviewResolvedEvent(
        event_id=npc_plan_review_event_id(
            command_id, ruleset_version, schedule.id, schedule.revision,
        ),
        world_id=world_id,
        branch_id=uuid4(),
        command_id=command_id,
        ruleset_version=ruleset_version,
        scheduled_event_id=schedule.id,
        expected_schedule_revision=schedule.revision,
        actor_id=actor_id,
        at_tick=schedule.due_tick if at_tick is None else at_tick,
        decision_request_id=decision_request_id,
        based_on_state_version=based_on_state_version,
        source=source,
        outcome=outcome,
        plan_id=plan_id,
        model_call_id=model_call_id,
        reason=reason or outcome,
        visibility=visibility or {"policy": "author_only"},
    )


def _state_with_schedule(state: WorldState, schedule: ScheduledEventRecord) -> WorldState:
    return state.model_copy(update={"scheduled_events": [schedule]})


def _plan(actor_id: UUID, goal_id: UUID, *, plan_id: UUID, tick: int) -> NpcPlan:
    return NpcPlan(
        plan_id=plan_id,
        actor_id=actor_id,
        goal_ids=[goal_id],
        revision=1,
        status="ready",
        created_tick=tick,
        updated_tick=tick,
        next_review_tick=tick,
        steps=[NpcPlanStep(capability_id="world.action.explore.v1", arguments={})],
        current_step=0,
        reason="A bounded plan proposal.",
    )


def test_review_schedule_is_stable_registered_and_finitely_enumerated():
    world_id, actor_id, _, state = _world()
    schedule = _schedule(state, world_id, actor_id, repeat_ticks=20, max_runs=3)

    assert schedule.id == npc_plan_review_schedule_id(world_id, actor_id)
    assert schedule.id == npc_plan_review_schedule_id(world_id, actor_id)
    assert schedule.phase == "decision"
    assert schedule.handler_id == NPC_PLAN_REVIEW_HANDLER_ID
    assert schedule.handler_version == NPC_PLAN_REVIEW_HANDLER_VERSION
    assert schedule.payload == {"actor_id": str(actor_id)}
    assert validate_npc_plan_review_schedule(state, schedule, world_id, require_eligible=True) == actor_id
    assert queued_npc_plan_reviews(_state_with_schedule(state, schedule), world_id) == [schedule]
    assert queued_npc_plan_reviews(
        _state_with_schedule(state, schedule.model_copy(update={"due_tick": state.current_tick + 1})),
        world_id,
    ) == []


@pytest.mark.parametrize("kwargs", [{"max_runs": 2}, {"repeat_ticks": 0}, {"max_runs": 1_000_001}])
def test_review_schedule_rejects_unbounded_or_invalid_repeat_configuration(kwargs):
    world_id, actor_id, _, state = _world()
    with pytest.raises(InvalidEvent):
        build_npc_plan_review_schedule(state, world_id, actor_id, state.current_tick, **kwargs)


def test_failure_review_respects_cooldown_deadline_and_run_limit():
    world_id, actor_id, goal_id, state = _world(tick=100)
    review = _schedule(state, world_id, actor_id, repeat_ticks=20, max_runs=2)
    state = state.model_copy(update={"metadata": {NPC_FAILURE_REVIEW_POLICY: True}})
    plan = _plan(actor_id, goal_id, plan_id=uuid4(), tick=100).model_copy(update={
        "status": "blocked", "last_failure": "Unavailable stock", "next_review_tick": 220,
    })
    assert build_npc_failure_review(_state_with_schedule(state, review), world_id, plan) is None

    completed = review.model_copy(update={"status": "completed", "completed_runs": 2, "revision": 3})
    state = _state_with_schedule(state, completed)
    rearmed = build_npc_failure_review(state, world_id, plan)
    assert rearmed is not None
    assert rearmed.id == completed.id and rearmed.revision == 4
    assert rearmed.due_tick == 100 + max(120, state.calendar.day_seconds // 24)
    assert rearmed.completed_runs == 2 and rearmed.max_runs == 3

    expiring = state.model_copy(update={"goals": [state.goals[0].model_copy(update={"deadline": 220})]})
    assert build_npc_failure_review(expiring, world_id, plan) is None
    capped = completed.model_copy(update={"completed_runs": 13, "max_runs": 13})
    assert build_npc_failure_review(_state_with_schedule(state, capped), world_id, plan) is None


def test_provider_unavailability_rearms_one_due_review_with_bounded_cadence():
    world_id, actor_id, _, base_state = _world(tick=100)
    schedule = _schedule(base_state, world_id, actor_id)
    state = _state_with_schedule(base_state, schedule)
    event = _event(
        world_id, actor_id, schedule, outcome="defer", source="policy",
        reason=NPC_MODEL_UNAVAILABLE_REASON,
    )

    result = apply_npc_plan_review_resolved(state, event)

    retry, = result.scheduled_events
    assert result.current_tick == state.current_tick
    assert retry.id == schedule.id
    assert retry.status == "queued"
    assert retry.due_tick == state.current_tick + 3_600
    assert retry.revision == schedule.revision + 2
    assert retry.completed_runs == 1 and retry.max_runs == 2
    assert apply_event(state, event) == result
    with pytest.raises(InvalidEvent):
        apply_npc_plan_review_resolved(result, event)


@pytest.mark.parametrize("reason", sorted(NPC_MODEL_OUTPUT_DEFER_REASONS))
def test_unusable_model_output_defers_and_preserves_an_existing_registered_plan(reason):
    world_id, actor_id, goal_id, base_state = _world(tick=100)
    plan = _plan(actor_id, goal_id, plan_id=uuid4(), tick=base_state.current_tick)
    state = base_state.model_copy(update={"npc_plans": [plan]})
    schedule = _schedule(state, world_id, actor_id)
    state = _state_with_schedule(state, schedule)
    event = _event(
        world_id, actor_id, schedule, outcome="defer", source="model",
        model_call_id="call-structured-output", reason=reason,
    )

    result = apply_npc_plan_review_resolved(state, event)

    assert result.npc_plans == [plan]
    retry, = result.scheduled_events
    assert retry.id == schedule.id
    assert retry.status == "queued" and retry.due_tick == 100 + 3_600
    assert retry.completed_runs == 1 and retry.max_runs == 2


def test_provider_unavailability_respects_existing_schedule_and_total_retry_cap():
    world_id, actor_id, _, state = _world(tick=100)
    repeating = _schedule(state, world_id, actor_id, repeat_ticks=10_000, max_runs=3)
    queued = _state_with_schedule(state, repeating)
    result = apply_npc_plan_review_resolved(queued, _event(
        world_id, actor_id, repeating, outcome="defer", source="policy",
        reason=NPC_MODEL_UNAVAILABLE_REASON,
    ))
    next_review, = result.scheduled_events
    assert next_review.due_tick == 100 + 10_000
    assert next_review.max_runs == repeating.max_runs
    assert next_review.completed_runs == 1

    exhausted = repeating.model_copy(update={
        "status": "completed", "completed_runs": 3, "revision": 4,
    })
    retry = build_npc_provider_unavailable_review(_state_with_schedule(state, exhausted), world_id, actor_id)
    assert retry.due_tick == 100 + 4 * 3_600

    capped = repeating.model_copy(update={
        "status": "completed", "completed_runs": 13, "max_runs": 13,
        "revision": 14,
    })
    capped_state = _state_with_schedule(state, capped)
    assert build_npc_provider_unavailable_review(capped_state, world_id, actor_id) is None

    expiring = state.model_copy(update={
        "goals": [state.goals[0].model_copy(update={"deadline": state.current_tick + 3_599})],
        "scheduled_events": [capped],
    })
    assert build_npc_provider_unavailable_review(expiring, world_id, actor_id) is None


def test_review_schedule_rejects_player_controlled_dead_or_goal_less_actors():
    world_id, actor_id, _, state = _world()
    player_state = state.model_copy(update={"player_id": actor_id})
    with pytest.raises(InvalidEvent):
        _schedule(player_state, world_id, actor_id)

    dead_world_id, dead_actor_id, _, dead_state = _world(actor_status="dead")
    with pytest.raises(InvalidEvent):
        _schedule(dead_state, dead_world_id, dead_actor_id)

    goal_less = state.model_copy(update={"goals": []})
    with pytest.raises(InvalidEvent):
        _schedule(goal_less, world_id, actor_id)

    with pytest.raises(InvalidEvent):
        build_npc_plan_review_schedule(
            state, world_id, actor_id, state.current_tick, excluded_actor_ids={actor_id},
        )


def test_review_event_resolves_defer_and_advances_only_the_scheduled_occurrence():
    world_id, actor_id, _, base_state = _world()
    schedule = _schedule(base_state, world_id, actor_id, repeat_ticks=10, max_runs=2)
    state = _state_with_schedule(base_state, schedule)
    event = _event(world_id, actor_id, schedule, outcome="defer", source="model", model_call_id="call-123")
    before_hash = state_hash(state)

    result = apply_npc_plan_review_resolved(state, event)

    assert state_hash(state) == before_hash
    assert result.current_tick == state.current_tick
    assert result.scheduled_events[0].status == "queued"
    assert result.scheduled_events[0].due_tick == state.current_tick + 10
    assert result.scheduled_events[0].completed_runs == 1
    assert result.scheduled_events[0].revision == schedule.revision + 1

    next_tick = state.current_tick + 10
    next_state = result.model_copy(update={
        "current_tick": next_tick,
        "scheduled_events": [result.scheduled_events[0].model_copy(update={"due_tick": next_tick})],
    })
    next_event = _event(
        world_id,
        actor_id,
        next_state.scheduled_events[0],
        outcome="defer",
        source="policy",
        reason="second_review",
        at_tick=next_tick,
    )
    finished = apply_npc_plan_review_resolved(next_state, next_event)
    assert finished.scheduled_events[0].status == "completed"
    assert finished.scheduled_events[0].completed_runs == 2


@pytest.mark.parametrize("outcome", ["defer", "invalid", "skipped"])
def test_non_ready_outcomes_complete_schedule_without_plan(outcome: str):
    world_id, actor_id, _, base_state = _world()
    schedule = _schedule(base_state, world_id, actor_id)
    state = _state_with_schedule(base_state, schedule)
    event = _event(
        world_id,
        actor_id,
        schedule,
        outcome=outcome,
        source="policy",
    )

    assert parse_event(event) == event
    result = apply_event(state, event)

    assert result.current_tick == state.current_tick
    assert result.scheduled_events[0].status == "completed"
    assert result.scheduled_events[0].completed_runs == 1


def test_skipped_review_cleans_up_schedule_for_dead_actor():
    world_id, actor_id, _, state = _world(actor_status="active")
    schedule = _schedule(state, world_id, actor_id)
    dead_entities = dict(state.entities)
    dead_entities[actor_id] = dead_entities[actor_id].model_copy(update={"status": "dead"})
    dead_state = state.model_copy(update={"entities": dead_entities, "scheduled_events": [schedule]})

    result = apply_npc_plan_review_resolved(
        dead_state,
        _event(world_id, actor_id, schedule, outcome="skipped", source="policy", reason="actor_unavailable"),
    )

    assert result.scheduled_events[0].status == "completed"


def test_ready_review_requires_matching_new_plan_and_binds_its_id():
    world_id, actor_id, goal_id, base_state = _world()
    schedule = _schedule(base_state, world_id, actor_id)
    state = _state_with_schedule(base_state, schedule)
    command_id = uuid4()
    decision_request_id = npc_plan_review_request_id(command_id, "test.rules.1", schedule)
    expected_plan_id = uuid5(decision_request_id, f"npc.plan.v1:{actor_id}:8")
    plan = _plan(actor_id, goal_id, plan_id=expected_plan_id, tick=state.current_tick)
    event = _event(
        world_id,
        actor_id,
        schedule,
        outcome="ready",
        source="model",
        model_call_id="call-ready",
        plan_id=expected_plan_id,
        command_id=command_id,
    )

    with pytest.raises(InvalidEvent):
        apply_npc_plan_review_resolved(state, event)

    state_with_plan = state.model_copy(update={"npc_plans": [plan]})
    updated = apply_npc_plan_review_resolved(state_with_plan, event)
    assert updated.scheduled_events[0].status == "completed"
    assert updated.npc_plans == [plan]
    assert state.scheduled_events[0] == schedule


def test_ready_review_rejects_wrong_actor_or_wrong_deterministic_plan_id():
    world_id, actor_id, goal_id, state = _world()
    other_actor_id = uuid4()
    other_goal_id = uuid4()
    entities = dict(state.entities)
    entities[other_actor_id] = WorldEntity(id=other_actor_id, kind="character", name="Bea")
    goals = [*state.goals, GoalRecord(id=other_goal_id, owner_id=other_actor_id, title="Other goal")]
    state = state.model_copy(update={"entities": entities, "goals": goals})
    schedule = _schedule(state, world_id, actor_id)
    state = _state_with_schedule(state, schedule)
    command_id = uuid4()
    request_id = npc_plan_review_request_id(command_id, "test.rules.1", schedule)
    matching_id = uuid5(request_id, f"npc.plan.v1:{actor_id}:8")
    wrong_actor_plan = _plan(other_actor_id, other_goal_id, plan_id=matching_id, tick=state.current_tick)
    bad_actor_state = state.model_copy(update={"npc_plans": [wrong_actor_plan]})
    wrong_actor_event = _event(
        world_id,
        actor_id,
        schedule,
        outcome="ready",
        source="model",
        model_call_id="call-ready",
        plan_id=matching_id,
        command_id=command_id,
    )
    with pytest.raises(InvalidEvent):
        apply_npc_plan_review_resolved(bad_actor_state, wrong_actor_event)

    wrong_id = uuid4()
    wrong_plan = _plan(actor_id, goal_id, plan_id=wrong_id, tick=state.current_tick)
    wrong_id_state = state.model_copy(update={"npc_plans": [wrong_plan]})
    wrong_id_event = _event(
        world_id,
        actor_id,
        schedule,
        outcome="ready",
        source="model",
        model_call_id="call-ready",
        plan_id=wrong_id,
        command_id=command_id,
    )
    with pytest.raises(InvalidEvent):
        apply_npc_plan_review_resolved(wrong_id_state, wrong_id_event)


def test_review_rejects_wrong_tick_stale_revision_and_non_private_event():
    world_id, actor_id, _, state = _world()
    schedule = _schedule(state, world_id, actor_id)
    state = _state_with_schedule(state, schedule)

    with pytest.raises(InvalidEvent):
        apply_npc_plan_review_resolved(
            state,
            _event(world_id, actor_id, schedule, at_tick=state.current_tick + 1),
        )

    stale_schedule = schedule.model_copy(update={"revision": schedule.revision + 1})
    stale_state = _state_with_schedule(state, stale_schedule)
    with pytest.raises(InvalidEvent):
        apply_npc_plan_review_resolved(stale_state, _event(world_id, actor_id, schedule))

    with pytest.raises(ValidationError):
        _event(world_id, actor_id, schedule, visibility={"policy": "public"})


@pytest.mark.parametrize(
    "updates",
    [
        {"phase": "maintenance"},
        {"handler_version": "2.0.0"},
        {"payload": {"actor_id": str(uuid4()), "extra": True}},
    ],
)
def test_review_rejects_wrong_handler_version_phase_or_payload(updates):
    world_id, actor_id, _, base_state = _world()
    schedule = _schedule(base_state, world_id, actor_id)
    malformed = schedule.model_copy(update=updates)
    state = _state_with_schedule(base_state, malformed)
    with pytest.raises(InvalidEvent):
        validate_npc_plan_review_schedule(state, malformed, world_id)
    with pytest.raises(InvalidEvent):
        apply_npc_plan_review_resolved(state, _event(world_id, actor_id, malformed))


def test_enumerator_rejects_unknown_registered_handler_version_and_overdue_schedule():
    world_id, actor_id, _, state = _world()
    schedule = _schedule(state, world_id, actor_id)
    wrong = schedule.model_copy(update={"handler_version": "2.0.0"})
    with pytest.raises(InvalidEvent):
        queued_npc_plan_reviews(_state_with_schedule(state, wrong), world_id)
    overdue = schedule.model_copy(update={"due_tick": state.current_tick - 1})
    with pytest.raises(InvalidEvent):
        queued_npc_plan_reviews(_state_with_schedule(state, overdue), world_id)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": "model", "model_call_id": None},
        {"source": "policy", "model_call_id": "claimed-model-call"},
        {"source": "model", "model_call_id": "bad\ncall"},
        {"reason": "RuntimeError: raw failure"},
    ],
)
def test_review_event_rejects_unverifiable_source_or_unsafe_reason(kwargs):
    world_id, actor_id, _, state = _world()
    schedule = _schedule(state, world_id, actor_id)
    with pytest.raises(ValidationError):
        _event(world_id, actor_id, schedule, **kwargs)


def test_model_call_trace_identifier_is_extracted_without_inventing_one():
    class Result:
        class Trace:
            request_id = "trace-abc-123"

        call_trace = Trace()

    assert _safe_model_call_id(Result()) == "trace-abc-123"
    assert _safe_model_call_id(object()) is None
    assert _safe_model_call_id(type("Bad", (), {"call_trace": type("T", (), {"request_id": " x "})()})()) is None

