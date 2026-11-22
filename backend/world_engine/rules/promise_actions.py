"""Trusted rule-side event construction for private item-return promises.

Model proposals provide only semantic action fields. Stable IDs, selected
accounts, revisions, FIFO lot manifests, schedules and visibility all come
from the committed command context and authoritative world state.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from world_engine.domain.components import (
    InventoryComponent,
    ResourceName,
    ScheduledEventRecord,
)
from world_engine.domain.errors import InsufficientResources, InvalidEvent
from world_engine.domain.inventory import FifoLotAllocation, allocate_fifo_lots
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.promise_events import (
    PromiseDeadlineBreachedEvent,
    PromiseItemReturnedEvent,
    PromiseReleasedEvent,
    RelationshipPromiseRecordedEvent,
    participant_visibility,
    promise_event_id,
    promise_id_for_command,
)
from world_engine.domain.promise_reducers import validate_promise_event
from world_engine.domain.promise_late_return_event import PromiseItemReturnedLateEvent
from world_engine.domain.promise_trust_settlement import build_trusted_promise_settlement, configured_promise_trust_policy
from world_engine.domain.relationship_promises import (
    PROMISE_DEADLINE_HANDLER,
    PROMISE_DEADLINE_HANDLER_VERSION,
    PROMISE_TRUST_DEADLINE_HANDLER_VERSION,
    RelationshipPromise,
    ReturnItemTerms,
    ReturnLotTransfer,
    build_promise_deadline_schedule,
    create_return_promise,
    promise_return_lot_id,
)

from .contracts import RuleContext


def build_record_return_promise_event(
    state: WorldState,
    context: RuleContext,
    *,
    promisee_id: UUID,
    resource_id: ResourceName,
    quantity: Decimal | str | int,
    deadline_tick: int,
    private_text: str,
) -> RelationshipPromiseRecordedEvent:
    """Build a private pledge using the promisee's first registered holder account."""

    promisor = _active_character(state, context.actor_id, "Promise promisor")
    promisee = _active_character(state, promisee_id, "Promise promisee")
    _require_colocated(promisor, promisee, "Recording a promise requires both participants to be co-located.")
    if deadline_tick <= state.current_tick:
        raise InvalidEvent("A return promise deadline must be later than the current world tick.")

    account_id = _first_holder_account(state, owner_id=promisee_id, resource_id=resource_id)
    if account_id is None:
        raise InvalidEvent("The promisee has no registered holder account for the promised resource.")

    promise_id = promise_id_for_command(context.command_id, context.ruleset_version)
    if any(item.id == promise_id for item in state.relationship_promises):
        raise InvalidEvent("A relationship promise for this command already exists.")
    try:
        terms = ReturnItemTerms(
            resource_id=resource_id,
            quantity=quantity,
            promisee_account_id=account_id,
        )
        promise = create_return_promise(
            promise_id=promise_id,
            promisor_id=context.actor_id,
            promisee_id=promisee_id,
            private_text=private_text,
            terms=terms,
            created_tick=state.current_tick,
            deadline_tick=deadline_tick,
            trust_policy_id=configured_promise_trust_policy(state),
        )
        event = RelationshipPromiseRecordedEvent(
            event_id=promise_event_id(context.command_id, context.ruleset_version, "recorded"),
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            ruleset_version=context.ruleset_version,
            actor_id=context.actor_id,
            recorded_tick=state.current_tick,
            promise=promise,
            visibility=participant_visibility(context.actor_id, promisee_id),
        )
    except (ValidationError, ValueError) as exc:
        raise InvalidEvent("Return promise terms are invalid.") from exc
    return _validated_candidate(state, context, event)


def build_fulfill_return_promise_event(
    state: WorldState,
    context: RuleContext,
    *,
    promise_id: UUID,
) -> PromiseItemReturnedEvent:
    """Build an exact FIFO return event from the promisor's eligible stock."""

    promise = _active_promise(state, promise_id)
    actor = _active_character(state, context.actor_id, "Promise returner")
    recipient = _active_character(state, promise.promisee_id, "Promise recipient")
    if actor.id != promise.promisor_id:
        raise InvalidEvent("Only the registered promisor can return the promised item.")
    _require_colocated(actor, recipient, "Returning a promise requires both participants to be co-located.")
    if state.current_tick > promise.deadline_tick:
        raise InvalidEvent("A return promise can only be fulfilled on or before its inclusive deadline.")

    schedule = _queued_promise_schedule(state, promise)
    event = _build_return_transfer(state, context, promise, schedule, actor, recipient)
    return _validated_candidate(state, context, event)


