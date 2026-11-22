from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import EVENT_TYPES, parse_event, validate_npc_attendance_scope
from world_engine.domain.npc_attendance import NpcAttendance
from world_engine.domain.npc_attendance_events import (
    NPC_ATTENDANCE_CANCELLED_EVENT_TYPE,
    NPC_ATTENDANCE_SETTLED_EVENT_TYPE,
    NPC_ATTENDANCE_STARTED_EVENT_TYPE,
    NpcAttendanceCancelledEvent,
    NpcAttendanceSettledEvent,
    NpcAttendanceStartedEvent,
    npc_attendance_event_id,
    parse_npc_attendance_event,
)
from world_engine.domain.work_shift import PaidWorkShiftPlan, WorkShiftLedgerEntry, WorkShiftTerms


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
COMMAND_ACTOR_ID = UUID("40000000-0000-4000-8000-000000000004")
NPC_ID = UUID("50000000-0000-4000-8000-000000000005")
ASSIGNMENT_ID = UUID("60000000-0000-4000-8000-000000000006")
WORKPLACE_ID = UUID("70000000-0000-4000-8000-000000000007")
WINDOW_ID = UUID("80000000-0000-4000-8000-000000000008")
RULESET = "attendance-test.v1"
START_TICK = 3_600
END_TICK = 7_200
EMPLOYER_ID = UUID("90000000-0000-4000-8000-000000000009")
POSTING_ID = UUID("a0000000-0000-4000-8000-00000000000a")
WORKER_ACCOUNT_ID = UUID("b0000000-0000-4000-8000-00000000000b")
EMPLOYER_ACCOUNT_ID = UUID("c0000000-0000-4000-8000-00000000000c")


def _attendance(*, assignment_id=ASSIGNMENT_ID, actor_id=NPC_ID):
    return NpcAttendance(
        assignment_id=assignment_id,
        actor_id=actor_id,
        workplace_id=WORKPLACE_ID,
        window_id=WINDOW_ID,
        expected_assignment_revision=4,
        expected_posting_revision=7,
        starts_at_tick=START_TICK,
        ends_at_tick=END_TICK,
    )


def _started(*, attendance=None, event_id=None, visibility=None):
    attendance = attendance or _attendance()
    return NpcAttendanceStartedEvent(
        event_id=event_id or npc_attendance_event_id(
            COMMAND_ID, RULESET, "started", attendance.assignment_id,
        ),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=COMMAND_ACTOR_ID,
        ruleset_version=RULESET,
        attendance=attendance,
        **({"visibility": visibility} if visibility is not None else {}),
    )


def _cancelled(*, attendance=None, effective_tick=END_TICK, event_id=None, visibility=None):
    attendance = attendance or _attendance()
    return NpcAttendanceCancelledEvent(
        event_id=event_id or npc_attendance_event_id(
            COMMAND_ID, RULESET, "cancelled", attendance.assignment_id,
        ),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=COMMAND_ACTOR_ID,
        ruleset_version=RULESET,
        attendance=attendance,
        effective_tick=effective_tick,
        **({"visibility": visibility} if visibility is not None else {}),
    )


def _payment(*, attendance=None, assignment_id=None):
    attendance = attendance or _attendance()
    return PaidWorkShiftPlan(
        terms=WorkShiftTerms(
            posting_id=POSTING_ID,
            duration_seconds=attendance.ends_at_tick - attendance.starts_at_tick,
            resource_id="core.credits",
            wage="12.50",
        ),
        actor_id=attendance.actor_id,
        employer_id=EMPLOYER_ID,
        workplace_id=attendance.workplace_id,
        assignment_id=assignment_id or attendance.assignment_id,
        posting_revision=attendance.expected_posting_revision,
        assignment_revision=attendance.expected_assignment_revision,
        shift_started_tick=attendance.starts_at_tick,
        completed_tick=attendance.ends_at_tick,
        ledger_entries=(
            WorkShiftLedgerEntry(
                account_id=WORKER_ACCOUNT_ID,
                owner_id=attendance.actor_id,
                resource_id="core.credits",
                amount="12.50",
                balance_before="2.00",
                balance_after="14.50",
            ),
            WorkShiftLedgerEntry(
                account_id=EMPLOYER_ACCOUNT_ID,
                owner_id=EMPLOYER_ID,
                resource_id="core.credits",
                amount="-12.50",
                balance_before="100.00",
                balance_after="87.50",
            ),
        ),
    )


def _settled(*, attendance=None, effective_tick=END_TICK, payment=None, event_id=None, visibility=None):
    attendance = attendance or _attendance()
    return NpcAttendanceSettledEvent(
        event_id=event_id or npc_attendance_event_id(
            COMMAND_ID, RULESET, "settled", attendance.assignment_id,
        ),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=COMMAND_ACTOR_ID,
        ruleset_version=RULESET,
        attendance=attendance,
        effective_tick=effective_tick,
        **({"payment": payment} if payment is not None else {}),
        **({"visibility": visibility} if visibility is not None else {}),
    )


def test_started_event_binds_command_assignment_and_private_audience():
    event = _started()

    assert event.type == NPC_ATTENDANCE_STARTED_EVENT_TYPE
    assert event.event_id == npc_attendance_event_id(COMMAND_ID, RULESET, "started", ASSIGNMENT_ID)
    assert event.actor_id == COMMAND_ACTOR_ID
    assert event.actor_id != event.attendance.actor_id  # command actor is the authenticated world-advance owner
    assert event.attendance.starts_at_tick == START_TICK
    assert event.attendance.ends_at_tick == END_TICK
    assert event.visibility == {"policy": "author_only"}
    assert parse_npc_attendance_event(event.model_dump(mode="json")) == event


