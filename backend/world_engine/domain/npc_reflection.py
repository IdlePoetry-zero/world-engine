"""Pure, actor-safe selectors for automatic NPC reflection opportunities.

Reflection is deliberately triggered only by committed typed events whose
meaning can be checked against the corresponding validated world snapshot.
This module does not schedule work, persist notes, load private context, or call
models.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from .components import GoalRecord, KnowledgeRecord, ScheduledEventRecord
from .gameplay_events import InvestigationCaseClosedEvent, KnowledgeAcquiredEvent
from .investigation import validate_assessment_record
from .investigation_cases import InvestigationCaseFile
from .models import WorldEntity, WorldState
from .npc_plan_events import NpcPlanRevisedEvent
from .npc_plan_review import validate_npc_plan_review_schedule
from .npc_plan_review_events import NpcPlanReviewResolvedEvent
from .npc_plans import NpcPlan


NpcReflectionKind = Literal[
    "goal_completion",
    "npc_plan_review",
    "evidence_conflict",
    "investigation_case_contested",
]
NpcReflectionMode = Literal[
    "salient_events",
    "salient_events_and_plans",
    "salient_events_plans_and_conflicts",
]
_AUTHOR_ONLY = {"policy": "author_only"}
_CHARACTER_KINDS = frozenset({"character", "person"})
_MODE_KINDS: dict[str, tuple[NpcReflectionKind, ...]] = {
    "salient_events": ("goal_completion",),
    "salient_events_and_plans": ("goal_completion", "npc_plan_review"),
    "salient_events_plans_and_conflicts": (
        "goal_completion",
        "evidence_conflict",
        "investigation_case_contested",
        "npc_plan_review",
    ),
}
# A goal completion is the strongest salient event, then an explicit material
# conflict, then a plan review cycle. Within each kind, source event order wins.
_PRIORITY: tuple[NpcReflectionKind, ...] = (
    "goal_completion",
    "evidence_conflict",
    "investigation_case_contested",
    "npc_plan_review",
)


class NpcReflectionTrigger(BaseModel):
    """Stable, small reference to one committed source event."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    event_id: UUID
    actor_id: UUID
    tick: StrictInt = Field(ge=-(2**63), le=2**63 - 1)
    kind: NpcReflectionKind


class NpcReflectionSourceEvidence(NpcReflectionTrigger):
    """Minimal evidence safe to pass to the selected NPC's reflection task."""

    status: Literal["succeeded", "ready", "defer", "conflict", "contested"]
    title: StrictStr | None = Field(default=None, min_length=1, max_length=240)
    predicate: Literal["entity_status", "entity_location"] | None = None
    effective_tick: StrictInt | None = Field(default=None, ge=-(2**63), le=2**63 - 1)

    @model_validator(mode="after")
    def evidence_matches_kind(self) -> NpcReflectionSourceEvidence:
        if self.kind == "goal_completion":
            valid = self.status == "succeeded" and self.title is not None
        elif self.kind == "npc_plan_review":
            valid = self.status in {"ready", "defer"} and self.title is None
        elif self.kind == "evidence_conflict":
            valid = (
                self.status == "conflict"
                and self.title is None
                and self.predicate is not None
                and self.effective_tick is not None
            )
        else:
            valid = self.status == "contested" and self.title is not None
        if not valid:
            raise ValueError("NPC reflection evidence fields do not match its typed source kind")
        if self.kind != "evidence_conflict" and (self.predicate is not None or self.effective_tick is not None):
            raise ValueError("only typed evidence conflict may expose a predicate and effective tick")
        return self


def _validated_source_event(event: object) -> object | None:
    """Re-validate supported event instances, including forged model copies."""
    event_type: type[BaseModel] | None = None
    if isinstance(event, NpcPlanRevisedEvent):
        event_type = NpcPlanRevisedEvent
    elif isinstance(event, NpcPlanReviewResolvedEvent):
        event_type = NpcPlanReviewResolvedEvent
    elif isinstance(event, KnowledgeAcquiredEvent):
        event_type = KnowledgeAcquiredEvent
    elif isinstance(event, InvestigationCaseClosedEvent):
        event_type = InvestigationCaseClosedEvent
    if event_type is None or type(event) is not event_type:
        return None
    try:
        return event_type.model_validate(event.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError):
        return None


def _actor_is_reflectable(
    state: WorldState,
    actor_id: UUID,
    externally_controlled_actor_ids: Collection[UUID],
) -> bool:
    actor = state.entities.get(actor_id)
    if actor is None:
        return False
    try:
        actor = WorldEntity.model_validate(actor.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError):
        return False
    return (
        actor.id == actor_id
        and actor.kind in _CHARACTER_KINDS
        and actor.status == "active"
        and actor_id != state.player_id
        and actor_id not in externally_controlled_actor_ids
    )


def _private_event(event: object) -> bool:
    return getattr(event, "visibility", None) == _AUTHOR_ONLY


