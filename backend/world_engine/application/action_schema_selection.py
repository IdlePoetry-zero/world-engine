"""Conservatively select a smaller action schema for clear, scene-bound commands."""

from __future__ import annotations

import copy
import re
import unicodedata


_MAX_COMMAND_CHARACTERS = 80
_MOVE_COMMAND = re.compile(r"^(?:请)?(?:现在)?(?:前往|去|走到|移动到)\s*(?P<target>[^，,。.!?？;；]+)$")
_MOVE_COMMAND_EN = re.compile(r"^(?:please\s+)?(?:move\s+to|go\s+to)\s+(?P<target>[^,.;!?]+)$", re.IGNORECASE)
_EXPLORE_COMMANDS = (
    re.compile(r"^(?:请)?(?:搜索|搜查|探索|调查)(?:一下)?(?:当前地点|目前地点|这里|此地)$"),
    re.compile(r"^(?:请)?(?:在)?(?:当前地点|目前地点|这里|此地)(?:进行)?(?:搜索|搜查|探索|调查)(?:一下)?$"),
)
_COMPOUND_MARKERS = ("然后", "并且", "顺便", "同时", "之后", "或者", "还是", "再去")
_NO_SCENE = object()


def narrow_action_proposal_schema(full_schema: dict, player_text: str, scene: dict) -> dict:
    """Return a copy narrowed to one clear move/explore action, else ``full_schema``.

    Narrowing is enabled only for short, anchored commands that identify one
    opportunity in the provided player scene. Questions, roleplay/meta text,
    compound commands, missing context, unmatched names, and duplicate matches
    keep the original schema. The top-level ``action: null`` option remains so
    the model can still ask for clarification.
    """
    if not isinstance(full_schema, dict) or not isinstance(scene, dict) or not scene:
        return full_schema
    if not isinstance(player_text, str):
        return full_schema

    command = unicodedata.normalize("NFKC", player_text).strip()
    if not command or len(command) > _MAX_COMMAND_CHARACTERS:
        return full_schema
    command = command.rstrip("。.!！ ")
    if not command or any(marker in command for marker in _COMPOUND_MARKERS):
        return full_schema

    intent = _scene_bound_intent(command, scene)
    if intent is None:
        return full_schema
    return _narrow_schema(full_schema, intent)


def _scene_bound_intent(command: str, scene: dict) -> str | None:
    match = _MOVE_COMMAND.fullmatch(command) or _MOVE_COMMAND_EN.fullmatch(command)
    if match is not None:
        target = _normalize_name(match.group("target"))
        if not target:
            return None
        routes = scene.get("routes")
        if not isinstance(routes, list):
            return None
        matches = [
            route for route in routes
            if isinstance(route, dict)
            and isinstance(route.get("destination_name"), str)
            and _normalize_name(route["destination_name"]) == target
            and isinstance(route.get("to_place_id"), str)
            and isinstance(route.get("route_id"), str)
        ]
        return "move" if len(matches) == 1 else None

    if not any(pattern.fullmatch(command) for pattern in _EXPLORE_COMMANDS):
        return None
    if scene.get("own_active_travel", _NO_SCENE) is not None:
        return None
    player_id = scene.get("player_id")
    entities = scene.get("entities")
    if not isinstance(player_id, str) or not isinstance(entities, dict):
        return None
    actor = entities.get(player_id)
    if not isinstance(actor, dict):
        return None
    location_id = actor.get("location_id")
    place = entities.get(location_id) if isinstance(location_id, str) else None
    if not isinstance(place, dict) or place.get("kind") not in {"place", "location"}:
        return None
    return "explore"


def _narrow_schema(full_schema: dict, intent: str) -> dict:
    properties = full_schema.get("properties")
    action = properties.get("action") if isinstance(properties, dict) else None
    branches = action.get("anyOf") if isinstance(action, dict) else None
    if not isinstance(branches, list):
        return full_schema
    null_branches = [branch for branch in branches if isinstance(branch, dict) and branch.get("type") == "null"]
    adapters = [
        branch for branch in branches
        if isinstance(branch, dict)
        and isinstance(branch.get("discriminator"), dict)
        and isinstance(branch.get("discriminator", {}).get("mapping"), dict)
        and isinstance(branch.get("oneOf"), list)
    ]
    if len(null_branches) != 1 or len(adapters) != 1:
        return full_schema

    adapter = adapters[0]
    discriminator = adapter["discriminator"]
    mapping = discriminator["mapping"]
    selected_ref = mapping.get(intent)
    if not isinstance(selected_ref, str):
        return full_schema
    selected_variants = [
        variant for variant in adapter["oneOf"]
        if isinstance(variant, dict) and variant.get("$ref") == selected_ref
    ]
    definitions = full_schema.get("$defs")
    definition_name = _local_definition_name(selected_ref)
    if len(selected_variants) != 1 or not isinstance(definitions, dict) or definition_name not in definitions:
        return full_schema

    narrowed = copy.deepcopy(full_schema)
    narrowed_action = narrowed["properties"]["action"]
    narrowed_adapter = copy.deepcopy(adapter)
    narrowed_discriminator = copy.deepcopy(discriminator)
    narrowed_discriminator["mapping"] = {intent: selected_ref}
    narrowed_adapter["discriminator"] = narrowed_discriminator
    narrowed_adapter["oneOf"] = [copy.deepcopy(selected_variants[0])]
    narrowed_action["anyOf"] = [narrowed_adapter, copy.deepcopy(null_branches[0])]

    if not _prune_unreachable_definitions(narrowed):
        return full_schema
    return narrowed


def _prune_unreachable_definitions(schema: dict) -> bool:
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return True

    root = {key: value for key, value in schema.items() if key != "$defs"}
    needed = _local_refs(root)
    pending = list(needed)
    visited = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        if name not in definitions:
            return False
        visited.add(name)
        nested = _local_refs(definitions[name]) - visited
        pending.extend(nested)

    if visited:
        schema["$defs"] = {name: definition for name, definition in definitions.items() if name in visited}
    else:
        schema.pop("$defs", None)
    return True


def _local_refs(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str):
            name = _local_definition_name(reference)
            if name is not None:
                found.add(name)
        for child in value.values():
            found.update(_local_refs(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_local_refs(child))
    return found


def _local_definition_name(reference: str) -> str | None:
    match = re.fullmatch(r"#/\$defs/([^/]+)(?:/.*)?", reference)
    if match is None:
        return None
    return match.group(1).replace("~1", "/").replace("~0", "~")


def _normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
