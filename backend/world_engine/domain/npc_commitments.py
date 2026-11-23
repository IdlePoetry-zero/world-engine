"""Bind an observable obligation to the normal NPC planning and state lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid5

from .components import GoalRecord
from .errors import InvalidEvent
from .npc_plan_review import schedule_npc_commitment_review
from .promise_events import (
    PromiseDeadlineBreachedEvent, PromiseEvent, PromiseItemReturnedEvent, PromiseReleasedEvent,
    RelationshipPromiseRecordedEvent,
)

if TYPE_CHECKING:
    from .models import WorldState


def update_npc_return_commitment(state: WorldState, event: PromiseEvent) -> WorldState:
    promise_id = event.promise.id if isinstance(event, RelationshipPromiseRecordedEvent) else event.promise_id
    goal_id = uuid5(promise_id, "npc.commitment:return")
    existing = next((goal for goal in state.goals if goal.id == goal_id), None)
    if isinstance(event, RelationshipPromiseRecordedEvent):
        promise = event.promise
        if promise.promisor_id == state.player_id:
            return state
        if existing is not None:
            raise InvalidEvent("A new promise conflicts with an existing commitment goal.")
        goal = GoalRecord(
            id=goal_id, owner_id=promise.promisor_id, title="Return the promised goods",
            description=f"Assess and address the private return commitment {promise.id} before its deadline.",
            deadline=promise.deadline_tick, priority=85,
            created_tick=state.current_tick, updated_tick=state.current_tick,
        )
        state = _update(state, goals=[*state.goals, goal])
        schedule = schedule_npc_commitment_review(state, event.world_id, goal.owner_id, promise.deadline_tick)
        if schedule is None:
            return state
        schedules = [schedule if row.id == schedule.id else row for row in state.scheduled_events]
        if all(row.id != schedule.id for row in state.scheduled_events):
            schedules.append(schedule)
        return _update(state, scheduled_events=schedules)

    promisor = event.source_owner_id if isinstance(event, PromiseItemReturnedEvent) else event.promisor_id
    if existing is None or existing.status != "active" or existing.owner_id != promisor:
        return state
    if isinstance(event, PromiseItemReturnedEvent):
        outcome = {"status": "succeeded", "progress": 100}
    elif isinstance(event, PromiseReleasedEvent):
        outcome = {"status": "abandoned"}
    elif isinstance(event, PromiseDeadlineBreachedEvent):
        outcome = {"status": "failed"}
    else:
        return state
    settled = existing.model_copy(update={
        **outcome, "updated_tick": state.current_tick, "evidence_refs": [event.event_id],
    })
    return _update(state, goals=[settled if row.id == goal_id else row for row in state.goals])


def _update(state: WorldState, **updates: object) -> WorldState:
    return type(state).model_validate(state.model_dump(mode="python") | updates)
