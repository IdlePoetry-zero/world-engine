"""Author-only event for one exact, scheduled NPC plan review occurrence."""

from __future__ import annotations

from typing import Annotated, Literal, TYPE_CHECKING
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, TypeAdapter, field_validator, model_validator

from .components import ScheduledEventRecord
from .errors import InvalidEvent
from .relationship_promises import PromiseReviewChoice
from .npc_plan_review import (
    NPC_PLAN_REVIEW_HANDLER_ID,
    NPC_PLAN_REVIEW_HANDLER_VERSION,
    NPC_PLAN_REVIEW_PHASE,
    NPC_MODEL_UNAVAILABLE_REASON,
    NPC_MODEL_OUTPUT_DEFER_REASONS,
    build_npc_plan_review_schedule,
    build_npc_provider_unavailable_review,
    npc_plan_review_schedule_id,
    _npc_plan_review_request_id_for_occurrence,
    validate_npc_plan_review_schedule,
)

if TYPE_CHECKING:
    from .models import WorldState

NPC_PLAN_REVIEW_RESOLVED_EVENT_TYPE = "core.npc_plan_review_resolved.v1"
NPC_PLAN_REVIEW_OUTCOMES = frozenset({"ready", "defer", "invalid", "skipped"})
_AUTHOR_ONLY = {"policy": "author_only"}
_MIN_TICK = -(2**63)
_MAX_TICK = 2**63 - 1
_MAX_REVISION = 2**63 - 1
_REASON_PATTERN = r"^[a-z][a-z0-9_.-]{0,79}$"
_CHARACTER_KINDS = frozenset({"character", "person"})


def npc_plan_review_event_id(
    command_id: UUID,
    ruleset_version: str,
    scheduled_event_id: UUID,
    expected_schedule_revision: int,
) -> UUID:
    """Return the deterministic event ID for one command-bound occurrence."""
    if not isinstance(command_id, UUID) or not isinstance(scheduled_event_id, UUID):
        raise ValueError("NPC plan review event IDs require command and schedule UUIDs")
    if not isinstance(ruleset_version, str) or not ruleset_version.strip():
        raise ValueError("NPC plan review event IDs require a ruleset version")
    if (
        type(expected_schedule_revision) is not int
        or not 1 <= expected_schedule_revision <= _MAX_REVISION
    ):
        raise ValueError("NPC plan review event revision must be a positive signed 64-bit integer")
    return uuid5(
        command_id,
        f"{ruleset_version}:npc_plan_review:{scheduled_event_id}:{expected_schedule_revision}",
    )


class NpcPlanReviewEventModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class NpcPlanReviewScheduledEvent(NpcPlanReviewEventModel):
    """Record an explicit, bounded author enrollment for an existing goal."""

    type: Literal["core.npc_plan_review_scheduled.v1"] = "core.npc_plan_review_scheduled.v1"
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    author_id: UUID
    actor_id: UUID
    expected_previous_revision: StrictInt | None = Field(default=None, ge=1)
    schedule: ScheduledEventRecord
    visibility: dict[str, str] = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    @model_validator(mode="after")
    def validate_binding(self):
        if self.event_id != uuid5(self.command_id, f"schedule-npc-review:{self.actor_id}"):
            raise ValueError("NPC review enrollment must use a command-bound event ID")
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("NPC review enrollment is author-only")
        return self


def plan_authored_npc_review(
    state: WorldState, world_id: UUID, actor_id: UUID, *, max_runs: int, repeat_ticks: int | None,
) -> tuple[ScheduledEventRecord, int | None]:
    schedule = build_npc_plan_review_schedule(
        state, world_id, actor_id, state.current_tick + state.calendar.day_seconds,
        max_runs=max_runs, repeat_ticks=repeat_ticks,
    )
    if any(
        row.handler_id == NPC_PLAN_REVIEW_HANDLER_ID and row.status == "queued"
        and row.payload.get("actor_id") == str(actor_id) for row in state.scheduled_events
    ):
        raise InvalidEvent("This NPC already has a queued review series.")
    previous = next((row for row in state.scheduled_events if row.id == schedule.id), None)
    if previous is None:
        return schedule, None
    validate_npc_plan_review_schedule(state, previous, world_id)
    return schedule.model_copy(update={"revision": previous.revision + 1}), previous.revision


def apply_npc_plan_review_scheduled(state: WorldState, event: NpcPlanReviewScheduledEvent) -> list[ScheduledEventRecord]:
    schedule, revision = plan_authored_npc_review(
        state, event.world_id, event.actor_id, max_runs=event.schedule.max_runs,
        repeat_ticks=event.schedule.repeat_ticks,
    )
    if event.schedule != schedule or event.expected_previous_revision != revision:
        raise InvalidEvent("NPC review enrollment does not match its current eligible actor and schedule.")
    return [*[row for row in state.scheduled_events if row.id != schedule.id], schedule]


