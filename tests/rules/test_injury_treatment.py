from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import (
    ComponentReplacedEvent,
    HealthRestoredEvent,
    InjuryTreatedEvent,
    ResourceConsumedEvent,
)
from world_engine.domain.models import KnowledgeRecord, ResourceAccount, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event
from world_engine.domain.treatment_validation import validate_treatment_batch
from world_engine.rules import RuleContext, RuleEngine, parse_action


def _state(*, target_is_self: bool = True, place_type: str = "settlement", hidden_target: bool = False):
    location_id, healer_id, patient_id, injury_id, other_injury_id = (uuid4() for _ in range(5))
    if target_is_self:
        patient_id = healer_id
    account_id, sink_id, lot_id = uuid4(), uuid4(), uuid4()
    healer = WorldEntity(
        id=healer_id,
        kind="character",
        name="Aster",
        location_id=location_id,
        components={
            "health": {
                "current": 90,
                "maximum": 100,
                "injuries": [
                    {"injury_id": str(injury_id), "injury_type": "core.injury.blunt", "severity": 10, "started_tick": 0},
                    {"injury_id": str(other_injury_id), "injury_type": "core.injury.deprivation", "severity": 3, "started_tick": 0},
                ],
            },
            "skills": {"skills": {"skill.care": {"level": "10"}}},
            "inventory": {"lots": [{
                "lot_id": str(lot_id), "account_id": str(account_id),
                "resource_id": "core.medicine", "quantity": "2",
            }]},
        },
    )
    entities = {
        location_id: WorldEntity(
            id=location_id,
            kind="place",
            name="Cedar Settlement",
            components={"place": {"place_type": place_type}},
        ),
        healer_id: healer,
    }
    if not target_is_self:
        entities[patient_id] = WorldEntity(
            id=patient_id,
            kind="character",
            name="Bran",
            location_id=location_id,
            components={
                "health": {
                    "current": 3,
                    "maximum": 100,
                    "injuries": [{
                        "injury_id": str(injury_id),
                        "injury_type": "combat.weapon",
                        "severity": 70,
                        "started_tick": 0,
                    }],
                },
                **({"hidden": True} if hidden_target else {}),
            },
        )
    knowledge = []
    if hidden_target:
        knowledge.append(KnowledgeRecord(
            id=uuid4(),
            owner_id=healer_id,
            subject_id=patient_id,
            claim="The patient is present and recognized.",
            kind="observation",
            received_tick=0,
        ))
    state = WorldState(
        entities=entities,
        player_id=healer_id,
        knowledge=knowledge,
        resources={
            account_id: ResourceAccount(resource_id="core.medicine", balance="2", owner_id=healer_id),
            sink_id: ResourceAccount(resource_id="core.medicine", balance="0", account_type="consumption_sink"),
        },
    )
    return state, location_id, healer_id, patient_id, injury_id, other_injury_id, account_id, sink_id


def _context(actor_id: UUID, command_id: UUID = UUID(int=300)) -> RuleContext:
    return RuleContext(
        world_id=UUID(int=1), branch_id=UUID(int=2), command_id=command_id,
        actor_id=actor_id, ruleset_version="1.0.0",
    )


def _replay(state: WorldState, events):
    current = state
    for event in events:
        current = apply_event(current, event)
    return current


def test_treat_injury_consumes_one_fifo_medicine_and_preserves_other_injuries():
    state, _place_id, healer_id, patient_id, injury_id, other_injury_id, account_id, sink_id = _state()
    context = _context(healer_id)
    intent = {
        "kind": "treat_injury",
        "target_id": str(patient_id),
        "injury_id": str(injury_id),
        "medicine_resource_id": "core.medicine",
    }
    assert parse_action(intent).kind == "treat_injury"
    result = RuleEngine().evaluate(state, intent, context)

    assert result.outcome.code == "injury_treated"
    assert result.elapsed_ticks == 1800
    treatment = next(event for event in result.events if isinstance(event, InjuryTreatedEvent))
    healing = next(event for event in result.events if isinstance(event, HealthRestoredEvent))
    consumed = next(event for event in result.events if isinstance(event, ResourceConsumedEvent))
    assert treatment.injury_id == injury_id
    assert treatment.healing_amount == healing.amount == 5
    assert treatment.visibility == {"policy": "subject_ids", "subject_ids": [str(healer_id)]}
    legacy_self_visibility = InjuryTreatedEvent.model_validate({
        **treatment.model_dump(mode="json"),
        "visibility": {"policy": "subject_ids", "subject_ids": [str(healer_id), str(healer_id)]},
    })
    assert legacy_self_visibility.event_id == treatment.event_id
    assert treatment.consumed_event_id == consumed.event_id
    assert consumed.purpose == "injury_treatment"
    assert consumed.flows[0].quantity == 1
    assert healing.source_event_ids == [consumed.event_id, treatment.event_id]
    assert any(isinstance(event, ComponentReplacedEvent) and event.component_name == "inventory" for event in result.events)

    after = _replay(state, result.events)
    patient_health = after.entities[patient_id].components["health"]
    selected = next(item for item in patient_health["injuries"] if item["injury_id"] == str(injury_id))
    untouched = next(item for item in patient_health["injuries"] if item["injury_id"] == str(other_injury_id))
    assert patient_health["current"] == 95
    assert selected["treated"] is True
    assert selected["treated_event_id"] == str(treatment.event_id)
    assert selected["treated_tick"] == 1800
    assert untouched["treated"] is False
    assert untouched["treated_event_id"] is None
    assert untouched["treated_tick"] is None
    assert after.resources[account_id].balance == 1
    assert after.resources[sink_id].balance == 1

    validate_treatment_batch(
        result.events,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        ruleset_version=context.ruleset_version,
        actor_id=healer_id,
        command_type="action.treat_injury",
        command_payload=intent,
        state=state,
    )


