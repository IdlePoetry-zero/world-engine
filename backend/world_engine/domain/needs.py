"""Shared deterministic passive-need arithmetic for rules and reducers."""

from __future__ import annotations

from dataclasses import dataclass

from .components import NeedValue, NeedsComponent


PASSIVE_NEED_RATES_PER_DAY: dict[str, int] = {"food": 18, "water": 30, "rest": 24}
DEPRIVATION_NEED_RATES_PER_DAY: dict[str, int] = {"food": 18, "water": 30}
FOOD_RESOURCE_SUFFIXES = frozenset({"food", "grain", "meal", "bread", "rice", "wheat", "ration", "fish", "meat"})
WATER_RESOURCE_SUFFIXES = frozenset({"water", "fresh_water", "drink"})


def resource_matches_need(metadata: dict[str, object], resource_id: str, need: str) -> bool:
    """Match a pack-registered need resource while retaining legacy suffixes."""
    if need not in {"food", "water"}:
        return False
    roles = metadata.get("resource_roles")
    registered = roles.get(f"{need}_resource_id") if isinstance(roles, dict) else None
    if registered == resource_id:
        return True
    suffix = resource_id.rsplit(".", 1)[-1].casefold()
    return suffix in (FOOD_RESOURCE_SUFFIXES if need == "food" else WATER_RESOURCE_SUFFIXES)


@dataclass(frozen=True, slots=True)
class PassiveNeedAdvance:
    needs: NeedsComponent
    changed: bool


def advance_passive_needs(
    needs: NeedsComponent,
    *,
    from_tick: int,
    to_tick: int,
    day_seconds: int,
    action: str = "continue",
    restore: tuple[str, int] | None = None,
) -> PassiveNeedAdvance:
    """Advance food, water and fatigue without applying injury or death.

    The interval must end no later than the first full day of deprivation
    damage. The caller resolves that boundary before allowing further time.
    """
    if to_tick <= from_tick or day_seconds <= 0:
        raise ValueError("passive needs require a positive interval and day length")
    if action not in {"continue", "eat", "drink", "rest"}:
        raise ValueError("background need action is not registered")
    duration = to_tick - from_tick
    updates: dict[str, NeedValue] = {}
    rates = dict(PASSIVE_NEED_RATES_PER_DAY)
    if action == "rest":
        rates["rest"] = -36
    for name, rate in rates.items():
        current = getattr(needs, name)
        if current is None:
            continue
        base_value = current.value
        base_remainder = current.decay_remainder
        last_satisfied_tick = current.last_satisfied_tick
        marker = current.last_critical_damage_tick
        if restore is not None and restore[0] == name:
            if restore[1] <= 0:
                raise ValueError("background need restoration must be positive")
            base_value = min(100, base_value + restore[1])
            base_remainder = 0
            last_satisfied_tick = from_tick
            if base_value >= current.critical_below:
                marker = None
        numerator = rate * duration + base_remainder
        change = _trunc_div(numerator, day_seconds)
        remainder = numerator - change * day_seconds
        value = max(0, min(100, base_value - change))
        if name in DEPRIVATION_NEED_RATES_PER_DAY:
            if value < current.critical_below:
                if base_value < current.critical_below:
                    if marker is None:
                        marker = from_tick
                else:
                    marker = _critical_crossing_tick(
                        current.model_copy(update={"value": base_value, "decay_remainder": base_remainder}),
                        rate_per_day=rate,
                        from_tick=from_tick,
                        day_seconds=day_seconds,
                    )
            else:
                marker = None
        # An advanced need always increments its revision, even when the
        # clamped value and the other fields are unchanged.
        updates[name] = current.model_copy(
            update={
                "value": value,
                "decay_remainder": remainder,
                "last_satisfied_tick": last_satisfied_tick,
                "last_critical_damage_tick": marker,
                "revision": current.revision + 1,
            }
        )

    if not updates:
        return PassiveNeedAdvance(needs=needs, changed=False)
    advanced = needs.model_copy(update={**updates, "revision": needs.revision + 1})
    return PassiveNeedAdvance(needs=advanced, changed=True)


def next_deprivation_damage_tick(needs: NeedsComponent, *, from_tick: int, day_seconds: int) -> int | None:
    """Find the first exact whole-day deprivation-damage boundary in the future."""
    if day_seconds <= 0:
        raise ValueError("day length must be positive")
    boundaries: list[int] = []
    for name, rate in DEPRIVATION_NEED_RATES_PER_DAY.items():
        current = getattr(needs, name)
        if current is None:
            continue
        if current.value < current.critical_below:
            critical_since = current.last_critical_damage_tick
            if critical_since is None:
                critical_since = from_tick
        else:
            critical_since = _critical_crossing_tick(
                current,
                rate_per_day=rate,
                from_tick=from_tick,
                day_seconds=day_seconds,
            )
        boundary = critical_since + day_seconds
        if boundary <= 2**63 - 1:
            boundaries.append(boundary)
    return min(boundaries) if boundaries else None


def next_need_threshold_tick(
    need: NeedValue,
    *,
    threshold: int,
    rate_per_day: int,
    from_tick: int,
    day_seconds: int,
) -> int:
    """Find the first tick when passive decay makes a need value <= threshold.

    The retained decay remainder is included so policy boundaries agree with
    the authoritative per-action need integration, including non-divisible
    rates and short windows.
    """

    if day_seconds <= 0 or rate_per_day <= 0:
        raise ValueError("need thresholds require positive rate and day length")
    if not 0 <= threshold <= 100:
        raise ValueError("need threshold must be within 0..100")
    if need.value <= threshold:
        return from_tick
    points_to_drop = need.value - threshold
    numerator = points_to_drop * day_seconds - need.decay_remainder
    duration = max(1, (numerator + rate_per_day - 1) // rate_per_day)
    return min(2**63 - 1, from_tick + duration)


def _critical_crossing_tick(current: NeedValue, *, rate_per_day: int, from_tick: int, day_seconds: int) -> int:
    """First integer tick at which decay puts a need strictly below its limit."""
    points_to_drop = current.value - current.critical_below + 1
    if points_to_drop <= 0:
        return from_tick
    numerator = points_to_drop * day_seconds - current.decay_remainder
    if numerator <= 0:
        return from_tick
    offset = (numerator + rate_per_day - 1) // rate_per_day
    if from_tick + offset > 2**63 - 1:
        return 2**63 - 1
    return from_tick + offset


def _trunc_div(numerator: int, denominator: int) -> int:
    """Integer division truncated toward zero, matching the action rules."""
    quotient = abs(numerator) // denominator
    return -quotient if numerator < 0 else quotient
