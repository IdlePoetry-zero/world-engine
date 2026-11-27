"""Evaluate explicitly authored conditions against authoritative world state."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from .components import GoalCondition, GoalRecord
    from .models import WorldState

NPC_GOAL_COMPLETION_POLICY = "npc_goal_completion_v1"
NPC_STEP_GOAL_COMPLETION_POLICY = "npc_goal_completion_each_step_v1"
NPC_GOAL_DEPENDENCY_COMPLETION_POLICY = "npc_goal_dependency_completion_v1"


def known_inspection_topic(
    state: WorldState, *, actor_id: UUID, place_id: UUID, topic_id: UUID, observation_tick: int,
) -> bool:
    """An inspection must derive from an active goal with genuinely known evidence."""
    return any(
        record.id == topic_id and record.holder_id == actor_id and record.subject_id == place_id
        and record.received_tick <= state.current_tick
        and (record.valid_from_tick is None or record.valid_from_tick <= state.current_tick)
        and record.visibility.policy == "subject_ids" and actor_id in record.visibility.subject_ids
        for record in state.knowledge
    ) and any(
        goal.owner_id == actor_id and goal.status == "active" and goal.created_tick <= state.current_tick
        and (goal.deadline is None or observation_tick <= goal.deadline)
        and any(
            condition.kind == "inspect_known_topic" and condition.subject_id == actor_id
            and condition.target_id == place_id and condition.evidence_id == topic_id
            for condition in goal.conditions
        )
        for goal in state.goals
    )


def npc_goal_fulfillment_enabled(state: WorldState, goal: GoalRecord, *, plan_completed: bool) -> bool:
    return (
        goal.auto_complete or state.metadata.get(NPC_STEP_GOAL_COMPLETION_POLICY) is True
        or (plan_completed and state.metadata.get(NPC_GOAL_COMPLETION_POLICY) is True)
    )


def npc_goal_fulfillment_allows_continuation(state: WorldState, goal: GoalRecord) -> bool:
    return goal.status == "succeeded" and (
        goal.auto_complete or state.metadata.get(NPC_STEP_GOAL_COMPLETION_POLICY) is True
    )


def npc_goal_conditions_met(
    state: WorldState, goal: GoalRecord, *, fulfilled: frozenset[UUID] = frozenset(),
) -> bool:
    return (
        bool(goal.conditions)
        and (goal.deadline is None or state.current_tick <= goal.deadline)
        and all(goal_condition_met(state, goal.owner_id, condition, fulfilled=fulfilled) for condition in goal.conditions)
    )


def npc_goal_fulfillment_closure(
    state: WorldState, goals: Iterable[GoalRecord], initially_fulfilled: Iterable[UUID] = (),
) -> frozenset[UUID]:
    """Find condition-satisfied goals, allowing only proven prerequisites in this revision."""
    remaining = [goal for goal in goals if goal.status == "active"]
    fulfilled = frozenset(initially_fulfilled)
    while remaining:
        ready = frozenset(goal.id for goal in remaining if npc_goal_conditions_met(state, goal, fulfilled=fulfilled))
        ready -= fulfilled
        if not ready:
            break
        fulfilled |= ready
        remaining = [goal for goal in remaining if goal.id not in fulfilled]
    return fulfilled


def goal_condition_met(
    state: WorldState, owner_id: UUID, condition: GoalCondition, *, fulfilled: frozenset[UUID] = frozenset(),
) -> bool:
    subject = state.entities.get(condition.subject_id)
    if condition.kind == "reach_location":
        return bool(subject and subject.id == owner_id and subject.location_id == condition.target_id)
    if condition.kind == "resource_balance_minimum":
        if subject is None or subject.id != owner_id or condition.quantity is None:
            return False
        accounts = (
            account.balance for account in state.resources.values()
            if account.owner_id == owner_id and account.account_type == "holder"
            and account.resource_id == condition.resource_id
        )
        with localcontext() as context:
            context.prec = 50
            return sum(accounts, Decimal(0)) >= condition.quantity
    if condition.kind == "relationship_dimension_minimum":
        return (
            condition.subject_id == owner_id
            and condition.threshold is not None
            and any(
                row.from_id == owner_id and row.to_id == condition.target_id
                and (row.visibility.policy in {"public", "private"} or (
                    row.visibility.policy == "subject_ids" and owner_id in row.visibility.subject_ids
                ))
                and condition.dimension in row.dimensions
                and row.dimensions[condition.dimension] >= condition.threshold
                for row in state.relations
            )
        )
    if condition.kind == "entity_status":
        return bool(subject and subject.id == owner_id and subject.status == condition.status)
    if condition.kind == "discuss_known_topic":
        return condition.subject_id == owner_id and any(
            memory.kind == "memory" and memory.owner_id == owner_id and memory.holder_id == condition.target_id
            and memory.subject_id == owner_id and condition.evidence_id in memory.source_refs
            and memory.visibility.policy == "subject_ids" and set(memory.visibility.subject_ids) == {owner_id, condition.target_id}
            for memory in state.knowledge
        )
    if condition.kind == "inspect_known_topic":
        return condition.subject_id == owner_id and any(
            record.owner_id == owner_id and record.subject_id == condition.target_id
            and record.source_refs == [condition.evidence_id]
            and record.topic_inspection in {"discovered", "nothing_hidden_found"}
            for record in state.knowledge
        )
    if condition.kind == "report_topic_inspection":
        return condition.subject_id == owner_id and any(
            record.kind == "memory" and record.owner_id == owner_id and record.holder_id == condition.target_id
            and record.subject_id == owner_id and len(record.source_refs) == 2
            and record.source_refs[1] == condition.evidence_id
            and record.visibility.policy == "subject_ids"
            and set(record.visibility.subject_ids) == {owner_id, condition.target_id}
            and any(
                source.id == record.source_refs[0] and source.owner_id == owner_id
                and source.source_refs == [condition.evidence_id]
                and source.topic_inspection in {"discovered", "nothing_hidden_found"}
                for source in state.knowledge
            )
            for record in state.knowledge
        )
    return any(
        prerequisite.id == condition.target_id and prerequisite.owner_id == owner_id
        and (prerequisite.status == "succeeded" or prerequisite.id in fulfilled)
        for prerequisite in state.goals
    )
