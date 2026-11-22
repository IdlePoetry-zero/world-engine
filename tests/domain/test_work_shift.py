from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import InventoryComponent, InventoryLot, TreasuryComponent
from world_engine.domain.models import ResourceAccount, WorldEntity, WorldState
from world_engine.domain.occupations import OccupationSource, WorkAssignment, WorkPosting, occupation_assignment_id
from world_engine.domain.work_shift import (
    MAX_WORK_SHIFT_SECONDS,
    PaidWorkShiftPlan,
    WorkShiftError,
    WorkShiftTerms,
    plan_paid_work_shift,
    revalidate_paid_work_shift,
)


WORKER_ID = UUID("10000000-0000-4000-8000-000000000001")
EMPLOYER_ID = UUID("20000000-0000-4000-8000-000000000002")
PLACE_ID = UUID("30000000-0000-4000-8000-000000000003")
OTHER_PLACE_ID = UUID("31000000-0000-4000-8000-000000000013")
POSTING_ID = UUID("40000000-0000-4000-8000-000000000004")
ASSIGNMENT_SOURCE_ID = UUID("50000000-0000-4000-8000-000000000005")
WORKER_ACCOUNT_ID = UUID("60000000-0000-4000-8000-000000000006")
EMPLOYER_ACCOUNT_ID = UUID("70000000-0000-4000-8000-000000000007")
RESOURCE_ID = "core.silver"
DURATION = 3_600
WAGE = Decimal("12.50")


def make_state(
    *,
    tick=DURATION,
    worker_balance="5",
    employer_balance="100",
    worker_status="active",
    employer_status="active",
    worker_location=PLACE_ID,
    employer_location=PLACE_ID,
    posting_status="open",
    posting_revision=4,
    assignment_revision=2,
    assignment_ended_tick=None,
    posting_valid_until=None,
    employer_lots=(),
    work_shift_terms=None,
):
    source = OccupationSource(kind="event", reference_id=str(ASSIGNMENT_SOURCE_ID))
    assignment = WorkAssignment(
        assignment_id=occupation_assignment_id(source, WORKER_ID, POSTING_ID),
        actor_id=WORKER_ID,
        posting_id=POSTING_ID,
        employer_id=EMPLOYER_ID,
        location_id=PLACE_ID,
        title="Registry clerk",
        required_skill=None,
        started_tick=0,
        ended_tick=assignment_ended_tick,
        revision=assignment_revision,
        source=source,
        end_source=(OccupationSource(kind="event", reference_id=str(uuid4()))
                    if assignment_ended_tick is not None else None),
    )
    employer_components = {
        "treasury": TreasuryComponent(accounts={RESOURCE_ID: EMPLOYER_ACCOUNT_ID}).model_dump(mode="json"),
    }
    if employer_lots:
        employer_components["inventory"] = InventoryComponent(lots=list(employer_lots)).model_dump(mode="json")
    registered = work_shift_terms or {
        "posting_id": str(POSTING_ID),
        "duration_seconds": DURATION,
        "resource_id": RESOURCE_ID,
        "wage": str(WAGE),
    }
    if isinstance(registered, dict):
        registered = [registered]
    return WorldState(
        current_tick=tick,
        entities={
            WORKER_ID: WorldEntity(
                id=WORKER_ID,
                kind="character",
                location_id=worker_location,
                status=worker_status,
            ),
            EMPLOYER_ID: WorldEntity(
                id=EMPLOYER_ID,
                kind="organization",
                location_id=employer_location,
                status=employer_status,
                components=employer_components,
            ),
            PLACE_ID: WorldEntity(id=PLACE_ID, kind="place", status="active"),
            OTHER_PLACE_ID: WorldEntity(id=OTHER_PLACE_ID, kind="place", status="active"),
        },
        resources={
            WORKER_ACCOUNT_ID: ResourceAccount(
                resource_id=RESOURCE_ID,
                balance=worker_balance,
                owner_id=WORKER_ID,
            ),
            EMPLOYER_ACCOUNT_ID: ResourceAccount(
                resource_id=RESOURCE_ID,
                balance=employer_balance,
                owner_id=EMPLOYER_ID,
            ),
        },
        work_postings=[WorkPosting(
            posting_id=POSTING_ID,
            employer_id=EMPLOYER_ID,
            location_id=PLACE_ID,
            title="Registry clerk",
            slots=1,
            valid_from_tick=0,
            valid_until_tick=posting_valid_until,
            status=posting_status,
            revision=posting_revision,
        )],
        work_assignments=[assignment],
        metadata={"work_shift_terms": registered},
    )


def terms(**changes):
    values = {
        "posting_id": POSTING_ID,
        "duration_seconds": DURATION,
        "resource_id": RESOURCE_ID,
        "wage": WAGE,
    }
    values.update(changes)
    return WorkShiftTerms(**values)


