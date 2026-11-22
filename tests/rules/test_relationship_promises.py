from uuid import UUID

import pytest
from pydantic import ValidationError

from world_engine.domain.relationship_promises import (
    CommittedReturnTransfer,
    PromiseDeadlineEvidence,
    PromiseReleaseEvidence,
    RelationshipPromise,
    ReturnItemTerms,
    ReturnLotTransfer,
    breach_promise_at_deadline,
    build_promise_deadline_schedule,
    can_view_promise,
    create_return_promise,
    fulfill_return_promise,
    promise_deadline_schedule_id,
    promise_return_lot_id,
    release_promise,
)


def uid(value: int) -> UUID:
    return UUID(int=value)


def make_promise(**updates) -> RelationshipPromise:
    values = {
        "promise_id": uid(1),
        "promisor_id": uid(2),
        "promisee_id": uid(3),
        "private_text": "我会把借来的三件药材送回给你。",
        "terms": ReturnItemTerms(
            resource_id="cultivation.herb",
            quantity="3",
            promisee_account_id=uid(30),
        ),
        "created_tick": 100,
        "deadline_tick": 200,
    }
    values.update(updates)
    return create_return_promise(**values)


def return_event(promise: RelationshipPromise, **updates) -> CommittedReturnTransfer:
    values = {
        "event_id": uid(40),
        "command_id": uid(41),
        "promise_id": promise.id,
        "expected_promise_revision": promise.revision,
        "actor_id": promise.promisor_id,
        "source_owner_id": promise.promisor_id,
        "recipient_owner_id": promise.promisee_id,
        "from_account_id": uid(20),
        "to_account_id": promise.terms.promisee_account_id,
        "source_account_type": "holder",
        "recipient_account_type": "holder",
        "resource_id": promise.terms.resource_id,
        "quantity": promise.terms.quantity,
        "transfer_tick": 200,
        "scheduled_event_id": promise.deadline_schedule_id,
        "expected_schedule_revision": 1,
        "expected_source_inventory_revision": 4,
        "expected_recipient_inventory_revision": 2,
        "expected_source_account_balance": "5",
        "expected_recipient_account_balance": "7",
        "lots": (
            ReturnLotTransfer(
                source_lot_id=uid(50),
                received_lot_id=promise_return_lot_id(uid(40), uid(50)),
                quantity=promise.terms.quantity,
            ),
        ),
    }
    values.update(updates)
    return CommittedReturnTransfer.model_validate(values)


def test_promise_is_private_to_the_two_named_participants() -> None:
    promise = make_promise()

    assert can_view_promise(promise, promise.promisor_id)
    assert can_view_promise(promise, promise.promisee_id)
    assert not can_view_promise(promise, uid(99))


def test_promise_bounds_parties_deadline_and_decimal_quantity() -> None:
    with pytest.raises(ValidationError, match="distinct promisor and promisee"):
        make_promise(promisee_id=uid(2))
    with pytest.raises(ValidationError, match="deadline cannot precede"):
        make_promise(deadline_tick=99)
    with pytest.raises(ValidationError, match="room for its due tick"):
        make_promise(deadline_tick=2**63 - 1)
    with pytest.raises(ValidationError, match="fractional|decimal values"):
        ReturnItemTerms(resource_id="cultivation.herb", quantity="1.0000001", promisee_account_id=uid(30))
    with pytest.raises(ValidationError, match="integer digits|decimal values"):
        ReturnItemTerms(resource_id="cultivation.herb", quantity="1000000000000000000000000000000", promisee_account_id=uid(30))
    with pytest.raises(ValidationError, match="decimal strings or integers"):
        ReturnItemTerms(resource_id="cultivation.herb", quantity=1.25, promisee_account_id=uid(30))


def test_creation_derives_stable_deadline_schedule_one_tick_after_inclusive_deadline() -> None:
    promise = make_promise()
    schedule = build_promise_deadline_schedule(promise)

    assert promise.deadline_schedule_id == promise_deadline_schedule_id(promise.id)
    assert schedule.id == promise.deadline_schedule_id
    assert schedule.due_tick == promise.deadline_tick + 1
    assert schedule.phase == "maintenance"
    assert schedule.handler_id == "relationship_promise.deadline"
    assert schedule.max_runs == 1
    assert schedule.payload == {"promise_id": str(promise.id)}
    assert make_promise().deadline_schedule_id == promise.deadline_schedule_id


def test_recorded_pledge_is_not_itself_a_verified_fulfillment() -> None:
    with pytest.raises(ValidationError, match="a closed promise requires"):
        RelationshipPromise.model_validate({
            **make_promise().model_dump(mode="python"),
            "status": "fulfilled",
            "resolved_tick": 200,
        })


def test_exact_committed_fifo_return_fulfills_same_promise_event_and_advances_revision() -> None:
    promise = make_promise()
    evidence = return_event(promise)
    result = fulfill_return_promise(promise, evidence)

    assert result.status == "fulfilled"
    assert result.resolved_tick == promise.deadline_tick
    assert result.revision == promise.revision + 1
    assert result.outcome_event_id == evidence.event_id
    assert not hasattr(result, "outcome_evidence_event_ids")
    assert promise.status == "active"
    assert promise.revision == 1


