from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.work_shift import (
    PaidWorkShiftPlan,
    WorkShiftLedgerEntry,
    WorkShiftTerms,
)
from world_engine.domain.work_shift_events import (
    WORK_SHIFT_PAID_EVENT_TYPE,
    WorkShiftPaidEvent,
    work_shift_paid_event_id,
)


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
ACTOR_ID = UUID("40000000-0000-4000-8000-000000000004")
EMPLOYER_ID = UUID("50000000-0000-4000-8000-000000000005")
WORKPLACE_ID = UUID("60000000-0000-4000-8000-000000000006")
POSTING_ID = UUID("70000000-0000-4000-8000-000000000007")
ASSIGNMENT_ID = UUID("80000000-0000-4000-8000-000000000008")
WORKER_ACCOUNT_ID = UUID("90000000-0000-4000-8000-000000000009")
EMPLOYER_ACCOUNT_ID = UUID("a0000000-0000-4000-8000-00000000000a")
RULESET = "work-shift-test.v1"


def _plan(*, actor_id=ACTOR_ID, assignment_id=ASSIGNMENT_ID):
    terms = WorkShiftTerms(
        posting_id=POSTING_ID,
        duration_seconds=3_600,
        resource_id="core.credits",
        wage="12.50",
    )
    return PaidWorkShiftPlan(
        terms=terms,
        actor_id=actor_id,
        employer_id=EMPLOYER_ID,
        workplace_id=WORKPLACE_ID,
        assignment_id=assignment_id,
        posting_revision=4,
        assignment_revision=1,
        shift_started_tick=7_200,
        completed_tick=10_800,
        ledger_entries=(
            WorkShiftLedgerEntry(
                account_id=WORKER_ACCOUNT_ID,
                owner_id=actor_id,
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


def _event(*, actor_id=ACTOR_ID, plan=None, event_id=None, visibility=None):
    plan = plan or _plan(actor_id=actor_id)
    return WorkShiftPaidEvent(
        event_id=event_id or work_shift_paid_event_id(COMMAND_ID, RULESET, plan.assignment_id),
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        actor_id=actor_id,
        ruleset_version=RULESET,
        plan=plan,
        **({"visibility": visibility} if visibility is not None else {}),
    )


def test_paid_shift_event_binds_command_ruleset_assignment_and_worker():
    event = _event()

    assert event.type == WORK_SHIFT_PAID_EVENT_TYPE
    assert event.event_id == work_shift_paid_event_id(COMMAND_ID, RULESET, ASSIGNMENT_ID)
    assert event.actor_id == event.plan.actor_id == ACTOR_ID
    assert event.plan.assignment_id == ASSIGNMENT_ID
    assert event.visibility == {"policy": "author_only"}
    assert _event().event_id == event.event_id

    assert event.event_id != work_shift_paid_event_id(uuid4(), RULESET, ASSIGNMENT_ID)
    assert event.event_id != work_shift_paid_event_id(COMMAND_ID, "other-ruleset", ASSIGNMENT_ID)
    assert event.event_id != work_shift_paid_event_id(COMMAND_ID, RULESET, uuid4())


def test_paid_shift_event_rejects_wrong_identity_actor_and_public_visibility():
    values = _event().model_dump(mode="python")

    with pytest.raises(ValidationError, match="bound to its command"):
        WorkShiftPaidEvent.model_validate({**values, "event_id": uuid4()})

    with pytest.raises(ValidationError, match="actor must match"):
        WorkShiftPaidEvent.model_validate({**values, "actor_id": uuid4()})

    with pytest.raises(ValidationError, match="visibility is fixed"):
        WorkShiftPaidEvent.model_validate({**values, "visibility": {"policy": "public"}})


def test_paid_shift_event_rejects_invalid_ruleset_and_plan_shape():
    with pytest.raises(ValueError, match="nonempty ruleset"):
        work_shift_paid_event_id(COMMAND_ID, "  ", ASSIGNMENT_ID)

    values = _event().model_dump(mode="python")
    replacement_plan = _plan(assignment_id=uuid4())
    with pytest.raises(ValidationError, match="bound to its command"):
        WorkShiftPaidEvent.model_validate({**values, "plan": replacement_plan})
