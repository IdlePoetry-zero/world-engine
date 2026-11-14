"""Deterministic reducers for the small, registered core event set."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import cast
from uuid import UUID

from pydantic_core import to_jsonable_python

from .errors import InsufficientResources, InvalidEvent
from .events import parse_event
from .models import (
    DomainEvent,
    EntityCreatedEvent,
    EntityFieldChangedEvent,
    ResourceTransferredEvent,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)


@dataclass(frozen=True, slots=True)
class LedgerPosting:
    """One balanced projection row emitted by a resource transfer."""

    account_id: UUID
    resource_id: str
    delta: Decimal


def apply_event(state: WorldState, raw_event: DomainEvent | dict[str, object]) -> WorldState:
    event = parse_event(raw_event)

    if isinstance(event, EntityCreatedEvent):
        if event.entity.id in state.entities:
            raise InvalidEvent(f"Entity {event.entity.id} already exists.")
        if event.entity.location_id is not None:
            _require_location(state, event.entity.location_id)
        new_entities = {**state.entities, event.entity.id: event.entity}
        return _validated_state(state, {"entities": new_entities})

    if isinstance(event, EntityFieldChangedEvent):
        entity = state.entities.get(event.entity_id)
        if entity is None:
            raise InvalidEvent(f"Entity {event.entity_id} does not exist.")
        if entity.status in {"dead", "retired"}:
            raise InvalidEvent(f"Entity {event.entity_id} is {entity.status}; its state is terminal.")
        value: object = event.value
        if event.field_name == "name":
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 240):
                raise InvalidEvent("Entity name must be non-empty text of at most 240 characters.")
        elif event.field_name == "location_id":
            value = _parse_optional_uuid(value, "location_id")
            if value is not None:
                _require_location(state, value)
        elif event.field_name == "status":
            if value is None or not isinstance(value, str) or not value.strip() or len(value) > 64:
                raise InvalidEvent("Entity status must be non-empty text of at most 64 characters.")
            if entity.status == "dead" and value != "dead":
                raise InvalidEvent("A dead entity cannot be revived by a core field change.")
        updated_entity = entity.model_copy(update={event.field_name: value})
        new_entities = {**state.entities, event.entity_id: updated_entity}
        return _validated_state(state, {"entities": new_entities})

    if isinstance(event, ResourceTransferredEvent):
        if event.from_account_id == event.to_account_id:
            raise InvalidEvent("A resource transfer must use two different accounts.")
        source = state.resources.get(event.from_account_id)
        target = state.resources.get(event.to_account_id)
        if source is None or target is None:
            raise InvalidEvent("Both resource accounts must exist before transfer.")
        if source.resource_id != event.resource_id or target.resource_id != event.resource_id:
            raise InvalidEvent("Both accounts and the transfer must use the same resource.")
        if source.balance < event.quantity:
            raise InsufficientResources(
                f"Account {event.from_account_id} has {source.balance} {event.resource_id}; "
                f"transfer requires {event.quantity}."
            )
        resources = dict(state.resources)
        with localcontext() as context:
            context.prec = 40
            source_balance = source.balance - event.quantity
            target_balance = target.balance + event.quantity
        resources[event.from_account_id] = source.model_copy(update={"balance": source_balance})
        resources[event.to_account_id] = target.model_copy(update={"balance": target_balance})
        return _validated_state(state, {"resources": resources})

    if isinstance(event, TimeAdvancedEvent):
        if event.to_tick <= state.current_tick:
            raise InvalidEvent("World time must advance monotonically.")
        return _validated_state(state, {"current_tick": event.to_tick})

    raise InvalidEvent(f"No reducer is registered for event {event.type!r}.")


def ledger_postings(event: DomainEvent) -> tuple[LedgerPosting, ...]:
    """Return resource ledger rows for an event, preserving zero-sum per resource."""
    if not isinstance(event, ResourceTransferredEvent):
        return ()
    postings = (
        LedgerPosting(event.from_account_id, event.resource_id, event.quantity.copy_negate()),
        LedgerPosting(event.to_account_id, event.resource_id, event.quantity),
    )
    with localcontext() as context:
        context.prec = _ledger_precision(postings)
        if sum((posting.delta for posting in postings), Decimal("0")) != 0:
            raise InvalidEvent("Resource transfer ledger postings must sum to zero.")
    return postings


def validate_ledger_postings(postings: tuple[LedgerPosting, ...]) -> None:
    totals: dict[str, Decimal] = {}
    with localcontext() as context:
        context.prec = _ledger_precision(postings)
        for posting in postings:
            if not posting.delta.is_finite():
                raise InvalidEvent("Ledger postings must be finite decimal values.")
            totals[posting.resource_id] = totals.get(posting.resource_id, Decimal("0")) + posting.delta
    unbalanced = {resource: total for resource, total in totals.items() if total != 0}
    if unbalanced:
        raise InvalidEvent(f"Ledger batch is unbalanced: {unbalanced}.")


def _ledger_precision(postings: tuple[LedgerPosting, ...]) -> int:
    max_digits = max((len(posting.delta.as_tuple().digits) for posting in postings), default=1)
    aggregation_margin = len(str(max(1, len(postings)))) + 2
    return max(40, max_digits + aggregation_margin)


def state_hash(state: WorldState) -> str:
    encoded = json.dumps(
        state.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def state_json(state: WorldState) -> dict[str, object]:
    return cast(dict[str, object], state.model_dump(mode="json"))


def _validated_state(state: WorldState, updates: dict[str, object]) -> WorldState:
    payload = state.model_dump(mode="json")
    payload.update(to_jsonable_python(updates))
    try:
        return WorldState.model_validate(payload)
    except Exception as exc:
        raise InvalidEvent(str(exc)) from exc


def _require_location(state: WorldState, location_id: UUID) -> WorldEntity:
    location = state.entities.get(location_id)
    if location is None or location.kind not in {"location", "place"}:
        raise InvalidEvent(f"Location {location_id} does not exist or is not a location entity.")
    if location.status in {"dead", "retired"}:
        raise InvalidEvent(f"Location {location_id} is retired.")
    return location


def _parse_optional_uuid(value: object, field: str) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise InvalidEvent(f"{field} must be a UUID or null.") from exc
    raise InvalidEvent(f"{field} must be a UUID or null.")
