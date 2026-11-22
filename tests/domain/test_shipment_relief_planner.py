from decimal import Decimal
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import select

from world_engine.domain.components import (
    GovernanceComponent,
    InventoryComponent,
    InventoryLot,
    NeedValue,
    NeedsComponent,
    PlaceComponent,
    ScheduledEventRecord,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.reducers import apply_event, ledger_postings, validate_ledger_postings
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    JobStatus,
    NewWorld,
    ResourceAccount,
    WorldEntity,
    WorldState,
)
from world_engine.domain.shipment import (
    ShipmentCargoManifest,
    ShipmentComponent,
    ShipmentDeliveredLot,
    ShipmentLotManifest,
)
from world_engine.domain.shipment_relief import plan_shipment_relief
from world_engine.domain.shipment_relief_events import (
    ShipmentReliefDistributedEvent,
    shipment_relief_event_id,
    shipment_relief_visibility,
)
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.simulation import WorldAdvancePlan
from world_engine.storage.database import Database
from world_engine.storage.models import EventRow, LedgerPostingRow
from world_engine.storage.repository import WorldRepository


FOOD = "feudal.food"


def _id(value: int) -> UUID:
    return UUID(int=value)


def _world(
    *,
    depot_region_id: UUID | None = None,
    headquarters_id: UUID | None = None,
    actor_location_id: UUID | None = None,
    recipient_location_id: UUID | None = None,
    leader_id: UUID | None = None,
    organization_kind: str = "polity",
    metadata: dict | None = None,
) -> tuple[WorldState, dict[str, UUID]]:
    actor_id, recipient_id = _id(1), _id(2)
    organization_id, region_id, depot_id = _id(3), _id(4), _id(5)
    origin_id, shipment_id = _id(6), _id(7)
    source_account_id, transit_account_id = _id(8), _id(9)
    depot_account_id, recipient_account_id = _id(10), _id(11)
    source_lot_one, source_lot_two = _id(12), _id(13)
    transit_lot_one, transit_lot_two = _id(14), _id(15)
    receipt_lot_one, receipt_lot_two = _id(16), _id(17)
    unrelated_lot = _id(18)
    arrival_schedule_id = _id(19)
    other_region_id = _id(30)

    manifest = ShipmentCargoManifest(
        resource_id=FOOD,
        source_account_id=source_account_id,
        transit_account_id=transit_account_id,
        destination_account_id=depot_account_id,
        quantity=Decimal("5"),
        lots=[
            ShipmentLotManifest(source_lot_id=source_lot_one, transit_lot_id=transit_lot_one, quantity=Decimal("2")),
            ShipmentLotManifest(source_lot_id=source_lot_two, transit_lot_id=transit_lot_two, quantity=Decimal("3")),
        ],
    )
    shipment = ShipmentComponent(
        dispatch_event_id=_id(21),
        arrival_schedule_id=arrival_schedule_id,
        sender_id=actor_id,
        source_owner_id=actor_id,
        destination_owner_id=depot_id,
        origin_place_id=origin_id,
        destination_place_id=depot_id,
        route_id=_id(22),
        departure_tick=0,
        arrival_tick=10,
        status="delivered",
        arrived_tick=10,
        cargo=[manifest],
        delivered_lots=[
            ShipmentDeliveredLot(
                transit_lot_id=transit_lot_one,
                destination_lot_id=receipt_lot_one,
                quantity=Decimal("2"),
                received_tick=10,
            ),
            ShipmentDeliveredLot(
                transit_lot_id=transit_lot_two,
                destination_lot_id=receipt_lot_two,
                quantity=Decimal("3"),
                received_tick=10,
            ),
        ],
    )

    entities = {
        actor_id: WorldEntity(
            id=actor_id,
            kind="character",
            location_id=actor_location_id or depot_id,
        ),
        recipient_id: WorldEntity(
            id=recipient_id,
            kind="character",
            location_id=recipient_location_id or depot_id,
            components={
                "inventory": InventoryComponent(
                    lots=[
                        InventoryLot(
                            lot_id=_id(23),
                            account_id=recipient_account_id,
                            resource_id=FOOD,
                            quantity=Decimal("2"),
                        )
                    ]
                ).model_dump(mode="json")
            },
        ),
        organization_id: WorldEntity(
            id=organization_id,
            kind=organization_kind,
            location_id=headquarters_id or region_id,
            components={
                "governance": GovernanceComponent(
                    members=[actor_id, recipient_id], leader_id=leader_id or actor_id
                ).model_dump(mode="json")
            },
        ),
        region_id: WorldEntity(
            id=region_id,
            kind="place",
            components={"place": PlaceComponent(place_type="region").model_dump(mode="json")},
        ),
        other_region_id: WorldEntity(
            id=other_region_id,
            kind="place",
            components={"place": PlaceComponent(place_type="region").model_dump(mode="json")},
        ),
        depot_id: WorldEntity(
            id=depot_id,
            kind="place",
            components={
                "place": PlaceComponent(
                    place_type="building", region_id=depot_region_id or region_id
                ).model_dump(mode="json"),
                "inventory": InventoryComponent(
                    lots=[
                        # This unrelated same-resource lot is deliberately first;
                        # allocations must be scoped to the delivered receipt set.
                        InventoryLot(
                            lot_id=unrelated_lot,
                            account_id=depot_account_id,
                            resource_id=FOOD,
                            quantity=Decimal("20"),
                        ),
                        InventoryLot(
                            lot_id=receipt_lot_one,
                            account_id=depot_account_id,
                            resource_id=FOOD,
                            quantity=Decimal("2"),
                            quality=42,
                            expiry_tick=500,
                        ),
                        InventoryLot(
                            lot_id=receipt_lot_two,
                            account_id=depot_account_id,
                            resource_id=FOOD,
                            quantity=Decimal("3"),
                            quality=55,
                            expiry_tick=600,
                        ),
                    ]
                ).model_dump(mode="json"),
            },
        ),
        origin_id: WorldEntity(
            id=origin_id,
            kind="place",
            components={"place": PlaceComponent(place_type="site").model_dump(mode="json")},
        ),
        shipment_id: WorldEntity(
            id=shipment_id,
            kind="shipment",
            location_id=depot_id,
            components={
                "shipment": shipment.model_dump(mode="json"),
                "inventory": InventoryComponent().model_dump(mode="json"),
            },
        ),
    }
    resources = {
        source_account_id: ResourceAccount(resource_id=FOOD, balance=Decimal("0"), owner_id=actor_id),
        transit_account_id: ResourceAccount(
            resource_id=FOOD, balance=Decimal("0"), owner_id=shipment_id
        ),
        depot_account_id: ResourceAccount(
            resource_id=FOOD, balance=Decimal("25"), owner_id=depot_id
        ),
        recipient_account_id: ResourceAccount(
            resource_id=FOOD, balance=Decimal("2"), owner_id=recipient_id
        ),
    }
    state = WorldState(
        current_tick=20,
        entities=entities,
        resources=resources,
        scheduled_events=[
            ScheduledEventRecord(
                id=arrival_schedule_id,
                due_tick=10,
                phase="delivery",
                handler_id="shipment.arrival",
                handler_version="1.0.0",
                status="completed",
                completed_runs=1,
                payload={"shipment_id": str(shipment_id)},
            )
        ],
            metadata=metadata if metadata is not None else {"resource_roles": {"food_resource_id": FOOD}},
    )
    return state, {
        "actor": actor_id,
        "recipient": recipient_id,
        "organization": organization_id,
        "region": region_id,
        "depot": depot_id,
        "shipment": shipment_id,
        "source_account": depot_account_id,
        "recipient_account": recipient_account_id,
        "unrelated_lot": unrelated_lot,
        "receipt_lot_one": receipt_lot_one,
        "receipt_lot_two": receipt_lot_two,
        "transit_lot_one": transit_lot_one,
        "transit_lot_two": transit_lot_two,
    }


