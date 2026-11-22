"""Bounded shipment relief options for the current actor's local scene."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal, localcontext
from uuid import UUID

from world_engine.domain.components import GovernanceComponent, InventoryComponent, PlaceComponent
from world_engine.domain.models import WorldEntity, WorldState
from world_engine.domain.shipment import ShipmentComponent


def shipment_relief_options(state: WorldState, actor_id: UUID, visible_ids: set[UUID]) -> list[dict]:
    actor = state.entities[actor_id]
    depot = state.entities.get(actor.location_id)
    roles = state.metadata.get("resource_roles")
    food = roles.get("food_resource_id") if isinstance(roles, dict) else None
    if actor.status != "active" or depot is None or not isinstance(food, str):
        return []
    raw_inventory = depot.components.get("inventory")
    region_id = _active_place_region(depot)
    if region_id is None or raw_inventory is None:
        return []
    full_inventory = InventoryComponent.model_validate(raw_inventory)
    organizations = [
        {"organization_id": str(entity.id), "name": entity.name}
        for entity in state.entities.values()
        if entity.kind in {"polity", "government"} and entity.status == "active"
        and "governance" in entity.components
        and GovernanceComponent.model_validate(entity.components["governance"]).leader_id == actor_id
        and _active_place_region(state.entities.get(entity.location_id)) == region_id
    ]
    if not organizations:
        return []
    food_holder_counts = Counter(
        account.owner_id for account in state.resources.values()
        if account.account_type == "holder" and account.resource_id == food
    )
    recipients = [
        {"recipient_id": str(entity.id), "name": entity.name}
        for entity in state.entities.values()
        if entity.id in visible_ids and food_holder_counts[entity.id] == 1 and entity.id != actor_id
        and entity.kind in {"character", "person"} and entity.status == "active" and entity.location_id == depot.id
    ]
    if not recipients:
        return []
    available_lots = {
        lot.lot_id: lot for lot in full_inventory.lots
        if lot.resource_id == food and (lot.expiry_tick is None or lot.expiry_tick > state.current_tick)
    }
    shipments = []
    for entity in state.entities.values():
        if (entity.kind != "shipment" or entity.status != "active"
            or entity.location_id != depot.id or "shipment" not in entity.components):
            continue
        shipment = ShipmentComponent.model_validate(entity.components["shipment"])
        if shipment.status != "delivered" or shipment.destination_owner_id != depot.id or shipment.destination_place_id != depot.id:
            continue
        food_manifest = next((line for line in shipment.cargo if line.resource_id == food), None)
        if food_manifest is None:
            continue
        transit_lot_ids = {lot.transit_lot_id for lot in food_manifest.lots}
        with localcontext() as context:
            context.prec = 80
            available = sum(
                (available_lots[row.destination_lot_id].quantity for row in shipment.delivered_lots
                 if row.transit_lot_id in transit_lot_ids and row.destination_lot_id in available_lots
                 and available_lots[row.destination_lot_id].account_id == food_manifest.destination_account_id),
                Decimal(0),
            )
        if available > 0:
            shipments.append({
                "shipment_id": str(entity.id), "depot_name": depot.name, "resource_id": food,
                "available_quantity": format(available, "f"),
                "organizations": organizations, "recipients": recipients,
            })
    return shipments


def _active_place_region(place: WorldEntity | None) -> UUID | None:
    if place is None or place.kind not in {"place", "location"} or place.status != "active":
        return None
    raw = place.components.get("place")
    if raw is None:
        return None
    component = PlaceComponent.model_validate(raw)
    return component.region_id or (place.id if component.place_type == "region" else None)
