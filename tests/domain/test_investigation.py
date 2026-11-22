from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import KnowledgeRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import KnowledgeAcquiredEvent
from world_engine.domain.investigation import (
    EvidenceAssessment,
    assessment_text,
    compare_evidence_sources,
    facts_agree,
    source_roots,
    validate_assessment_record,
)
from world_engine.domain.models import TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event


def _record(*, fact: dict[str, object], source_event_id=None, source_refs=None):
    return KnowledgeRecord(
        id=uuid4(),
        owner_id=uuid4(),
        subject_id=uuid4(),
        claim="A source assertion.",
        kind="observation",
        fact=fact,
        source_event_id=source_event_id,
        source_refs=source_refs or [],
    )


def test_fact_predicates_are_closed_and_time_bounded():
    place_id = uuid4()
    location_fact = KnowledgeRecord.model_validate({
        "id": str(uuid4()),
        "owner_id": str(uuid4()),
        "subject_id": str(uuid4()),
        "claim": "The witness was at the bridge.",
        "kind": "observation",
        "source_event_id": str(uuid4()),
        "fact": {
            "predicate": "entity_location",
            "value": str(place_id),
            "effective_tick": 3_600,
        },
    })
    assert location_fact.fact is not None
    assert location_fact.fact.model_dump(mode="json") == {
        "predicate": "entity_location", "value": str(place_id), "effective_tick": 3_600,
    }

    with pytest.raises(ValidationError):
        KnowledgeRecord.model_validate({
            **location_fact.model_dump(mode="json"),
            "fact": {"predicate": "free_text", "value": "anything", "effective_tick": 3_600},
        })
    with pytest.raises(ValidationError):
        KnowledgeRecord.model_validate({
            **location_fact.model_dump(mode="json"),
            "fact": {"predicate": "entity_location", "value": str(place_id), "effective_tick": 3_600,
                     "notes": "untyped extra"},
        })


def test_fact_agreement_requires_exact_predicate_value_and_effective_tick():
    place_a, place_b = uuid4(), uuid4()
    first = _record(fact={"predicate": "entity_location", "value": place_a, "effective_tick": 600})
    same = _record(fact={"predicate": "entity_location", "value": place_a, "effective_tick": 600})
    different_place = _record(fact={"predicate": "entity_location", "value": place_b, "effective_tick": 600})
    different_time = _record(fact={"predicate": "entity_location", "value": place_a, "effective_tick": 601})
    different_predicate = _record(fact={"predicate": "entity_status", "value": "active", "effective_tick": 600})

    assert facts_agree(first.fact, same.fact)
    assert not facts_agree(first.fact, different_place.fact)
    assert not facts_agree(first.fact, different_time.fact)
    assert not facts_agree(first.fact, different_predicate.fact)


def test_source_roots_follow_derived_records_and_reject_cycles():
    root_event, second_event = uuid4(), uuid4()
    source = _record(
        fact={"predicate": "entity_status", "value": "active", "effective_tick": 0},
        source_event_id=root_event,
    )
    derived = _record(
        fact={"predicate": "entity_status", "value": "active", "effective_tick": 0},
        source_event_id=second_event,
        source_refs=[source.id],
    )
    roots = {source.id: source, derived.id: derived}
    assert source_roots(source.id, roots) == frozenset({("event", str(root_event))})
    assert source_roots(derived.id, roots) == frozenset({
        ("event", str(root_event)), ("event", str(second_event)),
    })
    assert source_roots(source.id, roots) & source_roots(derived.id, roots)

    cycle = source.model_copy(update={"source_refs": [derived.id]})
    with pytest.raises(ValueError, match="cycle"):
        source_roots(source.id, {source.id: cycle, derived.id: derived})