def test_planner_uses_fifo_only_from_exact_shipment_receipts_and_supports_fractional_quantity():
    state, ids = _world()

    plan = plan_shipment_relief(
        state,
        actor_id=ids["actor"],
        organization_id=ids["organization"],
        shipment_id=ids["shipment"],
        recipient_id=ids["recipient"],
        quantity=Decimal("3.5"),
    )

    assert plan.actor_id == ids["actor"]
    assert plan.region_id == ids["region"]
    assert plan.depot_id == ids["depot"]
    assert plan.resource_id == FOOD
    assert plan.quantity == Decimal("3.5")
    assert plan.expected_source_balance == Decimal("25")
    assert plan.expected_recipient_balance == Decimal("2")
    assert plan.expected_depot_inventory_revision == 1
    assert plan.expected_recipient_inventory_revision == 1
    assert plan.expected_shipment_revision == 1
    assert [(row.destination_lot_id, row.quantity) for row in plan.allocations] == [
        (ids["receipt_lot_one"], Decimal("2")),
        (ids["receipt_lot_two"], Decimal("1.5")),
    ]
    assert [row.transit_lot_id for row in plan.allocations] == [
        ids["transit_lot_one"], ids["transit_lot_two"]
    ]
    assert all(row.destination_lot_id != ids["unrelated_lot"] for row in plan.allocations)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"actor_location_id": _id(30)}, "both people at that exact place"),
        ({"recipient_location_id": _id(30)}, "both people at that exact place"),
        ({"leader_id": _id(2)}, "current member leader"),
        ({"organization_kind": "company"}, "active polity or government"),
        ({"headquarters_id": _id(30)}, "headquartered in the depot region"),
        ({"depot_region_id": _id(30)}, "headquartered in the depot region"),
        ({"metadata": {}}, "no registered food resource"),
        ({"metadata": {"resource_roles": {"food_resource_id": "core.water"}}}, "manifest for the registered food"),
    ],
)
def test_planner_rejects_invalid_authority_location_region_or_food_registration(overrides, message):
    state, ids = _world(**overrides)

    with pytest.raises(InvalidEvent, match=message):
        plan_shipment_relief(
            state,
            actor_id=ids["actor"],
            organization_id=ids["organization"],
            shipment_id=ids["shipment"],
            recipient_id=ids["recipient"],
            quantity=Decimal("1"),
        )


