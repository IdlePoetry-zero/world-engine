"""Persistent, bounded NPC plan records and pure state application.

Plans reference registered capabilities but do not execute them. A trusted
simulation host must validate capability registration and resolve evidence
UUIDs to committed execution events before accepting the plan events.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

from .components import ResourceName
from .json_types import JsonObject
from .npc_goal_completion import (
    NPC_GOAL_DEPENDENCY_COMPLETION_POLICY, npc_goal_conditions_met, npc_goal_fulfillment_allows_continuation,
    npc_goal_fulfillment_closure, npc_goal_fulfillment_enabled,
)
from .travel_journey_events import travel_event_id
from .work_shift import Tick

if TYPE_CHECKING:
    from .components import GoalRecord, ScheduledEventRecord
    from .models import WorldState
    from .npc_plan_events import NpcPlanCreatedEvent, NpcPlanRevisedEvent


MAX_NPC_PLAN_STEPS = 32
MAX_NPC_PLAN_GOALS = 16
MAX_NPC_PLAN_EVIDENCE_REFS = 64
MAX_NPC_PLAN_ARGUMENT_BYTES = 16_384
MAX_NPC_PLAN_TOTAL_ARGUMENT_BYTES = 65_536
MAX_NPC_PLAN_JSON_DEPTH = 8
MAX_NPC_PLAN_JSON_NODES = 2_048
_MAX_REVISION = 2**63 - 1
_MAX_ARGUMENT_STRING_LENGTH = 8_192
_MAX_ARGUMENT_KEY_LENGTH = 256
_CAPABILITY_ID_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_.-]*$"
_ACTIVE_STATUSES = frozenset({"ready", "running", "blocked"})
_TERMINAL_STATUSES = frozenset({"completed", "cancelled", "abandoned"})
_ALLOWED_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "ready": frozenset({"ready", "running", "blocked", "cancelled", "abandoned"}),
    "running": frozenset({"ready", "running", "blocked", "completed", "cancelled", "abandoned"}),
    "blocked": frozenset({"ready", "running", "blocked", "cancelled", "abandoned"}),
}


class NpcPlanModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class NpcPlanStep(NpcPlanModel):
    """One capability reference with a bounded JSON argument object."""

    capability_id: Annotated[
        StrictStr,
        Field(min_length=1, max_length=160, pattern=_CAPABILITY_ID_PATTERN),
    ]
    arguments: JsonObject = Field(default_factory=dict)

    @field_validator("arguments", mode="before")
    @classmethod
    def arguments_are_bounded_json(cls, value: object) -> JsonObject:
        if not isinstance(value, dict):
            raise ValueError("NPC plan step arguments must be a JSON object")
        if _json_depth_and_size(value) > MAX_NPC_PLAN_ARGUMENT_BYTES:
            raise ValueError("NPC plan step arguments exceed the JSON depth, node, or byte limit")
        return deepcopy(value)


class ActiveNpcExecution(NpcPlanModel):
    """A typed reference to an action that spans world intervals.

    This record does not prove that its referenced events were committed. A
    simulation host must verify the matching journey and resolution receipt
    against the current branch before consuming a step.
    """

    kind: Literal["journey"] = "journey"
    journey_id: UUID
    start_event_id: UUID
    start_command_id: UUID
    world_id: UUID
    branch_id: UUID
    ruleset_version: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    started_tick: Tick
    due_tick: Tick

    @model_validator(mode="after")
    def valid_journey_window(self) -> ActiveNpcExecution:
        if self.due_tick <= self.started_tick:
            raise ValueError("An active NPC journey must have a future due tick")
        if self.start_event_id != travel_event_id(
            self.start_command_id,
            self.ruleset_version,
            "depart",
            self.journey_id,
        ):
            raise ValueError("An active NPC journey start reference must match its command-bound departure")
        return self


class ActiveNpcExploration(NpcPlanModel):
    kind: Literal["exploration"] = "exploration"
    place_id: UUID
    start_event_id: UUID
    start_command_id: UUID
    world_id: UUID
    branch_id: UUID
    ruleset_version: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    started_tick: Tick
    due_tick: Tick

    @model_validator(mode="after")
    def exact_exploration_duration(self) -> ActiveNpcExploration:
        if self.due_tick - self.started_tick != NPC_EXPLORATION_TICKS:
            raise ValueError("An NPC exploration must occupy the registered search duration")
        return self


class ActiveNpcTrade(NpcPlanModel):
    kind: Literal["trade"] = "trade"
    market_id: UUID
    listing_id: UUID
    seller_id: UUID
    stock_account_id: UUID
    resource_id: Annotated[StrictStr, Field(min_length=1, max_length=160)]
    currency_resource_id: Annotated[StrictStr, Field(min_length=1, max_length=160)]
    unit_price_minor: Annotated[StrictInt, Field(ge=0, le=10**30 - 1)]
    start_event_id: UUID
    start_command_id: UUID
    world_id: UUID
    branch_id: UUID
    ruleset_version: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    started_tick: Tick
    due_tick: Tick

    @model_validator(mode="after")
    def exact_trade_duration(self) -> ActiveNpcTrade:
        if self.due_tick - self.started_tick != NPC_TRADE_TICKS:
            raise ValueError("An NPC trade must occupy the registered market duration")
        return self


class ActiveNpcTraining(NpcPlanModel):
    kind: Literal["training"] = "training"
    place_id: UUID | None
    skill_id: ResourceName
    start_event_id: UUID
    start_command_id: UUID
    world_id: UUID
    branch_id: UUID
    ruleset_version: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    started_tick: Tick
    due_tick: Tick

    @model_validator(mode="after")
    def bounded_training_duration(self) -> ActiveNpcTraining:
        if not 0 < self.due_tick - self.started_tick <= 8 * 60 * 60:
            raise ValueError("An NPC training interval must be positive and at most eight hours")
        return self


class ActiveNpcProduction(NpcPlanModel):
    kind: Literal["production"] = "production"
    place_id: UUID | None
    producer_id: UUID
    recipe_id: ResourceName
    recipe_terms_hash: Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]
    start_event_id: UUID
    start_command_id: UUID
    world_id: UUID
    branch_id: UUID
    ruleset_version: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    started_tick: Tick
    due_tick: Tick

    @model_validator(mode="after")
    def positive_production_duration(self) -> ActiveNpcProduction:
        if self.due_tick <= self.started_tick:
            raise ValueError("An NPC production interval must have a future due tick")
        return self


NpcTimedExecution = ActiveNpcExploration | ActiveNpcTrade | ActiveNpcTraining | ActiveNpcProduction
NPC_EXPLORATION_TICKS = 600
NPC_SHARED_EXPLORATION_POLICY = "npc_shared_exploration_v1"
NPC_TRADE_TICKS = 300
NPC_SHARED_TRADE_POLICY = "npc_shared_trade_v1"
NPC_SHARED_TRAINING_POLICY = "npc_shared_training_v1"
NPC_SHARED_PRODUCTION_POLICY = "npc_shared_production_v1"
_TIMED_CAPABILITY = {"exploration": "explore", "trade": "trade", "training": "train", "production": "produce"}
_TIMED_POLICY = {
    "exploration": NPC_SHARED_EXPLORATION_POLICY,
    "trade": NPC_SHARED_TRADE_POLICY,
    "training": NPC_SHARED_TRAINING_POLICY,
    "production": NPC_SHARED_PRODUCTION_POLICY,
}


class NpcPlan(NpcPlanModel):
    """A revisioned NPC intention, separate from executed world events.

    Evidence references are UUID-shaped links only at this layer; this model
    does not assert that the referenced execution events exist.
    """

    plan_id: UUID
    actor_id: UUID
    goal_ids: list[UUID] = Field(min_length=1, max_length=MAX_NPC_PLAN_GOALS)
    revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    status: Literal["ready", "running", "blocked", "completed", "cancelled", "abandoned"]
    created_tick: Tick
    updated_tick: Tick
    next_review_tick: Tick | None = None
    steps: list[NpcPlanStep] = Field(min_length=1, max_length=MAX_NPC_PLAN_STEPS)
    current_step: StrictInt = Field(ge=0, le=MAX_NPC_PLAN_STEPS)
    attempts: StrictInt = Field(default=0, ge=0, le=_MAX_REVISION)
    last_failure: Annotated[StrictStr, Field(min_length=1, max_length=2000)] | None = None
    reason: Annotated[StrictStr, Field(min_length=1, max_length=2000)]
    evidence_refs: list[UUID] = Field(default_factory=list, max_length=MAX_NPC_PLAN_EVIDENCE_REFS)
    active_execution: ActiveNpcExecution | NpcTimedExecution | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @field_validator("goal_ids")
    @classmethod
    def goal_references_are_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("NPC plan goal_ids must be unique")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def evidence_references_are_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("NPC plan evidence_refs must be unique")
        return value

    @model_validator(mode="after")
    def lifecycle_shape_is_valid(self) -> NpcPlan:
        if self.updated_tick < self.created_tick:
            raise ValueError("NPC plan updated_tick cannot precede created_tick")
        if self.current_step > len(self.steps):
            raise ValueError("NPC plan current_step cannot exceed its step count")
        if self.status in _ACTIVE_STATUSES:
            if self.current_step >= len(self.steps):
                raise ValueError("An active NPC plan must have an unconsumed step")
            if self.next_review_tick is None or self.next_review_tick < self.updated_tick:
                raise ValueError("An active NPC plan requires a next_review_tick at or after updated_tick")
        else:
            if self.next_review_tick is not None:
                raise ValueError("A terminal NPC plan cannot have a next_review_tick")
        if self.status == "completed":
            if self.current_step != len(self.steps):
                raise ValueError("A completed NPC plan must have consumed every step")
            if not self.evidence_refs:
                raise ValueError("A completed NPC plan requires at least one evidence reference")
            if self.last_failure is not None:
                raise ValueError("A completed NPC plan must explicitly clear last_failure")
        if self.active_execution is not None:
            if self.status != "running":
                raise ValueError("An active NPC execution requires a running plan")
            if self.active_execution.started_tick != self.updated_tick:
                raise ValueError("An active NPC execution must start at the plan's updated tick")
            if self.next_review_tick != self.active_execution.due_tick:
                raise ValueError("An active NPC execution must be reviewed at its due tick")
            if isinstance(self.active_execution, NpcTimedExecution):
                active = self.active_execution
                action = _TIMED_CAPABILITY[active.kind]
                if self.steps[self.current_step].capability_id != f"world.action.{action}.v1":
                    raise ValueError("An active NPC action must match its capability step")
                if active.start_event_id != uuid5(
                    active.start_command_id, f"{active.ruleset_version}:npc_plan.revised:{self.plan_id}:{self.revision}",
                ):
                    raise ValueError("NPC timed execution must reference its exact start revision")
        total_argument_bytes = sum(_json_depth_and_size(step.arguments) for step in self.steps)
        if total_argument_bytes > MAX_NPC_PLAN_TOTAL_ARGUMENT_BYTES:
            raise ValueError("NPC plan step arguments exceed the total JSON byte limit")
        return self


def npc_journey_ids(state: WorldState) -> set[UUID]:
    return {
        plan.active_execution.journey_id for plan in state.npc_plans
        if isinstance(plan.active_execution, ActiveNpcExecution)
    }


def occupied_npc_ids(state: WorldState, at_tick: int) -> frozenset[UUID]:
    return frozenset(
        plan.actor_id for plan in state.npc_plans
        if isinstance(plan.active_execution, NpcTimedExecution)
        and plan.active_execution.started_tick <= at_tick < plan.active_execution.due_tick
    )


def _json_depth_and_size(value: JsonObject) -> int:
    """Validate JSON shape and return encoded byte length, or raise ValueError."""
    node_count = 0

    def visit(node: object, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > MAX_NPC_PLAN_JSON_NODES:
            raise ValueError("NPC plan JSON contains too many values")
        if depth > MAX_NPC_PLAN_JSON_DEPTH:
            raise ValueError("NPC plan JSON is nested too deeply")
        if node is None or type(node) is bool:
            return
        if type(node) is int:
            if not -(2**63) <= node <= 2**63 - 1:
                raise ValueError("NPC plan JSON integers must fit a signed 64-bit value")
            return
        if type(node) is float:
            if not math.isfinite(node):
                raise ValueError("NPC plan JSON numbers must be finite")
            return
        if isinstance(node, str):
            if len(node) > _MAX_ARGUMENT_STRING_LENGTH:
                raise ValueError("NPC plan JSON strings exceed the per-value length limit")
            return
        if isinstance(node, list):
            for item in node:
                visit(item, depth + 1)
            return
        if isinstance(node, dict):
            for key, item in node.items():
                if not isinstance(key, str) or len(key) > _MAX_ARGUMENT_KEY_LENGTH:
                    raise ValueError("NPC plan JSON object keys must be bounded strings")
                visit(item, depth + 1)
            return
        raise ValueError("NPC plan arguments must contain only JSON values")

    if not isinstance(value, dict):
        raise ValueError("NPC plan step arguments must be a JSON object")
    visit(value, 1)
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("NPC plan arguments are not valid UTF-8 JSON") from exc
    if size > MAX_NPC_PLAN_ARGUMENT_BYTES:
        raise ValueError("NPC plan step arguments exceed the JSON byte limit")
    return size


def _validated_event_payload(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    return value


def _validate_actor_and_goals(
    state: WorldState,
    plan: NpcPlan,
    *,
    mode: Literal["create", "continue", "complete", "terminate"],
) -> None:
    actor = state.entities.get(plan.actor_id)
    if actor is None or actor.kind not in {"character", "person"}:
        raise ValueError("NPC plan actor must reference an existing character")
    if mode in {"create", "continue"} and (
        actor.status != "active"
        or plan.actor_id == state.player_id
    ):
        raise ValueError("Creating or continuing an NPC plan requires an active non-player character")

    goals_by_id = {goal.id: goal for goal in state.goals}
    for goal_id in plan.goal_ids:
        goal = goals_by_id.get(goal_id)
        if goal is None or goal.owner_id != plan.actor_id:
            raise ValueError("NPC plan goals must belong to its actor")
        if mode == "continue" and npc_goal_fulfillment_allows_continuation(state, goal):
            continue
        if mode in {"create", "continue"} and (
            goal.status != "active" or (goal.deadline is not None and goal.deadline < state.current_tick)
        ):
            raise ValueError("NPC plan goals must remain active and pursuable")
        if mode == "complete" and (
            goal.status not in {"active", "succeeded"}
            or (goal.status == "active" and goal.deadline is not None and goal.deadline < state.current_tick)
        ):
            raise ValueError("A completed NPC plan cannot claim a failed, abandoned, or expired goal")


def _validate_plan_time(plan: NpcPlan, current_tick: int, *, creating: bool) -> None:
    if plan.updated_tick != current_tick:
        raise ValueError("NPC plan updated_tick must equal the current world tick")
    if plan.updated_tick < plan.created_tick:
        raise ValueError("NPC plan updated_tick cannot precede created_tick")
    if creating and plan.created_tick != current_tick:
        raise ValueError("A new NPC plan must be created at the current world tick")


def _apply_plan_payload(
    state: WorldState, plans: list[NpcPlan], *, review_schedule: ScheduledEventRecord | None = None,
    goals: list[GoalRecord] | None = None,
) -> WorldState:
    from .models import WorldState as RuntimeWorldState

    payload = state.model_dump(mode="python")
    payload["npc_plans"] = [plan.model_dump(mode="python") for plan in plans]
    if goals is not None:
        payload["goals"] = goals
    if review_schedule is not None:
        payload["scheduled_events"] = [
            review_schedule if row.id == review_schedule.id else row for row in state.scheduled_events
        ]
    return RuntimeWorldState.model_validate(payload)


def apply_npc_plan_created(
    state: WorldState,
    event: NpcPlanCreatedEvent | dict[str, object],
) -> WorldState:
    """Apply one new revision-1 ready plan and validate the complete world state."""
    from .errors import InvalidEvent
    from .npc_plan_events import NpcPlanCreatedEvent

    try:
        event = NpcPlanCreatedEvent.model_validate(_validated_event_payload(event))
        event.require_author_visibility()
        plan = NpcPlan.model_validate(event.plan.model_dump(mode="python"))
        if event.actor_id != plan.actor_id:
            raise ValueError("NPC plan creation event actor must match the plan actor")
        if plan.revision != 1 or plan.status != "ready" or plan.current_step != 0:
            raise ValueError("A new NPC plan must be revision 1, ready, and at step 0")
        if plan.attempts != 0 or plan.last_failure is not None or plan.evidence_refs:
            raise ValueError("A new NPC plan must have no attempts, failure, or completion evidence")
        _validate_plan_time(plan, state.current_tick, creating=True)
        _validate_actor_and_goals(state, plan, mode="create")
        if any(existing.plan_id == plan.plan_id for existing in state.npc_plans):
            raise ValueError("NPC plan ID already exists")
        if plan.next_review_tick is None or plan.next_review_tick < state.current_tick:
            raise ValueError("A new NPC plan requires a next_review_tick at or after the current world tick")
        plans = sorted([*state.npc_plans, plan], key=lambda item: str(item.plan_id))
        return _apply_plan_payload(state, plans)
    except InvalidEvent:
        raise
    except Exception as exc:
        raise InvalidEvent(f"NPC plan creation is invalid: {exc}") from exc


def _validate_plan_revision(previous: NpcPlan, revised: NpcPlan, expected_revision: int) -> None:
    if previous.revision != expected_revision:
        raise ValueError("NPC plan expected_revision does not match stored state")
    if revised.revision != expected_revision + 1:
        raise ValueError("NPC plan revision must increment exactly once")
    if revised.plan_id != previous.plan_id or revised.actor_id != previous.actor_id:
        raise ValueError("NPC plan ID and actor are immutable")
    if revised.created_tick != previous.created_tick:
        raise ValueError("NPC plan created_tick is immutable")
    if previous.status in _TERMINAL_STATUSES:
        raise ValueError(f"A {previous.status} NPC plan cannot be revised")
    if revised.updated_tick < previous.updated_tick:
        raise ValueError("NPC plan updated_tick cannot move backwards")
    if revised.current_step < previous.current_step:
        raise ValueError("NPC plan current_step cannot move backwards")
    if revised.steps[:previous.current_step] != previous.steps[:previous.current_step]:
        raise ValueError("Consumed NPC plan steps are immutable")
    if revised.attempts < previous.attempts:
        raise ValueError("NPC plan attempts cannot move backwards")
    if revised.status not in _ALLOWED_STATUS_TRANSITIONS[previous.status]:
        raise ValueError(f"NPC plan cannot transition from {previous.status} to {revised.status}")
    if revised.status == "completed":
        if revised.current_step != len(revised.steps):
            raise ValueError("A completed NPC plan must have consumed every step")
        if not revised.evidence_refs:
            raise ValueError("A completed NPC plan requires at least one evidence reference")
        if revised.last_failure is not None:
            raise ValueError("A completed NPC plan must explicitly clear last_failure")


def apply_npc_plan_revised(
    state: WorldState,
    event: NpcPlanRevisedEvent | dict[str, object],
) -> WorldState:
    """Apply one compare-and-swap plan revision and validate the complete world state."""
    from .errors import InvalidEvent
    from .npc_plan_events import NpcPlanRevisedEvent

    try:
        event = NpcPlanRevisedEvent.model_validate(_validated_event_payload(event))
        event.require_author_visibility()
        revised = NpcPlan.model_validate(event.plan.model_dump(mode="python"))
        if event.actor_id != revised.actor_id:
            raise ValueError("NPC plan revision event actor must match the plan actor")
        stored = next((plan for plan in state.npc_plans if plan.plan_id == revised.plan_id), None)
        if stored is None:
            raise ValueError("NPC plan to revise does not exist")
        previous = NpcPlan.model_validate(stored.model_dump(mode="python"))
        _validate_plan_revision(previous, revised, event.expected_revision)
        _validate_plan_time(revised, state.current_tick, creating=False)
        results = [
            (kind, result) for kind, result in (
                ("exploration", event.exploration_result), ("trade", event.trade_result),
                ("training", event.training_result), ("production", event.production_result),
            ) if result is not None
        ]
        if results:
            (kind, result), = results
            active = previous.active_execution
            if not isinstance(active, NpcTimedExecution) or active.kind != kind or active.due_tick != state.current_tick:
                raise ValueError("A timed result must settle a matching due action")
            completed = result in {
                "discovered", "nothing_hidden_found", "trade_completed", "training_completed", "production_completed",
            }
            if revised.current_step != previous.current_step + int(completed):
                raise ValueError("A timed result cannot consume a different step")
            if completed and event.event_id not in revised.evidence_refs:
                raise ValueError("A completed timed action must retain its result receipt")
        elif isinstance(previous.active_execution, NpcTimedExecution) and revised.status not in {"cancelled", "abandoned"}:
            raise ValueError("An active timed action requires a result or terminal interruption")
        if isinstance(revised.active_execution, NpcTimedExecution):
            active = revised.active_execution
            place_id = active.market_id if isinstance(active, ActiveNpcTrade) else active.place_id
            if state.metadata.get(_TIMED_POLICY[active.kind]) is not True:
                raise ValueError("The shared NPC action is not enabled in this world")
            if revised.actor_id in occupied_npc_ids(state, state.current_tick):
                raise ValueError("An NPC cannot start overlapping timed actions")
            if (
                previous.status not in {"ready", "blocked"}
                or previous.current_step != revised.current_step
                or previous.steps != revised.steps
                or state.entities[revised.actor_id].location_id != place_id
                or (active.start_event_id, active.start_command_id, active.world_id, active.branch_id, active.ruleset_version)
                != (event.event_id, event.command_id, event.world_id, event.branch_id, event.ruleset_version)
            ):
                raise ValueError("A timed action must start at its exact actor, step and command boundary")
        validation_mode: Literal["continue", "complete", "terminate"] = (
            "complete" if revised.status == "completed"
            else "terminate" if revised.status in {"cancelled", "abandoned"}
            else "continue"
        )
        _validate_actor_and_goals(state, revised, mode=validation_mode)
        review_schedule = None
        if event.automatic_review_tick is not None:
            from .npc_plan_review import build_npc_failure_review

            review_schedule = build_npc_failure_review(state, event.world_id, revised)
            if review_schedule is None or review_schedule.due_tick != event.automatic_review_tick:
                raise ValueError("Automatic review does not match the exhausted actor schedule")
        plans = [revised if plan.plan_id == revised.plan_id else plan for plan in state.npc_plans]
        plans.sort(key=lambda item: str(item.plan_id))
        goals = None
        if event.fulfilled_goal_ids:
            new_evidence = [reference for reference in revised.evidence_refs if reference not in previous.evidence_refs]
            if (
                revised.current_step != previous.current_step + 1
                or not new_evidence
            ):
                raise ValueError("Goal fulfillment requires a newly consumed step with actual evidence")
            claimed_ids = frozenset(event.fulfilled_goal_ids)
            claimed_goals = [
                goal for goal in state.goals if goal.id in claimed_ids and goal.status == "active"
                and npc_goal_fulfillment_enabled(state, goal, plan_completed=revised.status == "completed")
            ]
            if state.metadata.get(NPC_GOAL_DEPENDENCY_COMPLETION_POLICY) is True:
                verified_ids = npc_goal_fulfillment_closure(state, claimed_goals)
            else:
                verified_ids = frozenset(goal.id for goal in claimed_goals if npc_goal_conditions_met(state, goal))
            if verified_ids != claimed_ids:
                raise ValueError("Only still-active goals whose conditions hold can be fulfilled")
            goals = []
            for goal in state.goals:
                if goal.id in verified_ids:
                    goal = goal.model_copy(update={
                        "status": "succeeded", "progress": 100, "updated_tick": state.current_tick,
                        "revision": goal.revision + 1,
                        "evidence_refs": list(dict.fromkeys([*goal.evidence_refs, *new_evidence])),
                    })
                goals.append(goal)
        if event.goal_followup_review_tick is not None:
            from .npc_plan_review import build_npc_goal_followup_review

            review_schedule = build_npc_goal_followup_review(state, event.world_id, revised, event.fulfilled_goal_ids)
            if (revised.current_step != previous.current_step + 1 or review_schedule is None
                    or review_schedule.due_tick != event.goal_followup_review_tick):
                raise ValueError("Goal follow-up review does not match the actor schedule")
        return _apply_plan_payload(state, plans, review_schedule=review_schedule, goals=goals)
    except InvalidEvent:
        raise
    except Exception as exc:
        raise InvalidEvent(f"NPC plan revision is invalid: {exc}") from exc