def validate_authored_npc_review_scope(events, *, world_id, branch_id, command_id, command_actor_id):
    for event in events:
        if isinstance(event, NpcPlanReviewScheduledEvent) and (
            event.world_id, event.branch_id, event.command_id, event.author_id,
        ) != (world_id, branch_id, command_id, command_actor_id):
            raise InvalidEvent("NPC review enrollment does not match the current command scope.")


class NpcPlanReviewResolvedEvent(NpcPlanReviewEventModel):
    """Resolve one due review as a proposal result, without executing its plan."""

    type: Literal["core.npc_plan_review_resolved.v1"] = NPC_PLAN_REVIEW_RESOLVED_EVENT_TYPE
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    scheduled_event_id: UUID
    expected_schedule_revision: StrictInt = Field(ge=1, le=_MAX_REVISION)
    actor_id: UUID
    at_tick: StrictInt = Field(ge=_MIN_TICK, le=_MAX_TICK)
    decision_request_id: UUID
    based_on_state_version: StrictInt = Field(ge=0, le=_MAX_REVISION)
    source: Literal["model", "policy"]
    outcome: Literal["ready", "defer", "invalid", "skipped"]
    plan_id: UUID | None = None
    promise_choice: PromiseReviewChoice | None = Field(default=None, exclude_if=lambda value: value is None)
    model_call_id: Annotated[StrictStr, Field(min_length=1, max_length=128)] | None = None
    reason: Annotated[StrictStr, Field(min_length=1, max_length=80, pattern=_REASON_PATTERN)]
    visibility: dict[str, str] = Field(default_factory=lambda: dict(_AUTHOR_ONLY))

    def require_author_visibility(self) -> None:
        if self.visibility != _AUTHOR_ONLY:
            raise ValueError("NPC plan review events have fixed author_only visibility")

    @field_validator("model_call_id")
    @classmethod
    def model_call_id_is_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("model_call_id must be a bounded single-line trace identifier")
        return value

    @model_validator(mode="after")
    def review_event_is_bound_and_private(self) -> NpcPlanReviewResolvedEvent:
        if self.event_id != npc_plan_review_event_id(
            self.command_id,
            self.ruleset_version,
            self.scheduled_event_id,
            self.expected_schedule_revision,
        ):
            raise ValueError("NPC plan review event ID must bind command, schedule, and revision")
        if self.decision_request_id != _npc_plan_review_request_id_for_occurrence(
            self.command_id,
            self.ruleset_version,
            self.scheduled_event_id,
            self.expected_schedule_revision,
        ):
            raise ValueError("NPC plan review request ID must bind command, ruleset, schedule, and revision")
        if self.promise_choice is not None and self.outcome != "defer":
            raise ValueError("A recorded promise choice requires a deferred plan review.")
        if (self.outcome == "ready") != (self.plan_id is not None):
            raise ValueError("Only a ready NPC plan review may reference a plan_id")
        if self.source == "model" and self.model_call_id is None:
            raise ValueError("Model-sourced NPC plan review requires a model_call_id")
        if self.source == "policy" and self.model_call_id is not None:
            raise ValueError("Policy-sourced NPC plan review cannot claim a model_call_id")
        if self.outcome == "skipped" and self.source != "policy":
            raise ValueError("A skipped NPC plan review must be resolved by policy")
        if self.reason == NPC_MODEL_UNAVAILABLE_REASON and (
            self.source != "policy" or self.outcome != "defer"
        ):
            raise ValueError("Model unavailability must be recorded as a policy-sourced defer")
        if self.reason in NPC_MODEL_OUTPUT_DEFER_REASONS and self.outcome == "defer" and (
            self.source != "model" or self.model_call_id is None
        ):
            raise ValueError("An unusable model planning result must retain its model call as a defer")
        self.require_author_visibility()
        return self


NpcPlanReviewEvent = Annotated[NpcPlanReviewResolvedEvent, Field(discriminator="type")]
NPC_PLAN_REVIEW_EVENT_ADAPTER = TypeAdapter(NpcPlanReviewEvent)
NPC_PLAN_REVIEW_EVENTS = (NpcPlanReviewResolvedEvent,)


def parse_npc_plan_review_event(
    value: NpcPlanReviewEvent | dict[str, object],
) -> NpcPlanReviewResolvedEvent:
    """Parse one registered-shape NPC review event."""
    return NPC_PLAN_REVIEW_EVENT_ADAPTER.validate_python(value)


def _actor_has_current_goal(state: WorldState, actor_id: UUID) -> bool:
    return any(
        goal.owner_id == actor_id
        and goal.status == "active"
        and goal.created_tick <= state.current_tick
        and (goal.deadline is None or goal.deadline >= state.current_tick)
        for goal in state.goals
    )


