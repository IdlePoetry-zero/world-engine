from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from world_engine.application.visibility import project_event, project_player_state
from world_engine.domain.adjudication import (
    IMPROVISED_EXAMINE_POLICY,
    improvised_examine_chance,
    improvised_roll_digest,
)
from world_engine.domain.components import HistoricalEvidenceArtifact, KnowledgeRecord, SkillsComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.events import event_payload, validate_adjudication_batch
from world_engine.domain.gameplay_events import ImprovisedCheckResolvedEvent, KnowledgeAcquiredEvent
from world_engine.domain.investigation import compare_evidence_sources
from world_engine.domain.models import TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.rules import RuleContext, RuleEngine, parse_action


TEXT = "A brass bell with a cracked wooden handle."


def _state(*, description: str | None = TEXT, hidden: bool = False, skill_level: int = 50):
    place_id, actor_id, target_id = uuid4(), uuid4(), uuid4()
    entities = {
        place_id: WorldEntity(
            id=place_id,
            kind="place",
            name="Old Market",
            components={"place": {"place_type": "settlement"}},
        ),
        actor_id: WorldEntity(
            id=actor_id,
            kind="character",
            name="Player",
            location_id=place_id,
            components={
                "needs": {
                    "food": {"value": 60, "critical_below": 20},
                    "water": {"value": 70, "critical_below": 20},
                    "rest": {"value": 40},
                },
                "skills": {"skills": {"skill.investigation": {"level": skill_level}}},
            },
        ),
        target_id: WorldEntity(
            id=target_id,
            kind="item",
            name="Brass Bell",
            location_id=place_id,
            components={
                **({"public_description": description} if description is not None else {}),
                "hidden": hidden,
            },
        ),
    }
    state = WorldState(entities=entities, player_id=actor_id)
    return state, place_id, actor_id, target_id


def _context(actor_id, command_id):
    return RuleContext(
        world_id=UUID(int=10),
        branch_id=UUID(int=20),
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version="1.0.0",
    )


def _command_for(state, want_pass: bool) -> UUID:
    skill = SkillsComponent.model_validate(state.entities[state.player_id].components["skills"]).skills[
        "skill.investigation"
    ]
    chance = improvised_examine_chance(skill.level)
    for value in range(1, 10_000):
        command_id = UUID(int=value)
        digest = improvised_roll_digest(
            world_id=UUID(int=10),
            branch_id=UUID(int=20),
            command_id=command_id,
            ruleset_version="1.0.0",
        )
        passed = int(digest, 16) % 100 + 1 <= chance
        if passed == want_pass:
            return command_id
    raise AssertionError("test seed search did not find the requested deterministic check result")


def _replay(state, events):
    result = state
    for event in events:
        result = apply_event(result, event)
    return result


def test_success_records_only_existing_text_as_private_knowledge():
    state, _place_id, actor_id, target_id = _state()
    context = _context(actor_id, _command_for(state, True))
    result = RuleEngine().evaluate(
        state,
        {"kind": "improvised_check", "target_id": str(target_id), "skill_id": "skill.investigation"},
        context,
    )

    audit = next(event for event in result.events if isinstance(event, ImprovisedCheckResolvedEvent))
    knowledge = next(event for event in result.events if isinstance(event, KnowledgeAcquiredEvent))
    assert result.outcome.status == "succeeded"
    assert result.outcome.details == {
        "target_id": str(target_id), "skill_id": "skill.investigation", "passed": True
    }
    assert audit.policy_version == IMPROVISED_EXAMINE_POLICY
    assert audit.passed and audit.roll <= audit.chance_percent
    assert audit.source_description_hash == sha256(TEXT.encode("utf-8")).hexdigest()
    assert audit.visibility == {"policy": "author_only"}
    assert knowledge.record.owner_id == actor_id
    assert knowledge.record.subject_id == target_id
    assert knowledge.record.claim == TEXT
    assert knowledge.record.source_event_id == audit.event_id
    assert knowledge.record.visibility.policy == "private"
    assert knowledge.record.fact is not None
    assert knowledge.record.fact.predicate == "entity_location"
    assert knowledge.record.fact.value == state.entities[actor_id].location_id
    assert knowledge.record.fact.effective_tick == 60

    updated = _replay(state, result.events)
    assert updated.current_tick == 60
    assert updated.knowledge == [knowledge.record]
    other_player = uuid4()
    other_projection = project_player_state(
        {**updated.model_dump(mode="json"), "entities": {
            **updated.model_dump(mode="json")["entities"],
            str(other_player): {
                "id": str(other_player), "kind": "character", "name": "Other Player",
                "location_id": str(state.entities[actor_id].location_id), "components": {},
            },
        }},
        str(other_player),
    )
    assert other_projection["knowledge"] == []
    assert project_event(audit.model_dump(mode="json"), str(other_player)) is None


