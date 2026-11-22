from __future__ import annotations

from decimal import Decimal
from uuid import uuid4, uuid5

import pytest
from pydantic import ValidationError

from world_engine.domain.accusation_repair import (
    plan_accusation_repair_offer,
    plan_accusation_repair_resolution,
)
from world_engine.domain.components import RelationRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import (
    parse_event,
    requires_accusation_repair_validation,
    validate_accusation_repair_batch,
)
from world_engine.domain.gameplay_events import AccusationRepairTrustProof
from world_engine.domain.investigation_cases import (
    CaseEvidenceRequirement,
    CaseSourceReference,
    InvestigationCaseDefinition,
    InvestigationCaseFile,
    InvestigationCaseRoute,
)
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.public_accusation_corrections import plan_public_accusation_correction
from world_engine.domain.reducers import apply_event, state_json


WORLD_ID = uuid4()
BRANCH_ID = uuid4()
ACCUSER_ID = uuid4()
SUSPECT_ID = uuid4()
OTHER_ID = uuid4()
CASE_FILE_ID = uuid4()
CASE_ID = uuid4()
ANCHOR_ID = uuid4()
LOCATION_ID = uuid4()
SOURCE_CLOSURE_EVENT_ID = uuid4()
RELATION_ID = uuid4()
INVERSE_RELATION_ID = uuid4()
RULESET_VERSION = "test.1"


def _state(*, trust: Decimal | None = None) -> WorldState:
    case_definition = InvestigationCaseDefinition(
        case_id=CASE_ID,
        title="Private case title",
        brief="Private case brief.",
        routes=[
            InvestigationCaseRoute(
                route_id="rules_out",
                suspect_id=SUSPECT_ID,
                finding="rules_out",
                requirements=[CaseEvidenceRequirement(
                    subject_id=ACCUSER_ID,
                    predicate="entity_status",
                    effective_tick=50,
                    finding="conflict",
                    observation_provenance=[CaseSourceReference(kind="event", reference_id="public.record")],
                    claim_provenance=[CaseSourceReference(kind="genesis", reference_id="private.claim")],
                )],
            ),
            InvestigationCaseRoute(
                route_id="supports",
                suspect_id=SUSPECT_ID,
                finding="supports",
                requirements=[CaseEvidenceRequirement(
                    subject_id=ACCUSER_ID,
                    predicate="entity_status",
                    effective_tick=51,
                    finding="corroborated",
                    observation_provenance=[CaseSourceReference(kind="event", reference_id="other.record")],
                    claim_provenance=[CaseSourceReference(kind="genesis", reference_id="other.claim")],
                )],
            ),
        ],
    )
    entities = {
        ACCUSER_ID: WorldEntity(id=ACCUSER_ID, kind="character", name="Ari", location_id=LOCATION_ID),
        SUSPECT_ID: WorldEntity(id=SUSPECT_ID, kind="character", name="Jing", location_id=LOCATION_ID),
        OTHER_ID: WorldEntity(id=OTHER_ID, kind="character", name="Mei", location_id=LOCATION_ID),
        LOCATION_ID: WorldEntity(id=LOCATION_ID, kind="place", name="Spring pavilion"),
        ANCHOR_ID: WorldEntity(
            id=ANCHOR_ID,
            kind="item",
            name="Case anchor",
            location_id=LOCATION_ID,
            components={"investigation_case": case_definition.model_dump(mode="json")},
        ),
    }
    relations = []
    if trust is not None:
        relations = [
            RelationRecord(
                id=RELATION_ID,
                from_id=SUSPECT_ID,
                to_id=ACCUSER_ID,
                dimensions={"trust": trust},
                updated_tick=100,
            ),
            RelationRecord(
                id=INVERSE_RELATION_ID,
                from_id=ACCUSER_ID,
                to_id=SUSPECT_ID,
                dimensions={"trust": Decimal("25")},
                updated_tick=100,
            ),
        ]
    return WorldState(
        current_tick=200,
        entities=entities,
        relations=relations,
        investigation_cases=[InvestigationCaseFile(
            id=CASE_FILE_ID,
            case_id=CASE_ID,
            anchor_id=ANCHOR_ID,
            owner_id=ACCUSER_ID,
            definition_hash="a" * 64,
            title=case_definition.title,
            brief=case_definition.brief,
            status="closed",
            revision=4,
            opened_tick=0,
            updated_tick=100,
            closed_tick=100,
            outcome="disconfirmed",
            suspect_id=SUSPECT_ID,
            public_accusation=True,
        )],
    )


