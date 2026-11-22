from __future__ import annotations

from uuid import uuid4, uuid5

import pytest
from pydantic import ValidationError

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import (
    parse_event,
    requires_public_accusation_correction_validation,
    validate_public_accusation_correction_batch,
)
from world_engine.domain.gameplay_events import (
    PublicAccusationCorrectionRecordedEvent,
)
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
ACTOR_ID = uuid4()
SUSPECT_ID = uuid4()
CASE_FILE_ID = uuid4()
CASE_ID = uuid4()
ANCHOR_ID = uuid4()
LOCATION_ID = uuid4()
OTHER_LOCATION_ID = uuid4()
SOURCE_CLOSURE_EVENT_ID = uuid4()
COMMAND_ID = uuid4()
RULESET_VERSION = "test.1"


def _fixture(
    *,
    outcome: str = "disconfirmed",
    public_accusation: bool = True,
    suspect_id=SUSPECT_ID,
    actor_location_id=LOCATION_ID,
    suspect_location_id=LOCATION_ID,
    suspect_status="active",
    hidden_suspect: bool = False,
):
    case_id = CASE_ID
    anchor_id = ANCHOR_ID
    actor = WorldEntity(
        id=ACTOR_ID,
        kind="character",
        name="Ari",
        location_id=actor_location_id,
    )
    suspect = WorldEntity(
        id=SUSPECT_ID,
        kind="character",
        name="Jing Zhou",
        location_id=suspect_location_id,
        status=suspect_status,
        components={"hidden": hidden_suspect},
    )
    location = WorldEntity(id=LOCATION_ID, kind="place", name="Spring pavilion")
    other_location = WorldEntity(id=OTHER_LOCATION_ID, kind="place", name="Old gate")

    definition = InvestigationCaseDefinition(
        case_id=case_id,
        title="Private case title",
        brief="Private case brief.",
        routes=[
            InvestigationCaseRoute(
                route_id="rules_out_one",
                suspect_id=SUSPECT_ID,
                finding="rules_out",
                requirements=[CaseEvidenceRequirement(
                    subject_id=ACTOR_ID,
                    predicate="entity_status",
                    effective_tick=50,
                    finding="conflict",
                    observation_provenance=[CaseSourceReference(kind="event", reference_id="public.record")],
                    claim_provenance=[CaseSourceReference(kind="genesis", reference_id="private.claim")],
                )],
            ),
            InvestigationCaseRoute(
                route_id="supports_another",
                suspect_id=SUSPECT_ID,
                finding="supports",
                requirements=[CaseEvidenceRequirement(
                    subject_id=ACTOR_ID,
                    predicate="entity_status",
                    effective_tick=51,
                    finding="corroborated",
                    observation_provenance=[CaseSourceReference(kind="event", reference_id="other.record")],
                    claim_provenance=[CaseSourceReference(kind="genesis", reference_id="other.claim")],
                )],
            ),
        ],
    )
    case_file = InvestigationCaseFile(
        id=CASE_FILE_ID,
        case_id=case_id,
        anchor_id=anchor_id,
        owner_id=ACTOR_ID,
        definition_hash="a" * 64,
        title=definition.title,
        brief=definition.brief,
        status="closed",
        revision=4,
        evidence_refs=[],
        opened_tick=0,
        updated_tick=100,
        closed_tick=100,
        outcome=outcome,
        suspect_id=SUSPECT_ID,
        public_accusation=public_accusation,
    )
    entities = {
        ACTOR_ID: actor,
        SUSPECT_ID: suspect,
        LOCATION_ID: location,
        OTHER_LOCATION_ID: other_location,
        ANCHOR_ID: WorldEntity(
            id=ANCHOR_ID,
            kind="item",
            name="Case anchor",
            location_id=LOCATION_ID,
            components={"investigation_case": definition.model_dump(mode="json")},
        ),
    }
    state = WorldState(
        current_tick=200,
        entities=entities,
        investigation_cases=[case_file],
    )
    return state


def _plan(state: WorldState, *, actor_id=ACTOR_ID, expected_revision: int = 4):
    return plan_public_accusation_correction(
        state,
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        ruleset_version=RULESET_VERSION,
        actor_id=actor_id,
        case_file_id=CASE_FILE_ID,
        expected_revision=expected_revision,
        source_closure_event_id=SOURCE_CLOSURE_EVENT_ID,
    )