def test_typed_assessment_rechecks_owner_time_sources_and_finding():
    actor_id, other_id, subject_id = uuid4(), uuid4(), uuid4()
    observation = KnowledgeRecord(
        id=uuid4(), owner_id=actor_id, subject_id=subject_id,
        claim="I saw the witness at the square.", kind="observation", received_tick=0,
        source_event_id=uuid4(),
        fact={"predicate": "entity_status", "value": "active", "effective_tick": 0},
    )
    claim = KnowledgeRecord(
        id=uuid4(), owner_id=actor_id, subject_id=subject_id,
        claim="The witness had left.", kind="rumor", received_tick=0,
        source_event_id=uuid4(),
        fact={"predicate": "entity_status", "value": "retired", "effective_tick": 0},
    )
    finding = compare_evidence_sources(
        observation.id, claim.id, [observation, claim], actor_id=actor_id, at_tick=300,
    )
    expected_claim, expected_summary = assessment_text(observation.fact, finding)
    assessment = KnowledgeRecord(
        id=uuid4(), owner_id=actor_id, subject_id=subject_id,
        claim=expected_claim,
        summary=expected_summary, kind="belief", confidence=1,
        source_refs=[observation.id, claim.id], received_tick=300,
        assessment=EvidenceAssessment(
            finding=finding, observation_id=observation.id, claim_id=claim.id,
            predicate="entity_status", effective_tick=0,
        ),
        provenance=[{"kind": "rule", "reference_id": "investigation.weigh_evidence.v1"}],
    )
    assert finding == "conflict"
    assert validate_assessment_record(
        assessment, [observation, claim], actor_id=actor_id, at_tick=300,
    ) == "conflict"

    forged = assessment.model_copy(update={
        "assessment": assessment.assessment.model_copy(update={"finding": "corroborated"}),
    })
    with pytest.raises(ValueError, match="finding"):
        validate_assessment_record(forged, [observation, claim], actor_id=actor_id, at_tick=300)

    private_claim = claim.model_copy(update={"owner_id": other_id})
    with pytest.raises(ValueError, match="not available"):
        compare_evidence_sources(
            observation.id, private_claim.id, [observation, private_claim],
            actor_id=actor_id, at_tick=300,
        )

    derived_claim = claim.model_copy(update={"source_refs": [assessment.id], "source_event_id": None})
    with pytest.raises(ValueError, match="provenance"):
        compare_evidence_sources(
            observation.id, derived_claim.id, [observation, assessment, derived_claim],
            actor_id=actor_id, at_tick=300,
        )


def test_reducer_accepts_valid_assessment_and_rejects_forged_result_on_replay():
    actor_id, subject_id = uuid4(), uuid4()
    observation = KnowledgeRecord(
        id=uuid4(), owner_id=actor_id, subject_id=subject_id,
        claim="I saw the witness at the square.", kind="observation", received_tick=0,
        source_event_id=uuid4(),
        fact={"predicate": "entity_status", "value": "active", "effective_tick": 0},
    )
    claim = KnowledgeRecord(
        id=uuid4(), owner_id=actor_id, subject_id=subject_id,
        claim="A rumor says the witness had left.", kind="rumor", received_tick=0,
        source_event_id=uuid4(),
        fact={"predicate": "entity_status", "value": "retired", "effective_tick": 0},
    )
    expected_claim, expected_summary = assessment_text(observation.fact, "conflict")
    record = KnowledgeRecord(
        id=uuid4(), owner_id=actor_id, subject_id=subject_id,
        claim=expected_claim, summary=expected_summary, kind="belief", confidence=1,
        source_refs=[observation.id, claim.id], received_tick=300,
        assessment=EvidenceAssessment(
            finding="conflict", observation_id=observation.id, claim_id=claim.id,
            predicate="entity_status", effective_tick=0,
        ),
        provenance=[{"kind": "rule", "reference_id": "investigation.weigh_evidence.v1"}],
    )
    state = WorldState(
        player_id=actor_id,
        entities={
            actor_id: WorldEntity(id=actor_id, kind="character", name="Observer"),
            subject_id: WorldEntity(id=subject_id, kind="character", name="Witness"),
        },
        knowledge=[observation, claim],
    )
    advanced = apply_event(state, TimeAdvancedEvent(to_tick=300))
    updated = apply_event(advanced, KnowledgeAcquiredEvent(record=record))
    assert updated.knowledge[-1].assessment == record.assessment

    forged = record.model_copy(update={
        "assessment": record.assessment.model_copy(update={"finding": "corroborated"}),
    })
    with pytest.raises(InvalidEvent, match="finding"):
        apply_event(advanced, KnowledgeAcquiredEvent(record=forged))
