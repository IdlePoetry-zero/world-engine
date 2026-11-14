from copy import deepcopy

import pytest

from world_engine.application.visibility import ProjectionError, project_event, project_player_state


def state():
    return {
        "entities": {
            "p": {"id": "p", "kind": "character", "name": "玩家", "location_id": "town", "components": {"health": 90}},
            "n": {"id": "n", "kind": "character", "name": "商人", "location_id": "town", "components": {"public_role": "商人", "secret": "凶手", "health": 12}},
            "hidden": {"id": "hidden", "kind": "character", "name": "刺客", "location_id": "town", "components": {"hidden": True}},
            "far": {"id": "far", "kind": "character", "name": "远人", "location_id": "remote", "components": {}},
            "town": {"id": "town", "kind": "place", "name": "小镇", "components": {}},
        },
        "resources": {"mine": {"owner_id": "p", "resource_id": "coin", "balance": "5"}, "other": {"owner_id": "n", "balance": "900"}},
        "knowledge": [{"owner_id": "p", "claim": "听说有凶手", "secret_truth": "商人是凶手"}, {"owner_id": "n", "claim": "我是凶手"}],
        "goals": [{"owner_id": "n", "title": "灭口"}],
        "relations": [{"from_id": "n", "to_id": "p", "dimensions": {"hatred": 90}}],
        "scheduled_events": [{"title": "明日刺杀"}],
        "metadata": {"pack_id": "test", "killer": "n"},
    }


def test_projection_is_conservative_and_does_not_mutate_world():
    world = state()
    before = deepcopy(world)
    view = project_player_state(world, "p")
    assert set(view["entities"]) == {"p", "n", "town"}
    assert view["entities"]["n"]["components"] == {"public_role": "商人"}
    assert set(view["resources"]) == {"mine"}
    assert view["knowledge"] == [{"claim": "听说有凶手"}]
    assert view["goals"] == [] and view["relations"] == []
    assert view["metadata"] == {"pack_id": "test"}
    assert "scheduled_events" not in view
    view["entities"]["p"]["components"]["health"] = 0
    assert world == before


def test_unknown_actor_cannot_receive_world():
    with pytest.raises(ProjectionError):
        project_player_state(state(), "unknown")


def test_history_requires_audience_safe_payload_not_just_visible_event():
    event = {"event_id": "e", "type": "speech", "payload": {"secret": "truth"}, "visibility": {"policy": "public"}}
    assert project_event(event, "p") is None
    event["audience_views"] = {"public": {"said": "我不知道"}}
    assert project_event(event, "p")["payload"] == {"said": "我不知道"}
    event["visibility"] = {"policy": "participants", "subject_ids": ["n"]}
    assert project_event(event, "p") is None