@pytest.mark.parametrize(
    "change",
    [
        {"promise_id": uid(999)},
        {"source_owner_id": uid(999)},
        {"recipient_owner_id": uid(999)},
        {"actor_id": uid(999)},
        {"expected_promise_revision": 2},
        {"scheduled_event_id": uid(999)},
        {"transfer_tick": 201},
    ],
)
def test_return_rejects_wrong_promise_parties_schedule_tick_or_revision(change) -> None:
    promise = make_promise()

    with pytest.raises(ValueError):
        fulfill_return_promise(promise, return_event(promise, **change))


def test_return_requires_exact_resource_quantity_and_promisee_holder_account() -> None:
    promise = make_promise()
    altered_lot = ReturnLotTransfer(
        source_lot_id=uid(50),
        received_lot_id=promise_return_lot_id(uid(40), uid(50)),
        quantity="2",
    )
    evidence = return_event(promise, quantity="2", lots=(altered_lot,))
    with pytest.raises(ValueError, match="exact promise"):
        fulfill_return_promise(promise, evidence)

    evidence = return_event(promise, to_account_id=uid(99))
    with pytest.raises(ValueError, match="exact promise"):
        fulfill_return_promise(promise, evidence)

    with pytest.raises(ValidationError, match="Input should be 'holder'"):
        return_event(promise, recipient_account_type="consumption_sink")


def test_return_fifo_manifest_is_exact_unique_and_event_bound() -> None:
    promise = make_promise()
    with pytest.raises(ValidationError, match="exactly equal"):
        return_event(promise, lots=(ReturnLotTransfer(
            source_lot_id=uid(50),
            received_lot_id=promise_return_lot_id(uid(40), uid(50)),
            quantity="2",
        ),))
    with pytest.raises(ValidationError, match="unique"):
        return_event(promise, lots=(
            ReturnLotTransfer(source_lot_id=uid(50), received_lot_id=uid(60), quantity="1"),
            ReturnLotTransfer(source_lot_id=uid(50), received_lot_id=uid(61), quantity="2"),
        ))
    with pytest.raises(ValidationError, match="deterministically bound"):
        return_event(promise, lots=(ReturnLotTransfer(
            source_lot_id=uid(50), received_lot_id=uid(60), quantity="3",
        ),))
    with pytest.raises(ValidationError, match="source account must cover"):
        return_event(promise, expected_source_account_balance="2")


def test_promisee_can_release_but_promisor_cannot_and_stale_revision_fails() -> None:
    promise = make_promise()
    evidence = PromiseReleaseEvidence(
        event_id=uid(70),
        command_id=uid(71),
        promise_id=promise.id,
        expected_promise_revision=promise.revision,
        actor_id=promise.promisee_id,
        resolved_tick=150,
        scheduled_event_id=promise.deadline_schedule_id,
        expected_schedule_revision=1,
    )
    result = release_promise(promise, evidence)
    assert result.status == "released"
    assert result.resolved_tick == 150
    assert result.outcome_event_id == evidence.event_id
    assert result.revision == 2

    with pytest.raises(ValueError, match="only the promisee"):
        release_promise(promise, evidence.model_copy(update={"actor_id": promise.promisor_id}))
    with pytest.raises(ValueError, match="expected revision"):
        release_promise(promise, evidence.model_copy(update={"expected_promise_revision": 2}))
    with pytest.raises(ValueError, match="exact deadline schedule"):
        release_promise(promise, evidence.model_copy(update={"scheduled_event_id": uid(99)}))
    with pytest.raises(ValueError, match="before it is due"):
        release_promise(promise, evidence.model_copy(update={"resolved_tick": 201}))


def test_breach_requires_exact_schedule_exact_due_tick_and_active_revision() -> None:
    promise = make_promise()
    evidence = PromiseDeadlineEvidence(
        event_id=uid(80),
        command_id=uid(81),
        promise_id=promise.id,
        expected_promise_revision=promise.revision,
        resolved_tick=promise.deadline_tick + 1,
        scheduled_event_id=promise.deadline_schedule_id,
        expected_schedule_revision=1,
    )
    result = breach_promise_at_deadline(promise, evidence)
    assert result.status == "breached"
    assert result.resolved_tick == promise.deadline_tick + 1
    assert result.outcome_event_id == evidence.event_id
    assert result.revision == 2

    with pytest.raises(ValueError, match="exact due schedule"):
        breach_promise_at_deadline(promise, evidence.model_copy(update={"scheduled_event_id": uid(99)}))
    with pytest.raises(ValueError, match="exact due schedule"):
        breach_promise_at_deadline(promise, evidence.model_copy(update={"resolved_tick": 202}))
    with pytest.raises(ValueError, match="expected revision"):
        breach_promise_at_deadline(promise, evidence.model_copy(update={"expected_promise_revision": 2}))


def test_terminal_promises_reject_second_resolution() -> None:
    promise = make_promise()
    fulfilled = fulfill_return_promise(promise, return_event(promise))
    with pytest.raises(ValueError, match="active promise"):
        fulfill_return_promise(fulfilled, return_event(fulfilled))
