"""Pure state reducers and ledger postings for relationship promise events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID

from .components import InventoryComponent, InventoryLot, ScheduledEventRecord
from .errors import InsufficientResources, InvalidEvent
from .inventory import FifoLotAllocation, allocate_fifo_lots, reduce_fifo_inventory
from .promise_events import (
    PromiseDeadlineBreachedEvent,
    PromiseEvent,
    PromiseItemReturnedEvent,
    PromiseReleasedEvent,
    RelationshipPromiseRecordedEvent,
    parse_promise_event,
)
from .relationship_promises import (
    PROMISE_DEADLINE_HANDLER,
    RelationshipPromise,
    build_promise_deadline_schedule,
    breach_promise_at_deadline,
    fulfill_return_promise,
    release_promise,
    promise_return_lot_id,
)

if TYPE_CHECKING:
    from .models import WorldEntity, WorldState


class PromiseState(Protocol):
    current_tick: int
    entities: Mapping[UUID, object]
    resources: Mapping[UUID, object]
    scheduled_events: list[ScheduledEventRecord]
    relationship_promises: list[RelationshipPromise]

    def model_dump(self, *, mode: str) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class PromiseLedgerPosting:
    """A balanced resource posting emitted by an item-return event."""

    account_id: UUID
    resource_id: str
    delta: Decimal


def apply_promise_event(
    state: PromiseState,
    raw_event: PromiseEvent | dict[str, object],
) -> PromiseState:
    """Apply one typed promise event and validate the resulting state class.

    Existing v1 account transfers remain untouched. Purpose-specific promise
    returns move both the resource balance and the physical FIFO lots here.
    """

    event = parse_promise_event(raw_event)
    if isinstance(event, RelationshipPromiseRecordedEvent):
        updated = _apply_recorded(state, event)
    elif isinstance(event, PromiseItemReturnedEvent):
        updated = _apply_returned(state, event)
    elif isinstance(event, PromiseReleasedEvent):
        updated = _apply_released(state, event)
    elif isinstance(event, PromiseDeadlineBreachedEvent):
        updated = _apply_breached(state, event)
    else:
        raise InvalidEvent(f"No promise reducer is registered for {event.type!r}.")
    if getattr(updated, "metadata", {}).get("npc_return_commitment_reviews_v1") is True:
        from .npc_commitments import update_npc_return_commitment

        return update_npc_return_commitment(cast("WorldState", updated), event)
    return updated


def validate_promise_event(
    state: PromiseState,
    raw_event: PromiseEvent | dict[str, object],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    actor_id: UUID,
    ruleset_version: str,
) -> None:
    """Validate host command context and the full event/state transition.

    Repository commit, replay and restore validators can pass their trusted
    outer command context here. Applying the reducer to a detached state is
    intentionally the same state-aware validation used by replay.
    """

    event = parse_promise_event(raw_event)
    if (
        event.world_id != world_id
        or event.branch_id != branch_id
        or event.command_id != command_id
        or event.actor_id != actor_id
        or event.ruleset_version != ruleset_version
    ):
        raise InvalidEvent("Promise event must match its committed world, branch, command, actor and ruleset.")
    apply_promise_event(state, event)


def validate_promise_state(state: PromiseState) -> None:
    """Validate promise references and their sole deterministic due schedule.

    This function is safe for ``WorldState`` to call from its own model
    validator: this module imports domain models only inside the one reducer
    that needs ``ResourceAccount`` to rebuild typed account values.
    """

    if not hasattr(state, "relationship_promises"):
        # Old state envelopes may omit the additive list. Their compatibility
        # normalization is handled by the owning state serializer.
        return

    try:
        promises = state.relationship_promises
        if not promises:
            # The overwhelmingly common state has no promises. Keep validation
            # to one cheap handler scan so orphan records cannot hide there.
            if any(
                getattr(schedule, "handler_id", None) == PROMISE_DEADLINE_HANDLER
                for schedule in state.scheduled_events
            ):
                raise ValueError("Relationship promise deadline schedule has no owning promise.")
            return

        normalized = [
            RelationshipPromise.model_validate(
                raw_promise.model_dump(mode="python") if hasattr(raw_promise, "model_dump") else raw_promise
            )
            for raw_promise in promises
        ]
        promise_ids = [promise.id for promise in normalized]
        if len(promise_ids) != len(set(promise_ids)):
            raise ValueError("Relationship promise IDs must be unique within a world.")
        by_schedule_id = {promise.deadline_schedule_id: promise for promise in normalized}
        if len(by_schedule_id) != len(normalized):
            raise ValueError("Relationship promise deadline schedule IDs must be unique.")

        schedules_by_id: dict[UUID, list[ScheduledEventRecord]] = {}
        for schedule in state.scheduled_events:
            if getattr(schedule, "handler_id", None) == PROMISE_DEADLINE_HANDLER:
                schedule_id = getattr(schedule, "id", None)
                if schedule_id not in by_schedule_id:
                    raise ValueError("Relationship promise deadline schedule has no owning promise.")
                schedules_by_id.setdefault(schedule_id, []).append(schedule)

        for promise in normalized:
            promisor = state.entities.get(promise.promisor_id)
            promisee = state.entities.get(promise.promisee_id)
            _require_character_exists(promisor, "promisor")
            _require_character_exists(promisee, "promisee")
            _require_promise_account(
                state,
                promise.terms.promisee_account_id,
                owner_id=promise.promisee_id,
                resource_id=promise.terms.resource_id,
                label="promisee",
            )

            matches = schedules_by_id.get(promise.deadline_schedule_id, [])
            if len(matches) != 1:
                raise ValueError("Every relationship promise must have exactly one deterministic deadline schedule.")
            schedule = matches[0]
            expected_status = {
                "active": "queued",
                "fulfilled": "cancelled",
                "released": "cancelled",
                "breached": "completed",
            }[promise.status]
            expected_completed_runs = 1 if promise.status == "breached" else 0
            if (
                schedule.due_tick != promise.deadline_tick + 1
                or schedule.phase != "maintenance"
                or schedule.handler_id != PROMISE_DEADLINE_HANDLER
                or schedule.handler_version != promise.deadline_handler_version
                or schedule.status != expected_status
                or schedule.repeat_ticks is not None
                or schedule.max_runs != 1
                or schedule.completed_runs != expected_completed_runs
                or schedule.dependencies
                or schedule.priority != 0
                or schedule.payload != {"promise_id": str(promise.id)}
            ):
                raise ValueError("Relationship promise deadline schedule does not match its lifecycle status.")
    except InvalidEvent as exc:
        # Pydantic recognizes ValueError as a validation failure in the state
        # envelope; storage reducers wrap it back in the stable InvalidEvent.
        raise ValueError(str(exc)) from exc


def _require_character_exists(entity: object | None, label: str) -> None:
    if entity is None or getattr(entity, "kind", None) not in {"character", "person"}:
        raise ValueError(f"Relationship promise {label} must reference an existing character.")


def promise_ledger_postings(event: PromiseEvent) -> tuple[PromiseLedgerPosting, ...]:
    """Return the exact zero-sum ledger rows for the atomic item return."""

    typed_event = parse_promise_event(event)
    if not isinstance(typed_event, PromiseItemReturnedEvent):
        return ()
    return (
        PromiseLedgerPosting(
            account_id=typed_event.from_account_id,
            resource_id=typed_event.resource_id,
            delta=typed_event.quantity.copy_negate(),
        ),
        PromiseLedgerPosting(
            account_id=typed_event.to_account_id,
            resource_id=typed_event.resource_id,
            delta=typed_event.quantity,
        ),
    )


def _apply_recorded(state: PromiseState, event: RelationshipPromiseRecordedEvent) -> PromiseState:
    _require_tick(state, event.recorded_tick)
    _require_promise_state_field(state)
    promise = event.promise
    if any(item.id == promise.id for item in state.relationship_promises):
        raise InvalidEvent("Relationship promise ID already exists.")
    promisor = state.entities.get(promise.promisor_id)
    promisee = state.entities.get(promise.promisee_id)
    _require_active_character(promisor, "Promise promisor")
    _require_active_character(promisee, "Promise promisee")
    if promisor.id == promisee.id or promisor.location_id is None or promisor.location_id != promisee.location_id:
        raise InvalidEvent("A new promise requires two distinct active characters at the same location.")
    _require_promise_account(
        state,
        promise.terms.promisee_account_id,
        owner_id=promise.promisee_id,
        resource_id=promise.terms.resource_id,
        label="promisee",
    )
    schedule = build_promise_deadline_schedule(promise)
    if any(item.id == schedule.id for item in state.scheduled_events):
        raise InvalidEvent("Relationship promise deadline schedule ID already exists.")
    return _validated_state(state, {
        "relationship_promises": [*state.relationship_promises, promise],
        "scheduled_events": [*state.scheduled_events, schedule],
    })


def _apply_returned(state: PromiseState, event: PromiseItemReturnedEvent) -> PromiseState:
    _require_tick(state, event.transfer_tick)
    _require_promise_state_field(state)
    promise_index, promise = _promise_at_revision(
        state, event.promise_id, event.expected_promise_revision,
    )
    actor = state.entities.get(event.actor_id)
    recipient = state.entities.get(promise.promisee_id)
    _require_active_character(actor, "Promise returner")
    _require_active_character(recipient, "Promise recipient")
    if actor.id != promise.promisor_id or actor.location_id is None or actor.location_id != recipient.location_id:
        raise InvalidEvent("Promise return requires its active promisor and promisee to be co-located.")

    schedule_index, schedule = _schedule_for_promise(
        state,
        promise,
        event.scheduled_event_id,
        event.expected_schedule_revision,
        expected_status="queued",
    )
    if not state.current_tick <= promise.deadline_tick:
        raise InvalidEvent("Promise return must be completed on or before its inclusive deadline.")

    try:
        fulfilled = fulfill_return_promise(promise, event)
    except ValueError as exc:
        raise InvalidEvent(str(exc)) from exc

    updates = _return_transfer_updates(state, event, promise, actor, recipient)
    promises = list(state.relationship_promises)
    promises[promise_index] = fulfilled
    schedules = list(state.scheduled_events)
    schedules[schedule_index] = _cancel_schedule(schedule)
    return _validated_state(state, updates | {
        "relationship_promises": promises,
        "scheduled_events": schedules,
    })


def apply_late_promise_return(
    state: PromiseState, event: PromiseItemReturnedEvent, source_breach_event_id: UUID,
) -> PromiseState:
    """Reduce a verified late-return manifest without rewriting the breach record."""
    _require_tick(state, event.transfer_tick)
    _, promise = _promise_at_revision(state, event.promise_id, event.expected_promise_revision, expected_status="breached")
    if (promise.status != "breached" or promise.trust_policy_id is None
            or promise.outcome_event_id != source_breach_event_id or state.current_tick <= promise.deadline_tick):
        raise InvalidEvent("Late return requires the original committed breach on the same promise.")
    if (event.actor_id != promise.promisor_id or event.source_owner_id != promise.promisor_id
            or event.recipient_owner_id != promise.promisee_id or event.resource_id != promise.terms.resource_id
            or event.quantity != promise.terms.quantity):
        raise InvalidEvent("Late return must transfer the original promise terms between its participants.")
    _schedule_for_promise(
        state, promise, event.scheduled_event_id, event.expected_schedule_revision, expected_status="completed",
    )
    actor = state.entities.get(promise.promisor_id)
    recipient = state.entities.get(promise.promisee_id)
    _require_active_character(actor, "Promise returner")
    _require_active_character(recipient, "Promise recipient")
    if actor.location_id is None or actor.location_id != recipient.location_id:
        raise InvalidEvent("Late return requires both active participants to be co-located.")
    return _validated_state(state, _return_transfer_updates(state, event, promise, actor, recipient))


def _return_transfer_updates(
    state: PromiseState, event: PromiseItemReturnedEvent, promise: RelationshipPromise,
    actor: WorldEntity, recipient: WorldEntity,
) -> dict[str, object]:
    source_account = _require_promise_account(
        state,
        event.from_account_id,
        owner_id=promise.promisor_id,
        resource_id=promise.terms.resource_id,
        label="return source",
    )
    recipient_account = _require_promise_account(
        state,
        event.to_account_id,
        owner_id=promise.promisee_id,
        resource_id=promise.terms.resource_id,
        label="return recipient",
    )
    if event.to_account_id != promise.terms.promisee_account_id:
        raise InvalidEvent("Promise return must use the exact registered promisee holder account.")
    if (
        source_account.balance != event.expected_source_account_balance
        or recipient_account.balance != event.expected_recipient_account_balance
    ):
        raise InvalidEvent("Promise return account balance compare-and-swap failed.")
    if source_account.balance < event.quantity:
        raise InsufficientResources("Promise return source account does not cover the promised quantity.")

    try:
        source_inventory = InventoryComponent.model_validate(actor.components.get("inventory"))
        recipient_inventory_raw = recipient.components.get("inventory")
        recipient_inventory = (
            InventoryComponent.model_validate(recipient_inventory_raw)
            if recipient_inventory_raw is not None else None
        )
    except Exception as exc:
        raise InvalidEvent("Promise participants must have valid registered inventories.") from exc
    current_recipient_inventory_revision = recipient_inventory.revision if recipient_inventory is not None else None
    if source_inventory.revision != event.expected_source_inventory_revision:
        raise InvalidEvent("Promise return source inventory compare-and-swap failed.")
    if current_recipient_inventory_revision != event.expected_recipient_inventory_revision:
        raise InvalidEvent("Promise return recipient inventory compare-and-swap failed.")

    try:
        allocations = allocate_fifo_lots(
            source_inventory,
            resource_id=promise.terms.resource_id,
            quantity=promise.terms.quantity,
            at_tick=state.current_tick,
            account_id=event.from_account_id,
        )
    except ValueError as exc:
        raise InsufficientResources("Unexpired FIFO lots do not cover the promised return quantity.") from exc
    if len(allocations) != len(event.lots):
        raise InvalidEvent("Promise return manifest does not match the exact FIFO allocation.")
    for allocation, supplied in zip(allocations, event.lots, strict=True):
        if (
            allocation.lot_id != supplied.source_lot_id
            or allocation.quantity != supplied.quantity
            or supplied.received_lot_id != promise_return_lot_id(event.event_id, allocation.lot_id)
        ):
            raise InvalidEvent("Promise return manifest does not match the exact deterministic FIFO allocation.")

    source_lots_by_id = {lot.lot_id: lot for lot in source_inventory.lots}
    try:
        reduced_source_inventory = reduce_fifo_inventory(
            source_inventory,
            allocations,
        )
    except ValueError as exc:
        raise InvalidEvent("Promise return manifest cannot be applied to source inventory.") from exc
    received_lots = [
        InventoryLot(
            lot_id=supplied.received_lot_id,
            account_id=event.to_account_id,
            resource_id=promise.terms.resource_id,
            quantity=allocation.quantity,
            quality=source_lots_by_id[allocation.lot_id].quality,
            expiry_tick=source_lots_by_id[allocation.lot_id].expiry_tick,
            origin_event_id=source_lots_by_id[allocation.lot_id].origin_event_id,
        )
        for allocation, supplied in zip(allocations, event.lots, strict=True)
    ]
    existing_recipient_lots = recipient_inventory.lots if recipient_inventory is not None else []
    existing_lot_ids = {lot.lot_id for lot in existing_recipient_lots}
    if any(lot.lot_id in existing_lot_ids for lot in received_lots):
        raise InvalidEvent("Promise return received lot ID already exists in promisee inventory.")
    next_recipient_inventory = InventoryComponent(
        revision=(recipient_inventory.revision + 1 if recipient_inventory is not None else 1),
        lots=[*existing_recipient_lots, *received_lots],
    )

    try:
        from .models import ResourceAccount

        with localcontext() as context:
            context.prec = 80
            source_balance = source_account.balance - event.quantity
            recipient_balance = recipient_account.balance + event.quantity
        next_source_account = ResourceAccount.model_validate({
            **source_account.model_dump(mode="python"), "balance": source_balance,
        })
        next_recipient_account = ResourceAccount.model_validate({
            **recipient_account.model_dump(mode="python"), "balance": recipient_balance,
        })
    except Exception as exc:
        raise InvalidEvent("Promise return would violate registered resource account bounds.") from exc

    updated_actor = actor.model_copy(update={
        "components": {**actor.components, "inventory": reduced_source_inventory.model_dump(mode="json")}
    })
    updated_recipient = recipient.model_copy(update={
        "components": {**recipient.components, "inventory": next_recipient_inventory.model_dump(mode="json")}
    })
    entities = dict(state.entities)
    entities[actor.id] = updated_actor
    entities[recipient.id] = updated_recipient
    resources = dict(state.resources)
    resources[event.from_account_id] = next_source_account
    resources[event.to_account_id] = next_recipient_account
    return {
        "entities": entities,
        "resources": resources,
    }


def _apply_released(state: PromiseState, event: PromiseReleasedEvent) -> PromiseState:
    _require_tick(state, event.resolved_tick)
    _require_promise_state_field(state)
    promise_index, promise = _promise_at_revision(
        state, event.promise_id, event.expected_promise_revision,
    )
    if (event.promisor_id, event.promisee_id) != (promise.promisor_id, promise.promisee_id):
        raise InvalidEvent("Promise release participants do not match the stored promise.")
    actor = state.entities.get(event.actor_id)
    _require_active_character(actor, "Promise release actor")
    try:
        released = release_promise(promise, event)
    except ValueError as exc:
        raise InvalidEvent(str(exc)) from exc
    schedule_index, schedule = _schedule_for_promise(
        state,
        promise,
        event.scheduled_event_id,
        event.expected_schedule_revision,
        expected_status="queued",
    )
    promises = list(state.relationship_promises)
    promises[promise_index] = released
    schedules = list(state.scheduled_events)
    schedules[schedule_index] = _cancel_schedule(schedule)
    return _validated_state(state, {
        "relationship_promises": promises,
        "scheduled_events": schedules,
    })


def _apply_breached(state: PromiseState, event: PromiseDeadlineBreachedEvent) -> PromiseState:
    _require_tick(state, event.resolved_tick)
    _require_promise_state_field(state)
    promise_index, promise = _promise_at_revision(
        state, event.promise_id, event.expected_promise_revision,
    )
    if (event.promisor_id, event.promisee_id) != (promise.promisor_id, promise.promisee_id):
        raise InvalidEvent("Promise breach participants do not match the stored promise.")
    schedule_index, schedule = _schedule_for_promise(
        state,
        promise,
        event.scheduled_event_id,
        event.expected_schedule_revision,
        expected_status="queued",
    )
    try:
        breached = breach_promise_at_deadline(promise, event)
    except ValueError as exc:
        raise InvalidEvent(str(exc)) from exc
    schedules = list(state.scheduled_events)
    schedules[schedule_index] = _complete_schedule(schedule)
    promises = list(state.relationship_promises)
    promises[promise_index] = breached
    return _validated_state(state, {
        "relationship_promises": promises,
        "scheduled_events": schedules,
    })


def _require_tick(state: PromiseState, event_tick: int) -> None:
    if state.current_tick != event_tick:
        raise InvalidEvent("Promise event tick must equal the current world tick.")


def _require_promise_state_field(state: PromiseState) -> None:
    if not hasattr(state, "relationship_promises"):
        raise InvalidEvent("WorldState does not register relationship_promises.")


def _require_active_character(entity: object | None, label: str):
    if (
        entity is None
        or getattr(entity, "kind", None) not in {"character", "person"}
        or getattr(entity, "status", None) != "active"
    ):
        raise InvalidEvent(f"{label} must reference an active character.")
    return entity


def _promise_at_revision(
    state: PromiseState,
    promise_id: UUID,
    expected_revision: int,
    *,
    expected_status: str = "active",
) -> tuple[int, RelationshipPromise]:
    index = next((i for i, item in enumerate(state.relationship_promises) if item.id == promise_id), None)
    if index is None:
        raise InvalidEvent("Relationship promise does not exist.")
    promise = RelationshipPromise.model_validate(state.relationship_promises[index].model_dump(mode="python"))
    if promise.status != expected_status or promise.revision != expected_revision:
        raise InvalidEvent("Relationship promise does not match the expected status and revision.")
    return index, promise


def _require_promise_account(
    state: PromiseState,
    account_id: UUID,
    *,
    owner_id: UUID,
    resource_id: str,
    label: str,
):
    account = state.resources.get(account_id)
    if (
        account is None
        or getattr(account, "account_type", None) != "holder"
        or getattr(account, "owner_id", None) != owner_id
        or getattr(account, "resource_id", None) != resource_id
    ):
        raise InvalidEvent(f"Promise {label} holder account is missing or has the wrong owner/resource.")
    return account


def _schedule_for_promise(
    state: PromiseState,
    promise: RelationshipPromise,
    schedule_id: UUID,
    expected_revision: int,
    *,
    expected_status: str,
) -> tuple[int, ScheduledEventRecord]:
    index = next((i for i, item in enumerate(state.scheduled_events) if item.id == schedule_id), None)
    if index is None:
        raise InvalidEvent("Relationship promise deadline schedule does not exist.")
    schedule = ScheduledEventRecord.model_validate(state.scheduled_events[index].model_dump(mode="python"))
    expected = build_promise_deadline_schedule(promise)
    if (
        schedule_id != promise.deadline_schedule_id
        or schedule.revision != expected_revision
        or schedule.status != expected_status
        or schedule.id != expected.id
        or schedule.due_tick != expected.due_tick
        or schedule.phase != expected.phase
        or schedule.handler_id != expected.handler_id
        or schedule.handler_version != expected.handler_version
        or schedule.max_runs != expected.max_runs
        or schedule.repeat_ticks is not None
        or schedule.completed_runs != (1 if expected_status == "completed" else 0)
        or schedule.payload != expected.payload
    ):
        raise InvalidEvent("Relationship promise deadline schedule is stale or does not match the registered policy.")
    return index, schedule


def _cancel_schedule(schedule: ScheduledEventRecord) -> ScheduledEventRecord:
    try:
        return ScheduledEventRecord.model_validate({
            **schedule.model_dump(mode="python"),
            "status": "cancelled",
            "revision": schedule.revision + 1,
        })
    except Exception as exc:
        raise InvalidEvent("Relationship promise deadline schedule cannot be cancelled.") from exc


def _complete_schedule(schedule: ScheduledEventRecord) -> ScheduledEventRecord:
    try:
        return ScheduledEventRecord.model_validate({
            **schedule.model_dump(mode="python"),
            "status": "completed",
            "completed_runs": 1,
            "revision": schedule.revision + 1,
        })
    except Exception as exc:
        raise InvalidEvent("Relationship promise deadline schedule cannot be completed.") from exc


def _validated_state(state: PromiseState, changes: dict[str, object]) -> PromiseState:
    """Re-parse the whole registered state type after an atomic promise change."""

    try:
        raw = state.model_dump(mode="python")
        raw.update(changes)
        return type(state).model_validate(raw)
    except InvalidEvent:
        raise
    except Exception as exc:
        raise InvalidEvent("Promise event would violate registered world-state invariants.") from exc
