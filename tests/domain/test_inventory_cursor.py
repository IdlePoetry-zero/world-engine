from decimal import Decimal
from uuid import uuid4

import pytest

from world_engine.domain.components import InventoryComponent, InventoryLot
from world_engine.domain.inventory import FifoInventoryCursor, allocate_fifo_lots, reduce_fifo_inventory


def test_window_fifo_cursor_matches_repeated_fifo_reduction_with_expiry_and_revisions():
    lot_ids = [uuid4() for _ in range(4)]
    food_a, food_b, water_a = uuid4(), uuid4(), uuid4()
    inventory = InventoryComponent(
        revision=9,
        lots=[
            InventoryLot(lot_id=lot_ids[0], account_id=food_a, resource_id="core.food", quantity="0.5", expiry_tick=10, revision=4),
            InventoryLot(lot_id=lot_ids[1], account_id=food_b, resource_id="core.food", quantity="1.2", revision=2),
            InventoryLot(lot_id=lot_ids[2], account_id=food_a, resource_id="core.food", quantity="2", expiry_tick=100, revision=3),
            InventoryLot(lot_id=lot_ids[3], account_id=water_a, resource_id="core.water", quantity="1", revision=7),
        ],
    )
    requests = [
        ("core.food", Decimal("1"), 0),
        ("core.water", Decimal("0.25"), 0),
        ("core.food", Decimal("1"), 10),
    ]

    cursor = FifoInventoryCursor(inventory)
    cursor_results = [
        cursor.consume(resource_id=resource_id, quantity=quantity, at_tick=tick)
        for resource_id, quantity, tick in requests
    ]
    with pytest.raises(ValueError, match="do not cover"):
        cursor.consume(resource_id="core.food", quantity=Decimal("1"), at_tick=100)
    cursor_result = cursor.finish()

    reference = inventory
    reference_results = []
    for resource_id, quantity, tick in requests:
        allocations = allocate_fifo_lots(reference, resource_id=resource_id, quantity=quantity, at_tick=tick)
        reference_results.append(allocations)
        reference = reduce_fifo_inventory(reference, allocations)
    with pytest.raises(ValueError, match="do not cover"):
        allocate_fifo_lots(reference, resource_id="core.food", quantity=Decimal("1"), at_tick=100)
    reference = reference.model_copy(update={"revision": inventory.revision + 1})

    assert cursor_results == reference_results
    assert cursor_result == reference
    assert cursor_result.revision == 10
    remaining = {lot.lot_id: lot for lot in cursor_result.lots}
    assert set(remaining) == {lot_ids[2], lot_ids[3]}
    assert remaining[lot_ids[2]].quantity == Decimal("1.7")
    assert remaining[lot_ids[2]].revision == 4
    assert remaining[lot_ids[3]].quantity == Decimal("0.75")
    assert remaining[lot_ids[3]].revision == 8


def test_failed_fifo_cursor_request_does_not_consume_a_partial_prefix():
    lot_id, account_id = uuid4(), uuid4()
    inventory = InventoryComponent(lots=[
        InventoryLot(lot_id=lot_id, account_id=account_id, resource_id="core.food", quantity="0.5")
    ])
    cursor = FifoInventoryCursor(inventory)

    with pytest.raises(ValueError, match="do not cover"):
        cursor.consume(resource_id="core.food", quantity=Decimal("1"), at_tick=0)

    assert cursor.finish() == inventory
