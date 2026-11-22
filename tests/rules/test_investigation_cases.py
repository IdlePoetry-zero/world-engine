from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from world_engine.domain.components import HistoricalEvidenceArtifact, KnowledgeRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import validate_investigation_case_batch
from world_engine.domain.gameplay_events import (
    InvestigationCaseClosedEvent,
)
from world_engine.domain.investigation import EvidenceAssessment, WEIGH_EVIDENCE_POLICY, assessment_text
from world_engine.domain.investigation_cases import (
    CaseEvidenceRequirement,
    InvestigationCaseDefinition,
)
from world_engine.domain.models import TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, state_hash, state_json
from world_engine.rules import RuleContext, RuleEngine


def _fixture():
    place_id, actor_id, supported_id, excluded_id, subject_id, anchor_id = (uuid4() for _ in range(6))
    artifact_specs = [
        ("pack.case.archive.primary", "pack.case.witness.primary"),
        ("pack.case.archive.alternate", "pack.case.witness.alternate"),
    ]
    artifacts: list[HistoricalEvidenceArtifact] = []
    records: list[KnowledgeRecord] = []
    requirements: list[CaseEvidenceRequirement] = []
    for index, (observation_ref, claim_ref) in enumerate(artifact_specs):
        artifact = HistoricalEvidenceArtifact(
            evidence_id=uuid4(),
            subject_id=subject_id,
            claim=f"The bell was present in the hall on the winter feast {index}.",
            fact={"predicate": "entity_status", "value": "active", "effective_tick": -3_600},
            provenance=[{"kind": "pack", "reference_id": observation_ref}],
        )
        artifacts.append(artifact)
        observation = KnowledgeRecord(
            id=uuid4(), owner_id=actor_id, subject_id=subject_id,
            claim=artifact.claim, kind="observation", confidence=1,
            source_refs=[artifact.evidence_id], source_event_id=uuid4(),
            provenance=artifact.provenance, received_tick=0, fact=artifact.fact,
            visibility={"policy": "private"},
        )
        claim = KnowledgeRecord(
            id=uuid4(), owner_id=actor_id, subject_id=subject_id,
            claim=f"A witness remembers the bell at the hall {index}.", kind="claim", confidence="0.7",
            source_event_id=uuid4(), provenance=[{"kind": "pack", "reference_id": claim_ref}],
            received_tick=0,
            fact={"predicate": "entity_status", "value": "active", "effective_tick": -3_600},
            visibility={"policy": "private"},
        )
        assessed_claim, summary = assessment_text(observation.fact, "corroborated")
        assessment = KnowledgeRecord(
            id=uuid4(), owner_id=actor_id, subject_id=subject_id,
            claim=assessed_claim, summary=summary, confidence=1,
            source_refs=[observation.id, claim.id], received_tick=0, kind="belief",
            assessment=EvidenceAssessment(
                finding="corroborated", observation_id=observation.id, claim_id=claim.id,
                predicate="entity_status", effective_tick=-3_600,
            ),
            provenance=[{"kind": "rule", "reference_id": WEIGH_EVIDENCE_POLICY}],
            visibility={"policy": "private"},
        )
        records.extend((observation, claim, assessment))
        requirements.append(CaseEvidenceRequirement(
            subject_id=subject_id,
            predicate="entity_status",
            effective_tick=-3_600,
            finding="corroborated",
            observation_provenance=[{"kind": "pack", "reference_id": observation_ref}],
            claim_provenance=[{"kind": "pack", "reference_id": claim_ref}],
        ))

    case_definition = InvestigationCaseDefinition(
        case_id=uuid4(),
        title="钟楼旧案",
        brief="查明冬宴前后铃铛的去向。",
        routes=[
            {"route_id": "archive-ledger", "suspect_id": supported_id,
             "finding": "supports", "requirements": [requirements[0]]},
            {"route_id": "independent-recollection", "suspect_id": supported_id,
             "finding": "supports", "requirements": [requirements[1]]},
            {"route_id": "bell-ringer-cleared", "suspect_id": excluded_id,
             "finding": "rules_out", "requirements": [requirements[0]]},
        ],
        wrong_suspect_effect={"dimension": "trust", "delta": -12},
    )
    entities = {
        place_id: WorldEntity(id=place_id, kind="place", name="旧广场", components={"place": {"place_type": "settlement"}}),
        actor_id: WorldEntity(id=actor_id, kind="character", name="玩家", location_id=place_id),
        supported_id: WorldEntity(id=supported_id, kind="character", name="档案员", location_id=place_id),
        excluded_id: WorldEntity(id=excluded_id, kind="character", name="钟楼守卫", location_id=place_id),
        subject_id: WorldEntity(id=subject_id, kind="item", name="旧铃", location_id=place_id),
        anchor_id: WorldEntity(
            id=anchor_id, kind="item", name="调查告示", location_id=place_id,
            components={"public_description": "一张记录案件编号的告示。", "investigation_case": case_definition.model_dump(mode="json")},
        ),
        **{
            uuid4(): WorldEntity(
                id=uuid4(), kind="item", name=f"档案副本 {index}", location_id=place_id,
                components={"historical_evidence": artifact.model_dump(mode="json")},
            )
            for index, artifact in enumerate(artifacts)
        },
    }
    # Keep dictionary keys aligned with entity IDs even though UUIDs are generated here.
    entities = {entity.id: entity for entity in entities.values()}
    state = WorldState(current_tick=0, entities=entities, knowledge=records, player_id=actor_id)
    base_context = RuleContext(
        world_id=UUID(int=10), branch_id=UUID(int=20), command_id=uuid4(),
        actor_id=actor_id, ruleset_version="1.0.0",
    )
    assessment_ids = [record.id for record in records if record.assessment is not None]
    artifact_items = [entity.id for entity in state.entities.values() if "historical_evidence" in entity.components]
    return state, base_context, actor_id, anchor_id, supported_id, excluded_id, assessment_ids, artifact_items


