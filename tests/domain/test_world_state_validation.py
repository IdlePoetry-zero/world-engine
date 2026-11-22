import json
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from world_engine.domain.components import LifeComponent, SkillValue, SkillsComponent
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import EntityFieldChangedEvent, ResourceAccount, WorldEntity, WorldState
from world_engine.domain.reducers import (
    _validated_state,
    apply_event,
    canonical_state_payload,
    state_hash,
    state_json,
)


def test_json_state_validation_rejects_malformed_nested_resource_account():
    account_id = uuid4()
    owner_id = uuid4()
    payload = {
        "entities": {str(owner_id): {"id": str(owner_id), "kind": "character"}},
        "resources": {
            str(account_id): {
                "resource_id": "grain",
                "balance": "-1",
                "owner_id": str(owner_id),
                "account_type": "holder",
            }
        },
    }

    with pytest.raises(ValidationError, match="holder account balance cannot be negative"):
        WorldState.model_validate_json(json.dumps(payload))


def test_json_state_validation_still_checks_cross_record_graph_invariants():
    entity_id = uuid4()
    missing_location_id = uuid4()
    payload = {
        "entities": {
            str(entity_id): {
                "id": str(entity_id),
                "kind": "character",
                "location_id": str(missing_location_id),
            }
        }
    }

    with pytest.raises(ValidationError, match="missing or non-location location"):
        WorldState.model_validate_json(json.dumps(payload))


def test_json_entity_validation_rejects_non_object_components():
    payload = {"id": str(uuid4()), "kind": "character", "components": []}

    with pytest.raises(ValidationError, match="components must be an object"):
        WorldEntity.model_validate_json(json.dumps(payload))


def test_json_entity_validation_still_checks_registered_component_shapes():
    payload = {
        "id": str(uuid4()),
        "kind": "character",
        "components": {"needs": {"custom": []}},
    }

    with pytest.raises(ValidationError):
        WorldEntity.model_validate_json(json.dumps(payload))


def test_json_and_python_component_inputs_have_the_same_canonical_shape():
    entity_id = uuid4()
    known_place_id = uuid4()
    python_entity = WorldEntity.model_validate(
        {
            "id": entity_id,
            "kind": "character",
            "components": {
                "life": LifeComponent(birth_tick=0),
                "skills": SkillsComponent(skills={"foraging": SkillValue(level=Decimal("1.25"))}),
                "known_places": (known_place_id,),
            },
        }
    )
    json_entity = WorldEntity.model_validate_json(
        json.dumps(
            {
                "id": str(entity_id),
                "kind": "character",
                "components": {
                    "life": {"birth_tick": 0},
                    "skills": {"skills": {"foraging": {"level": "1.25"}}},
                    "known_places": [str(known_place_id)],
                },
            }
        )
    )

    assert json_entity.model_dump(mode="json") == python_entity.model_dump(mode="json")


def test_python_state_validation_reparses_nested_model_copy_updates():
    account_id = uuid4()
    owner_id = uuid4()
    forged_account = ResourceAccount(
        resource_id="grain",
        balance=Decimal("1"),
        owner_id=owner_id,
    ).model_copy(update={"balance": Decimal("-1")})

    with pytest.raises(ValidationError, match="holder account balance cannot be negative"):
        WorldState(
            entities={owner_id: WorldEntity(id=owner_id, kind="character")},
            resources={account_id: forged_account},
        )


def test_reducer_state_validation_rejects_forged_nested_model_update():
    entity_id = uuid4()
    state = WorldState(
        entities={entity_id: WorldEntity(id=entity_id, kind="character")},
    )
    forged = state.entities[entity_id].model_copy(update={"kind": "not a valid kind"})

    with pytest.raises(InvalidEvent):
        _validated_state(state, {"entities": {entity_id: forged}})