def test_planner_refuses_stock_from_nonreceipt_lots_and_nonpositive_or_inexact_quantity():
    state, ids = _world()

    with pytest.raises(InvalidEvent, match="delivered shipment food still available"):
        plan_shipment_relief(
            state,
            actor_id=ids["actor"],
            organization_id=ids["organization"],
            shipment_id=ids["shipment"],
            recipient_id=ids["recipient"],
            quantity=Decimal("6"),
        )
    for quantity in (Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("1.0000001")):
        with pytest.raises(InvalidEvent, match="quantity"):
            plan_shipment_relief(
                state,
                actor_id=ids["actor"],
                organization_id=ids["organization"],
                shipment_id=ids["shipment"],
                recipient_id=ids["recipient"],
                quantity=quantity,
            )
    with pytest.raises(InvalidEvent, match="quantity"):
        plan_shipment_relief(
            state,
            actor_id=ids["actor"],
            organization_id=ids["organization"],
            shipment_id=ids["shipment"],
            recipient_id=ids["recipient"],
            quantity=1.0,  # type: ignore[arg-type]
        )


def test_planner_rejects_missing_or_ambiguous_recipient_food_account():
    state, ids = _world()
    without_account = state.model_copy(
        update={"resources": {key: value for key, value in state.resources.items() if key != ids["recipient_account"]}}
    )
    with pytest.raises(InvalidEvent, match="one verified recipient food holder account"):
        plan_shipment_relief(
            without_account,
            actor_id=ids["actor"],
            organization_id=ids["organization"],
            shipment_id=ids["shipment"],
            recipient_id=ids["recipient"],
            quantity=Decimal("1"),
        )

    duplicate_id = _id(31)
    ambiguous = state.model_copy(update={
        "resources": {
            **state.resources,
            duplicate_id: ResourceAccount(resource_id=FOOD, balance=Decimal("0"), owner_id=ids["recipient"]),
        }
    })
    with pytest.raises(InvalidEvent, match="one verified recipient food holder account"):
        plan_shipment_relief(
            ambiguous,
            actor_id=ids["actor"],
            organization_id=ids["organization"],
            shipment_id=ids["shipment"],
            recipient_id=ids["recipient"],
            quantity=Decimal("1"),
        )


def test_event_reducer_transfers_balanced_food_and_preserves_receipt_lot_quality_and_expiry():
    state, ids = _world()
    plan = plan_shipment_relief(
        state,
        actor_id=ids["actor"],
        organization_id=ids["organization"],
        shipment_id=ids["shipment"],
        recipient_id=ids["recipient"],
        quantity=Decimal("3.5"),
    )
    command_id = _id(110)
    ruleset_version = "shipment-relief.v1"
    event = ShipmentReliefDistributedEvent(
        event_id=shipment_relief_event_id(command_id, ruleset_version, ids["shipment"], ids["recipient"]),
        world_id=_id(111),
        branch_id=_id(112),
        command_id=command_id,
        actor_id=ids["actor"],
        ruleset_version=ruleset_version,
        plan=plan,
        visibility=shipment_relief_visibility(ids["actor"], ids["recipient"]),
    )

    changed = apply_event(state, event)
    postings = ledger_postings(event)
    validate_ledger_postings(postings)

    assert changed.resources[plan.source_account_id].balance == Decimal("21.5")
    assert changed.resources[plan.recipient_account_id].balance == Decimal("5.5")
    assert [(row.account_id, row.resource_id, row.delta) for row in postings] == [
        (plan.source_account_id, FOOD, Decimal("-3.5")),
        (plan.recipient_account_id, FOOD, Decimal("3.5")),
    ]
    depot_inventory = InventoryComponent.model_validate(changed.entities[plan.depot_id].components["inventory"])
    recipient_inventory = InventoryComponent.model_validate(changed.entities[plan.recipient_id].components["inventory"])
    assert [lot.lot_id for lot in depot_inventory.lots] == [
        ids["unrelated_lot"], ids["receipt_lot_two"]
    ]
    assert depot_inventory.lots[1].quantity == Decimal("1.5")
    assert [
        (lot.quantity, lot.quality, lot.expiry_tick)
        for lot in recipient_inventory.lots[-2:]
    ] == [(Decimal("2"), 42, 500), (Decimal("1.5"), 55, 600)]


