from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.domain.components import InventoryComponent, InventoryLot
from world_engine.domain.shipment import (
    ShipmentCargoManifest,
    ShipmentComponent,
    ShipmentLotManifest,
    active_shipments_on_route,
    plan_dispatch_lots,
    shipment_destination_available,
)


def _shipment(*, status="in_transit", arrived_tick=None):
    route_id = uuid4()
    source_lot_id = uuid4()
    sender_id = uuid4()
    shipment = ShipmentComponent(
        dispatch_event_id=uuid4(),
        arrival_schedule_id=uuid4(),
        sender_id=sender_id,
        source_owner_id=sender_id,
        destination_owner_id=uuid4(),
        origin_place_id=uuid4(),
        destination_place_id=uuid4(),
        route_id=route_id,
        departure_tick=0,
        arrival_tick=10,
        status=status,
        arrived_tick=arrived_tick,
        cargo=[
            ShipmentCargoManifest(
                resource_id="resource.food",
                source_account_id=uuid4(),
                transit_account_id=uuid4(),
                destination_account_id=uuid4(),
                quantity="2",
                lots=[ShipmentLotManifest(source_lot_id=source_lot_id, transit_lot_id=uuid4(), quantity="2")],
            )
        ],
    )
    return shipment


def test_shipment_manifest_checks_quantity_and_arrival_state():
    shipment = _shipment()
    assert shipment.status == "in_transit"
    assert shipment.cargo[0].quantity == Decimal("2")

    with pytest.raises(ValueError, match="exactly equal"):
        ShipmentCargoManifest(
            resource_id="resource.food",
            source_account_id=uuid4(),
            transit_account_id=uuid4(),
            destination_account_id=uuid4(),
            quantity="3",
            lots=[ShipmentLotManifest(source_lot_id=uuid4(), transit_lot_id=uuid4(), quantity="2")],
        )

    with pytest.raises(ValueError, match="exact arrival tick"):
        shipment.model_copy(update={"status": "awaiting_receipt", "arrived_tick": 9})
        # model_copy bypasses validators, so explicitly re-parse the public payload.
        ShipmentComponent.model_validate(
            shipment.model_dump(mode="json") | {"status": "awaiting_receipt", "arrived_tick": 9}
        )


def test_dispatch_fifo_excludes_lots_expiring_at_or_before_arrival():
    account_id = uuid4()
    first = InventoryLot(
        lot_id=uuid4(), account_id=account_id, resource_id="resource.food", quantity="2", expiry_tick=100
    )
    second = InventoryLot(
        lot_id=uuid4(), account_id=account_id, resource_id="resource.food", quantity="3", expiry_tick=101
    )
    inventory = InventoryComponent(lots=[first, second])

    selected = plan_dispatch_lots(
        inventory,
        source_account_id=account_id,
        resource_id="resource.food",
        quantity=Decimal("2"),
        arrival_tick=100,
    )
    assert [(item.lot_id, item.quantity) for item in selected] == [(second.lot_id, Decimal("2"))]

    with pytest.raises(ValueError, match="unexpired FIFO"):
        plan_dispatch_lots(
            inventory,
            source_account_id=account_id,
            resource_id="resource.food",
            quantity=Decimal("4"),
            arrival_tick=100,
        )


def test_route_slot_counts_one_active_manifest_through_due_delivery_phase():
    first = _shipment()
    first = ShipmentComponent.model_validate(
        first.model_dump(mode="json")
        | {
            "cargo": [
                *[item.model_dump(mode="json") for item in first.cargo],
                ShipmentCargoManifest(
                    resource_id="resource.water",
                    source_account_id=uuid4(),
                    transit_account_id=uuid4(),
                    destination_account_id=uuid4(),
                    quantity="1",
                    lots=[ShipmentLotManifest(source_lot_id=uuid4(), transit_lot_id=uuid4(), quantity="1")],
                ).model_dump(mode="json"),
            ]
        }
    )
    second = _shipment()
    second = second.model_copy(update={"route_id": first.route_id})
    waiting = _shipment(status="awaiting_receipt", arrived_tick=10)
    waiting = waiting.model_copy(update={"route_id": first.route_id})
    entities = {
        uuid4(): SimpleNamespace(kind="shipment", components={"shipment": first.model_dump(mode="json")}),
        uuid4(): SimpleNamespace(kind="shipment", components={"shipment": second.model_dump(mode="json")}),
        uuid4(): SimpleNamespace(kind="shipment", components={"shipment": waiting.model_dump(mode="json")}),
    }
    state = SimpleNamespace(current_tick=9, entities=entities)

    assert active_shipments_on_route(state, first.route_id) == 2
    assert active_shipments_on_route(state, first.route_id, at_tick=10) == 2
    assert active_shipments_on_route(state, first.route_id, at_tick=11) == 0


def test_destination_availability_requires_fixed_place_or_present_active_person():
    place_id = uuid4()
    person_id = uuid4()
    place = SimpleNamespace(id=place_id, kind="place", status="active", location_id=None)
    person = SimpleNamespace(id=person_id, kind="character", status="active", location_id=place_id)
    state = SimpleNamespace(entities={place_id: place, person_id: person})

    assert shipment_destination_available(state, place_id, place_id)
    assert shipment_destination_available(state, person_id, place_id)

    person.location_id = uuid4()
    assert not shipment_destination_available(state, person_id, place_id)
    person.location_id = place_id
    person.status = "dead"
    assert not shipment_destination_available(state, person_id, place_id)

