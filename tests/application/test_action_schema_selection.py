from __future__ import annotations

import json
from copy import deepcopy
from uuid import uuid4

from jsonschema import Draft202012Validator

from world_engine.application.action_schema_selection import narrow_action_proposal_schema
from world_engine.application.actions import _ActionProposalSchemaGenerator, _build_action_proposal_schema
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER


def _full_schema():
    return _build_action_proposal_schema(
        ACTION_INTENT_ADAPTER, schema_generator=_ActionProposalSchemaGenerator,
    )


def _bytes(schema):
    return len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _move_scene(*names):
    return {"routes": [
        {"route_id": str(uuid4()), "to_place_id": str(uuid4()), "destination_name": name}
        for name in names
    ]}


def _selected_kinds(schema):
    action = schema["properties"]["action"]["anyOf"]
    adapter = next(item for item in action if "oneOf" in item)
    return adapter["discriminator"]["mapping"], adapter["oneOf"], action


def _referenced_definitions(value):
    refs = set()
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            refs.add(reference.split("/", 2)[2].replace("~1", "/").replace("~0", "~"))
        for child in value.values():
            refs.update(_referenced_definitions(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(_referenced_definitions(child))
    return refs


def test_clear_unique_move_narrows_schema_and_preserves_null_clarification():
    full = _full_schema()
    narrowed = narrow_action_proposal_schema(full, "前往 Harbor", _move_scene("Harbor"))

    mapping, variants, action_options = _selected_kinds(narrowed)
    assert mapping == {"move": "#/$defs/MoveIntent"}
    assert variants == [{"$ref": "#/$defs/MoveIntent"}]
    assert {item.get("type") for item in action_options} >= {"null"}
    assert "MoveIntent" in narrowed["$defs"]
    assert set(narrowed["$defs"]) == _referenced_definitions(narrowed)

    Draft202012Validator.check_schema(narrowed)
    validator = Draft202012Validator(narrowed)
    assert validator.is_valid({"action": {"kind": "move", "destination_id": str(uuid4())}, "clarification": ""})
    assert validator.is_valid({"action": None, "clarification": "请补充目的地。"})
    assert not validator.is_valid({"action": {"kind": "trade", "listing_id": str(uuid4()), "quantity": "1"}, "clarification": ""})

    before, after = _bytes(full), _bytes(narrowed)
    assert after < before
    assert after <= before * 0.02


def test_schema_pruning_keeps_transitive_nested_refs_of_selected_variant():
    schema = {
        "type": "object",
        "properties": {
            "action": {"anyOf": [
                {
                    "discriminator": {
                        "propertyName": "kind",
                        "mapping": {"move": "#/$defs/Move", "wait": "#/$defs/Wait"},
                    },
                    "oneOf": [{"$ref": "#/$defs/Move"}, {"$ref": "#/$defs/Wait"}],
                },
                {"type": "null"},
            ]},
            "clarification": {"type": "string"},
        },
        "$defs": {
            "Move": {"type": "object", "properties": {"options": {"$ref": "#/$defs/MoveOptions"}}},
            "MoveOptions": {"type": "object", "properties": {"destination": {"$ref": "#/$defs/PlaceName"}}},
            "PlaceName": {"type": "string"},
            "Wait": {"type": "object"},
            "Unused": {"type": "object"},
        },
    }

    narrowed = narrow_action_proposal_schema(schema, "go to Harbor", _move_scene("Harbor"))

    assert set(narrowed["$defs"]) == {"Move", "MoveOptions", "PlaceName"}
    assert _referenced_definitions(narrowed) == set(narrowed["$defs"])
    Draft202012Validator.check_schema(narrowed)


def test_explicit_explore_requires_a_visible_current_place_and_no_travel():
    schema = _full_schema()
    scene = {
        "player_id": "player",
        "own_active_travel": None,
        "entities": {
            "player": {"location_id": "harbor"},
            "harbor": {"kind": "place", "name": "Harbor"},
        },
    }

    narrowed = narrow_action_proposal_schema(schema, "搜索当前地点", scene)

    mapping, variants, _ = _selected_kinds(narrowed)
    assert mapping == {"explore": "#/$defs/ExploreIntent"}
    assert variants == [{"$ref": "#/$defs/ExploreIntent"}]
    assert {item.get("type") for item in narrowed["properties"]["action"]["anyOf"]} >= {"null"}

    traveling = deepcopy(scene)
    traveling["own_active_travel"] = {"status": "in_transit"}
    assert narrow_action_proposal_schema(schema, "搜索当前地点", traveling) is schema


def test_ambiguous_compound_out_of_context_or_unanchored_text_keeps_full_schema():
    schema = _full_schema()
    cases = (
        ("请解释如何前往 Harbor", _move_scene("Harbor")),
        ("去 Harbor 然后休息", _move_scene("Harbor")),
        ("前往 Harbor", _move_scene("Harbor", "Harbor")),
        ("前往 Harbor", _move_scene()),
        ("移动", _move_scene("Harbor")),
        ("搜索当前地点并与商人交谈", {
            "player_id": "player", "own_active_travel": None,
            "entities": {"player": {"location_id": "harbor"}, "harbor": {"kind": "place"}},
        }),
    )
    for text, scene in cases:
        assert narrow_action_proposal_schema(schema, text, scene) is schema
    assert narrow_action_proposal_schema(schema, "前往 Harbor", {}) is schema
