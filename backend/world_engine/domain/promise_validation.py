"""Bind private promise events to the host command that committed them."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from .errors import InvalidEvent
from .promise_late_return_event import PromiseItemReturnedLateEvent
from .promise_events import (
    PromiseDeadlineBreachedEvent, PromiseItemReturnedEvent, PromiseReleasedEvent, RelationshipPromiseRecordedEvent,
)
from .promise_trust_events import PromiseDeadlineBreachedWithTrustEvent, PromiseItemReturnedWithTrustEvent


_TRUST_SETTLEMENTS = (PromiseItemReturnedWithTrustEvent, PromiseDeadlineBreachedWithTrustEvent, PromiseItemReturnedLateEvent)
_EVENT_CLASSES = (RelationshipPromiseRecordedEvent, PromiseItemReturnedEvent, PromiseReleasedEvent,
                  PromiseDeadlineBreachedEvent, *_TRUST_SETTLEMENTS)
_PLAYER_COMMANDS = frozenset({
    "action.record_return_promise", "action.fulfill_return_promise", "action.release_return_promise",
    "action.late_return_promise",
})


def requires_promise_validation(command_type: str | None, events: Sequence[object]) -> bool:
    return command_type in _PLAYER_COMMANDS or any(isinstance(event, _EVENT_CLASSES) for event in events)


def validate_promise_batch(
    events: Sequence[object], *, world_id: UUID, branch_id: UUID, command_id: UUID,
    actor_id: UUID, ruleset_version: str, command_type: str | None,
    command_payload: Mapping[str, object] | None,
    causation_ids: Sequence[UUID | str] = (),
) -> None:
    """Check provenance before reduction. A restored batch has no original proposal."""
    if not requires_promise_validation(command_type, events):
        return
    if len(events) != 1 or not isinstance(events[0], _EVENT_CLASSES):
        raise InvalidEvent("A promise command must commit exactly one standalone promise event.")
    event = events[0]
    late = isinstance(event, PromiseItemReturnedLateEvent)
    if late and tuple(map(str, causation_ids)) != (str(event.source_breach_event_id),):
        raise InvalidEvent("Late return must cite exactly its committed breach as its cause.")
    if isinstance(event, _TRUST_SETTLEMENTS):
        event = event.settlement
    if (event.world_id, event.branch_id, event.command_id, event.actor_id, event.ruleset_version) != (
        world_id, branch_id, command_id, actor_id, ruleset_version,
    ):
        raise InvalidEvent("Promise event does not match its trusted command context.")
    if command_type is None and command_payload is None:
        return
    if command_type is None or command_payload is None:
        raise InvalidEvent("Promise command metadata must be complete.")

    if isinstance(event, PromiseDeadlineBreachedEvent):
        if command_type != "simulation.window":
            raise InvalidEvent("A promise deadline must be resolved by a simulation command.")
        return
    if isinstance(event, RelationshipPromiseRecordedEvent):
        promise = event.promise
        kind = "record_return_promise"
        expected = {
            "kind": kind, "promisee_id": str(promise.promisee_id), "resource_id": promise.terms.resource_id,
            "quantity": str(promise.terms.quantity), "deadline_tick": promise.deadline_tick, "private_text": promise.private_text,
        }
    else:
        kind = "late_return_promise" if late else (
            "fulfill_return_promise" if isinstance(event, PromiseItemReturnedEvent) else "release_return_promise"
        )
        expected = {"kind": kind, "promise_id": str(event.promise_id)}
    if command_type != f"action.{kind}" or command_payload != expected:
        raise InvalidEvent("Promise event does not match its normalized player action.")