def _validate(state: WorldState, event: PublicAccusationCorrectionRecordedEvent, *, payload=None, events=None):
    validate_public_accusation_correction_batch(
        state,
        ACTOR_ID,
        events if events is not None else [event],
        world_id=WORLD_ID,
        branch_id=BRANCH_ID,
        command_id=COMMAND_ID,
        ruleset_version=RULESET_VERSION,
        command_type="action.correct_public_accusation",
        command_payload=payload if payload is not None else {
            "kind": "correct_public_accusation",
            "case_file_id": str(CASE_FILE_ID),
            "expected_revision": 4,
            "source_closure_event_id": str(SOURCE_CLOSURE_EVENT_ID),
        },
    )


def test_correction_is_a_private_provenance_event_and_append_only_state_record():
    state = _fixture()
    assert "public_accusation_corrections" not in state_json(state)

    event = _plan(state)
    expected_event_id = uuid5(COMMAND_ID, f"{RULESET_VERSION}:investigation_public_accusation_corrected")
    assert event.type == "core.investigation_public_accusation_corrected.v1"
    assert event.event_id == expected_event_id
    assert event.visibility == {"policy": "author_only"}
    assert event.record.model_dump(mode="json") == {
        "event_id": str(expected_event_id),
        "case_file_id": str(CASE_FILE_ID),
        "expected_revision": 4,
        "source_closure_event_id": str(SOURCE_CLOSURE_EVENT_ID),
        "actor_id": str(ACTOR_ID),
        "suspect_id": str(SUSPECT_ID),
        "corrected_tick": 200,
        "location_id": str(LOCATION_ID),
    }
    assert requires_public_accusation_correction_validation(
        "action.correct_public_accusation", [event],
    )

    _validate(state, event)
    parsed = parse_event(event.model_dump(mode="json"))
    result = apply_event(state, parsed)
    assert result.public_accusation_corrections == [event.record]
    assert result.investigation_cases == state.investigation_cases
    assert result.relations == state.relations
    assert result.current_tick == state.current_tick
    assert state_json(result)["public_accusation_corrections"] == [event.record.model_dump(mode="json")]


@pytest.mark.parametrize(
    "updates",
    [
        {"outcome": "supported"},
        {"public_accusation": False},
        {"expected_revision": 3},
        {"suspect_location_id": OTHER_LOCATION_ID},
        {"suspect_status": "dead"},
        {"hidden_suspect": True},
    ],
)
def test_correction_requires_source_accuser_disconfirmed_public_case_and_present_visible_suspect(updates):
    state = _fixture(**{key: value for key, value in updates.items() if key != "expected_revision"})
    expected_revision = updates.get("expected_revision", 4)
    with pytest.raises(InvalidEvent, match="unavailable"):
        _plan(state, expected_revision=expected_revision)


def test_only_the_source_accuser_can_submit_the_correction():
    with pytest.raises(InvalidEvent, match="unavailable"):
        _plan(_fixture(), actor_id=uuid4())


def test_correction_batch_rejects_forged_record_duplicate_or_mismatched_command_payload():
    state = _fixture()
    event = _plan(state)
    forged_record = event.record.model_copy(update={"suspect_id": uuid4()})
    forged = event.model_copy(update={"record": forged_record})
    with pytest.raises(InvalidEvent, match="unavailable"):
        _validate(state, forged)

    with pytest.raises(InvalidEvent, match="unavailable"):
        _validate(state, event, payload={"kind": "correct_public_accusation", "case_file_id": str(CASE_FILE_ID)})
    with pytest.raises(InvalidEvent, match="unavailable"):
        _validate(state, event, events=[event, event])

    corrected = apply_event(state, event)
    with pytest.raises(InvalidEvent, match="unavailable"):
        _plan(corrected)
    with pytest.raises(ValidationError, match="corrected only once"):
        WorldState.model_validate({
            **state_json(corrected),
            "public_accusation_corrections": [event.record.model_dump(mode="json")] * 2,
        })


def test_correction_preserves_the_original_case_outcome_and_does_not_change_trust():
    state = _fixture()
    event = _plan(state)
    result = apply_event(state, event)
    original = result.investigation_cases[0]
    assert original.outcome == "disconfirmed"
    assert original.public_accusation is True
    assert original.suspect_id == SUSPECT_ID
    assert result.relations == []

