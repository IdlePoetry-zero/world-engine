"""Versioned, deterministic combat policy helpers shared by rules and reducers."""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from uuid import UUID

from .components import AttributesComponent, CombatEncounterComponent, SkillsComponent
from .models import WorldEntity, WorldState


COMBAT_ATTACK_POLICY = "combat.attack.v1"
COMBAT_ATTACK_ROLL_LABEL = "combat.attack"
COMBAT_EXCHANGE_DURATION = 10


def combat_roll_digest(
    *, world_id: UUID, branch_id: UUID, command_id: UUID, ruleset_version: str, label: str,
) -> str:
    """Match the stable command-scoped roll derivation used by RuleEngine._roll."""
    material = ":".join(
        (str(world_id), str(branch_id), str(command_id), ruleset_version, label)
    ).encode("utf-8")
    return sha256(material).hexdigest()


def combat_roll_value(digest: str, maximum: int) -> int:
    return int(digest, 16) % maximum + 1


def combat_skill_level(entity: WorldEntity, skill_ids: tuple[str, ...]) -> Decimal:
    raw = entity.components.get("skills")
    if raw is None:
        return Decimal("0")
    try:
        skills = SkillsComponent.model_validate(raw)
    except Exception:
        return Decimal("0")
    return max((skills.skills[item].level for item in skill_ids if item in skills.skills), default=Decimal("0"))


def combat_attribute_int(entity: WorldEntity, attribute_id: str, default: int = 0) -> int:
    raw = entity.components.get("attributes")
    if raw is None:
        return default
    try:
        attributes = AttributesComponent.model_validate(raw)
        value = attributes.values.get(attribute_id, default)
        if isinstance(value, bool):
            return default
        number = Decimal(str(value))
    except Exception:
        return default
    if not number.is_finite():
        return default
    return max(-100, min(100, int(number)))


def combat_attack_chance(actor: WorldEntity, target: WorldEntity) -> int:
    attack_skill = max(
        combat_skill_level(actor, ("skill.combat", "skill.melee", "skill.accuracy")),
        Decimal("0"),
    )
    defense_skill = max(
        combat_skill_level(target, ("skill.defense", "skill.evasion")),
        Decimal("0"),
    )
    strength = combat_attribute_int(actor, "attribute.strength", 0)
    agility = combat_attribute_int(target, "attribute.agility", 0)
    return max(
        5,
        min(95, 55 + int(attack_skill // Decimal("2")) + strength - int(defense_skill // Decimal("2")) - agility),
    )


def combat_damage(actor: WorldEntity) -> int:
    attack_skill = max(
        combat_skill_level(actor, ("skill.combat", "skill.melee", "skill.accuracy")),
        Decimal("0"),
    )
    strength = combat_attribute_int(actor, "attribute.strength", 0)
    return max(
        1,
        min(1_000_000, 5 + int(attack_skill // Decimal("20")) + max(-4, min(15, strength // 5))),
    )


def combat_death_protection_enabled(state: WorldState, target: WorldEntity) -> bool:
    capabilities = target.components.get("capabilities")
    return (
        target.id != state.player_id
        and target.status == "active"
        and isinstance(capabilities, dict)
        and capabilities.get("combat_death_protection") is True
    )


def active_combat_opponent(state: WorldState, actor: WorldEntity) -> tuple[CombatEncounterComponent, WorldEntity] | None:
    """Return a live, symmetric, co-located encounter; ignore stale closed-off threats."""
    raw = actor.components.get("combat_encounter")
    if raw is None:
        return None
    try:
        encounter = CombatEncounterComponent.model_validate(raw)
    except Exception:
        return None
    if encounter.status != "active" or actor.location_id != encounter.location_id:
        return None
    opponent = state.entities.get(encounter.opponent_id)
    if (
        opponent is None
        or opponent.kind not in {"character", "person"}
        or opponent.status != "active"
        or opponent.location_id != encounter.location_id
    ):
        return None
    try:
        other = CombatEncounterComponent.model_validate(opponent.components.get("combat_encounter"))
    except Exception:
        return None
    if (
        other.status != "active"
        or other.encounter_id != encounter.encounter_id
        or other.opponent_id != actor.id
        or other.location_id != encounter.location_id
        or other.started_tick != encounter.started_tick
        or other.exchange_count != encounter.exchange_count
        or other.last_event_id != encounter.last_event_id
        or other.last_damage_event_id != encounter.last_damage_event_id
    ):
        return None
    return encounter, opponent
