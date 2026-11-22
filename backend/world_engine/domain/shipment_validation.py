"""Commit-bound authorization and provenance checks for shipment spoilage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID, uuid5

from .components import InventoryComponent
from .errors import InvalidEvent
from .gameplay_events import ShipmentSpoilageResolvedEvent
from .models import DomainEvent, WorldState
from .shipment import ShipmentComponent


def validate_shipment_spoilage_batch(
    state: WorldState,
    actor_id: UUID,
    events: Sequence[DomainEvent],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    command_type: str | None,
    command_payload: Mapping[str, object] | None,
) -> None:
    """Bind one exact expiry effect to its pre-state schedule and host command.

    The rule resolver decides which exact boundary to process. This repository
    hook confirms that the event belongs to this committed world, branch and
    system command, and that the pre-state contains the matching shipment and
    due schedule. The reducer repeats custody, lot and sink compare-and-swap
    checks during replay.
    """

    spoilage_events = [event for event in events if isinstance(event, ShipmentSpoilageResolvedEvent)]
    if not spoilage_events:
        return
    if len(events) != 1 or len(spoilage_events) != 1:
        raise InvalidEvent("Shipment spoilage must be one atomic scheduled event.")
    event = spoilage_events[0]
    if (
        event.world_id != world_id
        or event.branch_id != branch_id
        or event.command_id != command_id
        or event.ruleset_version != ruleset_version
        or event.spoiled_tick != state.current_tick
        or event.event_id != uuid5(
            command_id,
            f"{ruleset_version}:shipment.spoilage:{event.shipment_id}:{event.scheduled_event_id}",
        )
    ):
        raise InvalidEvent("Shipment spoilage provenance does not match its committed system command.")
    if command_type is not None and command_type != "simulation.window":
        raise InvalidEvent("Shipment spoilage may be resolved only inside a fenced simulation window.")
    if command_type is not None and command_payload is None:
        raise InvalidEvent("Shipment spoilage system command must retain its job payload.")

    entity = state.entities.get(event.shipment_id)
    if entity is None or entity.kind != "shipment":
        raise InvalidEvent("Shipment spoilage target does not exist in the command pre-state.")
    try:
        shipment = ShipmentComponent.model_validate(entity.components.get("shipment"))
        inventory = InventoryComponent.model_validate(entity.components.get("inventory"))
    except Exception as exc:
        raise InvalidEvent("Shipment spoilage target has invalid typed custody state.") from exc
    if (
        shipment.lifecycle_version != "expiry_v2"
        or shipment.revision != event.expected_shipment_revision
        or shipment.status != "awaiting_receipt"
        or shipment.spoilage_schedule_id != event.scheduled_event_id
        or len(shipment.cargo) != 1
    ):
        raise InvalidEvent("Shipment spoilage event does not match the shipment custody revision.")
    schedule = next((item for item in state.scheduled_events if item.id == event.scheduled_event_id), None)
    arrival = next((item for item in state.scheduled_events if item.id == shipment.arrival_schedule_id), None)
    if (
        schedule is None
        or arrival is None
        or arrival.status != "completed"
        or schedule.revision != event.expected_schedule_revision
        or schedule.status != "queued"
        or schedule.handler_id != "shipment.spoilage"
        or schedule.handler_version != "1.0.0"
        or schedule.phase != "delivery"
        or schedule.due_tick != state.current_tick
        or schedule.max_runs != 1
        or schedule.repeat_ticks is not None
        or schedule.dependencies != [shipment.arrival_schedule_id]
        or schedule.payload != {"shipment_id": str(entity.id)}
    ):
        raise InvalidEvent("Shipment spoilage schedule is not the exact due pre-state occurrence.")
    cargo = shipment.cargo[0]
    transit = state.resources.get(cargo.transit_account_id)
    if (
        event.resource_id != cargo.resource_id
        or event.transit_account_id != cargo.transit_account_id
        or inventory.revision != event.expected_transit_inventory_revision
        or transit is None
        or transit.account_type != "holder"
        or transit.owner_id != entity.id
        or transit.resource_id != cargo.resource_id
        or transit.balance != event.expected_transit_account_balance
        or transit.balance != shipment.remaining_quantity
    ):
        raise InvalidEvent("Shipment spoilage event has stale account or inventory expectations.")
    earliest_expiry = min(
        (lot.expiry_tick for lot in inventory.lots if lot.expiry_tick is not None),
        default=None,
    )
    due_lots = sorted(
        (lot for lot in inventory.lots if lot.expiry_tick is not None and lot.expiry_tick <= state.current_tick),
        key=lambda lot: str(lot.lot_id),
    )
    if earliest_expiry != state.current_tick or len(due_lots) != len(event.spoiled_lots):
        raise InvalidEvent("Shipment spoilage event must cover every exact lot due at this tick.")
    for lot, spoiled in zip(due_lots, event.spoiled_lots, strict=True):
        if (
            lot.lot_id != spoiled.transit_lot_id
            or lot.quantity != spoiled.quantity
            or spoiled.spoiled_tick != state.current_tick
            or spoiled.event_id != event.event_id
            or spoiled.destruction_sink_account_id != event.destruction_sink_account_id
        ):
            raise InvalidEvent("Shipment spoilage receipt differs from the authoritative FIFO lot.")

    roles = state.metadata.get("resource_roles", {})
    configured = roles.get("destruction_sink_account_ids", {}) if isinstance(roles, dict) else {}
    has_configured_sink = isinstance(configured, dict) and cargo.resource_id in configured
    if has_configured_sink:
        try:
            sink_id = UUID(str(configured[cargo.resource_id]))
        except (TypeError, ValueError) as exc:
            raise InvalidEvent("Configured destruction sink ID is invalid.") from exc
        if sink_id != event.destruction_sink_account_id or event.create_destruction_sink:
            raise InvalidEvent("Shipment spoilage must use its configured existing destruction sink.")
    else:
        sink_id = uuid5(world_id, f"resource-destruction-sink:{cargo.resource_id}")
        if sink_id != event.destruction_sink_account_id:
            raise InvalidEvent("Unconfigured destruction sink must use its stable world/resource UUID.")
    sink = state.resources.get(sink_id)
    if sink is None:
        if has_configured_sink or not event.create_destruction_sink or event.expected_destruction_sink_balance is not None:
            raise InvalidEvent("Shipment spoilage sink is missing or cannot be created by this event.")
    elif (
        event.create_destruction_sink
        or sink.account_type != "destruction_sink"
        or sink.owner_id is not None
        or sink.resource_id != cargo.resource_id
        or sink.balance != event.expected_destruction_sink_balance
    ):
        raise InvalidEvent("Shipment spoilage sink compare-and-swap or account role is invalid.")

    expected_next_due = min(
        (
            lot.expiry_tick for lot in inventory.lots
            if lot.lot_id not in {item.transit_lot_id for item in event.spoiled_lots}
            and lot.expiry_tick is not None
        ),
        default=None,
    )
    if event.next_spoilage_due_tick != expected_next_due:
        raise InvalidEvent("Shipment spoilage event does not carry the exact next expiry boundary.")
    if expected_next_due is not None:
        expected_next_id = uuid5(
            command_id,
            f"{ruleset_version}:shipment.spoilage-schedule:{entity.id}:{expected_next_due}",
        )
        if event.next_spoilage_schedule_id != expected_next_id:
            raise InvalidEvent("Shipment next expiry schedule ID is not bound to its command.")
    elif event.next_spoilage_schedule_id is not None:
        raise InvalidEvent("Shipment spoilage event cannot add a schedule when no expiry remains.")
    if event.visibility != {
        "policy": "subject_ids",
        "subject_ids": sorted({str(shipment.sender_id), str(shipment.destination_owner_id)}),
    }:
        raise InvalidEvent("Shipment spoilage must remain visible only to its sender and designated receiver.")
