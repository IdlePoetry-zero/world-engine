"""Pure, bounded simulation of recurring NPC need care inside one window."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from .components import (
    DailyRoutineComponent,
    InventoryComponent,
    NeedValue,
    NeedsComponent,
    ResourceName,
)
from .inventory import FifoInventoryCursor, FifoLotAllocation
from .models import ResourceAccount
from .needs import DEPRIVATION_NEED_RATES_PER_DAY, PASSIVE_NEED_RATES_PER_DAY, resource_matches_need

_CARE_ORDER = ("water", "food")
_NEED_RATE = {"food": 18, "water": 30}
_ROUTINE_MAX_DAYS = 30
_UNIT_QUANTITY = Decimal("1")
_UNIT_RESTORATION = 10


@dataclass(frozen=True, slots=True)
class RoutineCareResult:
    need: str
    resource_id: str
    threshold: int
    action_count: int
    sink_account_id: UUID | None
    lots: tuple[FifoLotAllocation, ...]


@dataclass(frozen=True, slots=True)
class BackgroundRoutineResult:
    needs: NeedsComponent
    inventory: InventoryComponent | None
    care: tuple[RoutineCareResult, ...]
    account_deltas: dict[UUID, Decimal]
    expected_accounts: dict[UUID, ResourceAccount]
    deprivation_boundary: int | None


@dataclass(slots=True)
class _NeedProgress:
    """Mutable scalar working copy; Pydantic values are built once per window."""

    value: int
    decay_remainder: int
    last_satisfied_tick: int | None
    last_critical_damage_tick: int | None
    critical_below: int

    @classmethod
    def from_value(cls, need: NeedValue) -> _NeedProgress:
        return cls(
            value=need.value,
            decay_remainder=need.decay_remainder,
            last_satisfied_tick=need.last_satisfied_tick,
            last_critical_damage_tick=need.last_critical_damage_tick,
            critical_below=need.critical_below,
        )


def advance_background_routine(
    needs: NeedsComponent,
    *,
    inventory: InventoryComponent | None,
    resources: dict[UUID, ResourceAccount],
    metadata: dict[str, object],
    routine: DailyRoutineComponent | None,
    location_id: UUID | None,
    from_tick: int,
    to_tick: int,
    day_seconds: int,
) -> BackgroundRoutineResult:
    """Apply periodic rest and food/water thresholds over ``[from_tick,to_tick)``.

    A care action consumes exactly one whole resource unit. At an equal tick,
    drinking precedes eating. Rest follows the registered daily routine only
    while the character is at that window's place. If stock runs out before a
    deprivation boundary, the returned boundary lets the caller stop and
    resolve that consequence instead of committing it invisibly.
    """
    duration = to_tick - from_tick
    if duration <= 0 or day_seconds <= 0:
        raise ValueError("background routine requires a positive interval and day length")
    if duration > day_seconds * _ROUTINE_MAX_DAYS:
        raise ValueError("background routine windows cannot exceed 30 world days")

    working_needs = {
        name: _NeedProgress.from_value(value)
        for name in ("food", "water", "rest")
        if (value := getattr(needs, name)) is not None
    }
    inventory_cursor = FifoInventoryCursor(inventory) if inventory is not None else None
    care_specs: dict[str, tuple[str, int, UUID | None]] = {}
    care_enabled: dict[str, bool] = {}
    action_counts = {"food": 0, "water": 0}
    aggregate_lots: dict[str, dict[tuple[UUID, UUID], Decimal]] = {"food": {}, "water": {}}
    care_resources = {
        "food": _resource_for_need("food", inventory, metadata),
        "water": _resource_for_need("water", inventory, metadata),
    }
    for need_name in _CARE_ORDER:
        need_value = working_needs.get(need_name)
        resource_id = care_resources[need_name]
        if need_value is None or resource_id is None:
            care_enabled[need_name] = False
            continue
        threshold = min(25, max(0, need_value.critical_below - _UNIT_RESTORATION))
        sink_id = _sink_for_resource(resources, resource_id)
        care_specs[need_name] = (resource_id, threshold, sink_id)
        care_enabled[need_name] = sink_id is not None and inventory_cursor is not None

    rest_windows = tuple(
        window for window in (routine.windows if routine is not None else ())
        if window.activity == "rest" and location_id == window.place_id
    )
    now = from_tick
    boundary_at_end: int | None = None
    while now < to_tick:
        next_tick = to_tick
        for boundary in _next_rest_boundaries(rest_windows, now, to_tick, day_seconds):
            next_tick = min(next_tick, boundary)
        for need_name in _CARE_ORDER:
            if not care_enabled.get(need_name, False):
                continue
            current = working_needs.get(need_name)
            if current is None:
                care_enabled[need_name] = False
                continue
            _resource_id, threshold, _sink_id = care_specs[need_name]
            care_tick = _next_need_threshold_tick(
                current.value,
                current.decay_remainder,
                need_name=need_name,
                threshold=threshold,
                from_tick=now,
                day_seconds=day_seconds,
            )
            next_tick = min(next_tick, care_tick)

        deprivation_boundary = _next_deprivation_boundary(
            working_needs,
            from_tick=now,
            day_seconds=day_seconds,
        )
        if deprivation_boundary is not None and deprivation_boundary <= next_tick:
            if deprivation_boundary < to_tick:
                return _finish(
                    needs,
                    working_needs,
                    inventory_cursor,
                    care_specs,
                    action_counts,
                    aggregate_lots,
                    resources,
                    deprivation_boundary,
                )
            if deprivation_boundary == to_tick:
                boundary_at_end = to_tick

        if next_tick > now:
            _advance_need_progress(
                working_needs,
                from_tick=now,
                to_tick=next_tick,
                day_seconds=day_seconds,
                resting=_resting_at(rest_windows, now, day_seconds),
            )
        now = next_tick
        if now >= to_tick:
            break

        # Deterministic same-tick tie-break: water first, then food.
        for need_name in _CARE_ORDER:
            if not care_enabled.get(need_name, False):
                continue
            current = working_needs.get(need_name)
            if current is None:
                care_enabled[need_name] = False
                continue
            resource_id, threshold, sink_id = care_specs[need_name]
            if current.value > threshold:
                continue
            try:
                if inventory_cursor is None:
                    raise ValueError("no inventory is available")
                allocations = inventory_cursor.consume(
                    resource_id=resource_id,
                    quantity=_UNIT_QUANTITY,
                    at_tick=now,
                )
            except (ValueError, TypeError):
                # No complete unexpired unit remains. Continue passively; the
                # deprivation boundary check above will stop unsafe windows.
                care_enabled[need_name] = False
                continue
            action_counts[need_name] += 1
            lots_for_need = aggregate_lots[need_name]
            for allocation in allocations:
                key = (allocation.lot_id, allocation.account_id)
                lots_for_need[key] = lots_for_need.get(key, Decimal("0")) + allocation.quantity
            current.value = min(100, current.value + _UNIT_RESTORATION)
            current.decay_remainder = 0
            current.last_satisfied_tick = now
            if current.value >= current.critical_below:
                current.last_critical_damage_tick = None

    return _finish(
        needs,
        working_needs,
        inventory_cursor,
        care_specs,
        action_counts,
        aggregate_lots,
        resources,
        boundary_at_end,
    )


def _finish(
    original_needs: NeedsComponent,
    working_needs: dict[str, _NeedProgress],
    inventory_cursor: FifoInventoryCursor | None,
    care_specs: dict[str, tuple[str, int, UUID | None]],
    action_counts: dict[str, int],
    aggregate_lots: dict[str, dict[tuple[UUID, UUID], Decimal]],
    resources: dict[UUID, ResourceAccount],
    boundary: int | None,
) -> BackgroundRoutineResult:
    updated_needs: dict[str, NeedValue] = {}
    for need_name in ("food", "water", "rest"):
        old = getattr(original_needs, need_name)
        progress = working_needs.get(need_name)
        if old is None or progress is None:
            continue
        fields = {
            "value": progress.value,
            "decay_remainder": progress.decay_remainder,
            "last_satisfied_tick": progress.last_satisfied_tick,
            "last_critical_damage_tick": progress.last_critical_damage_tick,
        }
        if any(getattr(old, name) != value for name, value in fields.items()):
            updated_needs[need_name] = old.model_copy(
                update={**fields, "revision": old.revision + 1}
            )
    new_needs = (
        original_needs.model_copy(
            update={**updated_needs, "revision": original_needs.revision + 1}
        )
        if updated_needs
        else original_needs
    )
    final_inventory = inventory_cursor.finish() if inventory_cursor is not None else None

    care_results: list[RoutineCareResult] = []
    account_deltas: dict[UUID, Decimal] = {}
    expected_accounts: dict[UUID, ResourceAccount] = {}
    for need_name in _CARE_ORDER:
        spec = care_specs.get(need_name)
        if spec is None:
            continue
        resource_id, threshold, sink_id = spec
        lot_rows = aggregate_lots[need_name]
        ordered: list[FifoLotAllocation] = []
        for key, quantity in lot_rows.items():
            lot_id, account_id = key
            ordered.append(FifoLotAllocation(lot_id=lot_id, account_id=account_id, quantity=quantity))
            account = resources[account_id]
            expected_accounts[account_id] = account
            account_deltas[account_id] = account_deltas.get(account_id, Decimal("0")) - quantity
        if action_counts[need_name] > 0:
            if sink_id is None:
                raise ValueError("background routine consumed stock without a matching sink")
            sink = resources[sink_id]
            expected_accounts[sink_id] = sink
            quantity = Decimal(action_counts[need_name])
            account_deltas[sink_id] = account_deltas.get(sink_id, Decimal("0")) + quantity
        care_results.append(
            RoutineCareResult(
                need=need_name,
                resource_id=resource_id,
                threshold=threshold,
                action_count=action_counts[need_name],
                sink_account_id=sink_id if action_counts[need_name] else None,
                lots=tuple(ordered),
            )
        )
    return BackgroundRoutineResult(
        needs=new_needs,
        inventory=final_inventory,
        care=tuple(care_results),
        account_deltas=account_deltas,
        expected_accounts=expected_accounts,
        deprivation_boundary=boundary,
    )


def _resource_for_need(
    need: str,
    inventory: InventoryComponent | None,
    metadata: dict[str, object],
) -> str | None:
    roles = metadata.get("resource_roles")
    registered = roles.get(f"{need}_resource_id") if isinstance(roles, dict) else None
    if isinstance(registered, str) and resource_matches_need(metadata, registered, need):
        return registered
    if inventory is None:
        return None
    resources = sorted({lot.resource_id for lot in inventory.lots if resource_matches_need(metadata, lot.resource_id, need)})
    return resources[0] if resources else None


def _sink_for_resource(resources: dict[UUID, ResourceAccount], resource_id: str) -> UUID | None:
    sinks = sorted(
        (
            account_id for account_id, account in resources.items()
            if account.account_type == "consumption_sink" and account.resource_id == resource_id
        ),
        key=str,
    )
    return sinks[0] if sinks else None


def _advance_need_progress(
    needs: dict[str, _NeedProgress],
    *,
    from_tick: int,
    to_tick: int,
    day_seconds: int,
    resting: bool,
) -> None:
    duration = to_tick - from_tick
    rates = PASSIVE_NEED_RATES_PER_DAY
    for name, current in needs.items():
        rate = -36 if name == "rest" and resting else rates[name]
        base_value = current.value
        base_remainder = current.decay_remainder
        numerator = rate * duration + base_remainder
        change = _trunc_div(numerator, day_seconds)
        remainder = numerator - change * day_seconds
        value = max(0, min(100, base_value - change))
        marker = current.last_critical_damage_tick
        if name in DEPRIVATION_NEED_RATES_PER_DAY:
            if value < current.critical_below:
                if base_value < current.critical_below:
                    if marker is None:
                        marker = from_tick
                else:
                    marker = _critical_crossing_tick(
                        base_value,
                        base_remainder,
                        critical_below=current.critical_below,
                        rate_per_day=DEPRIVATION_NEED_RATES_PER_DAY[name],
                        from_tick=from_tick,
                        day_seconds=day_seconds,
                    )
            else:
                marker = None
        current.value = value
        current.decay_remainder = remainder
        current.last_critical_damage_tick = marker


def _next_deprivation_boundary(
    needs: dict[str, _NeedProgress], *, from_tick: int, day_seconds: int
) -> int | None:
    boundaries: list[int] = []
    for name, rate in DEPRIVATION_NEED_RATES_PER_DAY.items():
        current = needs.get(name)
        if current is None:
            continue
        if current.value < current.critical_below:
            critical_since = current.last_critical_damage_tick
            if critical_since is None:
                critical_since = from_tick
        else:
            critical_since = _critical_crossing_tick(
                current.value,
                current.decay_remainder,
                critical_below=current.critical_below,
                rate_per_day=rate,
                from_tick=from_tick,
                day_seconds=day_seconds,
            )
        boundary = critical_since + day_seconds
        if boundary <= 2**63 - 1:
            boundaries.append(boundary)
    return min(boundaries) if boundaries else None


def _critical_crossing_tick(
    value: int,
    decay_remainder: int,
    *,
    critical_below: int,
    rate_per_day: int,
    from_tick: int,
    day_seconds: int,
) -> int:
    points_to_drop = value - critical_below + 1
    if points_to_drop <= 0:
        return from_tick
    numerator = points_to_drop * day_seconds - decay_remainder
    if numerator <= 0:
        return from_tick
    offset = (numerator + rate_per_day - 1) // rate_per_day
    return min(2**63 - 1, from_tick + offset)


def _trunc_div(numerator: int, denominator: int) -> int:
    quotient = abs(numerator) // denominator
    return -quotient if numerator < 0 else quotient


def _next_need_threshold_tick(
    value: int,
    decay_remainder: int,
    *,
    need_name: str,
    threshold: int,
    from_tick: int,
    day_seconds: int,
) -> int:
    if value <= threshold:
        return from_tick
    rate = _NEED_RATE[need_name]
    numerator = (value - threshold) * day_seconds - decay_remainder
    if numerator <= 0:
        return from_tick
    offset = (numerator + rate - 1) // rate
    return min(2**63 - 1, from_tick + offset)


def _resting_at(windows: tuple[object, ...], tick: int, day_seconds: int) -> bool:
    phase = tick % day_seconds
    return any(window.start_second <= phase < window.start_second + window.duration_seconds for window in windows)


def _next_rest_boundaries(
    windows: tuple[object, ...],
    tick: int,
    end_tick: int,
    day_seconds: int,
) -> tuple[int, ...]:
    if not windows:
        return ()
    day_index = tick // day_seconds
    boundaries: set[int] = set()
    for current_day in (day_index, day_index + 1):
        base = current_day * day_seconds
        for window in windows:
            for boundary in (base + window.start_second, base + window.start_second + window.duration_seconds):
                if tick < boundary < end_tick:
                    boundaries.add(boundary)
    return tuple(sorted(boundaries))