def _context(base: RuleContext) -> RuleContext:
    return base.model_copy(update={"command_id": uuid4()})


def _commit_case_action(state: WorldState, actor_id: UUID, intent: dict[str, object], base: RuleContext):
    context = _context(base)
    resolution = RuleEngine().evaluate(state, intent, context)
    assert resolution.outcome.status == "succeeded", resolution.outcome
    validate_investigation_case_batch(
        state, actor_id, resolution.events,
        world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
        ruleset_version=context.ruleset_version,
        command_type=f"action.{intent['kind']}", command_payload=intent,
    )
    updated = state
    for event in resolution.events:
        updated = apply_event(updated, event)
    return updated, resolution


def test_private_case_file_alternative_routes_and_public_wrong_suspect_effect_replay():
    state, base, actor_id, anchor_id, _supported_id, excluded_id, assessment_ids, source_item_ids = _fixture()
    empty_hash = state_hash(state)
    assert "investigation_cases" not in state_json(state)

    state, opened = _commit_case_action(
        state, actor_id, {"kind": "open_investigation_case", "anchor_id": str(anchor_id)}, base,
    )
    case_file = state.investigation_cases[0]
    assert opened.outcome.code == "investigation_case_opened"
    assert case_file.revision == 1 and case_file.status == "open" and not case_file.evidence_refs
    assert case_file.definition_hash and len(case_file.definition_hash) == 64
    assert state_hash(state) != empty_hash
    state = apply_event(state, TimeAdvancedEvent(to_tick=3_600))

    state, _ = _commit_case_action(
        state, actor_id,
        {"kind": "attach_case_evidence", "case_file_id": str(case_file.id), "assessment_id": str(assessment_ids[0])},
        base,
    )
    case_file = state.investigation_cases[0]
    assert case_file.revision == 2 and case_file.evidence_refs == [assessment_ids[0]]

    # The original physical source is gone, but the actor's retained assessment remains usable.
    entities = dict(state.entities)
    source_item = entities[source_item_ids[0]]
    entities[source_item_ids[0]] = source_item.model_copy(update={"status": "destroyed"})
    state = WorldState.model_validate({**state.model_dump(mode="json"), "entities": {
        str(entity_id): entity.model_dump(mode="json") for entity_id, entity in entities.items()
    }})
    state = apply_event(state, TimeAdvancedEvent(to_tick=7_200))
    state, closed = _commit_case_action(
        state, actor_id,
        {"kind": "close_investigation_case", "case_file_id": str(case_file.id),
         "suspect_id": str(excluded_id), "public_accusation": True},
        base,
    )
    case_file = state.investigation_cases[0]
    assert closed.outcome.code == "case_disconfirmed"
    assert case_file.status == "closed" and case_file.outcome == "disconfirmed"
    assert case_file.public_accusation and case_file.suspect_id == excluded_id
    assert not hasattr(case_file, "matched_route_ids")
    assert all("route_id" not in event.model_dump(mode="json") for event in closed.events)
    relations = [relation for relation in state.relations if relation.from_id == excluded_id and relation.to_id == actor_id]
    assert len(relations) == 1 and relations[0].dimensions["trust"] == -12
    assert relations[0].evidence_refs == [next(e.event_id for e in closed.events if isinstance(e, InvestigationCaseClosedEvent))]
    assert case_file.opened_tick == 0 and case_file.updated_tick == 7_200
    assert state.current_tick == 7_200


