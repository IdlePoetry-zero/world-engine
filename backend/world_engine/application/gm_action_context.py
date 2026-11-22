"""Bounded, audience-safe action facts for read-only GM answers.

The public projection deliberately omits authored route and inventory
components. This module derives a small set of current, actor-authorized choices
from the ordinary action scene and passes only those choices to GM queries.
It never commits commands or returns raw world components.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any
from uuid import UUID

from world_engine.domain.components import PlaceComponent
from world_engine.domain.models import WorldState

from .institutional_finance_context import institutional_finance_context


MAX_CONTEXT_ROWS = 32


def build_gm_action_context(
    state: WorldState,
    actor_id: UUID,
    *,
    can_host: bool,
) -> tuple[dict[str, Any], set[str]]:
    """Return bounded actor-visible route, treaty, and shipment facts.

    ``state`` is used only as the input to existing read-only projection and
    rule helpers. The returned mapping contains no entity component dumps,
    inventory lots, account IDs, private counterpart balances, or unrelated
    world routes.
    """

    # ``actions`` imports ConversationApplication for the dispatcher. Keep this
    # import inside the call so conversation.py can safely import this helper.
    from .actions import action_scene

    actor_id_text = str(actor_id)
    raw_state = state.model_dump(mode="json")
    scene = action_scene(raw_state, actor_id_text, authoritative=state)
    visible_entities = scene["entities"]
    actor = state.entities.get(actor_id)
    origin = state.entities.get(actor.location_id) if actor is not None else None
    origin_id = str(origin.id) if origin is not None else None
    origin_name = origin.name if origin is not None else None

    travel_rows = []
    for route in scene.get("routes", []):
        destination_id = route.get("to_place_id")
        if not isinstance(destination_id, str):
            continue
        destination = state.entities.get(UUID(destination_id))
        if destination is None or (
            destination.components.get("hidden") and destination_id not in visible_entities
        ):
            continue
        travel_rows.append({
            "route_id": route["route_id"],
            "from_place_id": origin_id,
            "from_place_name": origin_name,
            "to_place_id": destination_id,
            "to_place_name": route.get("destination_name") or destination.name,
            "travel_ticks": str(route["travel_ticks"]),
        })
    travel_rows.sort(key=lambda row: (row["route_id"], row["to_place_id"]))
    travel_options = travel_rows[:MAX_CONTEXT_ROWS]

    # action_scene already adds the safe-conduct context when given the
    # authoritative snapshot. Reuse that result; the host-only permission is
    # narrowed below because action_scene intentionally uses its default.
    safe_context = scene
    accessible_route_ids = {row["route_id"] for row in travel_rows}
    safe_options = []
    if origin is not None:
        try:
            origin_place = PlaceComponent.model_validate(origin.components.get("place", {}))
        except (TypeError, ValueError):
            origin_place = None
        routes_by_id = (
            {str(route.route_id): route for route in origin_place.routes}
            if origin_place is not None else {}
        )
        for option in safe_context.get("safe_conduct_proposal_options", []):
            route_id = option.get("route_id")
            route = routes_by_id.get(route_id)
            recipient_id = option.get("recipient_organization_id")
            destination_id = option.get("to_place_id")
            proposer_id = option.get("proposer_organization_id")
            try:
                recipient = state.entities.get(UUID(recipient_id)) if isinstance(recipient_id, str) else None
                destination = state.entities.get(UUID(destination_id)) if isinstance(destination_id, str) else None
                proposer = state.entities.get(UUID(proposer_id)) if isinstance(proposer_id, str) else None
            except ValueError:
                continue
            if (
                route is None or recipient is None or destination is None or proposer is None
                or recipient.status != "active" or proposer.status != "active"
                or recipient.components.get("hidden") or proposer.components.get("hidden")
                or destination.components.get("hidden")
                or option.get("from_place_id") != origin_id
                or str(route.to_place_id) != destination_id
            ):
                continue
            safe_options.append({
                "route_id": route_id,
                "proposer_organization_id": proposer_id,
                "proposer_name": option.get("proposer_name") or proposer.name,
                "recipient_organization_id": recipient_id,
                "recipient_name": option.get("recipient_name") or recipient.name,
                "from_place_id": origin_id,
                "from_place_name": origin_name,
                "to_place_id": destination_id,
                "to_place_name": option.get("to_place_name") or destination.name,
                "travel_ticks": str(route.travel_ticks),
                "requires_safe_conduct": True,
                "travel_permitted_now": route_id in accessible_route_ids,
            })
    safe_options.sort(key=lambda row: (
        row["route_id"], row["recipient_organization_id"], row["to_place_id"],
    ))
    safe_options_count = len(safe_options)
    safe_options = safe_options[:MAX_CONTEXT_ROWS]

    own_treaties = []
    for treaty in safe_context.get("own_safe_conduct_treaties", []):
        # The projection already limits these records to a party or represented
        # organization; select fields instead of forwarding its whole mapping.
        row = {key: treaty[key] for key in (
            "treaty_id", "status", "revision", "proposer_organization_id", "proposer_name",
            "recipient_organization_id", "recipient_name", "route_id", "from_place_id",
            "from_place_name", "to_place_id", "to_place_name", "expires_at_tick",
            "counter_expires_at_tick", "expired", "represents_proposer", "represents_recipient",
            "can_ratify", "can_counter", "can_accept_counter", "can_request_response", "can_withdraw",
        ) if key in treaty}
        if not can_host:
            row["can_request_response"] = False
        own_treaties.append(row)
    own_treaties.sort(key=lambda row: row.get("treaty_id", ""))
    own_treaties = own_treaties[:MAX_CONTEXT_ROWS]

    held_by_resource: dict[str, Decimal] = defaultdict(Decimal)
    for account in scene.get("resources", {}).values():
        resource_id = account.get("resource_id")
        if not isinstance(resource_id, str):
            continue
        try:
            held_by_resource[resource_id] += Decimal(str(account.get("balance", "0")))
        except (ArithmeticError, TypeError, ValueError):
            continue
    held_rows = [
        {"resource_id": resource_id, "balance": format(balance, "f")}
        for resource_id, balance in sorted(held_by_resource.items())
        if balance != 0
    ]
    held_balances_truncated = len(held_rows) > MAX_CONTEXT_ROWS
    held_balances = held_rows[:MAX_CONTEXT_ROWS]

    accessible_routes = {
        row["route_id"]: row for row in travel_rows
    }
    shipment_rows = []
    for option in scene.get("shipment_dispatch_options", []):
        route = accessible_routes.get(option.get("route_id"))
        destination_id = option.get("destination_owner_id")
        if route is None or destination_id != route["to_place_id"]:
            continue
        shipment_rows.append({
            "route_id": route["route_id"],
            "from_place_id": route["from_place_id"],
            "from_place_name": route["from_place_name"],
            "to_place_id": route["to_place_id"],
            "to_place_name": route["to_place_name"],
            "resource_id": option["resource_id"],
            "available_quantity": option["available_quantity"],
        })
    shipment_rows.sort(key=lambda row: (
        row["route_id"], row["to_place_id"], row["resource_id"],
    ))
    shipment_options = shipment_rows[:MAX_CONTEXT_ROWS]

    context = {
        "snapshot_tick": state.current_tick,
        "travel_options": travel_options,
        "travel_options_truncated": len(travel_rows) > MAX_CONTEXT_ROWS,
        "safe_conduct_proposal_options": safe_options,
        "safe_conduct_proposal_options_truncated": safe_options_count > MAX_CONTEXT_ROWS,
        "own_safe_conduct_treaties": own_treaties,
        "own_safe_conduct_treaties_truncated": len(safe_context.get("own_safe_conduct_treaties", [])) > MAX_CONTEXT_ROWS,
        "held_resource_balances": held_balances,
        "held_resource_balances_truncated": held_balances_truncated,
        "shipment_dispatch_options": shipment_options,
        "shipment_dispatch_options_truncated": len(shipment_rows) > MAX_CONTEXT_ROWS,
    }

    reference_ids: set[str] = set()
    if (credit := scene.get("institutional_credit")) is not None:
        context["institutional_credit"] = credit
        reference_ids.update(row["credit_id"] for row in credit["institutional_credit_contracts"])
        for row in credit["institutional_credit_contracts"]:
            reference_ids.update(extension["extension_id"] for extension in row["extensions"])
        finance = institutional_finance_context(state, actor_id, credit)
        context["institutional_finance"] = finance
        for organization in finance["organizations"]:
            reference_ids.add(organization["organization_id"])
            for currency in organization["currencies"]:
                reference_ids.update(currency["due_credit_ids"])
    for row in own_treaties:
        for key in ("treaty_id", "proposer_organization_id", "recipient_organization_id",
                    "route_id", "from_place_id", "to_place_id"):
            value = row.get(key)
            if isinstance(value, str):
                reference_ids.add(value)
    for rows in (travel_options, safe_options, shipment_options):
        for row in rows:
            for key in (
                "route_id", "from_place_id", "to_place_id",
                "proposer_organization_id", "recipient_organization_id",
            ):
                value = row.get(key)
                if isinstance(value, str):
                    reference_ids.add(value)
    return context, reference_ids
