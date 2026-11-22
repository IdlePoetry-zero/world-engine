from uuid import uuid4

import pytest

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import parse_event
from world_engine.domain.gameplay_events import BackgroundNeedsAdvancedEvent, BackgroundRoutineAdvancedEvent
from world_engine.domain.models import TimeAdvancedEvent, WorldEntity
from world_engine.domain.reducers import apply_event
from world_engine.rules import RuleEngine

from test_rules_engine import _base_state, _context, _replay


def _needs(food: int = 60, water: int = 60, rest: int = 60):
    return {
        "food": {"value": food, "critical_below": 20},
        "water": {"value": water, "critical_below": 20},
        "rest": {"value": rest},
    }


def test_timed_action_batches_passive_need_advance_for_other_active_characters():
    npc_id = uuid4()
    npc = WorldEntity(
        id=npc_id,
        kind="character",
        name="Neighbor",
        components={"needs": _needs()},
    )
    state, _place_id, actor_id = _base_state(extra_entities={npc_id: npc})

    resolution = RuleEngine().evaluate(state, {"kind": "wait", "duration_ticks": 3600}, _context(actor_id))

    assert [event.type for event in resolution.events[:2]] == [
        "core.time_advanced.v1",
        "core.background_needs_advanced.v1",
    ]
    batch_events = [event for event in resolution.events if isinstance(event, BackgroundNeedsAdvancedEvent)]
    assert len(batch_events) == 1
    assert [item.entity_id for item in batch_events[0].expected_agents] == [npc_id]
    result = _replay(state, resolution.events)
    needs = result.entities[npc_id].components["needs"]
    assert needs["food"]["value"] == 60
    assert needs["food"]["decay_remainder"] == 64_800
    assert needs["water"]["value"] == 59
    assert needs["water"]["decay_remainder"] == 21_600
    assert needs["rest"]["value"] == 59
    assert result.entities[actor_id].components["needs"]["food"]["decay_remainder"] == 64_800
    legacy_payload = batch_events[0].model_dump(mode="json")
    legacy_payload.pop("expected_accounts")
    legacy_payload["expected_agents"] = [
        {"entity_id": agent["entity_id"], "expected_revision": agent["expected_revision"]}
        for agent in legacy_payload["expected_agents"]
    ]
    parsed = parse_event(legacy_payload)
    assert parsed.expected_agents[0].action == "continue"
    restored = _replay(state, [resolution.events[0], parsed, *resolution.events[2:]])
    assert restored == result


def test_background_boundary_is_exact_and_blocks_later_positive_time():
    npc_id = uuid4()
    npc = WorldEntity(
        id=npc_id,
        kind="character",
        name="Hungry Neighbor",
        components={"needs": _needs(food=20)},
    )
    state, _place_id, actor_id = _base_state(extra_entities={npc_id: npc})
    engine = RuleEngine()

    boundary = engine.next_background_boundary(state, actor_id=actor_id)
    assert boundary == 91_200

    at_boundary = engine.evaluate(
        state,
        {"kind": "wait", "duration_ticks": boundary},
        _context(actor_id),
    )
    assert at_boundary.outcome.status == "succeeded"
    result = _replay(state, at_boundary.events)
    assert engine.next_background_boundary(result, actor_id=actor_id) == boundary

    after_boundary = engine.evaluate(
        result,
        {"kind": "wait", "duration_ticks": 1},
        _context(actor_id),
    )
    assert after_boundary.outcome.status == "rejected"
    assert after_boundary.outcome.code == "background_deprivation_pending"


def test_background_event_rejects_incomplete_active_agent_manifest():
    npc_ids = [uuid4(), uuid4()]
    npcs = {
        npc_id: WorldEntity(
            id=npc_id,
            kind="character",
            name="Neighbor",
            components={"needs": _needs()},
        )
        for npc_id in npc_ids
    }
    state, _place_id, actor_id = _base_state(extra_entities=npcs)
    resolution = RuleEngine().evaluate(state, {"kind": "wait", "duration_ticks": 3600}, _context(actor_id))
    result = state
    for event in resolution.events:
        if isinstance(event, BackgroundNeedsAdvancedEvent):
            incomplete = event.model_copy(update={"expected_agents": event.expected_agents[:1]})
            with pytest.raises(InvalidEvent, match="every active nonacting character"):
                apply_event(result, incomplete)
        result = apply_event(result, event)


def test_world_advance_window_includes_every_character_under_account_actor_attribution():
    state, _place_id, player_id = _base_state()
    owner_account_id = uuid4()
    context = _context(owner_account_id)

    resolution = RuleEngine().resolve_background_window(
        state,
        from_tick=state.current_tick,
        to_tick=state.current_tick + 3600,
        context=context,
        choices=[],
        include_actor=True,
    )

    assert resolution.outcome.status == "succeeded"
    batch = next(event for event in resolution.events if isinstance(event, BackgroundRoutineAdvancedEvent))
    assert batch.actor_id == owner_account_id
    assert batch.include_actor is True
    assert [item.entity_id for item in batch.expected_agents] == [player_id]
    result = _replay(state, resolution.events)
    assert result.entities[player_id].components["needs"]["food"]["decay_remainder"] == 64_800


def test_focal_character_boundary_is_included_in_world_advance_mode():
    state, _place_id, actor_id = _base_state(actor_components={"needs": _needs(food=20)})
    engine = RuleEngine()

    assert engine.next_background_boundary(state, actor_id=actor_id) is None
    assert engine.next_background_boundary(state, actor_id=actor_id, include_actor=True) == 91_200
