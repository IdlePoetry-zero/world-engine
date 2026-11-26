from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from world_engine.agents.knowledge import build_agent_knowledge
from world_engine.application.visibility import project_player_state
from world_engine.content import ContentCatalog
from world_engine.domain.gameplay_events import (
    KnowledgeAcquiredEvent,
    RelationDimensionChangedEvent,
    RelationEstablishedEvent,
)
from world_engine.domain.models import TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, state_hash
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, RuleContext, SpeakIntent
from world_engine.rules.engine import RuleEngine
from world_engine.rules.speech import SpeechRelationEvidenceError, speech_relation_evidence


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


def _social_world_pair() -> tuple[WorldState, WorldEntity, WorldEntity, WorldEntity]:
    first_room = WorldEntity(id=uuid4(), kind="place", name="First room")
    second_room = WorldEntity(id=uuid4(), kind="place", name="Second room")
    first = WorldEntity(id=uuid4(), kind="character", name="First", location_id=first_room.id)
    second = WorldEntity(id=uuid4(), kind="character", name="Second", location_id=first_room.id)
    state = WorldState(
        entities={item.id: item for item in (first_room, second_room, first, second)},
        metadata={"social_world_v1": True},
    )
    return state, first_room, second_room, first


def _reply_events(
    state: WorldState,
    memory: KnowledgeAcquiredEvent,
    *,
    utterance: str,
) -> tuple[object, ...]:
    assert memory.record.owner_id is not None and memory.record.holder_id is not None
    result = RuleEngine().evaluate(
        state,
        SpeakIntent(
            target_ids=[memory.record.owner_id],
            utterance=utterance,
            reply_to_memory_id=memory.record.id,
        ),
        _context(memory.record.holder_id),
    )
    assert result.outcome.status == "succeeded"
    return tuple(result.events)


def _move_pair_to_room(
    state: WorldState,
    first_id: UUID,
    second_id: UUID,
    room_id: UUID,
) -> WorldState:
    entities = dict(state.entities)
    entities[first_id] = entities[first_id].model_copy(update={"location_id": room_id})
    entities[second_id] = entities[second_id].model_copy(update={"location_id": room_id})
    return WorldState.model_validate(state.model_copy(update={"entities": entities}).model_dump(mode="python"))


def test_social_world_reply_receipt_creates_separate_directed_familiarity_changes():
    state, _first_room, _second_room, first = _social_world_pair()
    second = next(entity for entity in state.entities.values() if entity.name == "Second")
    greeting = RuleEngine().evaluate(
        state,
        SpeakIntent(target_ids=[second.id], utterance="Could we talk about the delivery?"),
        _context(first.id),
    )
    assert len(greeting.events) == 1
    assert isinstance(greeting.events[0], KnowledgeAcquiredEvent)
    assert not any(isinstance(event, (RelationEstablishedEvent, RelationDimensionChangedEvent))
                   for event in greeting.events)
    state = _apply_all(state, greeting.events)

    reply = _reply_events(
        state,
        greeting.events[0],
        utterance="Yes. I have time to discuss it now.",
    )
    memory = next(event for event in reply if isinstance(event, KnowledgeAcquiredEvent))
    created = [event for event in reply if isinstance(event, RelationEstablishedEvent)]
    changes = [event for event in reply if isinstance(event, RelationDimensionChangedEvent)]
    assert len(created) == 2
    assert len(changes) == 2
    assert {event.relation.from_id for event in created} == {first.id, second.id}
    assert all(event.relation.dimensions == {"familiarity": 0} for event in created)
    assert all(event.social_policy_id == "social_world_v1" for event in changes)
    assert all(event.speech_memory_id == memory.record.id for event in changes)
    assert all(event.routine_occurrence == 1 for event in changes)
    assert all(event.evidence_refs == [memory.record.id] for event in changes)
    assert all(event.value == 1 for event in changes)
    for event in changes:
        relation = next(row.relation for row in created if row.relation.id == event.relation_id)
        assert event.event_id == uuid5(
            memory.record.id,
            f"social_world_v1:familiarity:{relation.from_id}:{relation.to_id}",
        )
        assert event.visibility == {
            "policy": "subject_ids",
            "subject_ids": sorted((str(first.id), str(second.id))),
        }

    settled = _apply_all(state, reply)
    assert len(settled.relations) == 2
    assert {(row.from_id, row.to_id, row.dimensions["familiarity"]) for row in settled.relations} == {
        (first.id, second.id, 1),
        (second.id, first.id, 1),
    }


def test_social_world_repeated_reply_pattern_survives_new_phrasing_and_room():
    state, _first_room, second_room, first = _social_world_pair()
    second = next(entity for entity in state.entities.values() if entity.name == "Second")
    greeting = RuleEngine().evaluate(
        state,
        SpeakIntent(target_ids=[second.id], utterance="I wanted to ask about the schedule."),
        _context(first.id),
    )
    state = _apply_all(state, greeting.events)
    last_memory = greeting.events[0]
    assert isinstance(last_memory, KnowledgeAcquiredEvent)

    observed = []
    for ordinal in range(1, 26):
        if ordinal == 8:
            state = _move_pair_to_room(state, first.id, second.id, second_room.id)
        reply = _reply_events(
            state,
            last_memory,
            utterance=f"Fresh wording for receipt {ordinal}.",
        )
        receipt_memory = next(event for event in reply if isinstance(event, KnowledgeAcquiredEvent))
        changes = [event for event in reply if isinstance(event, RelationDimensionChangedEvent)]
        assert len(changes) == 2
        assert {event.routine_occurrence for event in changes} == {ordinal}
        observed.append(changes[0].value - (changes[0].expected_previous or 0))
        state = _apply_all(state, reply)
        last_memory = receipt_memory

    assert observed[:4] == [Decimal("1.000000"), Decimal("0.500000"), Decimal("0.250000"), Decimal("0.125000")]
    assert observed[-1] == 0
    assert len(state.relations) == 2
    assert all(row.dimensions["familiarity"] == Decimal("1.999999") for row in state.relations)
    assert all(len(row.evidence_refs) == 25 for row in state.relations)


def test_social_world_speech_evidence_rejects_a_receipt_already_used_for_that_direction():
    state, _first_room, _second_room, first = _social_world_pair()
    second = next(entity for entity in state.entities.values() if entity.name == "Second")
    greeting = RuleEngine().evaluate(
        state,
        SpeakIntent(target_ids=[second.id], utterance="A first note."),
        _context(first.id),
    )
    state = _apply_all(state, greeting.events)
    reply = _reply_events(state, greeting.events[0], utterance="An answer.")
    state = _apply_all(state, reply)
    memory = next(event for event in reply if isinstance(event, KnowledgeAcquiredEvent))

    relation = next(row for row in state.relations if row.from_id == first.id)
    with pytest.raises(SpeechRelationEvidenceError, match="already settled"):
        speech_relation_evidence(
            state,
            memory_id=memory.record.id,
            observer_id=relation.from_id,
            counterpart_id=relation.to_id,
        )
