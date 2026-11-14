"""Conservative player projections, shared by UI and model-context construction.

Unknown fields are private by default. Author data is never sent to a model and
then merely hidden in the UI. Callers must authorize actor control separately.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


PUBLIC_COMPONENTS = frozenset({"public_description", "appearance", "public_role"})
SELF_COMPONENTS = PUBLIC_COMPONENTS | frozenset(
    {"health", "needs", "attributes", "capabilities", "progression", "known_places"}
)


class ProjectionError(ValueError):
    pass


def _owned(item: dict[str, Any], actor_id: str) -> bool:
    return item.get("owner_id") == actor_id or item.get("holder_id") == actor_id


def _visible(item: dict[str, Any], actor_id: str) -> bool:
    visibility = item.get("visibility", {})
    if not isinstance(visibility, dict):
        return False
    return visibility.get("policy") == "public" or actor_id in visibility.get("subject_ids", [])


def project_player_state(state: dict[str, Any], actor_id: str) -> dict[str, Any]:
    """Create an isolated allowlisted view; never mutate the authoritative state.

    Private records have to be converted into observer-owned knowledge by the
    observation system. Knowing a character's name does not reveal their present
    location, inventory, private goals or mental state.
    """
    entities = state.get("entities", {})
    actor = entities.get(actor_id)
    if not actor or actor.get("kind") != "character":
        raise ProjectionError("Actor is not a character in this world.")
    location = actor.get("location_id")
    known_places = set(actor.get("components", {}).get("known_places", []))
    if location:
        known_places.add(location)

    projected_entities: dict[str, Any] = {}
    for entity_id, entity in entities.items():
        own = entity_id == actor_id
        place = entity.get("kind") == "place" and entity_id in known_places
        present = bool(location) and entity.get("location_id") == location
        # Hidden entities require an observation/reveal event before inclusion.
        if not own and entity.get("components", {}).get("hidden", False):
            continue
        if not (own or place or present):
            continue
        allowed = SELF_COMPONENTS if own else PUBLIC_COMPONENTS
        entry = {key: deepcopy(entity[key]) for key in ("id", "kind", "name") if key in entity}
        if own or present or entity_id == location:
            entry.update({key: deepcopy(entity[key]) for key in ("location_id", "status") if key in entity})
        entry["components"] = {
            key: deepcopy(value)
            for key, value in entity.get("components", {}).items()
            if key in allowed
        }
        projected_entities[entity_id] = entry

    knowledge = [
        {key: deepcopy(item[key]) for key in (
            "id", "claim", "summary", "confidence", "source_refs", "received_tick", "kind"
        ) if key in item}
        for item in state.get("knowledge", [])
        if _owned(item, actor_id)
    ]
    goals = [
        {key: deepcopy(item[key]) for key in (
            "id", "title", "description", "status", "deadline", "progress", "owner_id"
        ) if key in item}
        for item in state.get("goals", [])
        if _owned(item, actor_id)
    ]
    relations = [
        {key: deepcopy(item[key]) for key in (
            "id", "from_id", "to_id", "dimensions", "label", "evidence_refs"
        ) if key in item}
        for item in state.get("relations", [])
        if item.get("from_id") == actor_id
    ]
    resources = {
        account_id: {key: deepcopy(account[key]) for key in (
            "resource_id", "balance", "owner_id", "name"
        ) if key in account}
        for account_id, account in state.get("resources", {}).items()
        if account.get("owner_id") == actor_id
    }
    return {
        "entities": projected_entities,
        "resources": resources,
        "knowledge": knowledge,
        "goals": goals,
        "relations": relations,
        "player_id": actor_id,
        "calendar": deepcopy(state.get("calendar", {})),
        # Author metadata, event queues and other actors' plans stay private.
        "metadata": {key: deepcopy(state.get("metadata", {})[key]) for key in (
            "pack_id", "public_title", "public_description"
        ) if key in state.get("metadata", {})},
    }


def project_event(event: dict[str, Any], actor_id: str) -> dict[str, Any] | None:
    """Use a precomputed audience-safe payload; do not expose raw event payloads."""
    if not _visible(event, actor_id):
        return None
    views = event.get("audience_views", {})
    payload = views.get(actor_id, views.get("public"))
    if payload is None:
        # Visible existence alone does not authorize every field in an event.
        return None
    return {
        key: deepcopy(event[key])
        for key in ("event_id", "type", "tick", "sequence")
        if key in event
    } | {"payload": deepcopy(payload)}
