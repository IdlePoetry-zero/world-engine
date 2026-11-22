from __future__ import annotations

from uuid import uuid4

import pytest

from world_engine.domain.adjudication import historical_evidence_digest
from world_engine.domain.components import HistoricalEvidenceArtifact, KnowledgeRecord
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import validate_historical_evidence_destruction_batch
from world_engine.domain.evidence_destruction import validate_destroyable_historical_item
from world_engine.domain.gameplay_events import HistoricalEvidenceDestroyedEvent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.rules import RuleContext, RuleEngine


def _state(*, hidden: bool = False, remote: bool = False):
    place_id, remote_place_id, actor_id, target_id, subject_id = (uuid4() for _ in range(5))
    artifact = HistoricalEvidenceArtifact(
        evidence_id=uuid4(), subject_id=subject_id,
        claim="The old register places the seal in the hall.",
        fact={"predicate": "entity_location", "value": str(place_id), "effective_tick": -3_600},
        provenance=[{"kind": "pack", "reference_id": "pack.test.register"}],
    )
    state = WorldState(entities={
        place_id: WorldEntity(id=place_id, kind="place", name="Hall"),
        remote_place_id: WorldEntity(id=remote_place_id, kind="place", name="Remote hall"),
        actor_id: WorldEntity(id=actor_id, kind="character", name="Player", location_id=place_id),
        subject_id: WorldEntity(id=subject_id, kind="character", name="Seal keeper"),
        target_id: WorldEntity(
            id=target_id, kind="item", name="Old register",
            location_id=remote_place_id if remote else place_id,
            components={
                "hidden": hidden,
                "public_description": "A weathered register with a torn cover.",
                "historical_evidence": artifact.model_dump(mode="json"),
            },
        ),
    }, player_id=actor_id)
    return state, actor_id, target_id, artifact


def test_destroyable_source_requires_local_visible_active_registered_item_and_digest():
    state, actor_id, target_id, artifact = _state()
    target, checked_artifact, location_id = validate_destroyable_historical_item(
        state, actor_id=actor_id, target_id=target_id,
        expected_evidence_id=artifact.evidence_id,
        expected_evidence_hash=historical_evidence_digest(artifact),
    )
    assert target.id == target_id and checked_artifact == artifact
    assert location_id == state.entities[actor_id].location_id

    with pytest.raises(ValueError, match="digest changed"):
        validate_destroyable_historical_item(
            state, actor_id=actor_id, target_id=target_id,
            expected_evidence_hash="0" * 64,
        )


@pytest.mark.parametrize("hidden,remote,reason", [
    (True, False, "not visible"),
    (False, True, "not local"),
])
def test_destroyable_source_rejects_hidden_or_remote_artifact(hidden: bool, remote: bool, reason: str):
    state, actor_id, target_id, _artifact = _state(hidden=hidden, remote=remote)
    with pytest.raises(ValueError, match=reason):
        validate_destroyable_historical_item(state, actor_id=actor_id, target_id=target_id)


def test_destroyable_source_rejects_an_already_destroyed_item():
    state, actor_id, target_id, _artifact = _state()
    entities = dict(state.entities)
    entities[target_id] = entities[target_id].model_copy(update={"status": "destroyed"})
    state = WorldState.model_validate({**state.model_dump(mode="json"), "entities": {
        str(entity_id): entity.model_dump(mode="json") for entity_id, entity in entities.items()
    }})
    with pytest.raises(ValueError, match="unavailable"):
        validate_destroyable_historical_item(state, actor_id=actor_id, target_id=target_id)


def test_destruction_is_a_private_zero_time_event_that_preserves_copies_and_blocks_future_examination():
    state, actor_id, target_id, artifact = _state()
    knowledge = KnowledgeRecord(
        id=uuid4(), owner_id=actor_id, subject_id=artifact.subject_id,
        kind="observation", claim=artifact.claim, summary="Historical observation",
        confidence="1", source_refs=[artifact.evidence_id], provenance=artifact.provenance,
        received_tick=0, fact=artifact.fact, visibility={"policy": "private"},
    )
    state = state.model_copy(update={"knowledge": [knowledge]})
    command_id = uuid4()
    context = RuleContext(
        world_id=uuid4(), branch_id=uuid4(), command_id=command_id,
        actor_id=actor_id, ruleset_version="1.0.0",
    )

    resolution = RuleEngine().evaluate(
        state,
        {"kind": "destroy_historical_evidence", "target_id": str(target_id)},
        context,
    )

    assert resolution.outcome.status == "succeeded"
    assert resolution.elapsed_ticks == 0
    assert len(resolution.events) == 1
    event = resolution.events[0]
    assert isinstance(event, HistoricalEvidenceDestroyedEvent)
    assert event.historical_evidence_id == artifact.evidence_id
    assert event.historical_evidence_hash == historical_evidence_digest(artifact)
    assert event.destroyed_tick == state.current_tick
    assert event.visibility == {"policy": "author_only"}
    validate_historical_evidence_destruction_batch(
        state, actor_id, resolution.events,
        world_id=context.world_id, branch_id=context.branch_id, command_id=command_id,
        ruleset_version=context.ruleset_version,
        command_type="action.destroy_historical_evidence",
        command_payload={"kind": "destroy_historical_evidence", "target_id": str(target_id)},
    )

    after = apply_event(state, event)
    assert after.current_tick == state.current_tick
    assert after.entities[target_id].status == "destroyed"
    assert after.entities[target_id].components["historical_evidence"] == state.entities[target_id].components["historical_evidence"]
    assert after.knowledge == state.knowledge
    with pytest.raises(InvalidEvent, match="unavailable or has changed"):
        apply_event(after, event)

    blocked = RuleEngine().evaluate(
        after,
        {"kind": "improvised_check", "target_id": str(target_id), "skill_id": "skill.investigation"},
        context.model_copy(update={"command_id": uuid4()}),
    )
    assert blocked.outcome.code == "improvised_check_target_unavailable"
    assert not blocked.events

    validate_historical_evidence_destruction_batch(
        state, actor_id, resolution.events,
        world_id=context.world_id, branch_id=context.branch_id, command_id=command_id,
        ruleset_version=context.ruleset_version, command_type=None, command_payload=None,
    )
    with pytest.raises(InvalidEvent, match="exact registered action proposal"):
        validate_historical_evidence_destruction_batch(
            state, actor_id, resolution.events,
            world_id=context.world_id, branch_id=context.branch_id, command_id=command_id,
            ruleset_version=context.ruleset_version,
            command_type="action.destroy_historical_evidence",
            command_payload={"kind": "destroy_historical_evidence", "target_id": str(uuid4())},
        )
