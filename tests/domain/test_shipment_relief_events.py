from decimal import Decimal
from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError

from world_engine.domain.errors import InvalidEvent
from world_engine.domain.shipment_relief import ShipmentReliefAllocation, ShipmentReliefPlan
from world_engine.domain.shipment_relief_events import (
    SHIPMENT_RELIEF_DISTRIBUTED_EVENT_TYPE,
    ShipmentReliefDistributedEvent,
    parse_shipment_relief_event,
    shipment_relief_event_id,
    shipment_relief_visibility,
    validate_shipment_relief_batch,
)


WORLD_ID = UUID("10000000-0000-4000-8000-000000000001")
BRANCH_ID = UUID("20000000-0000-4000-8000-000000000002")
COMMAND_ID = UUID("30000000-0000-4000-8000-000000000003")
ACTOR_ID = UUID("40000000-0000-4000-8000-000000000004")
ORGANIZATION_ID = UUID("50000000-0000-4000-8000-000000000005")
REGION_ID = UUID("60000000-0000-4000-8000-000000000006")
SHIPMENT_ID = UUID("70000000-0000-4000-8000-000000000007")
RECIPIENT_ID = UUID("80000000-0000-4000-8000-000000000008")
DEPOT_ID = UUID("90000000-0000-4000-8000-000000000009")
SOURCE_ACCOUNT_ID = UUID("a0000000-0000-4000-8000-00000000000a")
RECIPIENT_ACCOUNT_ID = UUID("b0000000-0000-4000-8000-00000000000b")
TRANSIT_LOT_ID = UUID("c0000000-0000-4000-8000-00000000000c")
DESTINATION_LOT_ID = UUID("d0000000-0000-4000-8000-00000000000d")
RULESET = "shipment-relief.v1"


def _plan(**overrides) -> ShipmentReliefPlan:
    quantity = Decimal("2.500000")
    allocation = ShipmentReliefAllocation(
        destination_lot_id=DESTINATION_LOT_ID,
        transit_lot_id=TRANSIT_LOT_ID,
        account_id=SOURCE_ACCOUNT_ID,
        quantity=quantity,
    )
    values = {
        "actor_id": ACTOR_ID,
        "organization_id": ORGANIZATION_ID,
        "region_id": REGION_ID,
        "shipment_id": SHIPMENT_ID,
        "recipient_id": RECIPIENT_ID,
        "depot_id": DEPOT_ID,
        "tick": 100,
        "resource_id": "core.food",
        "source_account_id": SOURCE_ACCOUNT_ID,
        "recipient_account_id": RECIPIENT_ACCOUNT_ID,
        "quantity": quantity,
        "allocations": (allocation,),
        "expected_source_balance": Decimal("10.000000"),
        "expected_recipient_balance": Decimal("0"),
        "expected_depot_inventory_revision": 4,
        "expected_recipient_inventory_revision": None,
        "expected_shipment_revision": 2,
    }
    return ShipmentReliefPlan.model_validate(values | overrides)


def _event(**overrides) -> ShipmentReliefDistributedEvent:
    plan = _plan()
    values = {
        "event_id": shipment_relief_event_id(COMMAND_ID, RULESET, SHIPMENT_ID, RECIPIENT_ID),
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "actor_id": ACTOR_ID,
        "ruleset_version": RULESET,
        "plan": plan,
        "visibility": shipment_relief_visibility(ACTOR_ID, RECIPIENT_ID),
    }
    return ShipmentReliefDistributedEvent.model_validate(values | overrides)


def _scope(**overrides):
    values = {
        "world_id": WORLD_ID,
        "branch_id": BRANCH_ID,
        "command_id": COMMAND_ID,
        "actor_id": ACTOR_ID,
        "ruleset_version": RULESET,
    }
    return values | overrides


def _payload(**overrides):
    values = {
        "kind": "distribute_shipment_relief",
        "organization_id": str(ORGANIZATION_ID),
        "shipment_id": str(SHIPMENT_ID),
        "recipient_id": str(RECIPIENT_ID),
        "quantity": "2.5",
    }
    return values | overrides


