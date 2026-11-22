from uuid import UUID

from world_engine.domain.components import DailyRoutineComponent, DailyRoutineWindow
from world_engine.domain.models import ResourceAccount, WorldState
from world_engine.domain.npc_work_shift import plan_npc_work_shift
from world_engine.domain.occupations import occupation_assignment_id
from world_engine.domain.work_shift import PaidWorkShiftPlan

from test_work_shift import (
    DURATION,
    PLACE_ID,
    RESOURCE_ID,
    WORKER_ID,
    make_state,
)


WINDOW_ID = UUID("81000000-0000-4000-8000-000000000001")
LEXICALLY_FIRST_ACCOUNT_ID = UUID("05000000-0000-4000-8000-000000000005")
WRONG_RESOURCE_ACCOUNT_ID = UUID("01000000-0000-4000-8000-000000000001")


def _state_with_routine(*, tick=DURATION, window_start=0, window_duration=DURATION,
                        window_place=PLACE_ID, employer_balance="100", player_id=None):
    state = make_state(tick=tick, employer_balance=employer_balance)
    payload = state.model_dump(mode="python")
    actor = payload["entities"][WORKER_ID]
    actor["components"]["daily_routine"] = DailyRoutineComponent(
        windows=[DailyRoutineWindow(
            window_id=WINDOW_ID,
            start_second=window_start,
            duration_seconds=window_duration,
            activity="work",
            place_id=window_place,
        )],
    ).model_dump(mode="python")
    payload["player_id"] = player_id
    return WorldState.model_validate(payload)


def _assignment_id(state):
    return next(row.assignment_id for row in state.work_assignments if row.actor_id == WORKER_ID)


def test_npc_work_shift_is_repeatable_at_exact_routine_window_end():
    payload = _state_with_routine().model_dump(mode="python")
    payload["resources"][LEXICALLY_FIRST_ACCOUNT_ID] = ResourceAccount(
        resource_id=RESOURCE_ID,
        balance="3",
        owner_id=WORKER_ID,
    ).model_dump(mode="python")
    payload["resources"][WRONG_RESOURCE_ACCOUNT_ID] = ResourceAccount(
        resource_id="core.copper",
        balance="1000",
        owner_id=WORKER_ID,
    ).model_dump(mode="python")
    state = WorldState.model_validate(payload)

    first = plan_npc_work_shift(state, _assignment_id(state))
    second = plan_npc_work_shift(state, _assignment_id(state))

    assert isinstance(first, PaidWorkShiftPlan)
    assert first == second
    assert first.shift_started_tick == 0
    assert first.completed_tick == DURATION
    assert first.actor_id == WORKER_ID
    assert first.ledger_entries[0].account_id == LEXICALLY_FIRST_ACCOUNT_ID


def test_npc_work_shift_matches_window_end_at_midnight_phase():
    day_seconds = 86_400
    state = make_state(tick=day_seconds)
    payload = state.model_dump(mode="python")
    actor = payload["entities"][WORKER_ID]
    actor["components"]["daily_routine"] = DailyRoutineComponent(
        windows=[DailyRoutineWindow(
            window_id=WINDOW_ID,
            start_second=day_seconds - DURATION,
            duration_seconds=DURATION,
            activity="work",
            place_id=PLACE_ID,
        )],
    ).model_dump(mode="python")
    state = WorldState.model_validate(payload)

    plan = plan_npc_work_shift(state, _assignment_id(state))

    assert plan is not None
    assert plan.shift_started_tick == day_seconds - DURATION
    assert plan.completed_tick == day_seconds


def test_npc_work_shift_returns_none_for_missing_policy_or_routine():
    no_routine = make_state()
    assert plan_npc_work_shift(no_routine, _assignment_id(no_routine)) is None

    no_terms_payload = _state_with_routine().model_dump(mode="python")
    no_terms_payload["metadata"].pop("work_shift_terms")
    no_terms = WorldState.model_validate(no_terms_payload)
    assert plan_npc_work_shift(no_terms, _assignment_id(no_terms)) is None


def test_npc_work_shift_returns_none_when_shift_is_not_at_window_end_or_location():
    not_at_end = _state_with_routine(tick=DURATION + 1)
    assert plan_npc_work_shift(not_at_end, _assignment_id(not_at_end)) is None

    wrong_place = _state_with_routine(window_place=UUID("31000000-0000-4000-8000-000000000013"))
    assert plan_npc_work_shift(wrong_place, _assignment_id(wrong_place)) is None


def test_npc_work_shift_returns_none_for_player_and_unaffordable_posting():
    player = _state_with_routine(player_id=WORKER_ID)
    assert plan_npc_work_shift(player, _assignment_id(player)) is None

    unaffordable = _state_with_routine(employer_balance="0")
    assert plan_npc_work_shift(unaffordable, _assignment_id(unaffordable)) is None


def test_npc_work_shift_requires_the_full_registered_duration_inside_window():
    too_short = _state_with_routine(window_duration=DURATION - 1)
    assert plan_npc_work_shift(too_short, _assignment_id(too_short)) is None
