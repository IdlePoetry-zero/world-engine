from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from world_engine.domain.components import InventoryComponent, InventoryLot, ScheduledEventRecord
from world_engine.domain.errors import InsufficientResources, InvalidEvent
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
from world_engine.domain.promise_events import (
    PromiseDeadlineBreachedEvent,
    PromiseItemReturnedEvent,
    PromiseReleasedEvent,
    RelationshipPromiseRecordedEvent,
    promise_id_for_command,
)
from world_engine.domain.promise_reducers import apply_promise_event
from world_engine.domain.relationship_promises import (
    PROMISE_DEADLINE_HANDLER,
    PROMISE_DEADLINE_HANDLER_VERSION,
    RelationshipPromise,
    ReturnItemTerms,
    build_promise_deadline_schedule,
    create_return_promise,
    promise_return_lot_id,
)
from world_engine.rules.contracts import RuleContext
from world_engine.rules.promise_actions import (
    build_fulfill_return_promise_event,
    build_record_return_promise_event,
    build_release_return_promise_event,
    build_scheduled_promise_breach_event,
)


WORLD_ID = UUID(int=800)
BRANCH_ID = UUID(int=801)
RULESET = "rules.test.1"
PLACE_ID = UUID(int=10)
OTHER_PLACE_ID = UUID(int=13)
PROMISOR_ID = UUID(int=11)
PROMISEE_ID = UUID(int=12)
RECIPIENT_ACCOUNT_ID = UUID(int=22)
SOURCE_ACCOUNT_A = UUID(int=20)
SOURCE_ACCOUNT_B = UUID(int=21)
RESOURCE_ID = "cultivation.herb"


def uid(value: int) -> UUID:
    return UUID(int=value)


def context(*, actor_id: UUID = PROMISOR_ID, command_id: UUID = uid(70)) -> RuleContext:
    return RuleContext(
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=RULESET,
    )


def make_promise(*, promise_id: UUID = uid(40), deadline_tick: int = 200) -> RelationshipPromise:
    return create_return_promise(
        promise_id=promise_id,
        promisor_id=PROMISOR_ID,
        promisee_id=PROMISEE_ID,
        private_text="我会把两份药材还给你。",
        terms=ReturnItemTerms(
            resource_id=RESOURCE_ID,
            quantity="2.5",
            promisee_account_id=RECIPIENT_ACCOUNT_ID,
        ),
        created_tick=100,
        deadline_tick=deadline_tick,
    )


def make_state(
    *,
    tick: int = 100,
    promise: RelationshipPromise | None = None,
    colocated: bool = True,
    promisor_status: str = "active",
    promisee_status: str = "active",
    source_lots: tuple[InventoryLot, ...] | None = None,
    include_source_b: bool = True,
    source_balance_a: str = "5",
    source_balance_b: str = "5",
    recipient_balance: str = "1",
) -> WorldState:
    source_lots = source_lots if source_lots is not None else (
        InventoryLot(
            lot_id=uid(31), account_id=SOURCE_ACCOUNT_A, resource_id=RESOURCE_ID,
            quantity="1", quality=80, expiry_tick=1000, origin_event_id=uid(32),
        ),
        InventoryLot(
            lot_id=uid(33), account_id=SOURCE_ACCOUNT_B, resource_id=RESOURCE_ID,
            quantity="3", quality=60, expiry_tick=None, origin_event_id=uid(34),
        ),
    )
    promisor_lots = [lot for lot in source_lots if lot.account_id in ({SOURCE_ACCOUNT_A, SOURCE_ACCOUNT_B} if include_source_b else {SOURCE_ACCOUNT_A})]
    entities = {
        PLACE_ID: WorldEntity(id=PLACE_ID, kind="place"),
        PROMISOR_ID: WorldEntity(
            id=PROMISOR_ID,
            kind="character",
            status=promisor_status,
            location_id=PLACE_ID,
            components={"inventory": InventoryComponent(lots=promisor_lots).model_dump(mode="json")},
        ),
        PROMISEE_ID: WorldEntity(
            id=PROMISEE_ID,
            kind="character",
            status=promisee_status,
            location_id=PLACE_ID if colocated else OTHER_PLACE_ID,
        ),
    }
    if not colocated:
        entities[OTHER_PLACE_ID] = WorldEntity(id=OTHER_PLACE_ID, kind="place")
    resources = {
        SOURCE_ACCOUNT_A: ResourceAccount(
            resource_id=RESOURCE_ID,
            balance=source_balance_a,
            owner_id=PROMISOR_ID,
            account_type="holder",
        ),
        RECIPIENT_ACCOUNT_ID: ResourceAccount(
            resource_id=RESOURCE_ID,
            balance=recipient_balance,
            owner_id=PROMISEE_ID,
            account_type="holder",
        ),
    }
    if include_source_b:
        resources[SOURCE_ACCOUNT_B] = ResourceAccount(
            resource_id=RESOURCE_ID,
            balance=source_balance_b,
            owner_id=PROMISOR_ID,
            account_type="holder",
        )
    return WorldState(
        current_tick=tick,
        entities=entities,
        resources=resources,
        scheduled_events=[build_promise_deadline_schedule(promise)] if promise else [],
        relationship_promises=[promise] if promise else [],
    )


