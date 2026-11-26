"""Identify the built-in modern opening without changing existing genesis or replay hashes."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from .gameplay_events import GoalUpdatedEvent

if TYPE_CHECKING:
    from .models import WorldState

OPENING_TITLE = "Investigate: A late transit outage strands clinic staff and freight workers."
ARRIVAL_PROGRESS = 50


def opening_target(metadata: dict, actor_id: str, goal: dict) -> str | None:
    if metadata.get("pack_id") != "modern_city":
        return None
    index = metadata.get("content_index")
    if not isinstance(index, dict):
        return None
    if (index.get("player_id") != actor_id or goal.get("owner_id") != actor_id
            or goal.get("created_tick") not in (0, "0")):
        return None
    registration = metadata.get("opening_investigation")
    if isinstance(registration, dict):
        target = registration.get("destination_id")
        if not isinstance(target, str) or goal.get("id") != registration.get("goal_id"):
            return None
    else:
        settlements = index.get("settlement_ids", [])
        if not isinstance(settlements, list) or len(settlements) < 2 or goal.get("title") != OPENING_TITLE:
            return None
        target = settlements[1]
    conditions = goal.get("conditions", [])
    if len(conditions) != 1:
        return None
    condition = conditions[0]
    if (condition.get("kind") == "reach_location" and condition.get("subject_id") == actor_id
            and condition.get("target_id") == target):
        return target
    return None


def opening_arrival_event(
    state: WorldState, actor_id: UUID, destination_id: UUID, receipt_id: UUID,
) -> GoalUpdatedEvent | None:
    metadata = state.metadata
    if metadata.get("pack_id") != "modern_city" or state.player_id != actor_id:
        return None
    for goal in state.goals:
        if goal.owner_id != actor_id:
            continue
        if opening_target(metadata, str(actor_id), goal.model_dump(mode="json")) != str(destination_id):
            continue
        if goal.status != "active" or goal.progress >= ARRIVAL_PROGRESS:
            return None
        return GoalUpdatedEvent(
            event_id=uuid5(receipt_id, "modern.opening.arrival"), goal_id=goal.id,
            expected_revision=goal.revision, progress=ARRIVAL_PROGRESS, evidence_refs=[receipt_id],
            visibility={"policy": "subject_ids", "subject_ids": [str(actor_id)]},
        )
    return None
