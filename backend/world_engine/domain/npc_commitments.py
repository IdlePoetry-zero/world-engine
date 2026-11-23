"""Bind an observable obligation to the normal NPC planning and state lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from .components import GoalRecord, ScheduledEventRecord
from .errors import InvalidEvent
from .npc_plan_review import schedule_npc_commitment_review
from .promise_events import (
    PromiseDeadlineBreachedEvent, PromiseEvent, PromiseItemReturnedEvent, PromiseReleasedEvent,
    RelationshipPromiseRecordedEvent,
)

if TYPE_CHECKING:
    from .models import WorldState
    from .relationship_invitation_events import RelationInvitationChangedEvent
    from .relationship_invitations import RelationshipInvitation

MEETING_COMMITMENT_HANDLER = "npc.meeting_commitment_due"
MEETING_COMMITMENT_VERSION = "1.0.0"


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
        return _queue_goal(state, event.world_id, goal, promise.deadline_tick)

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


def _meeting_goal_id(invitation_id: UUID, actor_id: UUID) -> UUID:
    return uuid5(invitation_id, f"npc.commitment:meeting:{actor_id}")


def update_npc_meeting_commitment(state: WorldState, event: RelationInvitationChangedEvent) -> WorldState:
    invitation = event.resulting_invitation
    meeting = invitation.meeting
    if meeting is None or meeting.venue_id is None:
        return state
    if event.operation == "accept":
        participants = (invitation.proposer_id, invitation.invitee_id)
        venue = state.entities[meeting.venue_id]
        for actor_id in participants:
            if actor_id == state.player_id:
                continue
            goal = GoalRecord(
                id=_meeting_goal_id(invitation.invitation_id, actor_id), owner_id=actor_id,
                title="Prepare for the accepted meeting",
                description=f"Reach the agreed venue {venue.name or venue.id} for the scheduled interval. "
                            f"Attendance remains a separate choice. Invitation ID: {invitation.invitation_id}.",
                deadline=meeting.end_tick - 1, priority=85,
                created_tick=state.current_tick, updated_tick=state.current_tick,
            )
            state = _queue_goal(state, event.world_id, goal, meeting.start_tick)
        expiry = ScheduledEventRecord(
            id=uuid5(invitation.invitation_id, "npc.commitment:meeting-deadline"), due_tick=meeting.end_tick,
            phase="observation", handler_id=MEETING_COMMITMENT_HANDLER, handler_version=MEETING_COMMITMENT_VERSION,
            payload={"invitation_id": str(invitation.invitation_id)},
        )
        return _update(state, scheduled_events=[*state.scheduled_events, expiry])
    if event.operation == "attend":
        return _update(state, goals=_finish_meeting_goals(state, invitation, event.event_id, "succeeded", event.actor_id))
    if event.operation == "end":
        return _update(state, goals=_finish_meeting_goals(state, invitation, event.event_id, "abandoned"))
    return state


def expire_npc_meeting_commitment(
    state: WorldState, scheduled: ScheduledEventRecord, event_id: UUID,
) -> list[GoalRecord]:
    invitation = _meeting_for_deadline(state, scheduled)
    return _finish_meeting_goals(state, invitation, event_id, "failed")


def validate_meeting_commitment_schedules(state: WorldState) -> None:
    for scheduled in state.scheduled_events:
        if scheduled.handler_id != MEETING_COMMITMENT_HANDLER:
            continue
        try:
            _meeting_for_deadline(state, scheduled)
        except InvalidEvent as error:
            raise ValueError(str(error)) from error


def _meeting_for_deadline(state: WorldState, scheduled: ScheduledEventRecord) -> RelationshipInvitation:
    invitation = next((entry for entry in state.relationship_invitations
                       if str(entry.invitation_id) == scheduled.payload.get("invitation_id")), None)
    if (invitation is None or invitation.meeting is None or invitation.meeting.venue_id is None
            or invitation.status not in {"accepted", "ended"} or scheduled.due_tick != invitation.meeting.end_tick
            or scheduled.id != uuid5(invitation.invitation_id, "npc.commitment:meeting-deadline")
            or scheduled.handler_version != MEETING_COMMITMENT_VERSION or scheduled.phase != "observation"
            or scheduled.repeat_ticks is not None or scheduled.max_runs != 1 or scheduled.dependencies
            or scheduled.priority != 0 or scheduled.status not in {"queued", "completed"}
            or set(scheduled.payload) != {"invitation_id"}):
        raise InvalidEvent("A meeting deadline must match its exact accepted venue and interval.")
    return invitation


def _finish_meeting_goals(
    state: WorldState, invitation: RelationshipInvitation, event_id: UUID, status: str,
    actor_id: UUID | None = None,
) -> list[GoalRecord]:
    participants = (actor_id,) if actor_id is not None else (invitation.proposer_id, invitation.invitee_id)
    ids = {_meeting_goal_id(invitation.invitation_id, participant) for participant in participants}
    return [
        goal.model_copy(update={
            "status": status, "progress": 100 if status == "succeeded" else goal.progress,
            "updated_tick": state.current_tick, "evidence_refs": [event_id],
        }) if goal.id in ids and goal.status == "active" else goal
        for goal in state.goals
    ]


def _queue_goal(state: WorldState, world_id: UUID, goal: GoalRecord, review_deadline: int) -> WorldState:
    state = _update(state, goals=[*state.goals, goal])
    schedule = schedule_npc_commitment_review(state, world_id, goal.owner_id, review_deadline)
    if schedule is None:
        return state
    schedules = [schedule if row.id == schedule.id else row for row in state.scheduled_events]
    if all(row.id != schedule.id for row in state.scheduled_events):
        schedules.append(schedule)
    return _update(state, scheduled_events=schedules)


def _update(state: WorldState, **updates: object) -> WorldState:
    return type(state).model_validate(state.model_dump(mode="python") | updates)