def test_record_builder_selects_first_promisee_holder_and_builds_private_stable_event() -> None:
    state = make_state()
    state = state.model_copy(update={
        "resources": {
            **state.resources,
            uid(19): ResourceAccount(
                resource_id=RESOURCE_ID, balance="0", owner_id=PROMISEE_ID, account_type="holder",
            ),
        },
    })
    event = build_record_return_promise_event(
        state,
        context(),
        promisee_id=PROMISEE_ID,
        resource_id=RESOURCE_ID,
        quantity="2.5",
        deadline_tick=200,
        private_text="我会把两份药材还给你。",
    )

    assert isinstance(event, RelationshipPromiseRecordedEvent)
    assert event.promise.terms.promisee_account_id == uid(19)
    assert event.promise.id == promise_id_for_command(uid(70), RULESET)
    assert event.visibility == {"policy": "subject_ids", "subject_ids": [str(PROMISOR_ID), str(PROMISEE_ID)]}
    recorded = apply_promise_event(state, event)
    assert recorded.relationship_promises[0] == event.promise
    assert recorded.scheduled_events[0].due_tick == 201


@pytest.mark.parametrize(
    "change",
    [
        {"colocated": False},
        {"promisor_status": "dead"},
        {"promisee_status": "retired"},
    ],
)
def test_record_builder_rejects_unavailable_or_non_colocated_participants(change) -> None:
    state = make_state(**change)
    with pytest.raises(InvalidEvent):
        build_record_return_promise_event(
            state,
            context(),
            promisee_id=PROMISEE_ID,
            resource_id=RESOURCE_ID,
            quantity="1",
            deadline_tick=200,
            private_text="还给你。",
        )


def test_record_builder_rejects_missing_holder_and_non_future_deadline() -> None:
    state = make_state()
    state = state.model_copy(update={
        "resources": {key: value for key, value in state.resources.items() if key != RECIPIENT_ACCOUNT_ID}
    })
    with pytest.raises(InvalidEvent, match="no registered holder"):
        build_record_return_promise_event(
            state, context(), promisee_id=PROMISEE_ID, resource_id=RESOURCE_ID,
            quantity="1", deadline_tick=200, private_text="还给你。",
        )
    with pytest.raises(InvalidEvent, match="later than the current"):
        build_record_return_promise_event(
            make_state(), context(), promisee_id=PROMISEE_ID, resource_id=RESOURCE_ID,
            quantity="1", deadline_tick=100, private_text="还给你。",
        )


def test_fulfill_builder_selects_first_account_with_enough_fifo_lots() -> None:
    promise = make_promise()
    state = make_state(tick=200, promise=promise)
    event = build_fulfill_return_promise_event(state, context(command_id=uid(71)), promise_id=promise.id)

    assert isinstance(event, PromiseItemReturnedEvent)
    assert event.from_account_id == SOURCE_ACCOUNT_B
    assert [row.source_lot_id for row in event.lots] == [uid(33)]
    assert [row.received_lot_id for row in event.lots] == [promise_return_lot_id(event.event_id, uid(33))]
    assert event.to_account_id == RECIPIENT_ACCOUNT_ID
    assert event.transfer_tick == 200
    assert apply_promise_event(state, event).relationship_promises[0].status == "fulfilled"