def test_treat_other_visible_co_located_patient_overdraws_health_cap_only():
    state, _, healer_id, patient_id, injury_id, _, account_id, _ = _state(
        target_is_self=False, hidden_target=True,
    )
    result = RuleEngine().evaluate(
        state,
        {"kind": "treat_injury", "target_id": str(patient_id), "injury_id": str(injury_id),
         "medicine_resource_id": "core.medicine"},
        _context(healer_id, UUID(int=301)),
    )
    assert result.outcome.code == "injury_treated"
    after = _replay(state, result.events)
    assert after.entities[patient_id].components["health"]["current"] == 8
    assert after.entities[healer_id].components["health"]["current"] == 90
    assert after.entities[healer_id].components["health"]["injuries"][0]["treated"] is False
    assert after.resources[account_id].balance == 1


def test_treat_injury_at_full_health_consumes_medicine_without_restoration_event():
    state, _place_id, healer_id, patient_id, injury_id, other_injury_id, account_id, sink_id = _state()
    healer = state.entities[healer_id]
    health = dict(healer.components["health"])
    health["current"] = health["maximum"]
    state = WorldState.model_validate({
        **state.model_dump(mode="json"),
        "entities": {
            **state.model_dump(mode="json")["entities"],
            str(healer_id): {**healer.model_dump(mode="json"), "components": {**healer.components, "health": health}},
        },
    })
    context = _context(healer_id, UUID(int=304))
    intent = {
        "kind": "treat_injury", "target_id": str(patient_id),
        "injury_id": str(injury_id), "medicine_resource_id": "core.medicine",
    }
    result = RuleEngine().evaluate(state, intent, context)
    assert result.outcome.code == "injury_treated"
    assert not any(isinstance(event, HealthRestoredEvent) for event in result.events)
    treatment = next(event for event in result.events if isinstance(event, InjuryTreatedEvent))
    assert treatment.healing_amount == 0
    after = _replay(state, result.events)
    injuries = after.entities[patient_id].components["health"]["injuries"]
    selected = next(item for item in injuries if item["injury_id"] == str(injury_id))
    untouched = next(item for item in injuries if item["injury_id"] == str(other_injury_id))
    assert after.entities[patient_id].components["health"]["current"] == 100
    assert selected["treated"] is True
    assert untouched["treated"] is False
    assert after.resources[account_id].balance == 1
    assert after.resources[sink_id].balance == 1
    validate_treatment_batch(
        result.events,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        ruleset_version=context.ruleset_version,
        actor_id=healer_id,
        command_type="action.treat_injury",
        command_payload=intent,
        state=state,
    )