def build_late_return_promise_event(
    state: WorldState, context: RuleContext, *, promise_id: UUID,
) -> PromiseItemReturnedLateEvent:
    promise = next((item for item in state.relationship_promises if item.id == promise_id), None)
    if (promise is None or promise.status != "breached" or promise.trust_policy_id is None
            or promise.outcome_event_id is None or context.actor_id != promise.promisor_id):
        raise InvalidEvent("Only the promisor can return a breached trust-enabled promise.")
    actor = _active_character(state, context.actor_id, "Promise returner")
    recipient = _active_character(state, promise.promisee_id, "Promise recipient")
    _require_colocated(actor, recipient, "Late return requires both participants to be co-located.")
    schedule = next((item for item in state.scheduled_events if item.id == promise.deadline_schedule_id), None)
    if schedule is None or schedule.status != "completed":
        raise InvalidEvent("A late return requires the exact completed breach schedule.")
    transfer = _build_return_transfer(state, context, promise, schedule, actor, recipient)
    event = build_trusted_promise_settlement(state, transfer, source_breach_event_id=promise.outcome_event_id)
    if not isinstance(event, PromiseItemReturnedLateEvent):
        raise InvalidEvent("A late return requires its atomic trust repair event.")
    return event


def _build_return_transfer(
    state: WorldState,
    context: RuleContext,
    promise: RelationshipPromise,
    schedule: ScheduledEventRecord,
    actor: WorldEntity,
    recipient: WorldEntity,
) -> PromiseItemReturnedEvent:
    recipient_account = state.resources.get(promise.terms.promisee_account_id)
    if not _is_holder_account(
        recipient_account,
        owner_id=promise.promisee_id,
        resource_id=promise.terms.resource_id,
    ):
        raise InvalidEvent("The promisee holder account stored in the pledge is no longer valid.")

    source = _first_stock_account(
        state,
        actor,
        resource_id=promise.terms.resource_id,
        quantity=promise.terms.quantity,
    )
    if source is None:
        raise InsufficientResources("The promisor has no eligible holder account with enough unexpired FIFO stock.")
    source_account_id, source_account, source_inventory, allocations = source
    recipient_inventory = _inventory_or_none(recipient)
    command_id = context.command_id
    event_id = promise_event_id(command_id, context.ruleset_version, "item_returned")
    lots = tuple(
        ReturnLotTransfer(
            source_lot_id=allocation.lot_id,
            received_lot_id=promise_return_lot_id(event_id, allocation.lot_id),
            quantity=allocation.quantity,
        )
        for allocation in allocations
    )
    return PromiseItemReturnedEvent(
        event_id=event_id,
        command_id=command_id,
        promise_id=promise.id,
        expected_promise_revision=promise.revision,
        actor_id=context.actor_id,
        source_owner_id=promise.promisor_id,
        recipient_owner_id=promise.promisee_id,
        from_account_id=source_account_id,
        to_account_id=promise.terms.promisee_account_id,
        source_account_type="holder",
        recipient_account_type="holder",
        resource_id=promise.terms.resource_id,
        quantity=promise.terms.quantity,
        transfer_tick=state.current_tick,
        scheduled_event_id=schedule.id,
        expected_schedule_revision=schedule.revision,
        expected_source_inventory_revision=source_inventory.revision,
        expected_recipient_inventory_revision=(
            recipient_inventory.revision if recipient_inventory is not None else None
        ),
        expected_source_account_balance=source_account.balance,
        expected_recipient_account_balance=recipient_account.balance,
        lots=lots,
        world_id=context.world_id,
        branch_id=context.branch_id,
        ruleset_version=context.ruleset_version,
        visibility=participant_visibility(promise.promisor_id, promise.promisee_id),
    )


