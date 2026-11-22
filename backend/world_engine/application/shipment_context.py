"""Safe action identifiers for shipments to fixed destinations on visible routes."""

from decimal import Decimal, localcontext
from typing import Any


def shipment_dispatch_options(
    state: dict[str, Any], visible: dict[str, Any], routes: list[dict[str, Any]], actor_id: str,
) -> list[dict[str, Any]]:
    entities = state["entities"]
    actor = entities[actor_id]
    inventory = actor.get("components", {}).get("inventory")
    if actor.get("status", "active") != "active" or not isinstance(inventory, dict):
        return []
    sources: dict[str, list[tuple[str, Decimal]]] = {}
    for account_id, account in visible["resources"].items():
        balance = Decimal(str(account["balance"]))
        if balance > 0:
            sources.setdefault(account["resource_id"], []).append((account_id, balance))
    if not sources:
        return []

    destinations = {
        route["to_place_id"]: entities[route["to_place_id"]]
        for route in routes
    }
    destination_accounts: dict[str, list[tuple[str, str]]] = {}
    for account_id, account in state.get("resources", {}).items():
        owner_id = account.get("owner_id")
        owner = destinations.get(owner_id)
        if (
            owner is not None and owner.get("kind") in {"place", "location"}
            and owner.get("status", "active") == "active" and "inventory" in owner.get("components", {})
            and account.get("account_type", "holder") == "holder" and account.get("resource_id") in sources
        ):
            destination_accounts.setdefault(owner_id, []).append((account["resource_id"], account_id))

    options = []
    current_tick = state.get("current_tick", 0)
    for route in routes:
        arrival_tick = current_tick + route["travel_ticks"]
        shippable: dict[tuple[str, str], Decimal] = {}
        with localcontext() as context:
            context.prec = 80
            for lot in inventory.get("lots", []):
                expiry = lot.get("expiry_tick")
                if expiry is None or expiry > arrival_tick:
                    key = (lot["account_id"], lot["resource_id"])
                    shippable[key] = shippable.get(key, Decimal(0)) + Decimal(str(lot["quantity"]))
        for resource_id, destination_id in destination_accounts.get(route["to_place_id"], ()):
            for source_id, source_balance in sources[resource_id]:
                available = min(source_balance, shippable.get((source_id, resource_id), Decimal(0)))
                if available <= 0:
                    continue
                options.append({
                    "route_id": route["route_id"],
                    "destination_name": route["destination_name"],
                    "source_owner_id": actor_id,
                    "destination_owner_id": route["to_place_id"],
                    "resource_id": resource_id,
                    "source_account_id": source_id,
                    "destination_account_id": destination_id,
                    "available_quantity": format(available, "f"),
                })
    return options