def _validate_ready_plan(state: WorldState, event: NpcPlanReviewResolvedEvent) -> None:
    from .npc_plans import NpcPlan

    if event.plan_id is None:
        raise InvalidEvent("A ready NPC plan review requires a plan ID.")
    plan = next((item for item in state.npc_plans if item.plan_id == event.plan_id), None)
    if plan is None:
        raise InvalidEvent("A ready NPC plan review must include its created plan in the transaction state.")
    if (
        plan.actor_id != event.actor_id
        or plan.status != "ready"
        or plan.revision != 1
        or plan.current_step != 0
        or plan.created_tick != state.current_tick
        or plan.updated_tick != state.current_tick
    ):
        raise InvalidEvent("A ready NPC plan review must reference this actor's new current-tick ready plan.")
    expected_plan_id = uuid5(
        event.decision_request_id,
        f"npc.plan.v1:{event.actor_id}:{event.based_on_state_version}",
    )
    if plan.plan_id != expected_plan_id:
        raise InvalidEvent("Ready NPC plan ID is not bound to the decision request and state version.")
    if event.plan_id != plan.plan_id:
        raise InvalidEvent("Ready NPC plan review references the wrong plan.")
    # The import validates the stored record schema without making this layer
    # responsible for capability registration or execution evidence lookup.
    NpcPlan.model_validate(plan.model_dump(mode="python"))


def apply_npc_plan_review_resolved(
    state: WorldState,
    event: NpcPlanReviewResolvedEvent | dict[str, object],
) -> WorldState:
    """Apply a review result and the exact scheduled occurrence completion.

    This pure transition does not advance time and does not execute a plan. A
    trusted host remains responsible for same-batch creation validation,
    branch/version compare-and-swap, external account control, and call trace
    provenance.
    """
    try:
        parsed = parse_npc_plan_review_event(event)
        parsed.require_author_visibility()
        if state.current_tick != parsed.at_tick:
            raise InvalidEvent("NPC plan review must resolve at the current world tick.")
        schedule = next(
            (item for item in state.scheduled_events if item.id == parsed.scheduled_event_id),
            None,
        )
        if schedule is None:
            raise InvalidEvent("NPC plan review schedule does not exist.")
        if schedule.revision != parsed.expected_schedule_revision:
            raise InvalidEvent("NPC plan review schedule revision is stale.")
        actor_id = validate_npc_plan_review_schedule(state, schedule, parsed.world_id)
        if (
            schedule.status != "queued"
            or schedule.due_tick != state.current_tick
            or schedule.phase != NPC_PLAN_REVIEW_PHASE
            or schedule.handler_id != NPC_PLAN_REVIEW_HANDLER_ID
            or schedule.handler_version != NPC_PLAN_REVIEW_HANDLER_VERSION
            or actor_id != parsed.actor_id
            or schedule.payload != {"actor_id": str(parsed.actor_id)}
        ):
            raise InvalidEvent("NPC plan review does not match the exact queued scheduled occurrence.")
        actor = state.entities.get(parsed.actor_id)
        if actor is None or actor.kind not in _CHARACTER_KINDS:
            raise InvalidEvent("NPC plan review actor must be an existing person.")

        actor_eligible = (
            actor.status == "active"
            and parsed.actor_id != state.player_id
            and _actor_has_current_goal(state, parsed.actor_id)
        )
        if parsed.outcome == "skipped":
            if parsed.plan_id is not None or parsed.source != "policy":
                raise InvalidEvent("A skipped review can only be a policy result without a plan.")
        else:
            if not actor_eligible:
                raise InvalidEvent("An ineligible actor's queued NPC plan review must be skipped.")
            if parsed.outcome == "ready":
                _validate_ready_plan(state, parsed)
            elif parsed.plan_id is not None:
                raise InvalidEvent("Non-ready NPC plan review outcomes cannot reference a plan.")

        from .gameplay_events import ScheduledEventCompletedEvent
        from .reducers import _apply_scheduled_event_completion

        completion = ScheduledEventCompletedEvent(
            event_id=uuid5(
                parsed.command_id,
                f"{parsed.ruleset_version}:npc_plan_review.schedule_completed:"
                f"{parsed.scheduled_event_id}:{parsed.expected_schedule_revision}",
            ),
            scheduled_event_id=parsed.scheduled_event_id,
            expected_revision=parsed.expected_schedule_revision,
            visibility=dict(_AUTHOR_ONLY),
        )
        updated = _apply_scheduled_event_completion(state, completion, npc_review_validated=True)
        if parsed.promise_choice is not None:
            from .npc_commitments import record_return_review

            updated = record_return_review(updated, parsed.actor_id, parsed.promise_choice, parsed.event_id)
        if (
            parsed.reason == NPC_MODEL_UNAVAILABLE_REASON
            or parsed.outcome == "defer" and parsed.reason in NPC_MODEL_OUTPUT_DEFER_REASONS
        ):
            retry = build_npc_provider_unavailable_review(updated, parsed.world_id, parsed.actor_id)
            if retry is not None:
                from .models import WorldState as RuntimeWorldState

                scheduled_events = [
                    retry if item.id == retry.id else item for item in updated.scheduled_events
                ]
                updated = RuntimeWorldState.model_validate(
                    updated.model_dump(mode="python") | {"scheduled_events": scheduled_events}
                )
        return updated
    except InvalidEvent:
        raise
    except Exception as exc:
        raise InvalidEvent("NPC plan review event is invalid or cannot be applied.") from exc