def test_event_is_typed_frozen_and_actor_recipient_private():
    event = _event()

    assert event.type == SHIPMENT_RELIEF_DISTRIBUTED_EVENT_TYPE
    assert event.plan.actor_id == event.actor_id == ACTOR_ID
    assert event.visibility == {
        "policy": "subject_ids", "subject_ids": sorted((str(ACTOR_ID), str(RECIPIENT_ID))),
    }
    assert parse_shipment_relief_event(event.model_dump(mode="json")) == event
    with pytest.raises(ValidationError):
        event.actor_id = uuid4()


def test_event_id_is_deterministic_and_bound_to_command_ruleset_shipment_and_recipient():
    event_id = shipment_relief_event_id(COMMAND_ID, RULESET, SHIPMENT_ID, RECIPIENT_ID)

    assert event_id == uuid5(
        COMMAND_ID, f"{RULESET}:shipment_relief.distributed:{SHIPMENT_ID}:{RECIPIENT_ID}",
    )
    assert event_id == shipment_relief_event_id(COMMAND_ID, RULESET, SHIPMENT_ID, RECIPIENT_ID)
    assert event_id != shipment_relief_event_id(uuid4(), RULESET, SHIPMENT_ID, RECIPIENT_ID)
    assert event_id != shipment_relief_event_id(COMMAND_ID, "other.v1", SHIPMENT_ID, RECIPIENT_ID)
    assert event_id != shipment_relief_event_id(COMMAND_ID, RULESET, uuid4(), RECIPIENT_ID)
    assert event_id != shipment_relief_event_id(COMMAND_ID, RULESET, SHIPMENT_ID, uuid4())
    with pytest.raises(ValueError, match="UUID command, shipment, and recipient"):
        shipment_relief_event_id("bad", RULESET, SHIPMENT_ID, RECIPIENT_ID)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nonempty ruleset"):
        shipment_relief_event_id(COMMAND_ID, " ", SHIPMENT_ID, RECIPIENT_ID)


def test_batch_binds_canonical_json_decimal_and_scope_and_allows_restoration():
    event = _event()

    validate_shipment_relief_batch(
        [event], command_type="action.distribute_shipment_relief", command_payload=_payload(), **_scope(),
    )
    # Restored history does not retain the originating command proposal.
    validate_shipment_relief_batch([event], command_type=None, command_payload=None, **_scope())

    for forged_payload in (
        _payload(quantity="2.500000"),
        _payload(shipment_id=str(uuid4())),
        _payload(recipient_id=str(uuid4())),
        _payload(organization_id=str(uuid4())),
        {key: value for key, value in _payload().items() if key != "quantity"},
        _payload(extra=True),
    ):
        with pytest.raises(InvalidEvent, match="does not match its command proposal"):
            validate_shipment_relief_batch(
                [event], command_type="action.distribute_shipment_relief",
                command_payload=forged_payload, **_scope(),
            )


def test_batch_requires_one_scoped_event_and_correct_command():
    event = _event()

    with pytest.raises(InvalidEvent, match="requires its typed event"):
        validate_shipment_relief_batch(
            [], command_type="action.distribute_shipment_relief", command_payload=_payload(), **_scope(),
        )
    with pytest.raises(InvalidEvent, match="standalone event"):
        validate_shipment_relief_batch(
            [event, object()], command_type="action.distribute_shipment_relief",
            command_payload=_payload(), **_scope(),
        )
    with pytest.raises(InvalidEvent, match="standalone event"):
        validate_shipment_relief_batch(
            [event], command_type="action.distribute_shipment_relief",
            command_payload=_payload(), **_scope(actor_id=uuid4()),
        )
    with pytest.raises(InvalidEvent, match="does not match its command proposal"):
        validate_shipment_relief_batch(
            [event], command_type="action.other", command_payload=_payload(), **_scope(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor_id", uuid4()),
        ("event_id", uuid4()),
        ("visibility", {"policy": "public"}),
        ("visibility", {"policy": "subject_ids", "subject_ids": [str(ACTOR_ID)]}),
        ("visibility", {"policy": "subject_ids", "subject_ids": [str(ACTOR_ID), str(uuid4())]}),
        ("unexpected", True),
    ],
)
def test_event_rejects_actor_identity_visibility_and_extra_field_forgery(field, value):
    values = _event().model_dump(mode="python")
    values[field] = value

    with pytest.raises(ValidationError):
        ShipmentReliefDistributedEvent.model_validate(values)
