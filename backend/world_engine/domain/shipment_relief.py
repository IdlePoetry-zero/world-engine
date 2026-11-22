"""Pure planning for transferring already delivered food to one local recipient."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .components import ExactDecimal, GovernanceComponent, InventoryComponent, PlaceComponent, ResourceName, Tick
from .errors import InvalidEvent
from .inventory import allocate_fifo_lots
from .shipment import ShipmentComponent

if TYPE_CHECKING:
    from .models import WorldState


_MAX_REVISION = 2**63 - 1
_EXACT_DECIMAL_ADAPTER = TypeAdapter(ExactDecimal)


class _ReliefModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class ShipmentReliefAllocation(_ReliefModel):
    """One FIFO slice from a delivered receipt lot still held by the depot."""

    destination_lot_id: UUID
    transit_lot_id: UUID
    account_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))


class ShipmentReliefPlan(_ReliefModel):
    """Exact, replayable stock and permission snapshot for one relief transfer."""

    actor_id: UUID
    organization_id: UUID
    region_id: UUID
    shipment_id: UUID
    recipient_id: UUID
    depot_id: UUID
    tick: Tick
    resource_id: ResourceName
    source_account_id: UUID
    recipient_account_id: UUID
    quantity: ExactDecimal = Field(gt=Decimal("0"))
    allocations: tuple[ShipmentReliefAllocation, ...] = Field(min_length=1, max_length=100_000)
    expected_source_balance: ExactDecimal = Field(ge=Decimal("0"))
    expected_recipient_balance: ExactDecimal = Field(ge=Decimal("0"))
    expected_depot_inventory_revision: int = Field(ge=1, le=_MAX_REVISION)
    expected_recipient_inventory_revision: int | None = Field(default=None, ge=1, le=_MAX_REVISION)
    expected_shipment_revision: int = Field(ge=1, le=_MAX_REVISION)

    @model_validator(mode="after")
    def allocation_matches_plan(self) -> ShipmentReliefPlan:
        if self.actor_id == self.recipient_id:
            raise ValueError("shipment relief recipient must be separate from its actor")
        if self.source_account_id == self.recipient_account_id:
            raise ValueError("shipment relief source and recipient accounts must be distinct")
        if len({item.destination_lot_id for item in self.allocations}) != len(self.allocations):
            raise ValueError("shipment relief allocations cannot repeat a destination lot")
        if len({item.transit_lot_id for item in self.allocations}) != len(self.allocations):
            raise ValueError("shipment relief allocations cannot repeat a transit lot")
        if any(item.account_id != self.source_account_id for item in self.allocations):
            raise ValueError("shipment relief allocations must use the exact depot source account")
        with localcontext() as context:
            context.prec = 80
            total = sum((item.quantity for item in self.allocations), Decimal("0"))
        if total != self.quantity:
            raise ValueError("shipment relief allocation quantities must equal the requested quantity")
        return self


def plan_shipment_relief(
    state: WorldState,
    *,
    actor_id: UUID,
    organization_id: UUID,
    shipment_id: UUID,
    recipient_id: UUID,
    quantity: Decimal,
) -> ShipmentReliefPlan:
    """Plan a local transfer from the exact still-present lots of one delivered shipment."""

    for label, value in (
        ("actor", actor_id),
        ("organization", organization_id),
        ("shipment", shipment_id),
        ("recipient", recipient_id),
    ):
        if not isinstance(value, UUID):
            raise InvalidEvent(f"Shipment relief {label} ID is invalid.")
    if actor_id == recipient_id:
        raise InvalidEvent("Shipment relief must name a different recipient.")
    if not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= 0:
        raise InvalidEvent("Shipment relief quantity must be a positive finite Decimal.")
    try:
        # Apply the same precision limits used by stored inventories and accounts.
        quantity = _EXACT_DECIMAL_ADAPTER.validate_python(quantity)
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Shipment relief quantity is outside the supported exact range.") from exc

    entities = getattr(state, "entities", None)
    resources = getattr(state, "resources", None)
    metadata = getattr(state, "metadata", None)
    current_tick = getattr(state, "current_tick", None)
    if not isinstance(entities, Mapping) or not isinstance(resources, Mapping) or not isinstance(metadata, Mapping):
        raise InvalidEvent("Shipment relief requires a valid world state.")
    if not isinstance(current_tick, int) or isinstance(current_tick, bool):
        raise InvalidEvent("Shipment relief world tick is invalid.")

    roles = metadata.get("resource_roles")
    food_resource_id = roles.get("food_resource_id") if isinstance(roles, Mapping) else None
    if not isinstance(food_resource_id, str) or not food_resource_id.strip():
        raise InvalidEvent("This world has no registered food resource for shipment relief.")

    actor = entities.get(actor_id)
    recipient = entities.get(recipient_id)
    if actor is None or actor.kind not in {"character", "person"} or actor.status != "active":
        raise InvalidEvent("Shipment relief actor must be an active character.")
    if recipient is None or recipient.kind not in {"character", "person"} or recipient.status != "active":
        raise InvalidEvent("Shipment relief recipient must be a different active character.")

    organization = entities.get(organization_id)
    if (
        organization is None
        or organization.kind not in {"polity", "government"}
        or organization.status != "active"
    ):
        raise InvalidEvent("Shipment relief requires an active polity or government.")
    try:
        governance = GovernanceComponent.model_validate(organization.components.get("governance"))
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Shipment relief organization has no valid governance record.") from exc
    if governance.leader_id != actor_id or actor_id not in governance.members:
        raise InvalidEvent("Only the current member leader may authorize this shipment relief.")

    shipment_entity = entities.get(shipment_id)
    if shipment_entity is None or shipment_entity.kind != "shipment" or shipment_entity.status != "active":
        raise InvalidEvent("Shipment relief requires an existing active delivered shipment.")
    try:
        shipment = ShipmentComponent.model_validate(shipment_entity.components.get("shipment"))
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Shipment relief shipment record is invalid.") from exc
    if shipment.status != "delivered":
        raise InvalidEvent("Shipment relief requires a delivered shipment.")
    if current_tick < shipment.arrival_tick or any(row.received_tick > current_tick for row in shipment.delivered_lots):
        raise InvalidEvent("Shipment relief cannot precede the recorded receipt.")

    depot_id = shipment.destination_place_id
    depot = entities.get(depot_id)
    if (
        depot is None
        or depot.kind not in {"place", "location"}
        or depot.status != "active"
        or shipment.destination_owner_id != depot_id
        or shipment_entity.location_id != depot_id
        or actor.location_id != depot_id
        or recipient.location_id != depot_id
    ):
        raise InvalidEvent("Shipment relief requires its active depot and both people at that exact place.")
    try:
        depot_place = PlaceComponent.model_validate(depot.components.get("place"))
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Shipment relief depot has no valid place record.") from exc
    if depot_place.region_id is not None:
        region_id = depot_place.region_id
    elif depot_place.place_type == "region":
        region_id = depot.id
    else:
        raise InvalidEvent("Shipment relief depot must identify its containing region.")
    if shipment.destination_place_id != depot.id:
        raise InvalidEvent("Shipment relief shipment is not owned by its exact destination place.")
    headquarters_id = organization.location_id
    headquarters = entities.get(headquarters_id) if headquarters_id is not None else None
    if headquarters is None or headquarters.kind not in {"place", "location"} or headquarters.status != "active":
        raise InvalidEvent("Shipment relief organization must have an active headquarters in the depot region.")
    try:
        headquarters_place = PlaceComponent.model_validate(headquarters.components.get("place"))
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Shipment relief organization headquarters has no valid place record.") from exc
    if headquarters_place.region_id is not None:
        headquarters_region_id = headquarters_place.region_id
    elif headquarters_place.place_type == "region":
        headquarters_region_id = headquarters.id
    else:
        raise InvalidEvent("Shipment relief organization headquarters has no region affiliation.")
    if headquarters_region_id != region_id:
        raise InvalidEvent("Shipment relief organization is not headquartered in the depot region.")

    try:
        depot_inventory = InventoryComponent.model_validate(depot.components.get("inventory"))
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Shipment relief depot inventory is invalid or missing.") from exc
    recipient_inventory_raw = recipient.components.get("inventory")
    try:
        recipient_inventory = (
            InventoryComponent.model_validate(recipient_inventory_raw)
            if recipient_inventory_raw is not None
            else None
        )
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Shipment relief recipient inventory is invalid.") from exc

    food_cargo = [line for line in shipment.cargo if line.resource_id == food_resource_id]
    if len(food_cargo) != 1:
        raise InvalidEvent("Shipment does not contain exactly one manifest for the registered food resource.")
    cargo = food_cargo[0]
    source_account_id = cargo.destination_account_id
    source_account = resources.get(source_account_id)
    if (
        source_account is None
        or source_account.account_type != "holder"
        or source_account.owner_id != depot_id
        or source_account.resource_id != food_resource_id
    ):
        raise InvalidEvent("Shipment depot food account is missing or has the wrong owner or resource.")

    recipient_accounts = [
        (account_id, account)
        for account_id, account in resources.items()
        if account.account_type == "holder"
        and account.owner_id == recipient_id
        and account.resource_id == food_resource_id
    ]
    if len(recipient_accounts) != 1:
        raise InvalidEvent("Shipment relief requires one verified recipient food holder account.")
    recipient_account_id, recipient_account = recipient_accounts[0]
    if recipient_account_id == source_account_id:
        raise InvalidEvent("Shipment relief source and recipient accounts must be distinct.")

    manifest_by_transit = {lot.transit_lot_id: lot for lot in cargo.lots}
    inventory_lots_by_id = {lot.lot_id: lot for lot in depot_inventory.lots}
    receipt_by_destination: dict[UUID, UUID] = {}
    subset_ids: set[UUID] = set()
    for receipt in shipment.delivered_lots:
        manifest = manifest_by_transit.get(receipt.transit_lot_id)
        if manifest is None:
            continue
        if receipt.destination_lot_id in receipt_by_destination:
            raise InvalidEvent("Shipment relief delivered receipts repeat a destination lot.")
        receipt_by_destination[receipt.destination_lot_id] = receipt.transit_lot_id
        lot = inventory_lots_by_id.get(receipt.destination_lot_id)
        if lot is None:
            continue
        if (
            lot.account_id != source_account_id
            or lot.resource_id != food_resource_id
            or lot.quantity > receipt.quantity
        ):
            raise InvalidEvent("A shipment receipt lot still present at the depot no longer matches its food account.")
        subset_ids.add(lot.lot_id)

    receipt_subset = InventoryComponent(lots=[lot for lot in depot_inventory.lots if lot.lot_id in subset_ids])
    try:
        selected = allocate_fifo_lots(
            receipt_subset,
            resource_id=food_resource_id,
            quantity=quantity,
            at_tick=current_tick,
            account_id=source_account_id,
        )
    except ValueError as exc:
        raise InvalidEvent("The exact delivered shipment food still available at this depot cannot cover that quantity.") from exc
    if source_account.balance < quantity:
        raise InvalidEvent("The depot food account balance cannot cover the requested relief quantity.")
    try:
        # Re-parse both resulting balances so the planner cannot authorize an
        # account change that its storage contract would reject.
        from .models import ResourceAccount

        with localcontext() as context:
            context.prec = 80
            next_recipient_balance = recipient_account.balance + quantity
            next_source_balance = source_account.balance - quantity
        ResourceAccount.model_validate(
            recipient_account.model_dump(mode="python") | {"balance": next_recipient_balance}
        )
        ResourceAccount.model_validate(
            source_account.model_dump(mode="python") | {"balance": next_source_balance}
        )
    except (TypeError, ValueError) as exc:
        raise InvalidEvent("Shipment relief would exceed the supported account balance range.") from exc

    transit_by_destination = {destination_id: transit_id for destination_id, transit_id in receipt_by_destination.items()}
    try:
        allocations = tuple(
            ShipmentReliefAllocation(
                destination_lot_id=allocation.lot_id,
                transit_lot_id=transit_by_destination[allocation.lot_id],
                account_id=allocation.account_id,
                quantity=allocation.quantity,
            )
            for allocation in selected
        )
        return ShipmentReliefPlan(
            actor_id=actor_id,
            organization_id=organization_id,
            region_id=region_id,
            shipment_id=shipment_id,
            recipient_id=recipient_id,
            depot_id=depot_id,
            tick=current_tick,
            resource_id=food_resource_id,
            source_account_id=source_account_id,
            recipient_account_id=recipient_account_id,
            quantity=quantity,
            allocations=allocations,
            expected_source_balance=source_account.balance,
            expected_recipient_balance=recipient_account.balance,
            expected_depot_inventory_revision=depot_inventory.revision,
            expected_recipient_inventory_revision=(recipient_inventory.revision if recipient_inventory is not None else None),
            expected_shipment_revision=shipment.revision,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidEvent("Shipment relief allocation does not match the delivered shipment receipts.") from exc