@pytest.mark.parametrize(("maximum", "restored_health"), [(1, 1), (100, 6)])
def test_treating_the_disabling_combat_injury_restores_protected_patient(maximum, restored_health):
    state, _, healer_id, patient_id, other_injury_id, _, account_id, _ = _state(target_is_self=False)
    patient = state.entities[patient_id]
    patient_health = {**patient.components["health"], "current": 1, "maximum": maximum}
    patient = patient.model_copy(update={"components": {
        **patient.components,
        "health": patient_health,
        "capabilities": {"combat_death_protection": True},
    }})
    state = WorldState.model_validate({
        **state.model_dump(mode="json"),
        "entities": {**state.entities, patient_id: patient},
    })
    engine = RuleEngine()
    attack = engine.evaluate(
        state, {"kind": "attack", "target_id": str(patient_id)}, _context(healer_id, UUID(int=400)),
    )
    assert attack.outcome.code == "combat_target_incapacitated"
    injured = _replay(state, attack.events)
    disabling_injury_id = injured.entities[patient_id].components["health"]["incapacitating_injury_id"]
    context = _context(healer_id, UUID(int=401))
    intent = {"kind": "treat_injury", "target_id": str(patient_id), "medicine_resource_id": "core.medicine"}

    wrong_injury = engine.evaluate(injured, {**intent, "injury_id": str(other_injury_id)}, context)
    assert wrong_injury.outcome.code == "injury_unavailable"
    assert wrong_injury.events == []

    intent["injury_id"] = disabling_injury_id
    treatment = engine.evaluate(injured, intent, context)
    assert treatment.outcome.code == "injury_treated"
    assert treatment.outcome.details["patient_recovered"] is True
    assert treatment.outcome.details["healing_amount"] == restored_health - 1
    validate_treatment_batch(
        treatment.events,
        world_id=context.world_id,
        branch_id=context.branch_id,
        command_id=context.command_id,
        ruleset_version=context.ruleset_version,
        actor_id=healer_id,
        command_type="action.treat_injury",
        command_payload=intent,
        state=injured,
    )
    after = _replay(injured, treatment.events)
    patient = after.entities[patient_id]
    assert patient.status == "active"
    health = patient.components["health"]
    assert health["current"] == restored_health
    assert health.get("incapacitating_injury_id") is None
    injuries = {injury["injury_id"]: injury for injury in health["injuries"]}
    assert injuries[disabling_injury_id]["treated"] is True
    assert injuries[str(other_injury_id)]["treated"] is False
    assert after.resources[account_id].balance == 1
    assert any(isinstance(event, HealthRestoredEvent) for event in treatment.events) == (maximum > 1)


@pytest.mark.parametrize(("place_type", "skill_level", "hidden", "expected_code"), [
    ("site", "10", False, "treatment_location_unavailable"),
    ("settlement", "9.99", False, "care_skill_insufficient"),
    ("settlement", "10", True, "target_unseen"),
])
def test_treatment_rejects_ineligible_location_skill_or_hidden_patient(place_type, skill_level, hidden, expected_code):
    state, _location_id, healer_id, patient_id, injury_id, _, _, _ = _state(
        target_is_self=False, place_type=place_type, hidden_target=hidden,
    )
    healer = state.entities[healer_id]
    skills = healer.components["skills"]
    skills["skills"]["skill.care"]["level"] = skill_level
    updated_healer = healer.model_copy(update={"components": {**healer.components, "skills": skills}})
    entities = {**state.entities, healer_id: updated_healer}
    if hidden:
        # Remove the observation that the fixture normally grants.
        state = state.model_copy(update={"knowledge": []})
    state = WorldState.model_validate({
        **state.model_dump(mode="json"),
        "entities": entities,
    })
    result = RuleEngine().evaluate(
        state,
        {"kind": "treat_injury", "target_id": str(patient_id), "injury_id": str(injury_id),
         "medicine_resource_id": "core.medicine"},
        _context(healer_id, UUID(int=302)),
    )
    assert result.outcome.code == expected_code
    assert result.events == [] and result.elapsed_ticks == 0


def test_treatment_event_requires_same_batch_dose_and_health_restore():
    state, _, healer_id, patient_id, injury_id, _, _, _ = _state()
    context = _context(healer_id, UUID(int=303))
    result = RuleEngine().evaluate(
        state,
        {"kind": "treat_injury", "target_id": str(patient_id), "injury_id": str(injury_id),
         "medicine_resource_id": "core.medicine"},
        context,
    )
    assert result.outcome.status == "succeeded"
    treatment = next(event for event in result.events if isinstance(event, InjuryTreatedEvent))
    tampered = treatment.model_copy(update={"expected_health_revision": 99})
    events = [tampered if event is treatment else event for event in result.events]
    with pytest.raises(InvalidEvent, match="health revision is stale"):
        _replay(state, events)

    without_supply = [event for event in result.events if not (
        isinstance(event, ResourceConsumedEvent) and event.purpose == "injury_treatment"
    )]
    with pytest.raises(InvalidEvent, match="medicine consumption"):
        validate_treatment_batch(
            without_supply,
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            ruleset_version=context.ruleset_version,
            actor_id=healer_id,
            command_type="action.treat_injury",
            state=state,
        )
