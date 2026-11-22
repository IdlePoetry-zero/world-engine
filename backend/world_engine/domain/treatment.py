"""Registered one-injury treatment policy shared by action rules and reducers."""

from __future__ import annotations

from decimal import Decimal
from typing import Final
from uuid import UUID

from .components import PlaceComponent, SkillsComponent
from .models import WorldEntity, WorldState


INJURY_TREATMENT_POLICY: Final = "injury.treatment.v1"
INJURY_TREATMENT_DURATION: Final = 1_800
INJURY_TREATMENT_HEALING: Final = 5
INJURY_TREATMENT_MIN_CARE: Final = Decimal("10")
MEDICINE_RESOURCE_SUFFIXES: Final = frozenset({"medicine", "medication", "herbs", "herb"})
SETTLED_PLACE_TYPES: Final = frozenset({"settlement", "building"})


def care_skill(entity: WorldEntity) -> tuple[int, Decimal] | None:
    """Return the typed skill-set revision and skill.care level, if registered."""
    raw = entity.components.get("skills")
    if raw is None:
        return None
    try:
        skills = SkillsComponent.model_validate(raw)
    except Exception:
        return None
    care = skills.skills.get("skill.care")
    if care is None:
        return None
    return skills.revision, care.level


def is_medicine_resource(resource_id: str) -> bool:
    """Match the suffix-based resource family already accepted by ConsumeIntent."""
    return resource_id.rsplit(".", 1)[-1].casefold() in MEDICINE_RESOURCE_SUFFIXES


def settled_treatment_place(state: WorldState, place_id: UUID) -> PlaceComponent | None:
    place = state.entities.get(place_id)
    if place is None or place.kind not in {"place", "location"} or place.status in {"dead", "retired"}:
        return None
    try:
        component = PlaceComponent.model_validate(place.components.get("place", {}))
    except Exception:
        return None
    return component if component.place_type in SETTLED_PLACE_TYPES else None
