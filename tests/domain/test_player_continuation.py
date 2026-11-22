from __future__ import annotations

from uuid import uuid4, uuid5

import pytest

from world_engine.domain import (
    InvalidEvent,
    PlayerCharacterContinuedEvent,
    PLAYER_CHARACTER_CONTINUED_EVENT_TYPE,
    PLAYER_CONTINUE_AFTER_DEATH_COMMAND,
    WorldState,
    is_player_continuation_candidate,
    plan_player_character_continuation,
    validate_player_character_continued_batch,
)
from world_engine.domain.components import RelationRecord
from world_engine.domain.events import parse_event
from world_engine.domain.models import ResourceAccount, WorldEntity
from world_engine.domain.reducers import apply_event


RULESET = "2.5.0"


def _state(*, previous_status="dead", target_status="active", target_life=True):
    place_id, previous_id, target_id, account_id = (uuid4() for _ in range(4))
    entities = {
        place_id: WorldEntity(id=place_id, kind="place", name="Market Square"),
        previous_id: WorldEntity(
            id=previous_id,
            kind="character",
            name="The Late Player",
            status=previous_status,
            location_id=place_id,
            components={"inventory": {"lots": []}},
        ),
        target_id: WorldEntity(
            id=target_id,
            kind="character",
            name="A Living Resident",
            status=target_status,
            location_id=place_id,
            components=(
                {"life": {"birth_tick": 0, "lifespan_ticks": 100}, "inventory": {"lots": []}}
                if target_life else {"inventory": {"lots": []}}
            ),
        ),
    }
    state = WorldState(
        current_tick=50,
        entities=entities,
        resources={
            account_id: ResourceAccount(
                resource_id="core.silver", balance="17", owner_id=previous_id,
            ),
        },
        relations=[RelationRecord(
            id=uuid4(), from_id=previous_id, to_id=target_id,
            dimensions={"familiarity": 11, "trust": -4}, updated_tick=49,
        )],
        player_id=previous_id,
    )
    return state, previous_id, target_id


def _plan(state, previous_id, target_id, *, source_death_event_id=None):
    world_id, branch_id, command_id = uuid4(), uuid4(), uuid4()
    source = source_death_event_id or uuid4()
    event = plan_player_character_continuation(
        state,
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        ruleset_version=RULESET,
        actor_id=previous_id,
        previous_player_id=previous_id,
        new_player_id=target_id,
        source_death_event_id=source,
    )
    return world_id, branch_id, command_id, source, event


def _batch(state, event, *, command_type=PLAYER_CONTINUE_AFTER_DEATH_COMMAND, payload=None):
    if payload is None:
        payload = {
            "previous_player_id": str(event.previous_player_id),
            "new_player_id": str(event.new_player_id),
            "source_death_event_id": str(event.source_death_event_id),
        }
    validate_player_character_continued_batch(
        state,
        event.actor_id,
        [event],
        world_id=event.world_id,
        branch_id=event.branch_id,
        command_id=event.command_id,
        ruleset_version=event.ruleset_version,
        command_type=command_type,
        command_payload=payload,
    )


def test_continuation_event_changes_only_branch_player_pointer_and_round_trips():
    state, previous_id, target_id = _state()
    world_id, branch_id, command_id, source_id, event = _plan(state, previous_id, target_id)

    assert isinstance(event, PlayerCharacterContinuedEvent)
    assert event.type == PLAYER_CHARACTER_CONTINUED_EVENT_TYPE
    assert event.event_id == uuid5(command_id, f"{RULESET}:player_character_continued")
    assert event.world_id == world_id
    assert event.branch_id == branch_id
    assert event.command_id == command_id
    assert event.actor_id == previous_id
    assert event.previous_player_id == previous_id
    assert event.new_player_id == target_id
    assert event.source_death_event_id == source_id
    assert event.current_tick == state.current_tick

    _batch(state, event)
    parsed = parse_event(event.model_dump(mode="json"))
    continued = apply_event(state, parsed)

    assert continued.player_id == target_id
    assert continued == state.model_copy(update={"player_id": target_id})
    assert continued.current_tick == state.current_tick
    assert continued.entities == state.entities
    assert continued.resources == state.resources
    assert continued.relations == state.relations
    assert continued.investigation_cases == state.investigation_cases