def plan(state=None, **kwargs):
    registered_terms = kwargs.pop("terms", terms())
    if state is None:
        state = make_state(work_shift_terms=registered_terms.model_dump(mode="json"))
    return plan_paid_work_shift(
        state,
        terms=registered_terms,
        actor_id=kwargs.pop("actor_id", WORKER_ID),
        assignment_id=kwargs.pop(
            "assignment_id",
            occupation_assignment_id(
                OccupationSource(kind="event", reference_id=str(ASSIGNMENT_SOURCE_ID)), WORKER_ID, POSTING_ID,
            ),
        ),
        worker_account_id=kwargs.pop("worker_account_id", WORKER_ACCOUNT_ID),
        shift_started_tick=kwargs.pop("shift_started_tick", 0),
        expected_posting_revision=kwargs.pop("expected_posting_revision", 4),
        expected_assignment_revision=kwargs.pop("expected_assignment_revision", 2),
        **kwargs,
    )


def test_paid_shift_plan_is_exact_balanced_and_revalidates():
    state = make_state()
    result = plan(state)

    assert result.completed_tick == DURATION
    assert result.shift_started_tick == 0
    assert result.posting_revision == 4
    assert result.assignment_revision == 2
    assert [entry.account_id for entry in result.ledger_entries] == [WORKER_ACCOUNT_ID, EMPLOYER_ACCOUNT_ID]
    assert [entry.amount for entry in result.ledger_entries] == [Decimal("12.50"), Decimal("-12.50")]
    assert [entry.balance_before for entry in result.ledger_entries] == [Decimal("5"), Decimal("100")]
    assert [entry.balance_after for entry in result.ledger_entries] == [Decimal("17.50"), Decimal("87.50")]
    assert sum((entry.amount for entry in result.ledger_entries), Decimal("0")) == 0
    assert state.resources[WORKER_ACCOUNT_ID].balance == Decimal("5")
    assert state.resources[EMPLOYER_ACCOUNT_ID].balance == Decimal("100")

    assert revalidate_paid_work_shift(state, result, terms=terms(), actor_id=WORKER_ID) == result


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"worker_status": "dead"}, "actor_inactive"),
        ({"worker_location": OTHER_PLACE_ID}, "actor_not_at_workplace"),
        ({"employer_status": "dissolved"}, "employer_inactive"),
        ({"posting_status": "closed"}, "posting_closed"),
        ({"employer_balance": "12.49"}, "insufficient_employer_funds"),
        ({"assignment_ended_tick": DURATION}, "assignment_not_current"),
        ({"tick": DURATION + 1}, "completion_tick_mismatch"),
        ({"posting_valid_until": DURATION - 1}, "posting_out_of_date"),
    ],
)
def test_plan_rejects_inactive_stale_unavailable_or_mistimed_shift(updates, code):
    with pytest.raises(WorkShiftError) as raised:
        plan(make_state(**updates))
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("expected_posting_revision", 3, "stale_posting_revision"),
        ("expected_assignment_revision", 1, "stale_assignment_revision"),
        ("shift_started_tick", 1, "completion_tick_mismatch"),
    ],
)
def test_plan_checks_expected_revisions_and_start_tick(field, value, code):
    with pytest.raises(WorkShiftError) as raised:
        plan(**{field: value})
    assert raised.value.code == code


def test_posting_valid_until_is_exclusive_but_a_shift_may_end_at_that_tick():
    assert plan(make_state(posting_valid_until=DURATION)).completed_tick == DURATION


def test_active_employer_hq_may_be_remote_from_satellite_workplace():
    result = plan(make_state(employer_location=OTHER_PLACE_ID))

    assert result.employer_id == EMPLOYER_ID
    assert result.workplace_id == PLACE_ID
    assert result.ledger_entries[1].owner_id == EMPLOYER_ID
    assert result.ledger_entries[1].account_id == EMPLOYER_ACCOUNT_ID


def test_tick_overflow_is_rejected_before_building_the_plan():
    start = 2**63 - 1 - DURATION + 1
    state = make_state(tick=2**63 - 1)
    with pytest.raises(WorkShiftError) as raised:
        plan(state, shift_started_tick=start)
    assert raised.value.code == "tick_overflow"


def test_host_selected_worker_account_must_be_owned_holder_of_same_currency():
    with pytest.raises(WorkShiftError) as raised:
        plan(worker_account_id=EMPLOYER_ACCOUNT_ID)
    assert raised.value.code == "accounts_not_distinct"

    wrong_owner_state = make_state()
    wrong_owner_state.resources[WORKER_ACCOUNT_ID] = ResourceAccount(
        resource_id=RESOURCE_ID, balance="5", owner_id=EMPLOYER_ID,
    )
    with pytest.raises(WorkShiftError) as raised:
        plan(wrong_owner_state)
    assert raised.value.code == "worker_account_mismatch"


