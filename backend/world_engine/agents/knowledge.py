"""Build an NPC's bounded, authorized view of world knowledge."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from world_engine.domain.models import WorldEntity, WorldState


@dataclass(frozen=True, slots=True)
class AgentVisibleEntity:
    """Only fields that an ordinary character can observe in a local scene."""

    id: UUID
    kind: str
    name: str | None
    location_id: UUID | None
    public_description: str | None
    appearance: str | None
    public_role: str | None


@dataclass(frozen=True, slots=True)
class AgentKnowledge:
    """Facts available to one actor at one tick.

    The view is intentionally not a projection of the full event log. It contains
    the actor's own records, public records, explicitly shared records, and a
    small set of public entities in the actor's current place.
    """

    actor_id: UUID
    tick: int
    known_records: tuple[object, ...]
    known_relations: tuple[object, ...]
    active_goals: tuple[object, ...]
    visible_entities: tuple[AgentVisibleEntity, ...]
    known_place_ids: frozenset[UUID]


def build_agent_knowledge(state: WorldState, actor_id: UUID, *, tick: int | None = None) -> AgentKnowledge:
    """Return only knowledge available to ``actor_id`` by ``tick``.

    Private knowledge belongs solely to its recorded holder or owner. Public
    records and records naming the actor as an allowed subject are shareable.
    Future-dated claims are excluded even if present in an imported snapshot.
    """

    actor = state.entities.get(actor_id)
    if actor is None or actor.kind not in {"character", "person"}:
        raise ValueError(f"Agent {actor_id} is not an existing character.")
    at_tick = state.current_tick if tick is None else tick
    if at_tick < state.current_tick:
        raise ValueError("Agent knowledge cannot be viewed before the current world tick.")

    records = tuple(
        record
        for record in state.knowledge
        if record.received_tick <= at_tick
        and (record.valid_from_tick is None or record.valid_from_tick <= at_tick)
        and record.visibility.policy != "author_only"
        and (
            actor_id in {record.owner_id, record.holder_id}
            or record.visibility.policy == "public"
            or (record.visibility.policy == "subject_ids" and actor_id in record.visibility.subject_ids)
        )
    )
    relations = tuple(
        record
        for record in state.relations
        if record.updated_tick <= at_tick
        and (
            record.visibility.policy == "public"
            or (record.visibility.policy == "subject_ids" and actor_id in record.visibility.subject_ids)
        )
    )
    goals = tuple(
        goal
        for goal in state.goals
        if goal.owner_id == actor_id and goal.created_tick <= at_tick and goal.status == "active"
    )
    known_places = frozenset(
        UUID(str(value)) for value in actor.components.get("known_places", [])
    )

    # Same-place visibility is a deliberately conservative physical observation.
    # Hidden entities remain unknown unless the actor's typed knowledge has an
    # explicit reference to them; global entity listings are never exposed.
    visible: list[AgentVisibleEntity] = []
    for entity in state.entities.values():
        if entity.id == actor_id or entity.kind == "shipment" or entity.status in {"dead", "retired"}:
            continue
        if actor.location_id is None or entity.location_id != actor.location_id:
            continue
        if entity.components.get("hidden") is True and not any(
            record.subject_id == entity.id and record.kind == "observation"
            and actor_id in {record.owner_id, record.holder_id}
            for record in records
        ):
            continue
        visible.append(
            AgentVisibleEntity(
                id=entity.id,
                kind=entity.kind,
                name=entity.name,
                location_id=entity.location_id,
                public_description=_public_text(entity, "public_description"),
                appearance=_public_text(entity, "appearance"),
                public_role=_public_text(entity, "public_role"),
            )
        )

    visible.sort(key=lambda entity: (entity.kind, entity.name or "", str(entity.id)))
    return AgentKnowledge(
        actor_id=actor_id,
        tick=at_tick,
        known_records=records,
        known_relations=relations,
        active_goals=goals,
        visible_entities=tuple(visible),
        known_place_ids=known_places,
    )


def _public_text(entity: WorldEntity, key: str) -> str | None:
    value = entity.components.get(key)
    return value if isinstance(value, str) else None