def _correction(state: WorldState, *, command_id=None):
    return plan_public_accusation_correction(
        state,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id or uuid4(),
        ruleset_version=RULESET_VERSION,
        actor_id=ACCUSER_ID,
        case_file_id=CASE_FILE_ID,
        expected_revision=4,
        source_closure_event_id=SOURCE_CLOSURE_EVENT_ID,
    )


def _offer(state: WorldState, *, command_id=None, statement="I should have asked you first."):
    return plan_accusation_repair_offer(
        state,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id or uuid4(),
        ruleset_version=RULESET_VERSION,
        actor_id=ACCUSER_ID,
        correction_event_id=state.public_accusation_corrections[0].event_id,
        statement=statement,
    )


def _validate(state, events, *, actor_id, command_id, command_type, payload, causation_ids):
    validate_accusation_repair_batch(
        state,
        actor_id,
        events,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        ruleset_version=RULESET_VERSION,
        command_type=command_type,
        command_payload=payload,
        causation_ids=causation_ids,
    )


def _state_with_offer(*, trust: Decimal | None = None):
    base = _state(trust=trust)
    correction = _correction(base)
    corrected = apply_event(base, correction)
    command_id = uuid4()
    event = _offer(corrected, command_id=command_id)
    payload = {
        "kind": "offer_accusation_repair",
        "correction_event_id": str(correction.event_id),
        "statement": event.record.statement,
    }
    assert requires_accusation_repair_validation("action.offer_accusation_repair", [event])
    _validate(
        corrected,
        [event],
        actor_id=ACCUSER_ID,
        command_id=command_id,
        command_type="action.offer_accusation_repair",
        payload=payload,
        causation_ids=[correction.event_id],
    )
    return corrected, correction, event, apply_event(corrected, parse_event(event.model_dump(mode="json")))


def test_offer_is_explicit_authored_append_only_and_legacy_state_omits_empty_collection():
    base = _state()
    assert "accusation_repair_offers" not in state_json(base)
    corrected = apply_event(base, _correction(base))
    event = _offer(corrected, statement="  I should have asked you first.  ")
    assert event.type == "core.accusation_repair_offer_recorded.v1"
    assert event.visibility == {"policy": "author_only"}
    assert event.record.statement == "  I should have asked you first.  "
    result = apply_event(corrected, event)
    assert result.accusation_repair_offers == [event.record]
    assert result.public_accusation_corrections == corrected.public_accusation_corrections
    assert result.investigation_cases == corrected.investigation_cases
    assert result.relations == corrected.relations
    assert result.current_tick == corrected.current_tick
    assert parse_event(event.model_dump(mode="json")) == event


def test_offer_rejects_empty_or_overlong_authored_statement():
    corrected, *_ = _state_with_offer()
    with pytest.raises(ValidationError):
        _offer(corrected, statement="   ")
    with pytest.raises(ValidationError):
        _offer(corrected, statement="x" * 501)


def test_offer_requires_source_accuser_and_one_offer_per_correction():
    corrected, correction, event, offered = _state_with_offer()
    with pytest.raises(InvalidEvent, match="unavailable"):
        plan_accusation_repair_offer(
            corrected,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=uuid4(),
            ruleset_version=RULESET_VERSION,
            actor_id=SUSPECT_ID,
            correction_event_id=correction.event_id,
            statement="I am sorry.",
        )
    with pytest.raises(InvalidEvent, match="unavailable"):
        _offer(offered)
    assert len(offered.accusation_repair_offers) == 1
    duplicate = event.record.model_copy(update={"offer_id": uuid4()})
    with pytest.raises(ValidationError, match="only one repair offer"):
        WorldState.model_validate({
            **state_json(offered),
            "accusation_repair_offers": [event.record.model_dump(mode="json"), duplicate.model_dump(mode="json")],
        })


