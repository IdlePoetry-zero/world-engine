from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from world_engine.agents.knowledge import build_agent_knowledge
from world_engine.application.visibility import project_player_state
from world_engine.content import ContentCatalog
from world_engine.domain.gameplay_events import KnowledgeAcquiredEvent
from world_engine.domain.models import TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, RuleContext, SpeakIntent
from world_engine.rules.engine import RuleEngine


@pytest.fixture(scope="module")
def full_state() -> WorldState:
    return ContentCatalog().build_world("modern_city", "Private speech rule tests", "Test Player", seed=117).state


def _context(actor_id: UUID, *, command_id: UUID | None = None) -> RuleContext:
    return RuleContext(
        world_id=UUID("10000000-0000-0000-0000-000000000001"),
        branch_id=UUID("20000000-0000-0000-0000-000000000002"),
        command_id=command_id or uuid4(),
        actor_id=actor_id,
        ruleset_version="1.0.0",
    )


def _people_in_scene(state: WorldState, *, count: int = 3) -> list[WorldEntity]:
    people = [
        entity for entity in state.entities.values()
        if entity.kind in {"character", "person"} and entity.status == "active"
        and entity.location_id is not None
    ]
    for actor in people:
        local = [item for item in people if item.id != actor.id and item.location_id == actor.location_id]
        if len(local) >= count - 1:
            return [actor, *local[:count - 1]]
    raise AssertionError("the catalog fixture must have three active people in one scene")


def _apply_all(state: WorldState, events) -> WorldState:
    projected = state
    for event in events:
        projected = apply_event(projected, event)
    return projected


def test_speak_contract_rejects_extra_fields_bad_size_and_duplicate_recipients():
    target_id = uuid4()
    with pytest.raises(ValidationError):
        SpeakIntent(target_ids=[target_id], utterance="hello", invented="field")
    with pytest.raises(ValidationError):
        SpeakIntent(target_ids=[], utterance="hello")
    with pytest.raises(ValidationError):
        SpeakIntent(target_ids=[target_id, target_id], utterance="hello")
    with pytest.raises(ValidationError):
        SpeakIntent(target_ids=[target_id], utterance="x" * 1201)

    parsed = ACTION_INTENT_ADAPTER.validate_python({
        "kind": "speak", "target_ids": [str(target_id)], "utterance": "hello",
    })
    assert isinstance(parsed, SpeakIntent)


def test_speech_records_only_pair_private_memories_without_time_or_relationship_effects(full_state: WorldState):
    speaker, first, second = _people_in_scene(full_state)
    utterance = "I may be wrong, but the old bridge feels quieter tonight."
    before_hash = state_hash(full_state)
    tick = full_state.current_tick

    result = RuleEngine().evaluate(
        full_state,
        SpeakIntent(target_ids=[first.id, second.id], utterance=utterance),
        _context(speaker.id),
    )

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "private_speech_recorded"
    assert result.elapsed_ticks == 0
    assert len(result.events) == 2
    assert not any(isinstance(event, TimeAdvancedEvent) for event in result.events)
    assert all(isinstance(event, KnowledgeAcquiredEvent) for event in result.events)
    assert len({event.event_id for event in result.events}) == 2
    assert state_hash(full_state) == before_hash

    for event in result.events:
        pair = sorted((str(speaker.id), str(event.record.holder_id)))
        assert event.visibility == {"policy": "subject_ids", "subject_ids": pair}
        assert event.record.visibility.policy == "subject_ids"
        assert sorted(str(item) for item in event.record.visibility.subject_ids) == pair
        assert event.record.owner_id == speaker.id
        assert event.record.subject_id == speaker.id
        assert event.record.holder_id in {first.id, second.id}
        assert event.record.kind == "memory"
        assert event.record.fact is None and event.record.assessment is None
        assert utterance in event.record.claim

    projected = _apply_all(full_state, result.events)
    for participant in (speaker, first, second):
        private_records = [record for record in build_agent_knowledge(projected, participant.id).known_records
                           if record.kind == "memory" and utterance in record.claim]
        assert private_records
    third_party = next(
        person for person in full_state.entities.values()
        if person.kind in {"character", "person"}
        and person.status == "active"
        and person.id not in {speaker.id, first.id, second.id}
    )
    assert not [record for record in build_agent_knowledge(projected, third_party.id).known_records
                if utterance in record.claim]

    target_view = project_player_state(projected.model_dump(mode="json"), str(first.id))
    third_view = project_player_state(projected.model_dump(mode="json"), str(third_party.id))
    assert any(utterance in row["claim"] for row in target_view["knowledge"])
    assert not any(utterance in row["claim"] for row in third_view["knowledge"])


def test_one_unavailable_target_rejects_the_whole_multi_recipient_action(full_state: WorldState):
    speaker, recipient, _other = _people_in_scene(full_state)
    before_hash = state_hash(full_state)
    missing_id = uuid4()

    result = RuleEngine().evaluate(
        full_state,
        {"kind": "speak", "target_ids": [str(recipient.id), str(missing_id)], "utterance": "private"},
        _context(speaker.id),
    )

    assert result.outcome.status == "rejected"
    assert result.outcome.code == "speech_recipient_unavailable"
    assert result.events == []
    assert result.elapsed_ticks == 0
    assert state_hash(full_state) == before_hash


def test_speech_never_creates_a_corroborated_fact_or_relation(full_state: WorldState):
    speaker, recipient, _other = _people_in_scene(full_state)
    state = _apply_all(full_state, RuleEngine().evaluate(
        full_state,
        SpeakIntent(target_ids=[recipient.id], utterance="The mayor resigned this morning."),
        _context(speaker.id),
    ).events)

    memories = [record for record in state.knowledge if "The mayor resigned" in record.claim]
    assert len(memories) == 1
    assert memories[0].kind == "memory"
    assert memories[0].fact is None
    assert memories[0].assessment is None
    assert not any("The mayor resigned" in record.claim and record.kind == "fact" for record in state.knowledge)
