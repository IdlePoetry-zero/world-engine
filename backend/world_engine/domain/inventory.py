"""Deterministic FIFO lot selection shared by rules and aggregate reducers."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from uuid import UUID

from .components import InventoryComponent, InventoryLot


@dataclass(frozen=True, slots=True)
class FifoLotAllocation:
    lot_id: UUID
    account_id: UUID
    quantity: Decimal


@dataclass(slots=True)
class _WorkingFifoLot:
    lot: InventoryLot
    remaining: Decimal
    partial_revision_increments: int = 0


@dataclass(slots=True)
class FifoInventoryCursor:
    """Window-local FIFO cursor that defers immutable inventory copies.

    Repeated small care actions otherwise scan every lot and rebuild the full
    immutable inventory after each unit. This cursor preserves the same lot
    order, expiry cutoffs, and per-lot revision increments while copying only
    final surviving lots once.
    """

    inventory: InventoryComponent
    _working_lots: list[_WorkingFifoLot] = field(init=False)
    _by_resource: dict[str, list[_WorkingFifoLot]] = field(init=False)
    _positions: dict[str, int] = field(init=False, default_factory=dict)
    _changed: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self._working_lots = [_WorkingFifoLot(lot=lot, remaining=lot.quantity) for lot in self.inventory.lots]
        self._by_resource = {}
        for working in self._working_lots:
            self._by_resource.setdefault(working.lot.resource_id, []).append(working)

    def consume(
        self, *, resource_id: str, quantity: Decimal, at_tick: int
    ) -> list[FifoLotAllocation]:
        """Allocate and consume one request without rebuilding the inventory."""
        if quantity <= 0:
            raise ValueError("FIFO allocation quantity must be positive")
        candidates = self._by_resource.get(resource_id, [])
        position = self._positions.get(resource_id, 0)
        first_usable: int | None = None
        remaining = quantity
        selected: list[tuple[_WorkingFifoLot, Decimal]] = []
        with localcontext() as context:
            context.prec = 80
            while position < len(candidates) and remaining > 0:
                working = candidates[position]
                if working.remaining <= 0 or (
                    working.lot.expiry_tick is not None and working.lot.expiry_tick <= at_tick
                ):
                    position += 1
                    continue
                if first_usable is None:
                    first_usable = position
                taken = min(remaining, working.remaining)
                if taken > 0:
                    selected.append((working, taken))
                    remaining -= taken
                if taken == working.remaining:
                    position += 1

            if remaining != 0:
                # Expired/depleted rows can be skipped permanently, but a
                # partially considered eligible row must remain at the cursor
                # because allocate_fifo_lots is all-or-nothing on failure.
                self._positions[resource_id] = (
                    first_usable if first_usable is not None else position
                )
                raise ValueError("unexpired FIFO inventory lots do not cover the requested quantity")

            for working, taken in selected:
                left = working.remaining - taken
                if left > 0:
                    working.partial_revision_increments += 1
                working.remaining = left
            self._positions[resource_id] = position
            self._changed = True

        return [
            FifoLotAllocation(working.lot.lot_id, working.lot.account_id, taken)
            for working, taken in selected
        ]

    def finish(self) -> InventoryComponent:
        """Return the exact immutable inventory result for the whole window."""
        if not self._changed:
            return self.inventory
        lots: list[InventoryLot] = []
        for working in self._working_lots:
            if working.remaining <= 0:
                continue
            if working.remaining == working.lot.quantity and working.partial_revision_increments == 0:
                lots.append(working.lot)
            else:
                lots.append(
                    working.lot.model_copy(
                        update={
                            "quantity": working.remaining,
                            "revision": working.lot.revision + working.partial_revision_increments,
                        }
                    )
                )
        return InventoryComponent(lots=lots, revision=self.inventory.revision + 1)


def allocate_fifo_lots(
    inventory: InventoryComponent,
    *,
    resource_id: str,
    quantity: Decimal,
    at_tick: int,
    account_id: UUID | None = None,
) -> list[FifoLotAllocation]:
    if quantity <= 0:
        raise ValueError("FIFO allocation quantity must be positive")
    allocations: list[FifoLotAllocation] = []
    remaining = quantity
    with localcontext() as context:
        context.prec = 80
        for lot in inventory.lots:
            if (
                lot.resource_id != resource_id
                or account_id is not None and lot.account_id != account_id
                or lot.expiry_tick is not None and lot.expiry_tick <= at_tick
                or remaining <= 0
            ):
                continue
            taken = min(remaining, lot.quantity)
            if taken > 0:
                allocations.append(FifoLotAllocation(lot.lot_id, lot.account_id, taken))
                remaining -= taken
    if remaining != 0:
        raise ValueError("unexpired FIFO inventory lots do not cover the requested quantity")
    return allocations


def reduce_fifo_inventory(
    inventory: InventoryComponent,
    allocations: list[FifoLotAllocation],
) -> InventoryComponent:
    if not allocations:
        raise ValueError("FIFO inventory reduction requires at least one lot")
    quantities: dict[UUID, Decimal] = {}
    for allocation in allocations:
        if allocation.lot_id in quantities:
            raise ValueError("FIFO allocation cannot repeat a lot")
        quantities[allocation.lot_id] = allocation.quantity
    lots: list[InventoryLot] = []
    with localcontext() as context:
        context.prec = 80
        for lot in inventory.lots:
            taken = quantities.get(lot.lot_id)
            if taken is None:
                lots.append(lot)
                continue
            if taken <= 0 or taken > lot.quantity:
                raise ValueError("FIFO allocation exceeds a registered lot quantity")
            if taken < lot.quantity:
                lots.append(lot.model_copy(update={"quantity": lot.quantity - taken, "revision": lot.revision + 1}))
    if set(quantities) - {lot.lot_id for lot in inventory.lots}:
        raise ValueError("FIFO allocation references an unknown lot")
    return inventory.model_copy(update={"lots": lots, "revision": inventory.revision + 1})