def test_defer_is_durable_revisioned_and_changes_no_relationship_or_case():
    _, _, _, offered = _state_with_offer(trust=Decimal("-15"))
    offer = offered.accusation_repair_offers[0]
    command_id = uuid4()
    response, relation_event = plan_accusation_repair_resolution(
        offered,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        ruleset_version=RULESET_VERSION,
        actor_id=SUSPECT_ID,
        offer_id=offer.offer_id,
        expected_revision=1,
        operation="defer",
    )
    assert relation_event is None
    assert response.resulting_offer.status == "pending"
    assert response.resulting_offer.revision == 2
    assert response.resulting_offer.last_operation == "defer"
    _validate(
        offered,
        [response],
        actor_id=SUSPECT_ID,
        command_id=command_id,
        command_type="action.resolve_accusation_repair",
        payload={"kind": "resolve_accusation_repair", "offer_id": str(offer.offer_id),
                 "expected_revision": 1, "operation": "defer"},
        causation_ids=[offer.offer_id],
    )
    result = apply_event(offered, response)
    assert result.accusation_repair_offers[0].revision == 2
    assert result.accusation_repair_offers[0].status == "pending"
    assert result.relations == offered.relations
    assert result.investigation_cases == offered.investigation_cases
    with pytest.raises(InvalidEvent, match="unavailable"):
        plan_accusation_repair_resolution(
            result,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=command_id,
            ruleset_version=RULESET_VERSION,
            actor_id=SUSPECT_ID,
            offer_id=offer.offer_id,
            expected_revision=1,
            operation="accept",
        )


def test_target_can_decline_without_forced_forgiveness_or_relation_change():
    _, _, _, offered = _state_with_offer(trust=Decimal("-15"))
    offer = offered.accusation_repair_offers[0]
    command_id = uuid4()
    response, relation_event = plan_accusation_repair_resolution(
        offered,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        ruleset_version=RULESET_VERSION,
        actor_id=SUSPECT_ID,
        offer_id=offer.offer_id,
        expected_revision=1,
        operation="decline",
    )
    assert relation_event is None
    assert response.resulting_offer.status == "declined"
    assert response.trust_recovery is None
    result = apply_event(offered, response)
    assert result.relations == offered.relations
    assert result.investigation_cases == offered.investigation_cases
    with pytest.raises(InvalidEvent, match="unavailable"):
        plan_accusation_repair_resolution(
            result,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=uuid4(),
            ruleset_version=RULESET_VERSION,
            actor_id=SUSPECT_ID,
            offer_id=offer.offer_id,
            expected_revision=2,
            operation="accept",
        )


def test_accept_recovery_is_proven_directed_and_bounded_to_one_third_of_actual_loss():
    _, correction, _, offered = _state_with_offer(trust=Decimal("-15"))
    offer = offered.accusation_repair_offers[0]
    proof = AccusationRepairTrustProof(
        case_file_id=CASE_FILE_ID,
        source_closure_event_id=SOURCE_CLOSURE_EVENT_ID,
        source_relation_effect_event_id=uuid4(),
        accuser_id=ACCUSER_ID,
        suspect_id=SUSPECT_ID,
        baseline_trust=Decimal("0"),
        post_effect_trust=Decimal("-15"),
    )
    command_id = uuid4()
    response, relation_event = plan_accusation_repair_resolution(
        offered,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        ruleset_version=RULESET_VERSION,
        actor_id=SUSPECT_ID,
        offer_id=offer.offer_id,
        expected_revision=1,
        operation="accept",
        trust_proof=proof,
    )
    assert response.trust_recovery is not None
    assert response.trust_recovery.applied_delta == 5
    assert relation_event is not None
    assert relation_event.value == Decimal("-10")
    assert relation_event.evidence_refs == [response.event_id]
    assert relation_event.visibility == {
        "policy": "subject_ids", "subject_ids": sorted((str(ACCUSER_ID), str(SUSPECT_ID))),
    }
    _validate(
        offered,
        [response, relation_event],
        actor_id=SUSPECT_ID,
        command_id=command_id,
        command_type="action.resolve_accusation_repair",
        payload={"kind": "resolve_accusation_repair", "offer_id": str(offer.offer_id),
                 "expected_revision": 1, "operation": "accept"},
        causation_ids=[offer.offer_id, proof.source_relation_effect_event_id],
    )
    result = apply_event(offered, parse_event(response.model_dump(mode="json")))
    result = apply_event(result, relation_event)
    assert next(item for item in result.relations if item.id == RELATION_ID).dimensions["trust"] == Decimal("-10")
    assert next(item for item in result.relations if item.id == INVERSE_RELATION_ID).dimensions["trust"] == Decimal("25")
    assert result.public_accusation_corrections == offered.public_accusation_corrections
    assert result.investigation_cases == offered.investigation_cases
    assert result.accusation_repair_offers[0].status == "accepted"
    assert correction.record.source_closure_event_id == proof.source_closure_event_id


