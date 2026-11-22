from uuid import uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.models import CalendarConfig, WorldEntity, WorldState


def window(window_id, place_id, start, duration, activity="rest"):
    return {
        "window_id": str(window_id),
        "start_second": start,
        "duration_seconds": duration,
        "activity": activity,
        "place_id": str(place_id),
    }


def test_daily_routine_is_a_character_component_and_allows_non_overlapping_daily_windows():
    character_id, place_id = uuid4(), uuid4()
    routine = {
        "windows": [
            window(uuid4(), place_id, 0, 2_400, "rest"),
            window(uuid4(), place_id, 4_800, 1_800, "study"),
        ]
    }
    state = WorldState(entities={
        place_id: WorldEntity(id=place_id, kind="place", name="Library"),
        character_id: WorldEntity(id=character_id, kind="character", name="Writer", components={"daily_routine": routine}),
    })
    assert len(state.entities[character_id].components["daily_routine"]["windows"]) == 2


def test_daily_routine_rejects_wrong_owner_overlaps_and_duplicate_window_ids():
    place_id, window_id = uuid4(), uuid4()
    with pytest.raises(ValidationError, match="not valid for entity kind"):
        WorldEntity(id=place_id, kind="place", components={"daily_routine": {"windows": []}})

    with pytest.raises(ValidationError, match="cannot overlap"):
        WorldEntity(id=uuid4(), kind="character", components={"daily_routine": {"windows": [
            window(window_id, place_id, 0, 100), window(uuid4(), place_id, 99, 20, "work"),
        ]}})

    with pytest.raises(ValidationError, match="window IDs must be unique"):
        WorldEntity(id=uuid4(), kind="character", components={"daily_routine": {"windows": [
            window(window_id, place_id, 0, 100), window(window_id, place_id, 100, 20, "work"),
        ]}})


def test_daily_routine_place_and_day_bounds_are_checked_against_world():
    character_id, place_id = uuid4(), uuid4()
    valid = WorldEntity(id=character_id, kind="character", components={"daily_routine": {"windows": [
        window(uuid4(), place_id, 80, 20),
    ]}})
    with pytest.raises(ValueError, match="must reference an existing place"):
        WorldState(entities={character_id: valid})

    retired_place = WorldEntity(id=place_id, kind="place", status="retired")
    with pytest.raises(ValueError, match="cannot reference a retired place"):
        WorldState(entities={place_id: retired_place, character_id: valid})

    active_place = WorldEntity(id=place_id, kind="place")
    with pytest.raises(ValueError, match="fit within one calendar day"):
        WorldState(
            entities={place_id: active_place, character_id: valid},
            calendar=CalendarConfig(day_seconds=90),
        )
