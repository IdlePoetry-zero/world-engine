from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from world_engine.domain.gameplay_events import KnowledgeAcquiredEvent
from world_engine.domain.components import KnowledgeRecord
from world_engine.domain.models import EntityFieldChangedEvent, ResourceAccount, WorldEntity, WorldState
from world_engine.domain.reducers import apply_event, author_value_hash, canonical_state_payload, state_hash, state_json


def _state(*, explicit_credit_limit: bool) -> WorldState:
    place_id, actor_id, source_id = uuid4(), uuid4(), uuid4()
    resource = {
        "resource_id": "core.wood",
        "balance": "0",
        "account_type": "production_source",
    }
    if explicit_credit_limit:
        resource["credit_limit"] = None
    return WorldState(
        entities={
            place_id: WorldEntity(id=place_id, kind="place", components={"place": {"place_type": "settlement"}}),
            actor_id: WorldEntity(
                id=actor_id,
                kind="character",
                location_id=place_id,
                components={"needs": {"food": {"value": 50}, "water": {"value": 70}, "rest": {"value": 60}}},
            ),
        },
        resources={source_id: ResourceAccount.model_validate(resource)},
        player_id=actor_id,
    )


def test_state_hash_keeps_absent_and_explicit_null_credit_limit_compatibility():
    old_state = _state(explicit_credit_limit=False)
    explicit_state = _state(explicit_credit_limit=True)
    old_payload = state_json(old_state)
    explicit_payload = state_json(explicit_state)
    old_account = next(iter(old_payload["resources"].values()))
    explicit_account = next(iter(explicit_payload["resources"].values()))
    assert "credit_limit" not in old_account
    assert explicit_account["credit_limit"] is None

    legacy_payload = old_state.model_dump(mode="json")
    legacy_payload.pop("investigation_cases", None)
    legacy_payload.pop("relationship_promises", None)
    for entity in legacy_payload["entities"].values():
        components = entity["components"]
        if "place" in components:
            components["place"].pop("renewable_harvests", None)
        for need in components.get("needs", {}).values():
            if isinstance(need, dict):
                need.pop("decay_remainder", None)
                need.pop("last_critical_damage_tick", None)
    for account in legacy_payload["resources"].values():
        account.pop("credit_limit", None)
    legacy_digest = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    ).hexdigest()
    assert state_hash(old_state) == legacy_digest
    assert state_hash(explicit_state) != state_hash(old_state)
    assert canonical_state_payload(explicit_state)["resources"] == explicit_payload["resources"]


def test_author_needs_compare_and_swap_omits_only_typed_need_defaults():
    old_needs = {"food": {"value": 50, "desired": 80, "critical_below": 20}}
    upgraded_needs = {
        "food": {
            "value": 50,
            "desired": 80,
            "critical_below": 20,
            "decay_remainder": 0,
            "last_critical_damage_tick": None,
        }
    }
    assert author_value_hash(old_needs, component_name="needs") == author_value_hash(
        upgraded_needs, component_name="needs"
    )
    assert author_value_hash({"decay_remainder": 0}) != author_value_hash({})
    assert author_value_hash(old_needs) != author_value_hash(upgraded_needs)


def test_personality_default_locks_preserve_canonical_state_and_author_hashes():
    character_id = uuid4()
    old_personality = {
        "revision": 1,
        "openness": "0.4",
        "conscientiousness": "0.5",
        "extraversion": "0.5",
        "agreeableness": "0.5",
        "neuroticism": "0.5",
        "traits": ["patient"],
        "values": [],
    }

    def state(personality):
        character = WorldEntity(id=character_id, kind="character", components={"personality": personality})
        return WorldState(entities={character_id: character})

    explicit_empty = {**old_personality, "locked_fields": []}
    old_state = state(old_personality)
    empty_locks_state = state(explicit_empty)
    assert state_json(old_state)["entities"][str(character_id)]["components"]["personality"] == old_personality
    assert state_json(empty_locks_state) == state_json(old_state)
    assert state_hash(empty_locks_state) == state_hash(old_state)
    assert state_hash(state({**old_personality, "locked_fields": ["traits"]})) != state_hash(old_state)
    assert author_value_hash(explicit_empty, component_name="personality") == author_value_hash(old_personality)
    assert author_value_hash(explicit_empty, component_name="attributes") != author_value_hash(old_personality)


