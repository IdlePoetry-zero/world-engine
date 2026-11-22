"""Versioned author-only events for persistent NPC plan lifecycle records."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from .json_types import JsonObject
from .npc_plans import MAX_NPC_PLAN_GOALS, NpcPlan
from .work_shift import Tick


NPC_PLAN_CREATED_EVENT_TYPE = "core.npc_plan_created.v1"
NPC_PLAN_REVISED_EVENT_TYPE = "core.npc_plan_revised.v1"
_AUTHOR_ONLY = {"policy": "author_only"}
_MAX_REVISION = 2**63 - 1


def npc_plan_event_id(
    command_id: UUID,
    ruleset_version: str,
    operation: Literal["created", "revised"],
    plan_id: UUID,
    revision: int,
) -> UUID:
    """Return a command-bound deterministic event ID for one plan revision."""
    if not isinstance(command_id, UUID) or not isinstance(plan_id, UUID):
        raise ValueError("NPC plan event IDs require UUID command and plan IDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("NPC plan event IDs require a nonempty ruleset version")
    if operation not in {"created", "revised"}:
        raise ValueError("NPC plan operation must be created or revised")
    if type(revision) is not int or not 1 <= revision <= _MAX_REVISION:
        raise ValueError("NPC plan event revision must be a positive signed 64-bit integer")
    return uuid5(
        command_id,
        f"{ruleset_version}:npc_plan.{operation}:{plan_id}:{revision}",
    )


class NpcPlanEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class NpcPlanCreatedEvent(NpcPlanEventModel):
    """Persist a new ready plan with its first revision."""

    type: Literal["core.npc_plan_created.v1"] = NPC_PLAN_CREATED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    plan: NpcPlan
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    def require_author_visibility(self) -> None:
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("NPC plan events have fixed author_only visibility")

    @model_validator(mode="after")
    def creation_event_is_bound_and_private(self) -> NpcPlanCreatedEvent:
        if self.event_id != npc_plan_event_id(
            self.command_id, self.ruleset_version, "created", self.plan.plan_id, self.plan.revision,
        ):
            raise ValueError("NPC plan creation event ID must be bound to its command, plan, and revision")
        if self.actor_id != self.plan.actor_id:
            raise ValueError("NPC plan creation event actor must match the plan actor")
        if self.plan.revision != 1 or self.plan.status != "ready" or self.plan.current_step != 0:
            raise ValueError("A new NPC plan must be revision 1, ready, and at step 0")
        self.require_author_visibility()
        return self


class NpcPlanRevisedEvent(NpcPlanEventModel):
    """Persist one compare-and-swap revision of an existing plan."""

    type: Literal["core.npc_plan_revised.v1"] = NPC_PLAN_REVISED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    actor_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    plan: NpcPlan
    expected_revision: StrictInt = Field(ge=1, le=_MAX_REVISION - 1)
    control_interruption: Literal["account_control"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    exploration_result: Literal["discovered", "nothing_hidden_found", "search_failed", "interrupted"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    trade_result: Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_]*$")] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    training_result: Literal["training_completed", "interrupted", "skill_not_registered", "skills_unavailable",
                             "progression_unavailable"] | None = Field(default=None, exclude_if=lambda value: value is None)
    production_result: Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-z][a-z0-9_]*$")] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    automatic_review_tick: Tick | None = Field(default=None, exclude_if=lambda value: value is None)
    goal_followup_review_tick: Tick | None = Field(default=None, exclude_if=lambda value: value is None)
    fulfilled_goal_ids: tuple[UUID, ...] = Field(default=(), max_length=MAX_NPC_PLAN_GOALS, exclude_if=lambda value: not value)
    visibility: JsonObject = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    def require_author_visibility(self) -> None:
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("NPC plan events have fixed author_only visibility")

    @model_validator(mode="after")
    def revision_event_is_bound_and_private(self) -> NpcPlanRevisedEvent:
        if self.event_id != npc_plan_event_id(
            self.command_id, self.ruleset_version, "revised", self.plan.plan_id, self.plan.revision,
        ):
            raise ValueError("NPC plan revision event ID must be bound to its command, plan, and revision")
        if self.actor_id != self.plan.actor_id:
            raise ValueError("NPC plan revision event actor must match the plan actor")
        if self.plan.revision != self.expected_revision + 1:
            raise ValueError("NPC plan revision event must increment expected_revision exactly once")
        if self.control_interruption is not None and (
            self.plan.status != "abandoned"
            or self.plan.active_execution is not None
            or self.plan.next_review_tick is not None
        ):
            raise ValueError(
                "An account-control interruption can appear only on a terminal abandoned NPC plan revision"
            )
        if self.exploration_result is not None and (
            self.plan.active_execution is not None
            or self.plan.status not in (
                {"ready", "completed"} if self.exploration_result in {"discovered", "nothing_hidden_found"} else {"blocked"}
            )
        ):
            raise ValueError("An exploration result must settle its matching plan step")
        if self.trade_result is not None and (
            self.exploration_result is not None or self.plan.active_execution is not None
            or self.plan.status not in ({"ready", "completed"} if self.trade_result == "trade_completed" else {"blocked"})
        ):
            raise ValueError("A trade result must settle its matching plan step")
        if self.training_result is not None and (
            self.exploration_result is not None or self.trade_result is not None or self.plan.active_execution is not None
            or self.plan.status not in ({"ready", "completed"} if self.training_result == "training_completed" else {"blocked"})
        ):
            raise ValueError("A training result must settle its matching plan step")
        if self.production_result is not None and (
            any(result is not None for result in (self.exploration_result, self.trade_result, self.training_result))
            or self.plan.active_execution is not None
            or self.plan.status not in ({"ready", "completed"} if self.production_result == "production_completed" else {"blocked"})
        ):
            raise ValueError("A production result must settle its matching plan step")
        if self.automatic_review_tick is not None and (
            self.plan.status != "blocked" or self.plan.next_review_tick != self.automatic_review_tick
            or self.automatic_review_tick <= self.plan.updated_tick or not self.plan.last_failure
        ):
            raise ValueError("An automatic review must bind to a future blocked-step retry")
        if self.goal_followup_review_tick is not None and (
            self.plan.status != "completed" or self.goal_followup_review_tick <= self.plan.updated_tick
        ):
            raise ValueError("A goal follow-up review requires a completed plan and a future tick")
        if self.fulfilled_goal_ids and (
            self.plan.status not in {"ready", "completed"}
            or tuple(sorted(set(self.fulfilled_goal_ids), key=str)) != self.fulfilled_goal_ids
            or not set(self.fulfilled_goal_ids).issubset(self.plan.goal_ids)
        ):
            raise ValueError("A consumed step may fulfill only its unique, sorted, linked goals")
        self.require_author_visibility()
        return self


NpcPlanEvent = Annotated[NpcPlanCreatedEvent | NpcPlanRevisedEvent, Field(discriminator="type")]
NPC_PLAN_EVENT_ADAPTER = TypeAdapter(NpcPlanEvent)
NPC_PLAN_EVENTS = (NpcPlanCreatedEvent, NpcPlanRevisedEvent)


def parse_npc_plan_event(value: NpcPlanEvent | dict[str, object]) -> NpcPlanEvent:
    """Parse one registered-shape NPC plan lifecycle event."""
    return NPC_PLAN_EVENT_ADAPTER.validate_python(value)
