from uuid import UUID

from pydantic import ValidationError
import pytest

from world_engine.domain.components import DailyRoutineComponent, DailyRoutineWindow
from world_engine.domain.models import WorldState
from world_engine.domain.npc_attendance import (
    NpcAttendance,
    npc_attendance_is_valid,
    plan_npc_attendance_start,
)

from test_work_shift import DURATION, PLACE_ID, WORKER_ID, make_state


DAY_SECONDS = 86_400
WINDOW_ID = UUID("82000000-0000-4000-8000-000000000001")


def _state(*, tick=DURATION, window_start=0, window_duration=2 * DURATION,
           window_place=PLACE_ID, employer_balance="100", player_id=None,
           posting_valid_until=None):
    state = make_state(tick=tick, employer_balance=employer_balance,
                       posting_valid_until=posting_valid_until)
    payload = state.model_dump(mode="python")
    payload["player_id"] = player_id
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
    return WorldState.model_validate(payload)


def _assignment_id(state):
    return next(row.assignment_id for row in state.work_assignments if row.actor_id == WORKER_ID)


def _with_tick(state, tick):
    payload = state.model_dump(mode="python")
    payload["current_tick"] = tick
    return WorldState.model_validate(payload)


def test_attendance_plans_exact_shift_start_and_is_valid_through_inclusive_end():
    state = _state(tick=DURATION, employer_balance="0")

    attendance = plan_npc_attendance_start(state, _assignment_id(state))

    assert isinstance(attendance, NpcAttendance)
    assert attendance.actor_id == WORKER_ID
    assert attendance.workplace_id == PLACE_ID
    assert attendance.window_id == WINDOW_ID
    assert attendance.starts_at_tick == DURATION
    assert attendance.ends_at_tick == 2 * DURATION
    assert npc_attendance_is_valid(state, attendance)
    assert npc_attendance_is_valid(_with_tick(state, 2 * DURATION - 1), attendance)
    assert npc_attendance_is_valid(_with_tick(state, 2 * DURATION), attendance)
    assert not npc_attendance_is_valid(_with_tick(state, 2 * DURATION + 1), attendance)


def test_attendance_matches_a_shift_start_at_midnight_phase():
    shift_start = DAY_SECONDS - DURATION
    state = _state(tick=shift_start, window_start=shift_start, window_duration=DURATION)

    attendance = plan_npc_attendance_start(state, _assignment_id(state))

    assert attendance is not None
    assert attendance.starts_at_tick == shift_start
    assert attendance.ends_at_tick == DAY_SECONDS
    assert npc_attendance_is_valid(_with_tick(state, DAY_SECONDS), attendance)


def test_attendance_requires_exact_work_window_start_place_and_duration():
    not_at_start = _state(tick=DURATION + 1)
    assert plan_npc_attendance_start(not_at_start, _assignment_id(not_at_start)) is None

    wrong_place = _state(window_place=UUID("31000000-0000-4000-8000-000000000013"))
    assert plan_npc_attendance_start(wrong_place, _assignment_id(wrong_place)) is None

    too_short = _state(window_duration=DURATION - 1)
    assert plan_npc_attendance_start(too_short, _assignment_id(too_short)) is None


def test_attendance_requires_registered_terms_player_exclusion_and_valid_posting_interval():
    player_state = _state(player_id=WORKER_ID)
    assert plan_npc_attendance_start(player_state, _assignment_id(player_state)) is None

    no_terms_payload = _state().model_dump(mode="python")
    no_terms_payload["metadata"].pop("work_shift_terms")
    no_terms = WorldState.model_validate(no_terms_payload)
    assert plan_npc_attendance_start(no_terms, _assignment_id(no_terms)) is None

    invalid_until = _state(posting_valid_until=2 * DURATION - 1)
    assert plan_npc_attendance_start(invalid_until, _assignment_id(invalid_until)) is None


def test_attendance_validity_rechecks_actor_presence_and_revision_snapshot():
    state = _state()
    attendance = plan_npc_attendance_start(state, _assignment_id(state))
    assert attendance is not None

    payload = state.model_dump(mode="python")
    payload["entities"][WORKER_ID]["location_id"] = UUID("31000000-0000-4000-8000-000000000013")
    departed = WorldState.model_validate(payload)
    assert not npc_attendance_is_valid(departed, attendance)

    changed_payload = state.model_dump(mode="python")
    changed_assignment = changed_payload["work_assignments"][0]
    changed_assignment["revision"] += 1
    changed = WorldState.model_validate(changed_payload)
    assert not npc_attendance_is_valid(changed, attendance)


def test_attendance_contract_is_frozen_and_requires_positive_duration():
    state = _state()
    attendance = plan_npc_attendance_start(state, _assignment_id(state))
    assert attendance is not None

    with pytest.raises(ValidationError):
        attendance.ends_at_tick = attendance.starts_at_tick