def _legacy_reducer_state() -> tuple[WorldState, object, object, object]:
    place_id, actor_id, legacy_account_id, explicit_account_id = (uuid4() for _ in range(4))
    state = WorldState.model_validate_json(json.dumps({
        "entities": {
            str(place_id): {
                "id": str(place_id),
                "kind": "place",
                "components": {"place": {"place_type": "settlement"}},
            },
            str(actor_id): {
                "id": str(actor_id),
                "kind": "character",
                "location_id": str(place_id),
                "components": {
                    "needs": {
                        "food": {"value": 50, "desired": 80, "critical_below": 20},
                        "water": {"value": 70, "desired": 90, "critical_below": 20},
                        "rest": {"value": 60, "desired": 80, "critical_below": 20},
                    },
                },
            },
        },
        "resources": {
            str(legacy_account_id): {
                "resource_id": "core.wood",
                "balance": "0",
                "account_type": "production_source",
            },
            str(explicit_account_id): {
                "resource_id": "core.stone",
                "balance": "0",
                "account_type": "production_source",
                "credit_limit": None,
            },
        },
        "player_id": str(actor_id),
    }))
    return state, actor_id, legacy_account_id, explicit_account_id


def test_reducer_replacements_preserve_canonical_state_and_legacy_field_presence(monkeypatch):
    state, actor_id, legacy_account_id, explicit_account_id = _legacy_reducer_state()
    updates = {
        "entities": dict(state.entities),
        "resources": dict(state.resources),
        "knowledge": list(state.knowledge),
        "scheduled_events": list(state.scheduled_events),
        # These are absent from the old canonical payload when empty. dict.update
        # appends them in this order; the optimized merge must retain that detail.
        "investigation_cases": [],
        "relationship_promises": [],
        "travel_journeys": [],
    }
    original_validate_json = WorldState.model_validate_json.__func__
    captured_json: list[str | bytes] = []

    def capture_validate_json(cls, data, *args, **kwargs):
        captured_json.append(data)
        return original_validate_json(cls, data, *args, **kwargs)

    monkeypatch.setattr(WorldState, "model_validate_json", classmethod(capture_validate_json))
    updated = _validated_state(state, updates)

    assert len(captured_json) == 1
    actual_payload = json.loads(captured_json[0])
    expected_order = list(canonical_state_payload(state))
    for field_name in updates:
        if field_name not in expected_order:
            expected_order.append(field_name)
    assert list(actual_payload) == expected_order

    assert state_json(updated) == state_json(state)
    assert state_hash(updated) == state_hash(state)
    accounts = state_json(updated)["resources"]
    assert "credit_limit" not in accounts[str(legacy_account_id)]
    assert accounts[str(explicit_account_id)]["credit_limit"] is None
    canonical_needs = state_json(updated)["entities"][str(actor_id)]["components"]["needs"]
    for need_name in ("food", "water", "rest"):
        need = canonical_needs[need_name]
        assert "decay_remainder" not in need
        assert "last_critical_damage_tick" not in need
    assert "investigation_cases" not in state_json(updated)
    assert "relationship_promises" not in state_json(updated)
    assert "travel_journeys" not in state_json(updated)


def test_reducer_replacements_keep_old_state_event_replay_and_reject_malformed_updates():
    state, actor_id, legacy_account_id, _explicit_account_id = _legacy_reducer_state()
    actor = state.entities[actor_id]
    event = EntityFieldChangedEvent(entity_id=actor.id, field_name="name", value="After replay")
    updated = apply_event(state, event)
    reopened = WorldState.model_validate_json(json.dumps(state_json(updated)))

    assert reopened == updated
    assert state_hash(reopened) == state_hash(updated)
    assert "credit_limit" not in state_json(updated)["resources"][str(legacy_account_id)]
    canonical_needs = state_json(updated)["entities"][str(actor_id)]["components"]["needs"]
    for need_name in ("food", "water", "rest"):
        need = canonical_needs[need_name]
        assert "decay_remainder" not in need
        assert "last_critical_damage_tick" not in need

    with pytest.raises(InvalidEvent):
        _validated_state(state, {"resources": []})
    with pytest.raises(InvalidEvent):
        _validated_state(state, {"unknown_state_field": True})