def test_cancelled_event_binds_effective_tick_and_accepts_end_boundary():
    event = _cancelled()

    assert event.type == NPC_ATTENDANCE_CANCELLED_EVENT_TYPE
    assert event.event_id == npc_attendance_event_id(COMMAND_ID, RULESET, "cancelled", ASSIGNMENT_ID)
    assert event.effective_tick == END_TICK
    assert event.visibility == {"policy": "author_only"}
    assert parse_npc_attendance_event(event.model_dump(mode="json")) == event


def test_settled_event_binds_command_assignment_and_omits_missing_payment():
    event = _settled(effective_tick=END_TICK + 100)
    payload = event.model_dump(mode="json")

    assert event.type == NPC_ATTENDANCE_SETTLED_EVENT_TYPE
    assert event.event_id == npc_attendance_event_id(COMMAND_ID, RULESET, "settled", ASSIGNMENT_ID)
    assert event.actor_id == COMMAND_ACTOR_ID
    assert event.actor_id != event.attendance.actor_id
    assert "payment" not in payload
    assert parse_npc_attendance_event(payload) == event
    parsed = parse_event(payload)
    assert isinstance(parsed, NpcAttendanceSettledEvent)
    assert EVENT_TYPES[NPC_ATTENDANCE_SETTLED_EVENT_TYPE] is NpcAttendanceSettledEvent
    validate_npc_attendance_scope(
        [parsed], world_id=WORLD_ID, branch_id=BRANCH_ID, command_id=COMMAND_ID,
        actor_id=COMMAND_ACTOR_ID, ruleset_version=RULESET,
    )
    with pytest.raises(InvalidEvent, match="match the world, branch"):
        validate_npc_attendance_scope(
            [parsed], world_id=uuid4(), branch_id=BRANCH_ID, command_id=COMMAND_ID,
            actor_id=COMMAND_ACTOR_ID, ruleset_version=RULESET,
        )


def test_settled_event_accepts_matching_payment_only_at_exact_end():
    attendance = _attendance()
    event = _settled(attendance=attendance, payment=_payment(attendance=attendance))

    assert event.effective_tick == attendance.ends_at_tick
    assert event.payment is not None
    assert parse_event(event.model_dump(mode="json")) == event

    payload = event.model_dump(mode="python")
    payload["effective_tick"] = attendance.ends_at_tick + 1
    with pytest.raises(ValidationError, match="exact scheduled end"):
        NpcAttendanceSettledEvent.model_validate(payload)

    with pytest.raises(ValidationError, match="match its registered assignment"):
        _settled(attendance=attendance, payment=_payment(attendance=attendance, assignment_id=uuid4()))


@pytest.mark.parametrize("effective_tick", [START_TICK, END_TICK - 1])
def test_settled_event_rejects_effective_tick_before_attendance_end(effective_tick):
    with pytest.raises(ValidationError, match="before its scheduled end"):
        _settled(effective_tick=effective_tick)


def test_event_ids_are_deterministic_and_distinguish_operation_scope():
    started = npc_attendance_event_id(COMMAND_ID, RULESET, "started", ASSIGNMENT_ID)
    assert started == npc_attendance_event_id(COMMAND_ID, RULESET, "started", ASSIGNMENT_ID)
    assert started != npc_attendance_event_id(uuid4(), RULESET, "started", ASSIGNMENT_ID)
    assert started != npc_attendance_event_id(COMMAND_ID, "other.v1", "started", ASSIGNMENT_ID)
    assert started != npc_attendance_event_id(COMMAND_ID, RULESET, "cancelled", ASSIGNMENT_ID)
    assert started != npc_attendance_event_id(COMMAND_ID, RULESET, "started", uuid4())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", UUID("90000000-0000-4000-8000-000000000009")),
        ("visibility", {"policy": "public"}),
        ("ruleset_version", " "),
    ],
)
def test_started_event_rejects_wrong_event_identity_or_non_private_shape(field, value):
    values = _started().model_dump(mode="python")
    values[field] = value
    with pytest.raises(ValidationError):
        NpcAttendanceStartedEvent.model_validate(values)


@pytest.mark.parametrize("effective_tick", [START_TICK - 1, END_TICK + 1])
def test_cancelled_event_rejects_tick_outside_attendance_interval(effective_tick):
    values = _cancelled().model_dump(mode="python")
    values["effective_tick"] = effective_tick
    with pytest.raises(ValidationError, match="scheduled interval"):
        NpcAttendanceCancelledEvent.model_validate(values)


def test_cancelled_event_rejects_wrong_identity_and_public_visibility():
    values = _cancelled().model_dump(mode="python")
    with pytest.raises(ValidationError, match="bound to its command"):
        NpcAttendanceCancelledEvent.model_validate({**values, "event_id": uuid4()})
    with pytest.raises(ValidationError, match="fixed to author_only"):
        NpcAttendanceCancelledEvent.model_validate({**values, "visibility": {"policy": "public"}})


def test_event_id_helper_rejects_invalid_scope():
    with pytest.raises(ValueError, match="UUID command and assignment"):
        npc_attendance_event_id("not-a-uuid", RULESET, "started", ASSIGNMENT_ID)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nonempty ruleset"):
        npc_attendance_event_id(COMMAND_ID, "  ", "started", ASSIGNMENT_ID)
    with pytest.raises(ValueError, match="operation must be"):
        npc_attendance_event_id(COMMAND_ID, RULESET, "ended", ASSIGNMENT_ID)  # type: ignore[arg-type]