def test_accept_caps_recovery_at_the_pre_harm_baseline_and_accepts_without_any_recovery():
    _, _, _, offered = _state_with_offer(trust=Decimal("-1"))
    offer = offered.accusation_repair_offers[0]
    proof = AccusationRepairTrustProof(
        case_file_id=CASE_FILE_ID,
        source_closure_event_id=SOURCE_CLOSURE_EVENT_ID,
        source_relation_effect_event_id=uuid4(),
        accuser_id=ACCUSER_ID,
        suspect_id=SUSPECT_ID,
        baseline_trust=Decimal("0"),
        post_effect_trust=Decimal("-15"),
    )
    response, relation_event = plan_accusation_repair_resolution(
        offered,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=uuid4(),
        ruleset_version=RULESET_VERSION,
        actor_id=SUSPECT_ID,
        offer_id=offer.offer_id,
        expected_revision=1,
        operation="accept",
        trust_proof=proof,
    )
    assert response.trust_recovery is not None
    assert response.trust_recovery.applied_delta == 1
    assert relation_event is not None and relation_event.value == Decimal("0")

    baseline_state = _state_with_offer(trust=Decimal("0"))[3]
    baseline_offer = baseline_state.accusation_repair_offers[0]
    accepted, no_change = plan_accusation_repair_resolution(
        baseline_state,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=uuid4(),
        ruleset_version=RULESET_VERSION,
        actor_id=SUSPECT_ID,
        offer_id=baseline_offer.offer_id,
        expected_revision=1,
        operation="accept",
        trust_proof=proof,
    )
    assert no_change is None
    assert accepted.trust_recovery is not None
    assert accepted.trust_recovery.applied_delta == 0


def test_trust_proof_cannot_be_used_for_decline_or_defer_and_must_match_offer_sources():
    _, _, _, offered = _state_with_offer(trust=Decimal("-15"))
    offer = offered.accusation_repair_offers[0]
    proof = AccusationRepairTrustProof(
        case_file_id=CASE_FILE_ID,
        source_closure_event_id=SOURCE_CLOSURE_EVENT_ID,
        source_relation_effect_event_id=uuid4(),
        accuser_id=ACCUSER_ID,
        suspect_id=SUSPECT_ID,
        baseline_trust=Decimal("0"),
        post_effect_trust=Decimal("-15"),
    )
    with pytest.raises(InvalidEvent, match="unavailable"):
        plan_accusation_repair_resolution(
            offered,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=uuid4(),
            ruleset_version=RULESET_VERSION,
            actor_id=SUSPECT_ID,
            offer_id=offer.offer_id,
            expected_revision=1,
            operation="decline",
            trust_proof=proof,
        )
    with pytest.raises(InvalidEvent, match="unavailable"):
        plan_accusation_repair_resolution(
            offered,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=uuid4(),
            ruleset_version=RULESET_VERSION,
            actor_id=SUSPECT_ID,
            offer_id=offer.offer_id,
            expected_revision=1,
            operation="accept",
            trust_proof=proof.model_copy(update={"case_file_id": uuid4()}),
        )


