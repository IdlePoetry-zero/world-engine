from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from world_engine.domain.components import KnowledgeRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import KnowledgeAcquiredEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, state_hash, state_json
from world_engine.rules import RuleContext, RuleEngine


PRIVATE_CLAIM = "The archivist says the seal was intact at the old winter feast."


def _case(*, hidden_speaker: bool = False, duplicate_source: bool = False):
    place_id, actor_id, speaker_id, subject_id = uuid4(), uuid4(), uuid4(), uuid4()
    source = KnowledgeRecord(
        id=uuid4(),
        holder_id=speaker_id,
        subject_id=subject_id,
        claim=PRIVATE_CLAIM,
        summary="Private witness account",
        confidence="0.7",
        source_event_id=uuid4(),
        provenance=[{"kind": "pack", "reference_id": "pack.test.1.0"}],
        received_tick=0,
        kind="rumor",
        fact={"predicate": "entity_status", "value": "active", "effective_tick": 0},
        disclosure_policy="if_asked",
        visibility={"policy": "private"},
    )
    entities = {
        place_id: WorldEntity(id=place_id, kind="place", name="Opening Square"),
        actor_id: WorldEntity(id=actor_id, kind="character", name="Player", location_id=place_id),
        speaker_id: WorldEntity(
            id=speaker_id,
            kind="character",
            name="Witness",
            location_id=place_id,
            components={"hidden": hidden_speaker, "public_role": "archivist"},
        ),
        subject_id: WorldEntity(id=subject_id, kind="item", name="Bronze Seal", location_id=place_id),
    }
    knowledge = [source]
    if duplicate_source:
        conflicting = source.model_dump(mode="json")
        conflicting.update({
            "id": str(uuid4()),
            "claim": "A conflicting account of the seal.",
            "fact": {"predicate": "entity_status", "value": "retired", "effective_tick": 0},
        })
        knowledge.append(KnowledgeRecord.model_validate(conflicting))
    state = WorldState(current_tick=600, player_id=actor_id, entities=entities, knowledge=knowledge)
    context = RuleContext(
        world_id=UUID(int=10),
        branch_id=UUID(int=20),
        command_id=uuid4(),
        actor_id=actor_id,
        ruleset_version="1.0.0",
    )
    intent = {
        "kind": "ask_npc_claim",
        "target_id": str(speaker_id),
        "subject_id": str(subject_id),
        "predicate": "entity_status",
        "effective_tick": 0,
    }
    return state, context, intent, source, actor_id, speaker_id, subject_id, place_id


def test_ask_npc_claim_copies_original_typed_claim_without_rewriting_historical_tick():
    state, context, intent, source, actor_id, speaker_id, _subject_id, place_id = _case()

    result = RuleEngine().evaluate(state, intent, context)

    assert result.outcome.status == "succeeded"
    assert result.outcome.code == "npc_claim_disclosed"
    assert result.elapsed_ticks == 0
    assert PRIVATE_CLAIM not in result.outcome.message
    event = next(event for event in result.events if isinstance(event, KnowledgeAcquiredEvent))
    disclosed = event.record
    assert disclosed.owner_id == actor_id and disclosed.holder_id is None
    assert disclosed.source_refs == [source.id]
    assert disclosed.claim == source.claim
    assert disclosed.summary == source.summary
    assert disclosed.confidence == source.confidence
    assert disclosed.source_event_id == source.source_event_id
    assert disclosed.provenance == source.provenance
    assert disclosed.fact == source.fact and disclosed.fact.effective_tick == 0
    assert disclosed.received_tick == 600
    assert disclosed.disclosure is not None
    assert disclosed.disclosure.source_record_id == source.id
    assert disclosed.disclosure.speaker_id == speaker_id
    assert disclosed.disclosure.recipient_id == actor_id
    assert disclosed.disclosure.location_id == place_id
    assert disclosed.disclosure.disclosed_tick == 600

    replayed = apply_event(state, event)
    assert replayed.current_tick == state.current_tick
    assert replayed.knowledge[-1] == disclosed
    reopened = WorldState.model_validate(state_json(replayed))
    assert state_hash(reopened) == state_hash(replayed)


@pytest.mark.parametrize("mutation", ["wrong_speaker", "wrong_location", "rewrite_fact", "rewrite_text"])
def test_reducer_rejects_forged_disclosure_receipts_and_source_copies(mutation: str):
    state, context, intent, _source, _actor_id, _speaker_id, _subject_id, _place_id = _case()
    resolution = RuleEngine().evaluate(state, intent, context)
    valid_event = next(event for event in resolution.events if isinstance(event, KnowledgeAcquiredEvent))
    record = valid_event.record
    receipt = record.disclosure
    assert receipt is not None
    if mutation == "wrong_speaker":
        bad_receipt = receipt.model_copy(update={"speaker_id": uuid4()})
        forged = record.model_copy(update={"disclosure": bad_receipt})
    elif mutation == "wrong_location":
        bad_receipt = receipt.model_copy(update={"location_id": uuid4()})
        forged = record.model_copy(update={"disclosure": bad_receipt})
    elif mutation == "rewrite_fact":
        forged = KnowledgeRecord.model_validate({
            **record.model_dump(mode="json"),
            "fact": {"predicate": "entity_status", "value": "retired", "effective_tick": 600},
        })
    else:
        forged = record.model_copy(update={"claim": "A changed claim"})

    with pytest.raises(InvalidEvent, match="NPC claim disclosure"):
        apply_event(state, KnowledgeAcquiredEvent(record=forged))


def test_ask_npc_claim_does_not_disclose_hidden_or_ambiguous_sources():
    state, context, intent, _source, _actor_id, _speaker_id, _subject_id, _place_id = _case(hidden_speaker=True)
    hidden = RuleEngine().evaluate(state, intent, context)
    assert hidden.outcome.status == "rejected"
    assert hidden.outcome.code == "npc_claim_speaker_unavailable"
    assert hidden.events == []

    state, context, intent, _source, _actor_id, _speaker_id, _subject_id, _place_id = _case(duplicate_source=True)
    ambiguous = RuleEngine().evaluate(state, intent, context.model_copy(update={"command_id": uuid4()}))
    assert ambiguous.outcome.status == "rejected"
    assert ambiguous.outcome.code == "npc_claim_unavailable"
    assert ambiguous.events == []
    assert PRIVATE_CLAIM not in ambiguous.outcome.message

