from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from world_engine.domain.components import InventoryComponent, InventoryLot
from world_engine.domain.errors import InsufficientResources, InvalidEvent
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
from world_engine.domain.promise_events import (
    PromiseDeadlineBreachedEvent,
    PromiseItemReturnedEvent,
    PromiseReleasedEvent,
    RelationshipPromiseRecordedEvent,
    participant_visibility,
    promise_event_id,
    promise_id_for_command,
)
from world_engine.domain.promise_reducers import (
    apply_promise_event,
    promise_ledger_postings,
    validate_promise_event,
    validate_promise_state,
)
from world_engine.domain.relationship_promises import (
    RelationshipPromise,
    ReturnItemTerms,
    ReturnLotTransfer,
    build_promise_deadline_schedule,
    create_return_promise,
    promise_return_lot_id,
)


WORLD_ID = UUID(int=700)
BRANCH_ID = UUID(int=701)
RULESET = "test.rules.v1"
PLACE_ID = UUID(int=10)
PROMISOR_ID = UUID(int=11)
PROMISEE_ID = UUID(int=12)
SOURCE_ACCOUNT_ID = UUID(int=21)
RECIPIENT_ACCOUNT_ID = UUID(int=22)
RESOURCE_ID = "cultivation.herb"


def uid(value: int) -> UUID:
    return UUID(int=value)


def make_promise(*, created_tick: int = 100, deadline_tick: int = 200, **updates: Any) -> RelationshipPromise:
    values: dict[str, Any] = {
        "promise_id": promise_id_for_command(uid(30), RULESET),
        "promisor_id": PROMISOR_ID,
        "promisee_id": PROMISEE_ID,
        "private_text": "我会把两份药材还给你。",
        "terms": ReturnItemTerms(
            resource_id=RESOURCE_ID,
            quantity="2.5",
            promisee_account_id=RECIPIENT_ACCOUNT_ID,
        ),
        "created_tick": created_tick,
        "deadline_tick": deadline_tick,
    }
    values.update(updates)
    return create_return_promise(**values)


def make_state(
    *,
    tick: int = 100,
    promise: RelationshipPromise | None = None,
    promisor_kind: str = "character",
    co_located: bool = True,
    source_balance: str = "5",
    recipient_balance: str = "4",
    source_lots: tuple[InventoryLot, ...] | None = None,
    recipient_lots: tuple[InventoryLot, ...] = (),
    source_inventory_revision: int = 1,
    recipient_inventory_revision: int = 2,
) -> WorldState:
    source_lots = source_lots or (
        InventoryLot(
            lot_id=uid(41), account_id=SOURCE_ACCOUNT_ID, resource_id=RESOURCE_ID,
            quantity="1", quality=63, expiry_tick=500, origin_event_id=uid(42),
        ),
        InventoryLot(
            lot_id=uid(43), account_id=SOURCE_ACCOUNT_ID, resource_id=RESOURCE_ID,
            quantity="3", quality=91, expiry_tick=None, origin_event_id=uid(44),
        ),
    )
    entities = {
        PLACE_ID: WorldEntity(id=PLACE_ID, kind="place"),
        PROMISOR_ID: WorldEntity(
            id=PROMISOR_ID, kind=promisor_kind, location_id=PLACE_ID,
            components={
                "inventory": InventoryComponent(revision=source_inventory_revision, lots=list(source_lots)).model_dump(mode="json")
            },
        ),
        PROMISEE_ID: WorldEntity(
            id=PROMISEE_ID, kind="character", location_id=PLACE_ID if co_located else uid(13),
            components={
                "inventory": InventoryComponent(revision=recipient_inventory_revision, lots=list(recipient_lots)).model_dump(mode="json")
            },
        ),
    }
    if not co_located:
        entities[uid(13)] = WorldEntity(id=uid(13), kind="place")
    resources = {
        SOURCE_ACCOUNT_ID: ResourceAccount(
            resource_id=RESOURCE_ID, balance=source_balance, owner_id=PROMISOR_ID, account_type="holder",
        ),
        RECIPIENT_ACCOUNT_ID: ResourceAccount(
            resource_id=RESOURCE_ID, balance=recipient_balance, owner_id=PROMISEE_ID, account_type="holder",
        ),
    }
    promises = [promise] if promise else []
    schedules = [build_promise_deadline_schedule(promise)] if promise else []
    return WorldState(
        current_tick=tick,
        entities=entities,
        resources=resources,
        scheduled_events=schedules,
        relationship_promises=promises,
    )


