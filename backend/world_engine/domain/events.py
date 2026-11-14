"""Event parsing helpers for the closed, versioned core event registry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from pydantic import TypeAdapter, ValidationError

from .errors import InvalidEvent
from .models import (
    DomainEvent,
    EntityCreatedEvent,
    EntityFieldChangedEvent,
    ResourceTransferredEvent,
    TimeAdvancedEvent,
)

EVENT_TYPES: dict[str, type] = {
    "core.entity_created.v1": EntityCreatedEvent,
    "core.entity_field_changed.v1": EntityFieldChangedEvent,
    "core.resource_transferred.v1": ResourceTransferredEvent,
    "core.time_advanced.v1": TimeAdvancedEvent,
}
_EVENT_ADAPTER = TypeAdapter(DomainEvent)


def parse_event(value: DomainEvent | Mapping[str, object]) -> DomainEvent:
    """Return a registered typed event or reject unknown/invalid input."""
    if isinstance(value, (EntityCreatedEvent, EntityFieldChangedEvent, ResourceTransferredEvent, TimeAdvancedEvent)):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise InvalidEvent("Events must be typed core events or JSON objects.")
    event_type = value.get("type")
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise InvalidEvent(f"Unregistered event type: {event_type!r}.")
    try:
        return cast(DomainEvent, _EVENT_ADAPTER.validate_python(dict(value)))
    except ValidationError as exc:
        raise InvalidEvent(f"Event {event_type!r} failed schema validation: {exc}") from exc


def event_payload(event: DomainEvent) -> dict[str, object]:
    """Get event-specific fields, excluding envelope type/id/visibility."""
    return event.model_dump(mode="json", exclude={"type", "event_id", "visibility"})