def _historical_case(*, hidden: bool = False):
    state, place_id, actor_id, target_id = _state(hidden=hidden)
    artifact = HistoricalEvidenceArtifact(
        evidence_id=uuid4(),
        subject_id=target_id,
        claim="The archive records the bell as present at the winter feast.",
        fact={"predicate": "entity_status", "value": "active", "effective_tick": -3_600},
        provenance=[{"kind": "pack", "reference_id": "pack.test.archive.bell-feast"}],
    )
    serialized = state.model_dump(mode="json")
    serialized["entities"][str(target_id)]["components"]["historical_evidence"] = artifact.model_dump(mode="json")
    state = WorldState.model_validate(serialized)
    return state, place_id, actor_id, target_id, artifact


def test_successful_check_records_fixed_authored_historical_evidence_and_replays():
    state, _place_id, actor_id, target_id, artifact = _historical_case()
    initial_projection = project_player_state(state.model_dump(mode="json"), str(actor_id))
    assert "historical_evidence" not in initial_projection["entities"][str(target_id)]["components"]
    assert all(item["claim"] != artifact.claim for item in initial_projection["knowledge"])
    context = _context(actor_id, _command_for(state, True))
    result = RuleEngine().evaluate(
        state,
        {"kind": "improvised_check", "target_id": str(target_id), "skill_id": "skill.investigation"},
        context,
    )

    assert result.outcome.status == "succeeded"
    assert result.elapsed_ticks == 60
    audit = next(event for event in result.events if isinstance(event, ImprovisedCheckResolvedEvent))
    observation_event = next(event for event in result.events if isinstance(event, KnowledgeAcquiredEvent))
    observation = observation_event.record
    assert audit.historical_evidence_id == artifact.evidence_id
    assert audit.historical_evidence_hash
    payload = event_payload(audit)
    assert payload["historical_evidence_id"] == str(artifact.evidence_id)
    assert payload["historical_evidence_hash"] == audit.historical_evidence_hash
    assert observation.subject_id == artifact.subject_id
    assert observation.claim == artifact.claim
    assert observation.fact == artifact.fact
    assert observation.fact.effective_tick == -3_600
    assert observation.received_tick == 60
    assert observation.source_refs == [artifact.evidence_id]
    assert observation.provenance == artifact.provenance
    assert observation.source_event_id == audit.event_id
    assert observation.owner_id == actor_id and observation.visibility.policy == "private"

    validate_adjudication_batch(
        state.current_tick,
        actor_id,
        result.events,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        ruleset_version=context.ruleset_version,
        state=state,
    )
    replayed = _replay(state, result.events)
    assert replayed.current_tick == 60
    assert replayed.knowledge[-1].fact == artifact.fact
    after_projection = project_player_state(replayed.model_dump(mode="json"), str(actor_id))
    assert "historical_evidence" not in after_projection["entities"][str(target_id)]["components"]
    assert any(item["claim"] == artifact.claim for item in after_projection["knowledge"])
    independent_claim = KnowledgeRecord(
        id=uuid4(), owner_id=actor_id, subject_id=artifact.subject_id,
        claim="A witness says the bell was present at the winter feast.",
        kind="rumor", received_tick=0, source_event_id=uuid4(), fact=artifact.fact,
        provenance=[{"kind": "pack", "reference_id": "pack.test.witness.bell-feast"}],
    )
    assert compare_evidence_sources(
        observation.id, independent_claim.id, [observation, independent_claim],
        actor_id=actor_id, at_tick=replayed.current_tick,
    ) == "corroborated"