def recorded_event(*, tick: int = 100, **updates: Any) -> RelationshipPromiseRecordedEvent:
    command_id = uid(30)
    promise = make_promise(created_tick=tick)
    values: dict[str, Any] = {
        "event_id": promise_event_id(command_id, RULESET, "recorded"),
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": command_id,
        "ruleset_version": RULESET,
        "actor_id": PROMISOR_ID,
        "recorded_tick": tick,
        "promise": promise,
        "visibility": participant_visibility(PROMISOR_ID, PROMISEE_ID),
    }
    values.update(updates)
    return RelationshipPromiseRecordedEvent.model_validate(values)


def return_event(promise: RelationshipPromise, **updates: Any) -> PromiseItemReturnedEvent:
    command_id = uid(50)
    event_id = promise_event_id(command_id, RULESET, "item_returned")
    values: dict[str, Any] = {
        "event_id": event_id,
        "command_id": command_id,
        "promise_id": promise.id,
        "expected_promise_revision": promise.revision,
        "actor_id": PROMISOR_ID,
        "source_owner_id": PROMISOR_ID,
        "recipient_owner_id": PROMISEE_ID,
        "from_account_id": SOURCE_ACCOUNT_ID,
        "to_account_id": RECIPIENT_ACCOUNT_ID,
        "source_account_type": "holder",
        "recipient_account_type": "holder",
        "resource_id": RESOURCE_ID,
        "quantity": "2.5",
        "transfer_tick": 200,
        "scheduled_event_id": promise.deadline_schedule_id,
        "expected_schedule_revision": 1,
        "expected_source_inventory_revision": 1,
        "expected_recipient_inventory_revision": 2,
        "expected_source_account_balance": "5",
        "expected_recipient_account_balance": "4",
        "lots": (
            ReturnLotTransfer(
                source_lot_id=uid(41),
                received_lot_id=promise_return_lot_id(event_id, uid(41)),
                quantity="1",
            ),
            ReturnLotTransfer(
                source_lot_id=uid(43),
                received_lot_id=promise_return_lot_id(event_id, uid(43)),
                quantity="1.5",
            ),
        ),
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "ruleset_version": RULESET,
        "visibility": participant_visibility(PROMISOR_ID, PROMISEE_ID),
    }
    if updates.get("quantity") == "2":
        values["lots"] = (
            ReturnLotTransfer(
                source_lot_id=uid(41),
                received_lot_id=promise_return_lot_id(event_id, uid(41)),
                quantity="1",
            ),
            ReturnLotTransfer(
                source_lot_id=uid(43),
                received_lot_id=promise_return_lot_id(event_id, uid(43)),
                quantity="1",
            ),
        )
    values.update(updates)
    return PromiseItemReturnedEvent.model_validate(values)


def test_record_event_atomically_creates_private_promise_and_exact_deadline_schedule() -> None:
    state = make_state()
    event = recorded_event()
    validate_promise_event(
        state, event,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=event.command_id,
        actor_id=PROMISOR_ID,
        ruleset_version=RULESET,
    )

    result = apply_promise_event(state, event)
    promise = result.relationship_promises[0]
    schedule = result.scheduled_events[0]
    assert promise == event.promise
    assert promise.status == "active"
    assert schedule.id == promise.deadline_schedule_id
    assert schedule.due_tick == 201
    assert schedule.handler_id == "relationship_promise.deadline"
    assert schedule.payload == {"promise_id": str(promise.id)}
    validate_promise_state(result)