def _project_plan_goal_completion(
    event: NpcPlanRevisedEvent,
    state: WorldState,
    externally_controlled_actor_ids: Collection[UUID],
) -> NpcReflectionSourceEvidence | None:
    if (
        not _private_event(event)
        or not event.fulfilled_goal_ids
        or not _actor_is_reflectable(state, event.actor_id, externally_controlled_actor_ids)
        or event.plan.updated_tick != state.current_tick
    ):
        return None
    try:
        event_plan = NpcPlan.model_validate(event.plan.model_dump(mode="python"))
        current_plans = [
            NpcPlan.model_validate(item.model_dump(mode="python"))
            for item in state.npc_plans
            if item.plan_id == event_plan.plan_id
        ]
    except (AttributeError, TypeError, ValueError):
        return None
    if len(current_plans) != 1 or current_plans[0] != event_plan:
        return None

    goals_by_id: dict[UUID, GoalRecord] = {}
    try:
        for item in state.goals:
            if item.id in event.fulfilled_goal_ids:
                goal = GoalRecord.model_validate(item.model_dump(mode="python"))
                goals_by_id[goal.id] = goal
    except (AttributeError, TypeError, ValueError):
        return None
    # The event validator binds these IDs to its plan; the final state proves
    # that the actor actually completed the goal and that the completion cites
    # evidence carried by that exact revision.
    for goal_id in event.fulfilled_goal_ids:
        goal = goals_by_id.get(goal_id)
        if (
            goal is not None
            and goal.owner_id == event.actor_id
            and goal.status == "succeeded"
            and goal.progress == 100
            and goal.updated_tick == state.current_tick
            and bool(set(goal.evidence_refs) & set(event_plan.evidence_refs))
        ):
            return NpcReflectionSourceEvidence(
                event_id=event.event_id,
                actor_id=event.actor_id,
                tick=event_plan.updated_tick,
                kind="goal_completion",
                status="succeeded",
                title=goal.title,
            )
    return None


def _actor_has_current_goal(state: WorldState, actor_id: UUID) -> bool:
    return any(
        goal.owner_id == actor_id
        and goal.status == "active"
        and goal.created_tick <= state.current_tick
        and (goal.deadline is None or goal.deadline >= state.current_tick)
        for goal in state.goals
    )


def _project_plan_review(
    event: NpcPlanReviewResolvedEvent,
    state: WorldState,
    externally_controlled_actor_ids: Collection[UUID],
) -> NpcReflectionSourceEvidence | None:
    if (
        not _private_event(event)
        or event.source != "model"
        or event.outcome not in {"ready", "defer"}
        or event.at_tick != state.current_tick
        or not _actor_is_reflectable(state, event.actor_id, externally_controlled_actor_ids)
        or not _actor_has_current_goal(state, event.actor_id)
    ):
        return None
    schedule = next((item for item in state.scheduled_events if item.id == event.scheduled_event_id), None)
    if schedule is None:
        return None
    try:
        checked_schedule = ScheduledEventRecord.model_validate(schedule.model_dump(mode="python"))
        scheduled_actor_id = validate_npc_plan_review_schedule(state, checked_schedule, event.world_id)
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        scheduled_actor_id != event.actor_id
        or checked_schedule.revision != event.expected_schedule_revision + 1
        or checked_schedule.completed_runs < 1
        or checked_schedule.status not in {"queued", "completed"}
    ):
        return None
    if event.outcome == "ready":
        try:
            plan = next(
                (
                    NpcPlan.model_validate(item.model_dump(mode="python"))
                    for item in state.npc_plans
                    if item.plan_id == event.plan_id
                ),
                None,
            )
        except (AttributeError, TypeError, ValueError):
            return None
        expected_plan_id = uuid5(
            event.decision_request_id,
            f"npc.plan.v1:{event.actor_id}:{event.based_on_state_version}",
        )
        if (
            plan is None
            or plan.plan_id != expected_plan_id
            or plan.actor_id != event.actor_id
            or plan.status != "ready"
            or plan.revision != 1
            or plan.current_step != 0
            or plan.created_tick != event.at_tick
            or plan.updated_tick != event.at_tick
        ):
            return None
    return NpcReflectionSourceEvidence(
        event_id=event.event_id,
        actor_id=event.actor_id,
        tick=event.at_tick,
        kind="npc_plan_review",
        status=event.outcome,
    )