def test_plan_dto_requires_allocations_to_match_exact_quantity_and_source_account():
    from pydantic import ValidationError

    from world_engine.domain.shipment_relief import ShipmentReliefAllocation, ShipmentReliefPlan

    allocation = ShipmentReliefAllocation(
        destination_lot_id=_id(100),
        transit_lot_id=_id(101),
        account_id=_id(102),
        quantity=Decimal("1"),
    )
    values = {
        "actor_id": _id(103),
        "organization_id": _id(104),
        "region_id": _id(105),
        "shipment_id": _id(106),
        "recipient_id": _id(107),
        "depot_id": _id(108),
        "tick": 20,
        "resource_id": FOOD,
        "source_account_id": _id(102),
        "recipient_account_id": _id(109),
        "quantity": Decimal("1"),
        "allocations": (allocation,),
        "expected_source_balance": Decimal("2"),
        "expected_recipient_balance": Decimal("0"),
        "expected_depot_inventory_revision": 1,
        "expected_recipient_inventory_revision": None,
        "expected_shipment_revision": 1,
    }
    assert ShipmentReliefPlan.model_validate(values).quantity == Decimal("1")
    with pytest.raises(ValidationError, match="must equal"):
        ShipmentReliefPlan.model_validate(values | {"quantity": Decimal("2")})
    with pytest.raises(ValidationError, match="exact depot source account"):
        ShipmentReliefPlan.model_validate(values | {
            "allocations": (allocation.model_copy(update={"account_id": _id(110)}),)
        })


def _critical_empty_food_state() -> tuple[WorldState, dict[str, UUID], UUID]:
    state, ids = _world()
    payload = state.model_dump(mode="json")
    recipient = payload["entities"][str(ids["recipient"])]
    recipient["components"]["inventory"] = InventoryComponent().model_dump(mode="json")
    recipient["components"]["needs"] = NeedsComponent(
        food=NeedValue(value=0, critical_below=20),
    ).model_dump(mode="json")
    payload["player_id"] = str(ids["actor"])
    payload["resources"][str(ids["recipient_account"])]["balance"] = "0"
    sink_account_id = _id(32)
    payload["resources"][str(sink_account_id)] = ResourceAccount(
        resource_id=FOOD,
        balance=Decimal("0"),
        account_type="consumption_sink",
    ).model_dump(mode="json")
    return WorldState.model_validate(payload), ids, sink_account_id


def _advance_with_leased_job(repository, scope, world_id, branch_id, *, target_tick, key):
    view = repository.read_world(scope, world_id, branch_id)
    jobs = SimulationJobs(repository)
    job = jobs.enqueue_world_advance(
        scope,
        world_id,
        branch_id,
        WorldAdvancePlan(started_tick=view.state.current_tick, target_tick=target_tick),
        idempotency_key=key,
        speed="balanced",
        depth="standard",
        expected_state_version=view.branch.state_version,
    )
    lease = repository.claim_job(f"shipment-relief-{key}", job_type="simulation.world_advance")
    assert lease is not None and lease.job_id == job.job_id
    for _ in range(12):
        jobs.advance(scope, lease)
        record = repository.read_job(scope, job.job_id)
        if record.status != JobStatus.RUNNING:
            break
    assert record.status == JobStatus.SUCCEEDED
    result = repository.read_world(scope, world_id, branch_id)
    assert result.state.current_tick == target_tick
    assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == result.state_hash
    return result