@pytest.mark.parametrize("change", [{"co_located": False}, {"promisor_kind": "organization"}])
def test_record_requires_two_active_colocated_characters(change: dict[str, Any]) -> None:
    with pytest.raises(InvalidEvent):
        apply_promise_event(make_state(**change), recorded_event())


def test_record_requires_exact_promisee_holder_account() -> None:
    state = make_state()
    changed = state.model_copy(update={
        "resources": {
            **state.resources,
            RECIPIENT_ACCOUNT_ID: ResourceAccount(
                resource_id=RESOURCE_ID, balance="4", owner_id=PROMISOR_ID, account_type="holder",
            ),
        },
    })
    with pytest.raises(InvalidEvent, match="wrong owner/resource"):
        apply_promise_event(changed, recorded_event())


def test_return_transfers_balances_and_fifo_lots_and_fulfills_in_one_event() -> None:
    promise = make_promise()
    state = make_state(tick=200, promise=promise)
    event = return_event(promise)
    result = apply_promise_event(state, event)

    assert result.resources[SOURCE_ACCOUNT_ID].balance == Decimal("2.5")
    assert result.resources[RECIPIENT_ACCOUNT_ID].balance == Decimal("6.5")
    source_inventory = InventoryComponent.model_validate(result.entities[PROMISOR_ID].components["inventory"])
    assert [(lot.lot_id, lot.quantity, lot.revision) for lot in source_inventory.lots] == [(uid(43), Decimal("1.5"), 2)]
    recipient_inventory = InventoryComponent.model_validate(result.entities[PROMISEE_ID].components["inventory"])
    assert recipient_inventory.revision == 3
    returned = recipient_inventory.lots
    assert [(lot.quantity, lot.quality, lot.expiry_tick, lot.origin_event_id) for lot in returned] == [
        (Decimal("1"), 63, 500, uid(42)),
        (Decimal("1.5"), 91, None, uid(44)),
    ]
    assert returned[0].lot_id == promise_return_lot_id(event.event_id, uid(41))
    assert all(lot.account_id == RECIPIENT_ACCOUNT_ID for lot in returned)
    closed = result.relationship_promises[0]
    assert (closed.status, closed.revision, closed.resolved_tick, closed.outcome_event_id) == (
        "fulfilled", 2, 200, event.event_id,
    )
    schedule = result.scheduled_events[0]
    assert (schedule.status, schedule.revision, schedule.completed_runs) == ("cancelled", 2, 0)
    postings = promise_ledger_postings(event)
    assert [(row.account_id, row.delta) for row in postings] == [
        (SOURCE_ACCOUNT_ID, Decimal("-2.5")), (RECIPIENT_ACCOUNT_ID, Decimal("2.5")),
    ]
    assert sum((row.delta for row in postings), Decimal("0")) == Decimal("0")
    validate_promise_state(result)


@pytest.mark.parametrize(
    "change",
    [
        {"expected_promise_revision": 2},
        {"expected_schedule_revision": 2},
        {"expected_source_inventory_revision": 2},
        {"expected_recipient_inventory_revision": 1},
        {"expected_source_account_balance": "4"},
        {"expected_recipient_account_balance": "3"},
        {"to_account_id": uid(999)},
        {"quantity": "2"},
    ],
)
def test_return_rejects_stale_cas_or_terms_mismatch(change: dict[str, Any]) -> None:
    promise = make_promise()
    with pytest.raises(InvalidEvent):
        apply_promise_event(make_state(tick=200, promise=promise), return_event(promise, **change))


def test_return_requires_active_colocation_and_unexpired_fifo_stock() -> None:
    promise = make_promise()
    with pytest.raises(InvalidEvent, match="co-located"):
        apply_promise_event(make_state(tick=200, promise=promise, co_located=False), return_event(promise))

    expired = (
        InventoryLot(
            lot_id=uid(41), account_id=SOURCE_ACCOUNT_ID, resource_id=RESOURCE_ID,
            quantity="3", expiry_tick=200,
        ),
    )
    with pytest.raises(InsufficientResources, match="Unexpired FIFO"):
        apply_promise_event(make_state(tick=200, promise=promise, source_lots=expired), return_event(promise))