def test_fulfill_builder_refuses_wrong_actor_separation_and_expired_or_insufficient_stock() -> None:
    promise = make_promise()
    state = make_state(tick=200, promise=promise)
    with pytest.raises(InvalidEvent, match="Only the registered promisor"):
        build_fulfill_return_promise_event(state, context(actor_id=PROMISEE_ID), promise_id=promise.id)
    with pytest.raises(InvalidEvent, match="co-located"):
        build_fulfill_return_promise_event(make_state(tick=200, promise=promise, colocated=False), context(), promise_id=promise.id)

    expired = (
        InventoryLot(
            lot_id=uid(35), account_id=SOURCE_ACCOUNT_A, resource_id=RESOURCE_ID,
            quantity="3", expiry_tick=200,
        ),
    )
    with pytest.raises(InsufficientResources):
        build_fulfill_return_promise_event(
            make_state(tick=200, promise=promise, source_lots=expired, include_source_b=False),
            context(), promise_id=promise.id,
        )

    too_little = (
        InventoryLot(
            lot_id=uid(36), account_id=SOURCE_ACCOUNT_A, resource_id=RESOURCE_ID,
            quantity="1", expiry_tick=None,
        ),
    )
    with pytest.raises(InsufficientResources):
        build_fulfill_return_promise_event(
            make_state(tick=150, promise=promise, source_lots=too_little, include_source_b=False),
            context(), promise_id=promise.id,
        )


def test_fulfill_builder_rejects_closed_or_overdue_promises() -> None:
    promise = make_promise()
    with pytest.raises(InvalidEvent, match="inclusive deadline"):
        build_fulfill_return_promise_event(make_state(tick=201, promise=promise), context(), promise_id=promise.id)

    fulfilled = promise.model_copy(update={"status": "fulfilled", "resolved_tick": 150, "revision": 2, "outcome_event_id": uid(90)})
    closed_state = make_state(tick=150, promise=promise).model_copy(update={"relationship_promises": [fulfilled]})
    with pytest.raises(InvalidEvent, match="active"):
        build_fulfill_return_promise_event(closed_state, context(), promise_id=promise.id)


def test_release_builder_is_promisee_only_and_does_not_require_promisor_presence() -> None:
    promise = make_promise()
    state = make_state(tick=150, promise=promise, colocated=False, promisor_status="dead")
    event = build_release_return_promise_event(state, context(actor_id=PROMISEE_ID, command_id=uid(72)), promise_id=promise.id)

    assert isinstance(event, PromiseReleasedEvent)
    assert event.actor_id == PROMISEE_ID
    assert apply_promise_event(state, event).relationship_promises[0].status == "released"
    wrong_actor_state = make_state(tick=150, promise=promise, colocated=False, promisor_status="active")
    with pytest.raises(InvalidEvent, match="Only the promisee"):
        build_release_return_promise_event(wrong_actor_state, context(actor_id=PROMISOR_ID), promise_id=promise.id)


def test_scheduled_breach_builder_requires_exact_queued_schedule_and_due_tick() -> None:
    promise = make_promise()
    event_record = build_promise_deadline_schedule(promise)
    state = make_state(tick=201, promise=promise)
    event = build_scheduled_promise_breach_event(state, context(actor_id=uid(99), command_id=uid(73)), event_record)

    assert isinstance(event, PromiseDeadlineBreachedEvent)
    assert event.resolved_tick == event_record.due_tick
    assert apply_promise_event(state, event).relationship_promises[0].status == "breached"

    with pytest.raises(InvalidEvent, match="due tick"):
        build_scheduled_promise_breach_event(
            make_state(tick=200, promise=promise), context(actor_id=uid(99)), event_record,
        )
    wrong = event_record.model_copy(update={"handler_id": "other.handler"})
    with pytest.raises(InvalidEvent, match="not the registered"):
        build_scheduled_promise_breach_event(state, context(actor_id=uid(99)), wrong)
