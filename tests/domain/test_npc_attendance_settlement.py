from decimal import Decimal
from uuid import uuid4

import pytest

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import event_payload, parse_event
from world_engine.domain.models import TimeAdvancedEvent, WorldState
from world_engine.domain.npc_attendance import plan_npc_attendance_start
from world_engine.domain.npc_attendance_events import (
    NpcAttendanceSettledEvent,
    NpcAttendanceStartedEvent,
    npc_attendance_event_id,
)
from world_engine.domain.npc_work_shift import plan_npc_work_shift
from world_engine.domain.reducers import apply_event, ledger_postings

from test_npc_attendance import _assignment_id, _state
from test_work_shift import OTHER_PLACE_ID, RESOURCE_ID, WORKER_ID


RULESET_VERSION = "npc-attendance-settlement.v1"


def _started_state(*, employer_balance="100"):
    state = _state(employer_balance=employer_balance)
    attendance = plan_npc_attendance_start(state, _assignment_id(state))
    assert attendance is not None
    command_id = uuid4()
    started = NpcAttendanceStartedEvent(
        event_id=npc_attendance_event_id(command_id, RULESET_VERSION, "started", attendance.assignment_id),
        world_id=uuid4(),
        branch_id=uuid4(),
        command_id=command_id,
        actor_id=WORKER_ID,
        ruleset_version=RULESET_VERSION,
        attendance=attendance,
    )
    return apply_event(state, started), attendance


def _settlement(attendance, *, effective_tick, payment=None):
    command_id = uuid4()
    return NpcAttendanceSettledEvent(
        event_id=npc_attendance_event_id(command_id, RULESET_VERSION, "settled", attendance.assignment_id),
        world_id=uuid4(),
        branch_id=uuid4(),
        command_id=command_id,
        actor_id=WORKER_ID,
        ruleset_version=RULESET_VERSION,
        attendance=attendance,
        effective_tick=effective_tick,
        payment=payment,
    )


def _at_end(state, attendance):
    return apply_event(state, TimeAdvancedEvent(to_tick=attendance.ends_at_tick))


def test_valid_end_settlement_removes_attendance_and_posts_exact_wage_once():
    state, attendance = _started_state()
    at_end = _at_end(state, attendance)
    payment = plan_npc_work_shift(at_end, attendance.assignment_id)
    assert payment is not None

    event = _settlement(attendance, effective_tick=attendance.ends_at_tick, payment=payment)
    settled = apply_event(at_end, event)

    worker_entry, employer_entry = payment.ledger_entries
    assert settled.npc_attendance == []
    assert settled.resources[worker_entry.account_id].balance == worker_entry.balance_after
    assert settled.resources[employer_entry.account_id].balance == employer_entry.balance_after
    assert [
        (posting.account_id, posting.resource_id, posting.delta)
        for posting in ledger_postings(event)
    ] == [
        (entry.account_id, RESOURCE_ID, entry.amount)
        for entry in payment.ledger_entries
    ]
    assert sum((posting.delta for posting in ledger_postings(event)), Decimal("0")) == 0

    wire_event = parse_event({
        "type": event.type,
        "event_id": str(event.event_id),
        "visibility": event.visibility,
        **event_payload(event),
    })
    assert ledger_postings(wire_event) == ledger_postings(event)
    assert apply_event(at_end, wire_event) == settled

    with pytest.raises(InvalidEvent, match="recorded interval"):
        apply_event(settled, event)


def test_exact_end_requires_the_recomputed_payment_and_matching_attendance():
    state, attendance = _started_state()
    at_end = _at_end(state, attendance)
    payment = plan_npc_work_shift(at_end, attendance.assignment_id)
    assert payment is not None

    with pytest.raises(InvalidEvent, match="exactly match"):
        apply_event(at_end, _settlement(attendance, effective_tick=attendance.ends_at_tick))

    forged_attendance = attendance.model_copy(update={"window_id": uuid4()})
    with pytest.raises(InvalidEvent, match="recorded interval"):
        apply_event(
            at_end,
            _settlement(forged_attendance, effective_tick=attendance.ends_at_tick, payment=payment),
        )

    with pytest.raises(InvalidEvent, match="current tick"):
        apply_event(
            state,
            _settlement(attendance, effective_tick=attendance.ends_at_tick, payment=payment),
        )


def test_invalid_attendance_and_old_records_can_only_be_cleaned_without_pay():
    state, attendance = _started_state()
    moved_payload = state.model_dump(mode="python")
    moved_payload["entities"][WORKER_ID]["location_id"] = OTHER_PLACE_ID
    moved = WorldState.model_validate(moved_payload)
    at_end = _at_end(moved, attendance)

    removed_invalid = apply_event(
        at_end,
        _settlement(attendance, effective_tick=attendance.ends_at_tick),
    )
    assert removed_invalid.npc_attendance == []
    assert removed_invalid.resources == at_end.resources

    still_working, old_attendance = _started_state()
    after_end_tick = old_attendance.ends_at_tick + 1
    after_end = apply_event(still_working, TimeAdvancedEvent(to_tick=after_end_tick))
    old_event = _settlement(old_attendance, effective_tick=after_end_tick)
    removed_old = apply_event(after_end, old_event)
    assert removed_old.npc_attendance == []
    assert removed_old.resources == after_end.resources
    assert ledger_postings(old_event) == ()


def test_unaffordable_shift_closes_attendance_without_payment():
    state, attendance = _started_state(employer_balance="1")
    at_end = _at_end(state, attendance)
    assert plan_npc_work_shift(at_end, attendance.assignment_id) is None

    event = _settlement(attendance, effective_tick=attendance.ends_at_tick)
    settled = apply_event(at_end, event)

    assert settled.npc_attendance == []
    assert settled.resources == at_end.resources
    assert ledger_postings(event) == ()