def build_release_return_promise_event(
    state: WorldState,
    context: RuleContext,
    *,
    promise_id: UUID,
) -> PromiseReleasedEvent:
    """Build the promisee's explicit release, whether or not the promisor is present."""

    promise = _active_promise(state, promise_id)
    actor = _active_character(state, context.actor_id, "Promise release actor")
    if actor.id != promise.promisee_id:
        raise InvalidEvent("Only the promisee can release a return promise.")
    if state.current_tick > promise.deadline_tick:
        raise InvalidEvent("A promise can only be released on or before its inclusive deadline.")
    schedule = _queued_promise_schedule(state, promise)
    event = PromiseReleasedEvent(
        event_id=promise_event_id(context.command_id, context.ruleset_version, "released"),
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        ruleset_version=context.ruleset_version,
        actor_id=context.actor_id,
        promise_id=promise.id,
        expected_promise_revision=promise.revision,
        promisor_id=promise.promisor_id,
        promisee_id=promise.promisee_id,
        resolved_tick=state.current_tick,
        scheduled_event_id=schedule.id,
        expected_schedule_revision=schedule.revision,
        visibility=participant_visibility(promise.promisor_id, promise.promisee_id),
    )
    return _validated_candidate(state, context, event)


def build_scheduled_promise_breach_event(
    state: WorldState,
    context: RuleContext,
    scheduled_event: ScheduledEventRecord,
) -> PromiseDeadlineBreachedEvent:
    """Build a breach only for the exact queued deadline due at the current tick."""

    try:
        schedule = ScheduledEventRecord.model_validate(
            scheduled_event.model_dump(mode="python")
            if hasattr(scheduled_event, "model_dump") else scheduled_event
        )
    except ValidationError as exc:
        raise InvalidEvent("Promise deadline schedule is invalid.") from exc
    promise_id = _promise_id_from_schedule(schedule)
    promise = _active_promise(state, promise_id)
    expected = build_promise_deadline_schedule(promise)
    if (
        schedule.id != expected.id
        or schedule.due_tick != expected.due_tick
        or schedule.phase != expected.phase
        or schedule.handler_id != expected.handler_id
        or schedule.handler_version != expected.handler_version
        or schedule.status != "queued"
        or schedule.repeat_ticks is not None
        or schedule.max_runs != 1
        or schedule.completed_runs != 0
        or schedule.dependencies
        or schedule.priority != 0
        or schedule.payload != expected.payload
        or state.current_tick != schedule.due_tick
    ):
        raise InvalidEvent("Promise breach must resolve its exact queued one-shot schedule at the due tick.")
    if not any(item.id == schedule.id and item.revision == schedule.revision for item in state.scheduled_events):
        raise InvalidEvent("Promise breach schedule is stale or not part of the current world state.")
    event = PromiseDeadlineBreachedEvent(
        event_id=promise_event_id(context.command_id, context.ruleset_version, "breached"),
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        ruleset_version=context.ruleset_version,
        actor_id=context.actor_id,
        promise_id=promise.id,
        expected_promise_revision=promise.revision,
        promisor_id=promise.promisor_id,
        promisee_id=promise.promisee_id,
        resolved_tick=state.current_tick,
        scheduled_event_id=schedule.id,
        expected_schedule_revision=schedule.revision,
        visibility=participant_visibility(promise.promisor_id, promise.promisee_id),
    )
    return _validated_candidate(state, context, event)


def _validated_candidate(state: WorldState, context: RuleContext, event: Any) -> Any:
    validate_promise_event(
        state,
        event,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        actor_id=context.actor_id,
        ruleset_version=context.ruleset_version,
    )
    return event


def _active_promise(state: WorldState, promise_id: UUID) -> RelationshipPromise:
    matches = [promise for promise in state.relationship_promises if promise.id == promise_id]
    if len(matches) != 1:
        raise InvalidEvent("Relationship promise does not exist uniquely in this world.")
    promise = matches[0]
    if promise.status != "active":
        raise InvalidEvent("Only an active relationship promise may be resolved.")
    return promise