def test_wage_debit_preserves_already_recorded_employer_inventory_floor():
    lot = InventoryLot(
        lot_id=UUID("a0000000-0000-4000-8000-00000000000a"),
        account_id=EMPLOYER_ACCOUNT_ID,
        resource_id=RESOURCE_ID,
        quantity="90",
    )
    with pytest.raises(WorkShiftError) as raised:
        plan(make_state(employer_balance="100", employer_lots=[lot]))
    assert raised.value.code == "employer_inventory_balance_conflict"


def test_big_balances_and_small_exact_wage_do_not_round_during_settlement_planning():
    state = make_state(
        worker_balance="999999999999999999999999999999",
        employer_balance="999999999999999999999999999999",
        work_shift_terms={
            "posting_id": str(POSTING_ID),
            "duration_seconds": DURATION,
            "resource_id": RESOURCE_ID,
            "wage": "0.000001",
        },
    )
    result = plan(state, terms=terms(wage=Decimal("0.000001")))
    worker_entry, employer_entry = result.ledger_entries
    assert worker_entry.balance_after == Decimal("999999999999999999999999999999.000001")
    assert employer_entry.balance_after == Decimal("999999999999999999999999999998.999999")
    assert worker_entry.amount + employer_entry.amount == 0


def test_revalidation_rejects_changed_balance_or_policy_without_mutating_plan():
    initial = make_state()
    accepted = plan(initial)
    changed = make_state(employer_balance="99")
    with pytest.raises(WorkShiftError) as raised:
        revalidate_paid_work_shift(changed, accepted, terms=terms(), actor_id=WORKER_ID)
    assert raised.value.code == "stale_work_shift_plan"
    with pytest.raises(WorkShiftError) as raised:
        revalidate_paid_work_shift(initial, accepted, terms=terms(wage="13"), actor_id=WORKER_ID)
    assert raised.value.code == "registered_terms_changed"
    with pytest.raises(WorkShiftError) as raised:
        revalidate_paid_work_shift(initial, accepted, terms=terms(), actor_id=EMPLOYER_ID)
    assert raised.value.code == "actor_changed"


def test_registered_shift_terms_are_positive_and_bounded():
    assert WorkShiftTerms(
        posting_id=POSTING_ID,
        duration_seconds=MAX_WORK_SHIFT_SECONDS,
        resource_id=RESOURCE_ID,
        wage="0.000001",
    ).duration_seconds == MAX_WORK_SHIFT_SECONDS
    for invalid in (0, -1, MAX_WORK_SHIFT_SECONDS + 1):
        with pytest.raises(ValidationError):
            terms(duration_seconds=invalid)
    with pytest.raises(ValidationError):
        terms(wage="0")


def test_registered_terms_parser_is_optional_but_closed_and_posting_bound():
    from world_engine.domain.work_shift import registered_work_shift_terms

    assert registered_work_shift_terms(WorldState()) == {}
    state = make_state()
    assert registered_work_shift_terms(state) == {POSTING_ID: terms()}

    duplicate = make_state().model_copy(update={"metadata": {"work_shift_terms": [
        terms().model_dump(mode="json"),
        terms(wage="13").model_dump(mode="json"),
    ]}})
    with pytest.raises(WorkShiftError) as raised:
        registered_work_shift_terms(duplicate)
    assert raised.value.code == "duplicate_work_shift_terms"

    unknown = make_state().model_copy(update={"metadata": {"work_shift_terms": [{
        "posting_id": str(uuid4()),
        "duration_seconds": DURATION,
        "resource_id": RESOURCE_ID,
        "wage": str(WAGE),
    }]}})
    with pytest.raises(WorkShiftError) as raised:
        registered_work_shift_terms(unknown)
    assert raised.value.code == "work_shift_posting_missing"

    malformed = make_state().model_copy(update={"metadata": {"work_shift_terms": [{"posting_id": str(POSTING_ID)}]}})
    with pytest.raises(WorkShiftError) as raised:
        registered_work_shift_terms(malformed)
    assert raised.value.code == "work_shift_terms_invalid"


def test_plan_model_itself_enforces_zero_sum_and_distinct_accounts():
    result = plan()
    forged = result.model_dump(mode="python")
    forged["ledger_entries"][1]["amount"] = Decimal("-12.49")
    forged["ledger_entries"][1]["balance_after"] = Decimal("87.51")
    with pytest.raises(ValidationError, match="ledger entries"):
        PaidWorkShiftPlan.model_validate(forged)