def test_failed_historical_check_has_no_artifact_reference_and_keeps_normal_clock_effects():
    state, _place_id, actor_id, target_id, _artifact = _historical_case()
    context = _context(actor_id, _command_for(state, False))
    result = RuleEngine().evaluate(
        state,
        {"kind": "improvised_check", "target_id": str(target_id), "skill_id": "skill.investigation"},
        context,
    )

    audit = next(event for event in result.events if isinstance(event, ImprovisedCheckResolvedEvent))
    assert result.outcome.status == "failed"
    assert audit.passed is False
    assert audit.historical_evidence_id is None and audit.historical_evidence_hash is None
    assert not any(isinstance(event, KnowledgeAcquiredEvent) for event in result.events)
    assert "historical_evidence_id" not in event_payload(audit)
    validate_adjudication_batch(
        state.current_tick,
        actor_id,
        result.events,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        ruleset_version=context.ruleset_version,
        state=state,
    )
    assert _replay(state, result.events).current_tick == 60


def test_historical_check_rejects_forged_source_copy_hash_and_hidden_item():
    state, _place_id, actor_id, target_id, _artifact = _historical_case()
    context = _context(actor_id, _command_for(state, True))
    result = RuleEngine().evaluate(
        state,
        {"kind": "improvised_check", "target_id": str(target_id), "skill_id": "skill.investigation"},
        context,
    )
    audit = next(event for event in result.events if isinstance(event, ImprovisedCheckResolvedEvent))
    knowledge_event = next(event for event in result.events if isinstance(event, KnowledgeAcquiredEvent))
    forged_record = KnowledgeRecord.model_validate({
        **knowledge_event.record.model_dump(mode="json"),
        "fact": {"predicate": "entity_status", "value": "retired", "effective_tick": 60},
    })
    forged_events = [
        event.model_copy(update={"record": forged_record})
        if isinstance(event, KnowledgeAcquiredEvent) else event
        for event in result.events
    ]
    with pytest.raises(InvalidEvent, match="verified description"):
        validate_adjudication_batch(
            state.current_tick, actor_id, forged_events,
            world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
            ruleset_version=context.ruleset_version, state=state,
        )

    changed_audit = audit.model_copy(update={"historical_evidence_hash": "0" * 64})
    advanced = _replay(state, [result.events[0]])
    with pytest.raises(InvalidEvent, match="Historical evidence audit"):
        apply_event(advanced, changed_audit)

    hidden_state, _place_id, actor_id, target_id, _artifact = _historical_case(hidden=True)
    hidden_result = RuleEngine().evaluate(
        hidden_state,
        {"kind": "improvised_check", "target_id": str(target_id), "skill_id": "skill.investigation"},
        _context(actor_id, _command_for(hidden_state, True)),
    )
    assert hidden_result.outcome.status == "rejected"
    assert hidden_result.events == []


def test_miss_is_audited_without_adding_an_observation():
    state, _place_id, actor_id, target_id = _state(skill_level=0)
    result = RuleEngine().evaluate(
        state,
        {"kind": "improvised_check", "target_id": str(target_id), "skill_id": "skill.investigation"},
        _context(actor_id, _command_for(state, False)),
    )

    audit = next(event for event in result.events if isinstance(event, ImprovisedCheckResolvedEvent))
    assert result.outcome.status == "failed"
    assert not audit.passed
    assert audit.observation_record_id is None
    assert not any(isinstance(event, KnowledgeAcquiredEvent) for event in result.events)
    assert _replay(state, result.events).knowledge == []