def _project_evidence_conflict(
    event: KnowledgeAcquiredEvent,
    state: WorldState,
    externally_controlled_actor_ids: Collection[UUID],
) -> NpcReflectionSourceEvidence | None:
    if not _private_event(event) or not isinstance(event.record, KnowledgeRecord):
        return None
    try:
        record = KnowledgeRecord.model_validate(event.record.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        record.assessment is None
        or record.assessment.finding != "conflict"
        or record.owner_id is None
        or not _actor_is_reflectable(state, record.owner_id, externally_controlled_actor_ids)
        or record.received_tick != state.current_tick
        or record.holder_id is not None
        or record.visibility.policy != "private"
        or record.visibility.subject_ids
    ):
        return None
    current_records = [item for item in state.knowledge if item.id == record.id]
    if len(current_records) != 1 or current_records[0] != record:
        return None
    try:
        finding = validate_assessment_record(
            record, state.knowledge, actor_id=record.owner_id, at_tick=state.current_tick,
        )
    except (AttributeError, TypeError, ValueError):
        return None
    if finding != "conflict":
        return None
    return NpcReflectionSourceEvidence(
        event_id=event.event_id,
        actor_id=record.owner_id,
        tick=record.received_tick,
        kind="evidence_conflict",
        status="conflict",
        predicate=record.assessment.predicate,
        effective_tick=record.assessment.effective_tick,
    )


def _project_contested_case(
    event: InvestigationCaseClosedEvent,
    state: WorldState,
    externally_controlled_actor_ids: Collection[UUID],
) -> NpcReflectionSourceEvidence | None:
    if (
        not _private_event(event)
        or event.outcome != "contested"
        or event.public_accusation
        or event.closed_tick != state.current_tick
        or not _actor_is_reflectable(state, event.actor_id, externally_controlled_actor_ids)
    ):
        return None
    current_cases = [item for item in state.investigation_cases if item.id == event.case_file_id]
    if len(current_cases) != 1:
        return None
    try:
        case_file = InvestigationCaseFile.model_validate(current_cases[0].model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError):
        return None
    if (
        case_file.owner_id != event.actor_id
        or case_file.status != "closed"
        or case_file.outcome != "contested"
        or case_file.public_accusation
        or case_file.revision != event.expected_revision + 1
        or case_file.closed_tick != event.closed_tick
        or case_file.updated_tick != event.closed_tick
    ):
        return None
    return NpcReflectionSourceEvidence(
        event_id=event.event_id,
        actor_id=event.actor_id,
        tick=event.closed_tick,
        kind="investigation_case_contested",
        status="contested",
        title=case_file.title,
    )


def project_npc_reflection_source(
    event: object,
    state: WorldState,
    *,
    externally_controlled_actor_ids: Collection[UUID],
) -> NpcReflectionSourceEvidence | None:
    """Project one typed source event into small actor-safe evidence.

    The caller must re-read the source event by `NpcReflectionTrigger.event_id`
    from the exact committed snapshot before calling this function. Only
    minimal completion/review/conflict/case metadata is returned; private
    claims, facts, source references, plans, and rationale are never copied.
    """
    if not isinstance(state, WorldState):
        return None
    parsed = _validated_source_event(event)
    if parsed is None:
        return None
    if isinstance(parsed, NpcPlanRevisedEvent):
        return _project_plan_goal_completion(parsed, state, externally_controlled_actor_ids)
    if isinstance(parsed, NpcPlanReviewResolvedEvent):
        return _project_plan_review(parsed, state, externally_controlled_actor_ids)
    if isinstance(parsed, KnowledgeAcquiredEvent):
        return _project_evidence_conflict(parsed, state, externally_controlled_actor_ids)
    if isinstance(parsed, InvestigationCaseClosedEvent):
        return _project_contested_case(parsed, state, externally_controlled_actor_ids)
    return None


def select_npc_reflection_trigger(
    events: Iterable[object],
    state: WorldState,
    *,
    mode: str,
    externally_controlled_actor_ids: Collection[UUID],
) -> NpcReflectionTrigger | None:
    """Select at most one deterministic reflection trigger from committed events.

    Unknown modes, malformed source events, duplicate event IDs, non-player
    actors under external control, and sources that do not match the final
    validated state all fail closed.
    """
    allowed_kinds = _MODE_KINDS.get(mode)
    if allowed_kinds is None or not isinstance(state, WorldState):
        return None
    parsed_events = [parsed for item in events if (parsed := _validated_source_event(item)) is not None]
    counts = Counter(event.event_id for event in parsed_events)
    evidence_by_event: dict[UUID, NpcReflectionSourceEvidence] = {}
    for event in parsed_events:
        if counts[event.event_id] != 1:
            continue
        evidence = project_npc_reflection_source(
            event,
            state,
            externally_controlled_actor_ids=externally_controlled_actor_ids,
        )
        if evidence is not None and evidence.kind in allowed_kinds:
            evidence_by_event[event.event_id] = evidence
    for kind in _PRIORITY:
        if kind not in allowed_kinds:
            continue
        for event in parsed_events:
            evidence = evidence_by_event.get(event.event_id)
            if evidence is not None and evidence.kind == kind:
                return NpcReflectionTrigger(
                    event_id=evidence.event_id,
                    actor_id=evidence.actor_id,
                    tick=evidence.tick,
                    kind=evidence.kind,
                )
    return None