def test_leased_world_advance_consumes_only_delivered_relief_stock_and_replays_both_forks(tmp_path):
    state, ids, sink_account_id = _critical_empty_food_state()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database = Database(f"sqlite:///{(tmp_path / 'relief-simulation.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        created = repository.create_world(scope, NewWorld(title="Shipment relief care", state=state))
        world_id = created.world.world_id
        no_relief = repository.fork(
            scope, world_id, created.branch.branch_id, fork_seq=0, label="No relief",
        )
        with_relief = repository.fork(
            scope, world_id, created.branch.branch_id, fork_seq=0, label="With relief",
        )

        view = repository.read_world(scope, world_id, with_relief.branch_id)
        relief_quantity = Decimal("3.5")
        plan = plan_shipment_relief(
            view.state,
            actor_id=ids["actor"],
            organization_id=ids["organization"],
            shipment_id=ids["shipment"],
            recipient_id=ids["recipient"],
            quantity=relief_quantity,
        )
        command = CommandEnvelope(
            command_id=uuid4(),
            actor_id=ids["actor"],
            idempotency_key="grant-relief-before-simulation",
            expected_state_version=view.branch.state_version,
            command_type="action.distribute_shipment_relief",
            payload={
                "kind": "distribute_shipment_relief",
                "organization_id": str(ids["organization"]),
                "shipment_id": str(ids["shipment"]),
                "recipient_id": str(ids["recipient"]),
                "quantity": "3.5",
            },
        )
        relief_event = ShipmentReliefDistributedEvent(
            event_id=shipment_relief_event_id(
                command.command_id, view.world.ruleset_version, ids["shipment"], ids["recipient"]
            ),
            world_id=world_id,
            branch_id=with_relief.branch_id,
            command_id=command.command_id,
            actor_id=ids["actor"],
            ruleset_version=view.world.ruleset_version,
            plan=plan,
            visibility=shipment_relief_visibility(ids["actor"], ids["recipient"]),
        )
        repository.commit_command(
            scope, world_id, with_relief.branch_id, command, [relief_event],
        )

        target_tick = state.current_tick + 3_600
        no_relief_after = _advance_with_leased_job(
            repository, scope, world_id, no_relief.branch_id,
            target_tick=target_tick, key="without-relief",
        )
        with_relief_after = _advance_with_leased_job(
            repository, scope, world_id, with_relief.branch_id,
            target_tick=target_tick, key="with-relief",
        )

        no_relief_need = NeedsComponent.model_validate(
            no_relief_after.state.entities[ids["recipient"]].components["needs"]
        ).food
        with_relief_need = NeedsComponent.model_validate(
            with_relief_after.state.entities[ids["recipient"]].components["needs"]
        ).food
        assert no_relief_need is not None and with_relief_need is not None
        assert no_relief_need.value == 0
        assert with_relief_need.value > no_relief_need.value

        no_relief_sink = no_relief_after.state.resources[sink_account_id]
        with_relief_sink = with_relief_after.state.resources[sink_account_id]
        assert no_relief_sink.balance == Decimal("0")
        assert with_relief_sink.balance > 0
        assert with_relief_sink.balance == Decimal("2")
        recipient_account_id = ids["recipient_account"]
        assert no_relief_after.state.resources[recipient_account_id].balance == Decimal("0")
        assert with_relief_after.state.resources[recipient_account_id].balance == relief_quantity - with_relief_sink.balance

        final_relief_lots = InventoryComponent.model_validate(
            with_relief_after.state.entities[ids["recipient"]].components["inventory"]
        ).lots
        final_lot_ids = {lot.lot_id for lot in final_relief_lots}
        first_relief_lot_id = uuid5(relief_event.event_id, f"relief:{plan.allocations[0].destination_lot_id}")
        assert first_relief_lot_id not in final_lot_ids
        assert sum((lot.quantity for lot in final_relief_lots), Decimal("0")) == Decimal("1.5")

        def sink_postings(branch_id):
            with database.session_factory() as session:
                event_ids = session.scalars(
                    select(EventRow.event_id).where(
                        EventRow.branch_id == branch_id,
                        EventRow.event_type == "core.background_routine_advanced.v1",
                    )
                ).all()
                return session.scalars(
                    select(LedgerPostingRow).where(
                        LedgerPostingRow.event_id.in_(event_ids),
                        LedgerPostingRow.account_id == sink_account_id,
                        LedgerPostingRow.resource_id == FOOD,
                    )
                ).all()

        assert sink_postings(no_relief.branch_id) == []
        assert sum((Decimal(row.delta) for row in sink_postings(with_relief.branch_id)), Decimal("0")) == Decimal("2")
    finally:
        database.dispose()
