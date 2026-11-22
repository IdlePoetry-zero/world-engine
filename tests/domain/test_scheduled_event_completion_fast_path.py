from __future__ import annotations

from uuid import uuid4

import pytest

from world_engine.domain import reducers
from world_engine.domain.components import ScheduledEventRecord
from world_engine.domain.gameplay_events import ScheduledEventCompletedEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, state_json


@pytest.mark.parametrize(
    ("handler_id", "handler_version", "completed_runs", "max_runs", "repeat_ticks"),
    [
        ("population.year_advance", "1.0.0", 0, 2, 30),
        ("environmental.harvest", "1.0.0", 1, 2, 30),
    ],
)
def test_known_schedule_completion_fast_path_matches_full_validation(
    handler_id: str,
    handler_version: str,
    completed_runs: int,
    max_runs: int,
    repeat_ticks: int,
):
    actor_id, dependency_id, schedule_id = (uuid4() for _ in range(3))
    dependency = ScheduledEventRecord(
        id=dependency_id,
        due_tick=30,
        phase="environment",
        handler_id="test.completed_dependency",
        handler_version="1.0.0",
        status="completed",
        completed_runs=1,
    )
    scheduled = ScheduledEventRecord(
        id=schedule_id,
        due_tick=60,
        phase="environment",
        dependencies=[dependency_id],
        handler_id=handler_id,
        handler_version=handler_version,
        repeat_ticks=repeat_ticks,
        max_runs=max_runs,
        completed_runs=completed_runs,
    )
    state = WorldState(
        current_tick=60,
        player_id=actor_id,
        entities={actor_id: WorldEntity(id=actor_id, kind="character", name="Player")},
        scheduled_events=[dependency, scheduled],
    )
    event = ScheduledEventCompletedEvent(
        scheduled_event_id=schedule_id,
        expected_revision=scheduled.revision,
    )

    completed_runs_next = completed_runs + 1
    repeats = repeat_ticks is not None and completed_runs_next < max_runs
    next_due_tick = scheduled.due_tick + repeat_ticks if repeats else scheduled.due_tick
    replacement = ScheduledEventRecord.model_validate(scheduled.model_dump(mode="python") | {
        "status": "queued" if repeats else "completed",
        "due_tick": next_due_tick,
        "completed_runs": completed_runs_next,
        "revision": scheduled.revision + 1,
    })
    full_validation_result = reducers._validated_state(state, {
        "scheduled_events": [dependency, replacement],
    })

    actual = apply_event(state, event)

    assert actual == full_validation_result
    assert state_json(actual) == state_json(full_validation_result)
    assert state.scheduled_events[1] == scheduled
    assert actual.scheduled_events[1].due_tick == next_due_tick
    assert actual.scheduled_events[1].status == ("queued" if repeats else "completed")


@pytest.mark.parametrize(
    ("handler_id", "handler_version"),
    [
        ("population.year_advance", "2.0.0"),
        ("test.custom", "1.0.0"),
    ],
)
def test_other_schedule_handlers_still_use_full_state_validation(
    monkeypatch: pytest.MonkeyPatch,
    handler_id: str,
    handler_version: str,
):
    actor_id, schedule_id = uuid4(), uuid4()
    scheduled = ScheduledEventRecord(
        id=schedule_id,
        due_tick=60,
        phase="environment",
        handler_id=handler_id,
        handler_version=handler_version,
    )
    state = WorldState(
        current_tick=60,
        player_id=actor_id,
        entities={actor_id: WorldEntity(id=actor_id, kind="character", name="Player")},
        scheduled_events=[scheduled],
    )
    calls: list[tuple[WorldState, dict[str, object]]] = []
    original = reducers._validated_state

    def recording_validation(source: WorldState, updates: dict[str, object]) -> WorldState:
        calls.append((source, updates))
        return original(source, updates)

    monkeypatch.setattr(reducers, "_validated_state", recording_validation)
    actual = apply_event(state, ScheduledEventCompletedEvent(
        scheduled_event_id=schedule_id,
        expected_revision=scheduled.revision,
    ))

    assert len(calls) == 1
    assert actual.scheduled_events[0].status == "completed"