@pytest.mark.parametrize(
    ("description", "hidden", "target_is_local", "skill_id", "expected_code"),
    [
        (None, False, True, "skill.investigation", "improvised_check_no_authored_detail"),
        (TEXT, True, True, "skill.investigation", "improvised_check_target_unseen"),
        (TEXT, False, False, "skill.investigation", "improvised_check_target_unavailable"),
        (TEXT, False, True, "skill.local_knowledge", "improvised_check_skill_unavailable"),
    ],
)
def test_improvised_check_rejects_missing_or_unsafe_preconditions(
    description, hidden, target_is_local, skill_id, expected_code
):
    state, _place_id, actor_id, target_id = _state(description=description, hidden=hidden)
    if not target_is_local:
        target = state.entities[target_id]
        far_place_id = uuid4()
        far_place = WorldEntity(
            id=far_place_id, kind="place", name="Far Place",
            components={"place": {"place_type": "settlement"}},
        )
        entities = dict(state.entities)
        entities[far_place_id] = far_place
        entities[target_id] = target.model_copy(update={"location_id": far_place_id})
        state = WorldState.model_validate({**state.model_dump(mode="python"), "entities": entities})

    result = RuleEngine().evaluate(
        state,
        {"kind": "improvised_check", "target_id": str(target_id), "skill_id": skill_id},
        _context(actor_id, uuid4()),
    )

    assert result.outcome.status == "rejected"
    assert result.outcome.code == expected_code
    assert result.events == []


def test_intent_is_closed_and_timing_is_fixed():
    state, _place_id, actor_id, target_id = _state()
    with pytest.raises(ValueError, match="registered schema"):
        parse_action({
            "kind": "improvised_check", "target_id": str(target_id),
            "skill_id": "skill.investigation", "difficulty": 1,
        })
    command_id = _command_for(state, True)
    event = RuleEngine().evaluate(
        state,
        {"kind": "improvised_check", "target_id": str(target_id), "skill_id": "skill.investigation"},
        _context(actor_id, command_id),
    ).events[1]
    with pytest.raises(ValueError, match="fixed 60-second"):
        ImprovisedCheckResolvedEvent.model_validate(
            event.model_dump(mode="python") | {"resolved_tick": 61}
        )


def test_reducer_rechecks_skill_revision_and_source_description():
    state, _place_id, actor_id, target_id = _state()
    result = RuleEngine().evaluate(
        state,
        {"kind": "improvised_check", "target_id": str(target_id), "skill_id": "skill.investigation"},
        _context(actor_id, _command_for(state, True)),
    )
    audit = next(event for event in result.events if isinstance(event, ImprovisedCheckResolvedEvent))
    advanced = apply_event(state, TimeAdvancedEvent(to_tick=audit.resolved_tick))

    forged = audit.model_copy(update={"skill_level": Decimal("0")})
    with pytest.raises(InvalidEvent, match="failed schema validation"):
        apply_event(advanced, forged)

    target = advanced.entities[target_id]
    entities = dict(advanced.entities)
    entities[target_id] = target.model_copy(update={
        "components": {**target.components, "public_description": "A different authored description."}
    })
    changed_source = WorldState.model_validate({**advanced.model_dump(mode="python"), "entities": entities})
    with pytest.raises(InvalidEvent, match="description hash"):
        apply_event(changed_source, audit)

    actor = advanced.entities[actor_id]
    skills = SkillsComponent.model_validate(actor.components["skills"])
    skill = skills.skills["skill.investigation"].model_copy(update={"level": Decimal("60")})
    changed_skills = skills.model_copy(update={
        "skills": {**skills.skills, "skill.investigation": skill},
        "revision": skills.revision + 1,
    })
    entities = dict(advanced.entities)
    entities[actor_id] = actor.model_copy(update={
        "components": {**actor.components, "skills": changed_skills.model_dump(mode="json")}
    })
    changed_skill_state = WorldState.model_validate({**advanced.model_dump(mode="python"), "entities": entities})
    with pytest.raises(InvalidEvent, match="skill compare-and-swap"):
        apply_event(changed_skill_state, audit)