def test_return_rejects_a_non_fifo_lot_manifest() -> None:
    promise = make_promise()
    event = return_event(promise)
    with pytest.raises(InvalidEvent, match="exact deterministic FIFO allocation"):
        apply_promise_event(
            make_state(tick=200, promise=promise),
            return_event(promise, lots=tuple(reversed(event.lots))),
        )


@pytest.mark.parametrize("action,status,scheduled_status,tick", [("release", "released", "cancelled", 150), ("breach", "breached", "completed", 201)])
def test_release_and_exact_deadline_breach_resolve_schedule_once(action, status, scheduled_status, tick) -> None:
    promise = make_promise()
    state = make_state(tick=tick, promise=promise)
    command_id = uid(61 if action == "release" else 62)
    event_cls = PromiseReleasedEvent if action == "release" else PromiseDeadlineBreachedEvent
    operation = "released" if action == "release" else "breached"
    event = event_cls(
        event_id=promise_event_id(command_id, RULESET, operation),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        ruleset_version=RULESET,
        actor_id=PROMISEE_ID if action == "release" else uid(90),
        promise_id=promise.id,
        expected_promise_revision=promise.revision,
        promisor_id=PROMISOR_ID,
        promisee_id=PROMISEE_ID,
        resolved_tick=tick,
        scheduled_event_id=promise.deadline_schedule_id,
        expected_schedule_revision=1,
        visibility=participant_visibility(PROMISOR_ID, PROMISEE_ID),
    )
    result = apply_promise_event(state, event)
    closed = result.relationship_promises[0]
    schedule = result.scheduled_events[0]
    assert (closed.status, closed.outcome_event_id, closed.resolved_tick) == (status, event.event_id, tick)
    assert (schedule.status, schedule.completed_runs, schedule.revision) == (
        scheduled_status, 1 if action == "breach" else 0, 2,
    )
    validate_promise_state(result)


def test_promise_state_validator_rejects_missing_or_inconsistent_schedule_and_account() -> None:
    promise = make_promise()
    state = make_state(promise=promise)

    with pytest.raises(ValueError, match="exactly one deterministic deadline schedule"):
        validate_promise_state(state.model_copy(update={"scheduled_events": []}))

    bad_account = ResourceAccount(
        resource_id=RESOURCE_ID, balance="4", owner_id=PROMISOR_ID, account_type="holder",
    )
    with pytest.raises(ValueError, match="wrong owner/resource"):
        validate_promise_state(state.model_copy(update={"resources": {**state.resources, RECIPIENT_ACCOUNT_ID: bad_account}}))

    other_promise = make_promise(promise_id=uid(333))
    orphan_schedule = build_promise_deadline_schedule(other_promise)
    with pytest.raises(ValueError, match="no owning promise"):
        validate_promise_state(make_state().model_copy(update={"scheduled_events": [orphan_schedule]}))

    cancelled_active_schedule = state.scheduled_events[0].model_copy(update={"status": "cancelled"})
    with pytest.raises(ValueError, match="does not match its lifecycle status"):
        validate_promise_state(state.model_copy(update={"scheduled_events": [cancelled_active_schedule]}))

    missing_promisor = state.model_copy(update={"entities": {k: v for k, v in state.entities.items() if k != PROMISOR_ID}})
    with pytest.raises(ValueError, match="promisor must reference an existing character"):
        validate_promise_state(missing_promisor)


def test_host_context_validation_rejects_cross_scope_or_actor_event() -> None:
    state = make_state()
    event = recorded_event()
    with pytest.raises(InvalidEvent, match="match its committed world"):
        validate_promise_event(
            state, event,
            world_id=uid(999),
            branch_id=BRANCH_ID,
            command_id=event.command_id,
            actor_id=PROMISOR_ID,
            ruleset_version=RULESET,
        )