def test_legacy_active_target_without_typed_lifespan_is_eligible():
    state, previous_id, target_id = _state(target_life=False)
    assert is_player_continuation_candidate(state, target_id) is True
    _world_id, _branch_id, _command_id, _source_id, event = _plan(state, previous_id, target_id)

    _batch(state, event)
    assert apply_event(state, event).player_id == target_id


@pytest.mark.parametrize(
    "state_options,target_life",
    [
        ({"previous_status": "active"}, True),
        ({"previous_status": "retired"}, True),
        ({"target_status": "dead"}, True),
        ({"target_status": "retired"}, True),
    ],
)
def test_planner_rejects_non_death_predecessor_or_non_active_target(state_options, target_life):
    state, previous_id, target_id = _state(**state_options, target_life=target_life)
    assert is_player_continuation_candidate(
        state, target_id,
    ) is (state_options.get("target_status", "active") == "active")
    with pytest.raises(InvalidEvent):
        plan_player_character_continuation(
            state,
            world_id=uuid4(),
            branch_id=uuid4(),
            command_id=uuid4(),
            ruleset_version=RULESET,
            actor_id=previous_id,
            previous_player_id=previous_id,
            new_player_id=target_id,
            source_death_event_id=uuid4(),
        )


def test_planner_rejects_stale_control_pointer_wrong_actor_and_already_expired_life():
    state, previous_id, target_id = _state()
    with pytest.raises(InvalidEvent):
        plan_player_character_continuation(
            state,
            world_id=uuid4(), branch_id=uuid4(), command_id=uuid4(), ruleset_version=RULESET,
            actor_id=target_id, previous_player_id=previous_id, new_player_id=target_id,
            source_death_event_id=uuid4(),
        )
    with pytest.raises(InvalidEvent):
        plan_player_character_continuation(
            state,
            world_id=uuid4(), branch_id=uuid4(), command_id=uuid4(), ruleset_version=RULESET,
            actor_id=previous_id, previous_player_id=target_id, new_player_id=previous_id,
            source_death_event_id=uuid4(),
        )

    expired_target = state.entities[target_id].model_copy(update={
        "components": {"life": {"birth_tick": 0, "lifespan_ticks": 50}, "inventory": {"lots": []}},
    })
    expired_state = state.model_copy(update={"entities": {**state.entities, target_id: expired_target}})
    assert is_player_continuation_candidate(expired_state, target_id) is False
    with pytest.raises(InvalidEvent):
        plan_player_character_continuation(
            expired_state,
            world_id=uuid4(), branch_id=uuid4(), command_id=uuid4(), ruleset_version=RULESET,
            actor_id=previous_id, previous_player_id=previous_id, new_player_id=target_id,
            source_death_event_id=uuid4(),
        )


def test_batch_rejects_forged_command_metadata_stale_tick_and_extra_event():
    state, previous_id, target_id = _state()
    _world_id, _branch_id, _command_id, _source_id, event = _plan(state, previous_id, target_id)
    good_payload = {
        "previous_player_id": str(previous_id),
        "new_player_id": str(target_id),
        "source_death_event_id": str(event.source_death_event_id),
    }

    forged_payload = {**good_payload, "source_death_event_id": str(uuid4())}
    with pytest.raises(InvalidEvent):
        _batch(state, event, payload=forged_payload)
    with pytest.raises(InvalidEvent):
        _batch(state, event, command_type="action.continue_after_death", payload=good_payload)

    stale_tick = event.model_copy(update={"current_tick": state.current_tick - 1})
    with pytest.raises(InvalidEvent):
        _batch(state, stale_tick)

    with pytest.raises(InvalidEvent):
        validate_player_character_continued_batch(
            state,
            previous_id,
            [event, event],
            world_id=event.world_id,
            branch_id=event.branch_id,
            command_id=event.command_id,
            ruleset_version=event.ruleset_version,
            command_type=PLAYER_CONTINUE_AFTER_DEATH_COMMAND,
            command_payload=good_payload,
        )


def test_batch_rejects_missing_event_for_registered_continuation_command():
    state, previous_id, target_id = _state()
    with pytest.raises(InvalidEvent):
        validate_player_character_continued_batch(
            state,
            previous_id,
            [],
            world_id=uuid4(),
            branch_id=uuid4(),
            command_id=uuid4(),
            ruleset_version=RULESET,
            command_type=PLAYER_CONTINUE_AFTER_DEATH_COMMAND,
            command_payload={
                "previous_player_id": str(previous_id),
                "new_player_id": str(target_id),
                "source_death_event_id": str(uuid4()),
            },
        )