def test_hosted_resolution_keeps_accuser_as_requester_and_target_as_decision_maker():
    _, _, _, offered = _state_with_offer()
    offer = offered.accusation_repair_offers[0]
    command_id = uuid4()
    response, relation_event = plan_accusation_repair_resolution(
        offered,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        ruleset_version=RULESET_VERSION,
        actor_id=SUSPECT_ID,
        offer_id=offer.offer_id,
        expected_revision=1,
        operation="defer",
        requested_by=ACCUSER_ID,
    )
    assert relation_event is None
    assert response.actor_id == SUSPECT_ID
    assert response.requested_by == ACCUSER_ID
    _validate(
        offered,
        [response],
        actor_id=ACCUSER_ID,
        command_id=command_id,
        command_type="action.request_accusation_repair_response",
        payload={"kind": "request_accusation_repair_response", "offer_id": str(offer.offer_id),
                 "expected_revision": 1},
        causation_ids=[offer.offer_id],
    )
    with pytest.raises(InvalidEvent, match="unavailable"):
        plan_accusation_repair_resolution(
            offered,
            world_id=WORLD_ID,
            branch_id=BRANCH_ID,
            command_id=uuid4(),
            ruleset_version=RULESET_VERSION,
            actor_id=SUSPECT_ID,
            offer_id=offer.offer_id,
            expected_revision=1,
            operation="defer",
            requested_by=OTHER_ID,
        )


def test_batch_rejects_forged_effect_stale_response_and_wrong_causation():
    _, _, _, offered = _state_with_offer(trust=Decimal("-15"))
    offer = offered.accusation_repair_offers[0]
    proof = AccusationRepairTrustProof(
        case_file_id=CASE_FILE_ID,
        source_closure_event_id=SOURCE_CLOSURE_EVENT_ID,
        source_relation_effect_event_id=uuid4(),
        accuser_id=ACCUSER_ID,
        suspect_id=SUSPECT_ID,
        baseline_trust=Decimal("0"),
        post_effect_trust=Decimal("-15"),
    )
    command_id = uuid4()
    response, relation_event = plan_accusation_repair_resolution(
        offered,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=command_id,
        ruleset_version=RULESET_VERSION,
        actor_id=SUSPECT_ID,
        offer_id=offer.offer_id,
        expected_revision=1,
        operation="accept",
        trust_proof=proof,
    )
    assert relation_event is not None
    forged_effect = relation_event.model_copy(update={"value": Decimal("-5")})
    with pytest.raises(InvalidEvent, match="exact bounded"):
        _validate(
            offered,
            [response, forged_effect],
            actor_id=SUSPECT_ID,
            command_id=command_id,
            command_type="action.resolve_accusation_repair",
            payload={"kind": "resolve_accusation_repair", "offer_id": str(offer.offer_id),
                     "expected_revision": 1, "operation": "accept"},
            causation_ids=[offer.offer_id, proof.source_relation_effect_event_id],
        )
    with pytest.raises(InvalidEvent, match="causation"):
        _validate(
            offered,
            [response, relation_event],
            actor_id=SUSPECT_ID,
            command_id=command_id,
            command_type="action.resolve_accusation_repair",
            payload={"kind": "resolve_accusation_repair", "offer_id": str(offer.offer_id),
                     "expected_revision": 1, "operation": "accept"},
            causation_ids=[offer.offer_id],
        )


def test_world_state_enforces_offer_source_and_hash_compatibility():
    _, _, _, offered = _state_with_offer()
    serialized = state_json(offered)
    assert serialized["accusation_repair_offers"][0]["statement"] == "I should have asked you first."
    bad_offer = offered.accusation_repair_offers[0].model_copy(update={"source_closure_event_id": uuid4()})
    with pytest.raises(ValidationError, match="match one recorded public correction"):
        WorldState.model_validate({
            **serialized,
            "accusation_repair_offers": [bad_offer.model_dump(mode="json")],
        })

