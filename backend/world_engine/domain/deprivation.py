"""Deterministic, replayable NPC deprivation consequences."""

from __future__ import annotations

from dataclasses import dataclass

from .components import NeedValue, NeedsComponent


@dataclass(frozen=True, slots=True)
class DeprivationResolution:
    needs: NeedsComponent
    damage: int
    changed: bool


def resolve_due_deprivation(
    needs: NeedsComponent,
    *,
    at_tick: int,
    day_seconds: int,
) -> DeprivationResolution:
    """Apply accumulated whole-day deprivation damage and advance its markers."""
    if day_seconds <= 0:
        raise ValueError("day length must be positive")
    updates: dict[str, NeedValue] = {}
    total_damage = 0
    for name in ("food", "water"):
        current = getattr(needs, name)
        if current is None:
            continue
        marker = current.last_critical_damage_tick
        if current.value >= current.critical_below:
            if marker is not None:
                updates[name] = current.model_copy(
                    update={"last_critical_damage_tick": None, "revision": current.revision + 1}
                )
            continue
        if marker is None:
            continue
        days = (at_tick - marker) // day_seconds
        if days <= 0:
            continue
        updates[name] = current.model_copy(
            update={
                "last_critical_damage_tick": marker + days * day_seconds,
                "revision": current.revision + 1,
            }
        )
        total_damage += days
    if not updates:
        return DeprivationResolution(needs=needs, damage=min(total_damage, 1_000_000), changed=False)
    advanced = needs.model_copy(update={**updates, "revision": needs.revision + 1})
    return DeprivationResolution(needs=advanced, damage=min(total_damage, 1_000_000), changed=True)


def resolve_deprivation(
    needs: NeedsComponent,
    *,
    at_tick: int,
    day_seconds: int,
) -> DeprivationResolution:
    """Consume all complete critical food/water days accumulated at `at_tick`."""
    if day_seconds <= 0:
        raise ValueError("day length must be positive")
    updates: dict[str, NeedValue] = {}
    total_damage = 0
    for name in ("food", "water"):
        current = getattr(needs, name)
        if current is None:
            continue
        marker = current.last_critical_damage_tick
        if current.value >= current.critical_below:
            if marker is not None:
                updates[name] = current.model_copy(
                    update={"last_critical_damage_tick": None, "revision": current.revision + 1}
                )
            continue
        if marker is None:
            # A legacy state may first be observed below the limit; it must spend
            # one full world-day there before its first deprivation consequence.
            continue
        days = (at_tick - marker) // day_seconds
        if days <= 0:
            continue
        next_marker = marker + days * day_seconds
        updates[name] = current.model_copy(
            update={"last_critical_damage_tick": next_marker, "revision": current.revision + 1}
        )
        total_damage += days

    if not updates:
        return DeprivationResolution(needs=needs, damage=min(total_damage, 1_000_000), changed=False)
    next_needs = needs.model_copy(update={**updates, "revision": needs.revision + 1})
    return DeprivationResolution(needs=next_needs, damage=min(total_damage, 1_000_000), changed=True)
