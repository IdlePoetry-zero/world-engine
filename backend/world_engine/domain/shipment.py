"""Typed shipment state and pure logistics planning helpers.

Shipments are world entities so their in-transit holder accounts have a real,
referentially valid owner. The shipment entity owns only the lots currently in
custody; dispatch and receipt reducers move those lots between owner-specific
inventories while the balanced ledger records the same transfer.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .components import (
    ComponentModel,
    ExactDecimal,
    InventoryComponent,
    PlaceComponent,
    ResourceName,
    ScheduledEventRecord,
    Tick,
)
from .inventory import FifoLotAllocation, allocate_fifo_lots

if TYPE_CHECKING:
    from .models import WorldState


class ShipmentLotManifest(ComponentModel):
    """One source lot and its deterministic lot identity while in transit."""

    source_lot_id: UUID
    transit_lot_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class ShipmentLotReceipt(ComponentModel):
    """Deterministic destination lot identity for one transit lot."""

    transit_lot_id: UUID
    destination_lot_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class ShipmentSpoiledLot(ComponentModel):
    """An immutable manifest lot later disposed at its exact expiry tick."""

    transit_lot_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    spoiled_tick: Tick
    event_id: UUID
    destruction_sink_account_id: UUID


class ShipmentDeliveredLot(ComponentModel):
    """An immutable manifest lot later accepted by the named destination owner."""

    transit_lot_id: UUID
    destination_lot_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    received_tick: Tick


class ShipmentAccountFlow(ComponentModel):
    """One resource/account leg transferred from transit to its receiver."""

    resource_id: ResourceName
    transit_account_id: UUID
    destination_account_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class ShipmentCargoManifest(ComponentModel):
    """Single-resource manifest with exact owner accounts and FIFO lot lineage."""

    resource_id: ResourceName
    source_account_id: UUID
    transit_account_id: UUID
    destination_account_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    lots: list[ShipmentLotManifest] = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def exact_unique_lot_manifest(self) -> ShipmentCargoManifest:
        if len({item.source_lot_id for item in self.lots}) != len(self.lots):
            raise ValueError("a shipment cannot repeat a source inventory lot")
        if len({item.transit_lot_id for item in self.lots}) != len(self.lots):
            raise ValueError("a shipment cannot repeat a transit inventory lot")
        with localcontext() as context:
            context.prec = 80
            lot_total = sum((item.quantity for item in self.lots), Decimal("0"))
        if lot_total != self.quantity:
            raise ValueError("shipment lot quantities must exactly equal the manifest quantity")
        return self


class ShipmentComponent(ComponentModel):
    """A single registered route transfer and its current physical custody."""

    dispatch_event_id: UUID
    arrival_schedule_id: UUID
    sender_id: UUID
    source_owner_id: UUID
    destination_owner_id: UUID
    origin_place_id: UUID
    destination_place_id: UUID
    route_id: UUID
    departure_tick: Tick
    arrival_tick: Tick
    lifecycle_version: Literal["legacy_v1", "expiry_v2"] = "legacy_v1"
    status: Literal["in_transit", "awaiting_receipt", "delivered", "spoiled"] = "in_transit"
    arrived_tick: Tick | None = None
    spoilage_schedule_id: UUID | None = None
    cargo: list[ShipmentCargoManifest] = Field(min_length=1, max_length=128)
    spoiled_lots: list[ShipmentSpoiledLot] = Field(default_factory=list, max_length=100_000)
    delivered_lots: list[ShipmentDeliveredLot] = Field(default_factory=list, max_length=100_000)

    @model_validator(mode="after")
    def shipment_state_is_consistent(self) -> ShipmentComponent:
        if self.source_owner_id != self.sender_id:
            raise ValueError("shipment source stock must belong to its local carrier actor")
        if self.arrival_tick <= self.departure_tick:
            raise ValueError("shipment arrival must follow its departure")
        if self.status == "in_transit" and self.arrived_tick is not None:
            raise ValueError("an in-transit shipment cannot already have arrived")
        if self.status in {"awaiting_receipt", "delivered", "spoiled"} and self.arrived_tick != self.arrival_tick:
            raise ValueError("a received or awaiting-receipt shipment must be recorded at its exact arrival tick")
        resource_ids = [item.resource_id for item in self.cargo]
        if len(resource_ids) != len(set(resource_ids)) or resource_ids != sorted(resource_ids):
            raise ValueError("shipment cargo resources must be unique and use stable resource-id order")
        transit_accounts = [item.transit_account_id for item in self.cargo]
        if len(transit_accounts) != len(set(transit_accounts)):
            raise ValueError("shipment cargo lines must use distinct transit accounts")
        transit_lots = [lot.transit_lot_id for cargo in self.cargo for lot in cargo.lots]
        if len(transit_lots) != len(set(transit_lots)):
            raise ValueError("shipment cargo lines cannot reuse a transit lot ID")
        all_accounts = [
            account_id
            for item in self.cargo
            for account_id in (
                item.source_account_id,
                item.transit_account_id,
                item.destination_account_id,
            )
        ]
        if len(all_accounts) != len(set(all_accounts)):
            raise ValueError("shipment cargo must use distinct source, transit, and destination accounts")
        manifest_lots = {
            lot.transit_lot_id: lot
            for cargo in self.cargo
            for lot in cargo.lots
        }
        spoiled_ids = [lot.transit_lot_id for lot in self.spoiled_lots]
        if len(spoiled_ids) != len(set(spoiled_ids)) or spoiled_ids != sorted(spoiled_ids, key=str):
            raise ValueError("shipment spoiled-lot receipts must be unique and use stable UUID order")
        for spoiled in self.spoiled_lots:
            manifest = manifest_lots.get(spoiled.transit_lot_id)
            if manifest is None or manifest.quantity != spoiled.quantity:
                raise ValueError("shipment spoilage must account for one exact original manifest lot")
            if spoiled.spoiled_tick < self.arrival_tick:
                raise ValueError("shipment spoilage cannot precede its arrival boundary")
        delivered_ids = [lot.transit_lot_id for lot in self.delivered_lots]
        if len(delivered_ids) != len(set(delivered_ids)) or delivered_ids != sorted(delivered_ids, key=str):
            raise ValueError("shipment delivery receipts must be unique and use stable UUID order")
        for delivered in self.delivered_lots:
            manifest = manifest_lots.get(delivered.transit_lot_id)
            if manifest is None or delivered.quantity != manifest.quantity:
                raise ValueError("shipment receipt must account for one exact remaining manifest lot")
            if delivered.received_tick < self.arrival_tick:
                raise ValueError("shipment receipt cannot precede its arrival boundary")
        if set(spoiled_ids) & set(delivered_ids):
            raise ValueError("a shipment lot cannot be both spoiled and received")
        remaining = self.remaining_quantity
        if self.status == "delivered" and remaining != 0:
            raise ValueError("a delivered shipment must have no remaining custody quantity")
        if self.status == "spoiled" and remaining != 0:
            raise ValueError("a fully spoiled shipment must have no remaining custody quantity")
        if self.status in {"in_transit", "awaiting_receipt"} and remaining <= 0:
            raise ValueError("an active shipment must retain some unspoiled custody quantity")
        return self

    @property
    def remaining_quantity(self) -> Decimal:
        if self.lifecycle_version == "legacy_v1" and self.status == "delivered":
            return Decimal("0")
        with localcontext() as context:
            context.prec = 80
            return sum((cargo.quantity for cargo in self.cargo), Decimal("0")) - sum(
                (lot.quantity for lot in self.spoiled_lots), Decimal("0")
            ) - sum((lot.quantity for lot in self.delivered_lots), Decimal("0"))

    def remaining_lot_quantities(self) -> dict[UUID, Decimal]:
        if self.lifecycle_version == "legacy_v1" and self.status == "delivered":
            return {}
        spoiled = {item.transit_lot_id for item in self.spoiled_lots}
        delivered = {item.transit_lot_id for item in self.delivered_lots}
        return {
            lot.transit_lot_id: lot.quantity
            for cargo in self.cargo
            for lot in cargo.lots
            if lot.transit_lot_id not in spoiled and lot.transit_lot_id not in delivered
        }


def validate_shipment_state(state: WorldState) -> None:
    """Validate shipment custody, account ownership, route slots, and due work."""

    from .models import ResourceAccount

    schedules = {item.id: item for item in state.scheduled_events}
    route_counts: dict[tuple[UUID, UUID], int] = {}
    shipments: list[tuple[object, ShipmentComponent]] = []

    for entity in state.entities.values():
        raw = entity.components.get("shipment")
        if entity.kind != "shipment" and raw is None:
            continue
        if entity.kind != "shipment" or raw is None:
            raise ValueError("shipment components must be attached to shipment entities")
        try:
            shipment = ShipmentComponent.model_validate(raw)
        except Exception as exc:
            raise ValueError("shipment entity has an invalid registered shipment component") from exc
        shipments.append((entity, shipment))

        sender = state.entities.get(shipment.sender_id)
        source_owner = state.entities.get(shipment.source_owner_id)
        destination_owner = state.entities.get(shipment.destination_owner_id)
        origin = state.entities.get(shipment.origin_place_id)
        destination = state.entities.get(shipment.destination_place_id)
        if sender is None or source_owner is None or source_owner.id != sender.id:
            raise ValueError("shipment source owner must be its existing sender entity")
        if destination_owner is None:
            raise ValueError("shipment destination owner must reference an existing entity")
        if origin is None or origin.kind not in {"place", "location"}:
            raise ValueError("shipment origin must reference a registered place")
        if destination is None or destination.kind not in {"place", "location"}:
            raise ValueError("shipment destination must reference a registered place")

        try:
            inventory = InventoryComponent.model_validate(entity.components.get("inventory"))
        except Exception as exc:
            raise ValueError("shipment entity must keep a typed transit inventory") from exc
        expected_transit_lots: dict[UUID, tuple[UUID, str, Decimal]] = {}
        remaining_lot_quantities = shipment.remaining_lot_quantities()
        for cargo in shipment.cargo:
            source = state.resources.get(cargo.source_account_id)
            transit = state.resources.get(cargo.transit_account_id)
            recipient = state.resources.get(cargo.destination_account_id)
            for account, account_id, owner_id, role in (
                (source, cargo.source_account_id, shipment.source_owner_id, "source"),
                (transit, cargo.transit_account_id, entity.id, "transit"),
                (recipient, cargo.destination_account_id, shipment.destination_owner_id, "destination"),
            ):
                if (
                    account is None
                    or account.account_type != "holder"
                    or account.owner_id != owner_id
                    or account.resource_id != cargo.resource_id
                ):
                    raise ValueError(f"shipment {role} account {account_id} has the wrong owner or resource")
            for lot in cargo.lots:
                remaining_quantity = remaining_lot_quantities.get(lot.transit_lot_id)
                if remaining_quantity is not None:
                    expected_transit_lots[lot.transit_lot_id] = (
                        cargo.transit_account_id,
                        cargo.resource_id,
                        remaining_quantity,
                    )
            expected_balance = sum(
                (quantity for lot_id, quantity in remaining_lot_quantities.items()
                 if lot_id in {lot.transit_lot_id for lot in cargo.lots}),
                Decimal("0"),
            )
            if transit.balance != expected_balance:
                raise ValueError("shipment transit account balance must equal remaining manifest custody")

        actual_transit_lots = {lot.lot_id: lot for lot in inventory.lots}
        if shipment.status in {"delivered", "spoiled"}:
            if actual_transit_lots:
                raise ValueError("a terminal shipment must not retain cargo inventory lots")
        else:
            if set(actual_transit_lots) != set(expected_transit_lots):
                raise ValueError("in-transit inventory lots must exactly match the shipment manifest")
            for lot_id, (account_id, resource_id, quantity) in expected_transit_lots.items():
                lot = actual_transit_lots[lot_id]
                if lot.account_id != account_id or lot.resource_id != resource_id or lot.quantity != quantity:
                    raise ValueError("in-transit inventory lots must match their manifest account/resource/quantity")

        schedule = schedules.get(shipment.arrival_schedule_id)
        if (
            schedule is None
            or schedule.handler_id != "shipment.arrival"
            or schedule.handler_version != "1.0.0"
            or schedule.phase != "delivery"
            or schedule.max_runs != 1
            or schedule.repeat_ticks is not None
            or schedule.payload != {"shipment_id": str(entity.id)}
            or schedule.due_tick != shipment.arrival_tick
        ):
            raise ValueError("shipment must reference its exact one-shot delivery schedule")
        if shipment.status == "in_transit":
            if entity.location_id is not None or schedule.status != "queued":
                raise ValueError("an in-transit shipment must remain off-map with a queued arrival schedule")
            route_counts[(shipment.origin_place_id, shipment.route_id)] = (
                route_counts.get((shipment.origin_place_id, shipment.route_id), 0) + 1
            )
        else:
            if entity.location_id != shipment.destination_place_id or schedule.status != "completed":
                # During pure batch application a route event is followed by
                # its schedule-completion event in the same command. Allow
                # that exact due-tick intermediate state; it cannot be stored
                # by a valid RuleEngine resolution.
                if not (
                    entity.location_id == shipment.destination_place_id
                    and schedule.status == "queued"
                    and schedule.due_tick == state.current_tick == shipment.arrival_tick
                ):
                    raise ValueError("arrived shipment must be at its destination with a completed arrival schedule")

        remaining_expiries = [
            lot.expiry_tick for lot in inventory.lots if lot.expiry_tick is not None
        ]
        spoilage_schedule = schedules.get(shipment.spoilage_schedule_id) if shipment.spoilage_schedule_id else None
        if shipment.lifecycle_version == "expiry_v2" and shipment.status in {"in_transit", "awaiting_receipt"} and remaining_expiries:
            expected_due = min(remaining_expiries)
            if (
                spoilage_schedule is None
                or spoilage_schedule.status != "queued"
                or spoilage_schedule.handler_id != "shipment.spoilage"
                or spoilage_schedule.handler_version != "1.0.0"
                or spoilage_schedule.phase != "delivery"
                or spoilage_schedule.max_runs != 1
                or spoilage_schedule.repeat_ticks is not None
                or spoilage_schedule.due_tick != expected_due
                or spoilage_schedule.payload != {"shipment_id": str(entity.id)}
                or spoilage_schedule.dependencies != [shipment.arrival_schedule_id]
            ):
                raise ValueError("shipment must retain its exact next one-shot spoilage boundary")
        elif shipment.spoilage_schedule_id is not None:
            raise ValueError("shipment without expiring transit stock cannot retain an active spoilage schedule")

    for (origin_id, route_id), count in route_counts.items():
        origin = state.entities.get(origin_id)
        if origin is None:
            continue
        try:
            place = PlaceComponent.model_validate(origin.components.get("place"))
        except Exception:
            continue
        route = next((item for item in place.routes if item.route_id == route_id), None)
        if route is not None and route.capacity is not None and count > route.capacity:
            raise ValueError("active shipment manifests exceed the registered route slot capacity")


def active_shipments_on_route(
    state: WorldState,
    route_id: UUID,
    *,
    at_tick: int | None = None,
) -> int:
    """Count one reserved slot for each manifest currently travelling a route."""

    tick = state.current_tick if at_tick is None else at_tick
    count = 0
    for entity in state.entities.values():
        if entity.kind != "shipment":
            continue
        raw = entity.components.get("shipment")
        if raw is None:
            continue
        shipment = ShipmentComponent.model_validate(raw)
        if (
            shipment.route_id == route_id
            and shipment.status == "in_transit"
            # The due tick still occupies its reservation until the delivery
            # phase commits arrival or deferral at that exact boundary.
            and shipment.departure_tick <= tick <= shipment.arrival_tick
        ):
            count += 1
    return count


def shipment_destination_available(
    state: WorldState,
    destination_owner_id: UUID,
    destination_place_id: UUID,
) -> bool:
    """Whether the named receiver is physically present at the delivery place.

    A fixed place can accept cargo in its own stock without an individual
    receiver. A character can accept it only while active and at that place.
    Other entity kinds need an explicit receiving policy before they can own
    physical shipments.
    """

    owner = state.entities.get(destination_owner_id)
    if owner is None or owner.status != "active":
        return False
    if owner.kind in {"place", "location"}:
        return owner.id == destination_place_id
    if owner.kind in {"character", "person"}:
        return owner.location_id == destination_place_id
    return False


def plan_dispatch_lots(
    inventory: InventoryComponent,
    *,
    source_account_id: UUID,
    resource_id: str,
    quantity: Decimal,
    arrival_tick: int,
) -> list[FifoLotAllocation]:
    """Select FIFO source lots that remain unexpired through the arrival tick.

    A quantity that cannot be covered by unexpired lots is rejected. This v1
    rule deliberately avoids implicit in-transit spoilage; that requires its
    own exact scheduled event and typed destruction postings.
    """

    return allocate_fifo_lots(
        inventory,
        resource_id=resource_id,
        quantity=quantity,
        at_tick=arrival_tick,
        account_id=source_account_id,
    )