def _active_character(state: WorldState, entity_id: UUID, label: str) -> WorldEntity:
    entity = state.entities.get(entity_id)
    if entity is None or entity.kind not in {"character", "person"} or entity.status != "active":
        raise InvalidEvent(f"{label} must be an active character in this world.")
    return entity


def _require_colocated(first: WorldEntity, second: WorldEntity, message: str) -> None:
    if first.id == second.id or first.location_id is None or first.location_id != second.location_id:
        raise InvalidEvent(message)


def _first_holder_account(
    state: WorldState,
    *,
    owner_id: UUID,
    resource_id: str,
) -> UUID | None:
    matches = sorted(
        (
            (account_id, account)
            for account_id, account in state.resources.items()
            if _is_holder_account(account, owner_id=owner_id, resource_id=resource_id)
        ),
        key=lambda pair: str(pair[0]),
    )
    return matches[0][0] if matches else None


def _first_stock_account(
    state: WorldState,
    actor: WorldEntity,
    *,
    resource_id: str,
    quantity: Decimal,
) -> tuple[UUID, Any, InventoryComponent, list[FifoLotAllocation]] | None:
    inventory = _inventory_or_none(actor)
    if inventory is None:
        return None
    candidates = sorted(
        (
            (account_id, account)
            for account_id, account in state.resources.items()
            if _is_holder_account(account, owner_id=actor.id, resource_id=resource_id)
            and account.balance >= quantity
        ),
        key=lambda pair: str(pair[0]),
    )
    for account_id, account in candidates:
        try:
            allocations = allocate_fifo_lots(
                inventory,
                resource_id=resource_id,
                quantity=quantity,
                at_tick=state.current_tick,
                account_id=account_id,
            )
        except ValueError:
            continue
        return account_id, account, inventory, allocations
    return None


def _is_holder_account(account: Any, *, owner_id: UUID, resource_id: str) -> bool:
    return (
        account is not None
        and account.account_type == "holder"
        and account.owner_id == owner_id
        and account.resource_id == resource_id
    )


def _inventory_or_none(entity: WorldEntity) -> InventoryComponent | None:
    raw_inventory = entity.components.get("inventory")
    if raw_inventory is None:
        return None
    try:
        return InventoryComponent.model_validate(raw_inventory)
    except ValidationError as exc:
        raise InvalidEvent("Promise participant has invalid registered inventory.") from exc


def _queued_promise_schedule(state: WorldState, promise: RelationshipPromise) -> ScheduledEventRecord:
    matches = [event for event in state.scheduled_events if event.id == promise.deadline_schedule_id]
    if len(matches) != 1:
        raise InvalidEvent("Promise deadline schedule does not exist uniquely.")
    schedule = matches[0]
    expected = build_promise_deadline_schedule(promise)
    if (
        schedule.status != "queued"
        or schedule.id != expected.id
        or schedule.due_tick != expected.due_tick
        or schedule.phase != expected.phase
        or schedule.handler_id != expected.handler_id
        or schedule.handler_version != expected.handler_version
        or schedule.repeat_ticks is not None
        or schedule.max_runs != 1
        or schedule.completed_runs != 0
        or schedule.dependencies
        or schedule.priority != 0
        or schedule.payload != expected.payload
    ):
        raise InvalidEvent("Promise deadline schedule is stale or does not match the registered policy.")
    return schedule


def _promise_id_from_schedule(schedule: ScheduledEventRecord) -> UUID:
    if schedule.handler_id != PROMISE_DEADLINE_HANDLER or schedule.handler_version not in {
        PROMISE_DEADLINE_HANDLER_VERSION, PROMISE_TRUST_DEADLINE_HANDLER_VERSION,
    }:
        raise InvalidEvent("Scheduled event is not the registered relationship-promise deadline handler.")
    raw_promise_id = schedule.payload.get("promise_id")
    if not isinstance(raw_promise_id, str):
        raise InvalidEvent("Promise deadline schedule is missing its promise reference.")
    try:
        return UUID(raw_promise_id)
    except ValueError as exc:
        raise InvalidEvent("Promise deadline schedule has an invalid promise reference.") from exc