def test_case_closure_rejects_forged_outcome_and_public_consequence():
    state, base, actor_id, anchor_id, _supported_id, excluded_id, assessment_ids, _source_items = _fixture()
    state, _ = _commit_case_action(
        state, actor_id, {"kind": "open_investigation_case", "anchor_id": str(anchor_id)}, base,
    )
    case_file_id = state.investigation_cases[0].id
    state, _ = _commit_case_action(
        state, actor_id,
        {"kind": "attach_case_evidence", "case_file_id": str(case_file_id), "assessment_id": str(assessment_ids[0])},
        base,
    )
    context = _context(base)
    result = RuleEngine().evaluate(state, {
        "kind": "close_investigation_case", "case_file_id": str(case_file_id),
        "suspect_id": str(excluded_id), "public_accusation": True,
    }, context)
    close = next(event for event in result.events if isinstance(event, InvestigationCaseClosedEvent))
    forged = close.model_copy(update={"outcome": "supported"})
    tampered = [forged, *[event for event in result.events if not isinstance(event, InvestigationCaseClosedEvent)]]
    with pytest.raises(InvalidEvent, match="outcome does not match"):
        validate_investigation_case_batch(
            state, actor_id, tampered,
            world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
            ruleset_version=context.ruleset_version,
            command_type="action.close_investigation_case",
            command_payload={"kind": "close_investigation_case", "case_file_id": str(case_file_id),
                             "suspect_id": str(excluded_id), "public_accusation": True},
        )


def test_private_wrong_suspect_filing_has_no_relation_effect_and_unresolved_file_can_close():
    state, base, actor_id, anchor_id, _supported_id, excluded_id, _assessment_ids, _source_items = _fixture()
    state, _ = _commit_case_action(
        state, actor_id, {"kind": "open_investigation_case", "anchor_id": str(anchor_id)}, base,
    )
    case_file_id = state.investigation_cases[0].id
    state, closed = _commit_case_action(
        state, actor_id,
        {"kind": "close_investigation_case", "case_file_id": str(case_file_id),
         "suspect_id": str(excluded_id), "public_accusation": False},
        base,
    )
    assert closed.outcome.code == "case_unresolved"
    assert state.investigation_cases[0].outcome == "unresolved"
    assert not [relation for relation in state.relations if relation.from_id == excluded_id and relation.to_id == actor_id]


def test_event_registry_and_legacy_empty_state_serialization():
    state, _base, _actor_id, _anchor_id, *_ = _fixture()
    empty = WorldState(entities=state.entities, player_id=state.player_id)
    serialized = state_json(empty)
    assert "investigation_cases" not in serialized
    round_tripped = WorldState.model_validate(serialized)
    assert round_tripped.investigation_cases == []