def test_new_population_and_quota_defaults_preserve_old_typed_hashes():
    character_id, group_id, producer_id = uuid4(), uuid4(), uuid4()
    old_character = {
        "id": str(character_id),
        "kind": "character",
        "components": {
            "life": {"birth_tick": 0, "lifespan_ticks": 3_153_600_000},
            "attributes": {"values": {"population_group_id": "custom-value"}},
        },
    }
    new_character = {
        **old_character,
        "components": {
            **old_character["components"],
            "life": {**old_character["components"]["life"], "population_group_id": None},
        },
    }
    old_group = {
        "id": str(group_id),
        "kind": "group",
        "components": {"population": {"species_id": "core.human", "headcount": 50}},
    }
    new_group = {
        **old_group,
        "components": {
            "population": {**old_group["components"]["population"], "unmodeled_headcount": None}
        },
    }
    old_producer = {"id": str(producer_id), "kind": "place", "components": {"production": {"recipes": []}}}
    new_producer = {
        **old_producer,
        "components": {"production": {"recipes": [], "quotas": []}},
    }
    schedule_id = uuid4()
    old_schedule = {
        "id": str(schedule_id),
        "due_tick": 100,
        "phase": "environment",
        "handler_id": "production.quota_renewal",
        "handler_version": "1.0.0",
    }
    new_schedule = {
        **old_schedule,
        "repeat_ticks": None,
        "max_runs": 1,
        "completed_runs": 0,
    }
    old_state = WorldState.model_validate({"entities": {
        str(character_id): old_character,
        str(group_id): old_group,
        str(producer_id): old_producer,
    }, "scheduled_events": [old_schedule]})
    new_state = WorldState.model_validate({"entities": {
        str(character_id): new_character,
        str(group_id): new_group,
        str(producer_id): new_producer,
    }, "scheduled_events": [new_schedule]})

    assert state_hash(old_state) == state_hash(new_state)
    actor = old_state.entities[character_id]
    updated_state = apply_event(
        old_state,
        EntityFieldChangedEvent(entity_id=actor.id, field_name="name", value="After migration"),
    )
    persisted_schedule = state_json(updated_state)["scheduled_events"][0]
    assert "repeat_ticks" not in persisted_schedule
    assert "completed_runs" not in persisted_schedule
    assert author_value_hash(old_character["components"]["life"], component_name="life") == author_value_hash(
        new_character["components"]["life"], component_name="life"
    )
    assert author_value_hash(old_group["components"]["population"], component_name="population") == author_value_hash(
        new_group["components"]["population"], component_name="population"
    )
    # Compatibility is component-specific; authored arbitrary keys remain intact.
    assert author_value_hash(
        old_character["components"]["attributes"], component_name="attributes"
    ) == author_value_hash(new_character["components"]["attributes"], component_name="attributes")


def test_unrelated_reducer_event_preserves_absent_resource_field_presence():
    state = _state(explicit_credit_limit=False)
    actor = next(entity for entity in state.entities.values() if entity.kind == "character")
    updated = apply_event(
        state,
        EntityFieldChangedEvent(entity_id=actor.id, field_name="name", value="After commit"),
    )
    account_id = next(iter(updated.resources))
    assert "credit_limit" not in state_json(updated)["resources"][str(account_id)]
    assert "credit_limit" not in updated.resources[account_id].model_fields_set
    reopened = WorldState.model_validate(state_json(updated))
    assert state_hash(reopened) == state_hash(updated)


def test_new_injury_treatment_provenance_defaults_do_not_change_legacy_health_hashes():
    place_id, actor_id, injury_id = uuid4(), uuid4(), uuid4()
    old_state = WorldState.model_validate({"entities": {
        str(place_id): {
            "id": str(place_id), "kind": "place",
            "components": {"place": {"place_type": "settlement"}},
        },
        str(actor_id): {
            "id": str(actor_id), "kind": "character", "location_id": str(place_id),
            "components": {"health": {"current": 80, "maximum": 100, "injuries": [{
                "injury_id": str(injury_id), "injury_type": "core.injury.blunt",
                "severity": 20, "started_tick": 0,
            }]}},
        },
    }})
    expanded_payload = old_state.model_dump(mode="json")
    injury = expanded_payload["entities"][str(actor_id)]["components"]["health"]["injuries"][0]
    injury["treated_event_id"] = None
    injury["treated_tick"] = None
    expanded_state = WorldState.model_validate(expanded_payload)

    assert state_hash(old_state) == state_hash(expanded_state)
    canonical_injury = state_json(expanded_state)["entities"][str(actor_id)]["components"]["health"]["injuries"][0]
    assert "treated_event_id" not in canonical_injury
    assert "treated_tick" not in canonical_injury


def test_new_knowledge_fact_null_default_preserves_legacy_hash_and_state_json():
    actor_id, subject_id = uuid4(), uuid4()
    legacy_record = {
        "id": str(uuid4()),
        "owner_id": str(actor_id),
        "subject_id": str(subject_id),
        "claim": "A recorded observation.",
        "source_event_id": str(uuid4()),
        "kind": "observation",
    }
    entities = {
        actor_id: WorldEntity(id=actor_id, kind="character", name="Observer"),
        subject_id: WorldEntity(id=subject_id, kind="item", name="Evidence"),
    }
    legacy_state = WorldState.model_validate({
        "player_id": actor_id, "entities": entities, "knowledge": [legacy_record],
    })
    expanded_state = WorldState.model_validate({
        "player_id": actor_id, "entities": entities,
        "knowledge": [{**legacy_record, "fact": None}],
    })
    legacy_payload = legacy_state.model_dump(mode="json")
    legacy_payload.pop("investigation_cases", None)
    legacy_payload.pop("relationship_promises", None)
    for item in legacy_payload["knowledge"]:
        item.pop("fact", None)
        item.pop("assessment", None)
        item.pop("disclosure", None)
        item.pop("disclosure_policy", None)
    legacy_digest = hashlib.sha256(
        json.dumps(
            legacy_payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    assert state_hash(legacy_state) == legacy_digest
    assert state_hash(expanded_state) == legacy_digest
    assert "fact" not in state_json(expanded_state)["knowledge"][0]
    assert "assessment" not in state_json(expanded_state)["knowledge"][0]
    assert "disclosure" not in state_json(expanded_state)["knowledge"][0]
    assert "disclosure_policy" not in state_json(expanded_state)["knowledge"][0]

    appended = KnowledgeRecord(
        id=uuid4(), owner_id=actor_id, subject_id=subject_id,
        claim="Another observation.", kind="observation", source_event_id=uuid4(),
    )
    updated = apply_event(expanded_state, KnowledgeAcquiredEvent(record=appended))
    assert all(
        "fact" not in item and "assessment" not in item
        and "disclosure" not in item and "disclosure_policy" not in item
        for item in state_json(updated)["knowledge"]
    )
