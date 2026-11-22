"""Registered scheduled-review policy for persistent NPC plans.

The schedule carries only its actor reference. A host may ask for a new,
version-bound plan proposal at an exact due tick; this module never infers
actions from authored titles and never advances the simulation clock.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal, get_args
from uuid import UUID, uuid5

from .components import ScheduledEventRecord
from .errors import InvalidEvent
from .npc_goal_completion import npc_goal_conditions_met

if TYPE_CHECKING:
    from .models import WorldState
    from .npc_plan_events import NpcPlanRevisedEvent
    from .npc_plans import NpcPlan

NPC_PLAN_REVIEW_HANDLER_ID = "npc.goal_review"
NPC_PLAN_REVIEW_HANDLER_VERSION = "1.0.0"
NPC_PLAN_REVIEW_PHASE = "decision"
_CHARACTER_KINDS = frozenset({"character", "person"})
_MIN_TICK = -(2**63)
_MAX_TICK = 2**63 - 1
_MAX_RUNS = 1_000_000
NPC_FAILURE_REVIEW_POLICY = "npc_failure_review_v1"
NPC_GOAL_FOLLOWUP_REVIEW_POLICY = "npc_goal_followup_review_v1"
_AUTOMATIC_REVIEW_RUN_LIMIT = 13

NpcPlanningRejection = Literal[
    "invalid_state_version",
    "invalid_ruleset_version",
    "npc_actor_request_mismatch",
    "stale_state_version",
    "npc_actor_unavailable",
    "npc_actor_is_player",
    "npc_actor_externally_controlled",
    "npc_knowledge_unavailable",
    "npc_capabilities_unavailable",
    "npc_context_invalid",
    "invalid_npc_plan_output",
    "npc_plan_goal_not_available",
    "invalid_npc_plan_steps",
    "npc_plan_first_step_unavailable",
    "npc_plan_semantic_rejected",
    "npc_plan_semantic_unavailable",
]
NPC_PLANNING_REJECTIONS = frozenset(get_args(NpcPlanningRejection))


def npc_plan_review_schedule_id(namespace_id: UUID, actor_id: UUID) -> UUID:
    """Return the stable per-world (or registered catalog) actor schedule ID."""
    if not isinstance(namespace_id, UUID) or not isinstance(actor_id, UUID):
        raise ValueError("NPC plan review schedule IDs require UUID namespace and actor IDs")
    return uuid5(namespace_id, f"npc.goal_review.v1:{actor_id}")


def npc_plan_review_request_id(
    command_id: UUID,
    ruleset_version: str,
    schedule: ScheduledEventRecord,
) -> UUID:
    """Return the stable model request ID for one scheduled occurrence."""
    if not isinstance(schedule, ScheduledEventRecord):
        raise ValueError("NPC plan review request ID requires a scheduled event record")
    return _npc_plan_review_request_id_for_occurrence(
        command_id,
        ruleset_version,
        schedule.id,
        schedule.revision,
    )


def _npc_plan_review_request_id_for_occurrence(
    command_id: UUID,
    ruleset_version: str,
    scheduled_event_id: UUID,
    expected_schedule_revision: int,
) -> UUID:
    if not isinstance(command_id, UUID) or not isinstance(scheduled_event_id, UUID):
        raise ValueError("NPC plan review request IDs require command and schedule UUIDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("NPC plan review request IDs require a ruleset version")
    if type(expected_schedule_revision) is not int or expected_schedule_revision < 1:
        raise ValueError("NPC plan review request IDs require a positive schedule revision")
    return uuid5(
        command_id,
        f"{ruleset_version}:npc_plan_review.request:{scheduled_event_id}:{expected_schedule_revision}",
    )


def _has_active_owned_goal(state: WorldState, actor_id: UUID) -> bool:
    return any(
        goal.owner_id == actor_id
        and goal.status == "active"
        and goal.created_tick <= state.current_tick
        and (goal.deadline is None or goal.deadline >= state.current_tick)
        for goal in state.goals
    )


def build_npc_plan_review_schedule(
    state: WorldState,
    namespace_id: UUID,
    actor_id: UUID,
    due_tick: int,
    *,
    repeat_ticks: int | None = None,
    max_runs: int = 1,
    excluded_actor_ids: Iterable[UUID] = (),
) -> ScheduledEventRecord:
    """Build the sole registered review schedule after current eligibility checks.

    The schedule is finite. By default it runs once; recurring reviews require
    an explicit positive interval and bounded run count. The host supplies
    account-controlled actors through ``excluded_actor_ids``.
    """
    excluded = set(excluded_actor_ids)
    actor = state.entities.get(actor_id)
    if (
        actor is None
        or actor.kind not in _CHARACTER_KINDS
        or actor.status != "active"
        or actor_id == state.player_id
        or actor_id in excluded
    ):
        raise InvalidEvent("NPC plan review requires an active, uncontrolled, non-player person.")
    if not _has_active_owned_goal(state, actor_id):
        raise InvalidEvent("NPC plan review requires at least one active goal owned by the actor.")
    if type(due_tick) is not int or not state.current_tick <= due_tick <= _MAX_TICK:
        raise InvalidEvent("NPC plan review due_tick must be at or after the current tick.")
    if type(max_runs) is not int or not 1 <= max_runs <= _MAX_RUNS:
        raise InvalidEvent("NPC plan review max_runs must be a bounded positive integer.")
    if repeat_ticks is not None and (
        type(repeat_ticks) is not int or not 1 <= repeat_ticks <= _MAX_TICK
    ):
        raise InvalidEvent("NPC plan review repeat_ticks must be a bounded positive integer.")
    if max_runs > 1 and repeat_ticks is None:
        raise InvalidEvent("A recurring NPC plan review requires repeat_ticks.")
    if repeat_ticks is not None and due_tick + repeat_ticks * (max_runs - 1) > _MAX_TICK:
        raise InvalidEvent("NPC plan review repeats exceed the signed world-clock range.")
    try:
        return ScheduledEventRecord(
            id=npc_plan_review_schedule_id(namespace_id, actor_id),
            due_tick=due_tick,
            phase=NPC_PLAN_REVIEW_PHASE,
            handler_id=NPC_PLAN_REVIEW_HANDLER_ID,
            handler_version=NPC_PLAN_REVIEW_HANDLER_VERSION,
            repeat_ticks=repeat_ticks,
            max_runs=max_runs,
            payload={"actor_id": str(actor_id)},
        )
    except Exception as exc:
        raise InvalidEvent("NPC plan review schedule configuration is invalid.") from exc


def validate_npc_plan_review_schedule(
    state: WorldState,
    schedule: ScheduledEventRecord,
    namespace_id: UUID,
    *,
    require_eligible: bool = False,
) -> UUID:
    """Validate the registered schedule envelope and return its actor ID.

    ``require_eligible=False`` intentionally permits an existing schedule to
    be cleaned up after actor death, player assignment, or goal expiry.
    """
    try:
        if not isinstance(namespace_id, UUID):
            raise ValueError("schedule namespace must be a UUID")
        parsed = ScheduledEventRecord.model_validate(schedule.model_dump(mode="python"))
        if (
            parsed.handler_id != NPC_PLAN_REVIEW_HANDLER_ID
            or parsed.handler_version != NPC_PLAN_REVIEW_HANDLER_VERSION
            or parsed.phase != NPC_PLAN_REVIEW_PHASE
            or set(parsed.payload) != {"actor_id"}
        ):
            raise ValueError("unregistered NPC goal-review schedule shape")
        actor_id = UUID(str(parsed.payload["actor_id"]))
        actor = state.entities.get(actor_id)
        if actor is None or actor.kind not in _CHARACTER_KINDS:
            raise ValueError("review actor is missing or is not a person")
        if parsed.id != npc_plan_review_schedule_id(namespace_id, actor_id):
            raise ValueError("review schedule ID is not stable for this namespace and actor")
        if require_eligible and (
            actor.status != "active"
            or actor_id == state.player_id
            or not _has_active_owned_goal(state, actor_id)
        ):
            raise ValueError("review actor has no current eligibility")
        return actor_id
    except InvalidEvent:
        raise
    except Exception as exc:
        raise InvalidEvent("NPC plan review schedule is malformed or unregistered.") from exc


def npc_planning_preflight_state(state: WorldState, namespace_id: UUID) -> WorldState:
    """Model only the clearing of current zero-time NPC review barriers.

    This is a hypothetical capability-preflight snapshot, never a committed
    review or evidence that a decision occurred. Environment, delivery and
    other world schedules are retained. The original state and proposal hash
    remain authoritative and execution must be checked again after review.
    """
    from .models import WorldState as RuntimeWorldState

    due = {schedule.id for schedule in queued_npc_plan_reviews(state, namespace_id)}
    if not due:
        return state
    schedules = []
    for schedule in state.scheduled_events:
        if schedule.id not in due:
            schedules.append(schedule)
            continue
        runs = schedule.completed_runs + 1
        repeats = schedule.repeat_ticks is not None and runs < schedule.max_runs
        next_tick = schedule.due_tick + schedule.repeat_ticks if repeats else schedule.due_tick
        if next_tick > 2**63 - 1:
            raise InvalidEvent("NPC review preflight exceeds the world-clock range.")
        schedules.append(schedule.model_copy(update={
            "revision": schedule.revision + 1, "completed_runs": runs,
            "status": "queued" if repeats else "completed", "due_tick": next_tick,
        }))
    return RuntimeWorldState.model_validate(state.model_dump(mode="python") | {"scheduled_events": schedules})


def build_npc_failure_review(state: WorldState, world_id: UUID, plan: NpcPlan) -> ScheduledEventRecord | None:
    """Rearm the original, exhausted review with an individually bounded occurrence."""
    if (
        state.metadata.get(NPC_FAILURE_REVIEW_POLICY) is not True
        or plan.status != "blocked" or not plan.last_failure
        or plan.next_review_tick is None or plan.next_review_tick <= state.current_tick
    ):
        return None
    old = _exhausted_npc_review(state, world_id, plan.actor_id)
    if old is None:
        return None
    due_tick = plan.next_review_tick
    if old.completed_runs > 1:
        due_tick = max(due_tick, old.due_tick + max(120, state.calendar.day_seconds // 24))
    if due_tick > _MAX_TICK or not any(
        goal.id in plan.goal_ids and goal.owner_id == plan.actor_id and goal.status == "active"
        and goal.created_tick <= state.current_tick
        and (goal.deadline is None or goal.deadline >= due_tick) for goal in state.goals
    ):
        return None
    return _rearm_npc_review(old, due_tick)


def _exhausted_npc_review(state: WorldState, world_id: UUID, actor_id: UUID) -> ScheduledEventRecord | None:
    schedule_id = npc_plan_review_schedule_id(world_id, actor_id)
    old = next((row for row in state.scheduled_events if row.id == schedule_id), None)
    if (
        old is None or old.status != "completed" or old.completed_runs != old.max_runs
        or old.completed_runs >= _AUTOMATIC_REVIEW_RUN_LIMIT
    ):
        return None
    validate_npc_plan_review_schedule(state, old, world_id)
    if state.entities[actor_id].status != "active" or actor_id == state.player_id:
        return None
    return old


def _rearm_npc_review(old: ScheduledEventRecord, due_tick: int) -> ScheduledEventRecord:
    return ScheduledEventRecord.model_validate(old.model_dump(mode="python") | {
        "revision": old.revision + 1,
        "status": "queued",
        "due_tick": due_tick,
        "max_runs": old.max_runs + 1,
        # Exactly one run remains after a rearm. The repeat field is required
        # by the existing schedule contract when the total run cap exceeds one.
        "repeat_ticks": old.repeat_ticks or 1,
    })


def build_npc_goal_followup_review(
    state: WorldState, world_id: UUID, plan: NpcPlan, fulfilled_ids: tuple[UUID, ...],
) -> ScheduledEventRecord | None:
    """Rearm an exhausted review once a plan unblocks an actor's next goal."""
    if state.metadata.get(NPC_GOAL_FOLLOWUP_REVIEW_POLICY) is not True or plan.status != "completed":
        return None
    old = _exhausted_npc_review(state, world_id, plan.actor_id)
    if old is None:
        return None
    due_tick = state.current_tick + 120
    if old.completed_runs > 1:
        due_tick = max(due_tick, old.due_tick + max(120, state.calendar.day_seconds // 24))
    if due_tick > _MAX_TICK:
        return None

    owned = [goal for goal in state.goals if goal.owner_id == plan.actor_id]
    newly_fulfilled = frozenset(fulfilled_ids)
    receipts = set(plan.evidence_refs)
    completed_by_plan = newly_fulfilled | {
        goal.id for goal in owned if goal.id in plan.goal_ids and goal.status == "succeeded"
        and receipts.intersection(goal.evidence_refs)
    }
    if not completed_by_plan:
        return None
    completed = newly_fulfilled | {goal.id for goal in owned if goal.status == "succeeded"}
    for goal in owned:
        if goal.id in completed or goal.status != "active" or (goal.deadline is not None and goal.deadline < due_tick):
            continue
        prerequisites = {condition.target_id for condition in goal.conditions if condition.kind == "goal_completed"}
        if (
            prerequisites & completed_by_plan and prerequisites <= completed
            and not npc_goal_conditions_met(state, goal, fulfilled=newly_fulfilled)
        ):
            return _rearm_npc_review(old, due_tick)
    return None


def with_npc_failure_review(state: WorldState, event: NpcPlanRevisedEvent) -> NpcPlanRevisedEvent:
    schedule = build_npc_failure_review(state, event.world_id, event.plan)
    if schedule is None:
        return event
    return event.model_copy(update={
        "automatic_review_tick": schedule.due_tick,
        "plan": event.plan.model_copy(update={"next_review_tick": schedule.due_tick}),
    })


def queued_npc_plan_reviews(
    state: WorldState,
    namespace_id: UUID,
    *,
    due_only: bool = True,
) -> list[ScheduledEventRecord]:
    """Enumerate registered queued reviews in stable due order.

    With ``due_only=True`` only an exact current-tick match is returned, and
    an overdue schedule is an error so the host cannot silently skip a review.
    """
    rows: list[ScheduledEventRecord] = []
    for schedule in state.scheduled_events:
        if schedule.handler_id != NPC_PLAN_REVIEW_HANDLER_ID or schedule.status != "queued":
            continue
        validate_npc_plan_review_schedule(state, schedule, namespace_id)
        if due_only:
            if schedule.due_tick < state.current_tick:
                raise InvalidEvent("NPC plan review schedule is overdue and must be handled explicitly.")
            if schedule.due_tick == state.current_tick:
                rows.append(schedule)
        else:
            rows.append(schedule)
    return sorted(rows, key=lambda item: (item.due_tick, item.priority, str(item.id)))
