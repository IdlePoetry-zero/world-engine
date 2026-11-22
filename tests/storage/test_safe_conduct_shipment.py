"""A dispatched shipment keeps custody after its departure treaty expires."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from world_engine.domain.components import (
    CapabilitiesComponent,
    GovernanceComponent,
    InventoryComponent,
    InventoryLot,
    OfficeRecord,
    PlaceComponent,
    PlaceRoute,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import (
    ShipmentArrivalDeliveredEvent,
    ShipmentDispatchedEvent,
)
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    NewWorld,
    ResourceAccount,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.domain.reducers import ledger_postings, state_hash
from world_engine.domain.safe_conduct import SAFE_CONDUCT_TAG
from world_engine.domain.safe_conduct_state import (
    PROPOSE_SAFE_CONDUCT_COMMAND,
    RESOLVE_SAFE_CONDUCT_COMMAND,
)
from world_engine.domain.shipment import ShipmentComponent
from world_engine.rules import RuleContext, RuleEngine, parse_action
from world_engine.storage.database import Database
from world_engine.storage.models import LedgerPostingRow
from world_engine.storage.repository import WorldRepository


START_TICK = 1_000
EXPIRY_TICK = 1_050
ARRIVAL_TICK = 1_060
RESOURCE_ID = "core.grain"


def _state() -> tuple[WorldState, dict[str, object]]:
    origin_id, destination_id = uuid4(), uuid4()
    sender_id, receiver_id = uuid4(), uuid4()
    source_org_id, recipient_org_id = uuid4(), uuid4()
    route_id, source_account_id, destination_account_id, source_lot_id = (uuid4() for _ in range(4))

    route = PlaceRoute(
        route_id=route_id,
        from_place_id=origin_id,
        to_place_id=destination_id,
        travel_ticks=ARRIVAL_TICK - START_TICK,
        access_tags=[SAFE_CONDUCT_TAG],
        travel_modes=["travel.walk"],
    )
    origin = WorldEntity(
        id=origin_id,
        kind="place",
        name="West Grain Port",
        components={"place": PlaceComponent(place_type="region", routes=[route]).model_dump(mode="json")},
    )
    destination = WorldEntity(
        id=destination_id,
        kind="place",
        name="East Grain Port",
        components={"place": PlaceComponent(place_type="region").model_dump(mode="json")},
    )
    sender_inventory = InventoryComponent(lots=[InventoryLot(
        lot_id=source_lot_id,
        account_id=source_account_id,
        resource_id=RESOURCE_ID,
        quantity=Decimal("5"),
        expiry_tick=2_000,
    )])
    sender = WorldEntity(
        id=sender_id,
        kind="character",
        name="Mira Vale",
        location_id=origin_id,
        components={
            "capabilities": CapabilitiesComponent(grants=["world.travel"]).model_dump(mode="json"),
            "inventory": sender_inventory.model_dump(mode="json"),
        },
    )
    receiver = WorldEntity(
        id=receiver_id,
        kind="character",
        name="Sera North",
        location_id=destination_id,
        components={"inventory": InventoryComponent().model_dump(mode="json")},
    )

    def organization(org_id, name, location_id, leader_id):
        governance = GovernanceComponent(
            members=[leader_id],
            leader_id=leader_id,
            offices=[OfficeRecord(
                office_id="office.leader",
                title="Representative",
                holder_id=leader_id,
                authority=["organization.lead", "authority.represent"],
            )],
            succession_policy="appointment",
        )
        return WorldEntity(
            id=org_id,
            kind="organization",
            name=name,
            location_id=location_id,
            components={"governance": governance.model_dump(mode="json")},
        )

    source_org = organization(source_org_id, "West Grain Compact", origin_id, sender_id)
    recipient_org = organization(recipient_org_id, "East Grain Assembly", destination_id, receiver_id)
    state = WorldState(
        current_tick=START_TICK,
        player_id=sender_id,
        entities={entity.id: entity for entity in (
            origin, destination, sender, receiver, source_org, recipient_org,
        )},
        resources={
            source_account_id: ResourceAccount(
                resource_id=RESOURCE_ID, balance="5", owner_id=sender_id,
            ),
            destination_account_id: ResourceAccount(
                resource_id=RESOURCE_ID, balance="0", owner_id=receiver_id,
            ),
        },
    )
    return state, {
        "origin_id": origin_id,
        "destination_id": destination_id,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "source_org_id": source_org_id,
        "recipient_org_id": recipient_org_id,
        "route_id": route_id,
        "source_account_id": source_account_id,
        "destination_account_id": destination_account_id,
        "source_lot_id": source_lot_id,
    }


def _prepare_action(repository, scope, world_id, branch_id, actor_id, payload):
    view = repository.read_world(scope, world_id, branch_id)
    command_id = uuid4()
    normalized = parse_action(payload).model_dump(mode="json")
    context = RuleContext(
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        actor_id=actor_id,
        ruleset_version=view.world.ruleset_version,
    )
    resolution = RuleEngine().evaluate(view.state, normalized, context)
    return view, context, normalized, resolution


def _commit_action(repository, scope, world_id, branch_id, actor_id, payload, idempotency_key):
    view, context, normalized, resolution = _prepare_action(
        repository, scope, world_id, branch_id, actor_id, payload,
    )
    assert resolution.outcome.status == "succeeded", resolution.outcome.model_dump(mode="json")
    command_type = {
        "propose_safe_conduct": PROPOSE_SAFE_CONDUCT_COMMAND,
        "resolve_safe_conduct": RESOLVE_SAFE_CONDUCT_COMMAND,
    }.get(normalized["kind"], f"action.{normalized['kind']}")
    command = CommandEnvelope(
        command_id=context.command_id,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        expected_state_version=view.branch.state_version,
        command_type=command_type,
        payload=normalized,
    )
    return repository.commit_command(scope, world_id, branch_id, command, resolution.events), resolution


def _ledger_rows(database, event_id):
    with database.session_factory() as session:
        rows = session.scalars(
            select(LedgerPostingRow)
            .where(LedgerPostingRow.event_id == event_id)
            .order_by(LedgerPostingRow.posting_index)
        ).all()
    return [(row.account_id, row.resource_id, Decimal(row.delta)) for row in rows]


def test_shipment_departure_authorized_before_treaty_expiry_and_arrives_after(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'safe-conduct-shipment.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        state, ids = _state()
        created = repository.create_world(
            scope,
            NewWorld(title="Treaty shipment", ruleset_version="safe-conduct-shipment.v1", state=state),
        )
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        sender_id = ids["sender_id"]
        receiver_id = ids["receiver_id"]

        _, proposal = _commit_action(
            repository, scope, world_id, branch_id, sender_id,
            {
                "kind": "propose_safe_conduct",
                "proposer_organization_id": str(ids["source_org_id"]),
                "recipient_organization_id": str(ids["recipient_org_id"]),
                "from_place_id": str(ids["origin_id"]),
                "route_id": str(ids["route_id"]),
                "expires_at_tick": EXPIRY_TICK,
            },
            "propose-grain-safe-conduct",
        )
        treaty_id = proposal.events[0].resulting_treaty.treaty_id
        _, ratification = _commit_action(
            repository, scope, world_id, branch_id, receiver_id,
            {"kind": "resolve_safe_conduct", "treaty_id": str(treaty_id),
             "expected_revision": 1, "operation": "ratify"},
            "ratify-grain-safe-conduct",
        )
        assert ratification.events[0].resulting_treaty.status == "active"

        dispatch_payload = {
            "kind": "dispatch_shipment",
            "route_id": str(ids["route_id"]),
            "source_owner_id": str(sender_id),
            "destination_owner_id": str(receiver_id),
            "resource_id": RESOURCE_ID,
            "quantity": "1",
            "source_account_id": str(ids["source_account_id"]),
            "destination_account_id": str(ids["destination_account_id"]),
        }
        dispatch_commit, dispatch_resolution = _commit_action(
            repository, scope, world_id, branch_id, sender_id, dispatch_payload,
            "dispatch-grain-before-treaty-expiry",
        )
        dispatch = next(event for event in dispatch_resolution.events if isinstance(event, ShipmentDispatchedEvent))
        assert dispatch.departure_tick == START_TICK
        assert dispatch.arrival_tick == ARRIVAL_TICK
        assert sum((row.delta for row in ledger_postings(dispatch)), Decimal("0")) == 0
        assert _ledger_rows(database, dispatch.event_id) == [
            (ids["source_account_id"], RESOURCE_ID, Decimal("-1")),
            (dispatch.transit_account_id, RESOURCE_ID, Decimal("1")),
        ]

        after_dispatch = repository.read_world(scope, world_id, branch_id)
        assert after_dispatch.state.resources[ids["source_account_id"]].balance == Decimal("4")
        assert after_dispatch.state.resources[dispatch.transit_account_id].balance == Decimal("1")
        assert after_dispatch.state.entities[sender_id].components["inventory"]["lots"][0]["quantity"] == "4"

        # Reach the treaty's inclusive end boundary before the cargo arrives.
        expiry_command = CommandEnvelope(
            actor_id=scope.user_id,
            idempotency_key="advance-to-safe-conduct-expiry",
            expected_state_version=after_dispatch.branch.state_version,
            command_type="simulation.test_clock",
            payload={"to_tick": EXPIRY_TICK},
        )
        repository.commit_command(
            scope, world_id, branch_id, expiry_command,
            [TimeAdvancedEvent(to_tick=EXPIRY_TICK)],
        )
        expired = repository.read_world(scope, world_id, branch_id)
        assert expired.state.current_tick == EXPIRY_TICK
        assert expired.state.safe_conduct_treaties[0].status == "active"

        denied_view, _context, _normalized, denied = _prepare_action(
            repository, scope, world_id, branch_id, sender_id, dispatch_payload,
        )
        assert denied.outcome.status == "rejected"
        assert denied.outcome.code == "route_access_denied"
        assert denied.events == []
        assert repository.read_world(scope, world_id, branch_id).state_hash == denied_view.state_hash

        # Prove storage independently rechecks the exact departure-time grant:
        # this event was valid against a hypothetical one-tick extension only.
        extended_treaty = expired.state.safe_conduct_treaties[0].model_copy(
            update={"expires_at_tick": EXPIRY_TICK + 1},
        )
        hypothetical = WorldState.model_validate({
            **expired.state.model_dump(mode="json"),
            "safe_conduct_treaties": [extended_treaty.model_dump(mode="json")],
        })
        command_id = uuid4()
        normalized = parse_action(dispatch_payload).model_dump(mode="json")
        context = RuleContext(
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            actor_id=sender_id,
            ruleset_version=expired.world.ruleset_version,
        )
        hypothetical_resolution = RuleEngine().evaluate(hypothetical, normalized, context)
        assert hypothetical_resolution.outcome.status == "succeeded"
        forged_departure = next(
            event for event in hypothetical_resolution.events if isinstance(event, ShipmentDispatchedEvent)
        )
        forged_command = CommandEnvelope(
            command_id=command_id,
            actor_id=sender_id,
            idempotency_key="attempt-dispatch-at-expired-treaty-boundary",
            expected_state_version=expired.branch.state_version,
            command_type="action.dispatch_shipment",
            payload=normalized,
        )
        with pytest.raises(InvalidEvent, match="no valid safe conduct at departure"):
            repository.commit_command(scope, world_id, branch_id, forged_command, [forged_departure])
        still_expired = repository.read_world(scope, world_id, branch_id)
        assert still_expired.state_hash == expired.state_hash
        assert still_expired.branch.state_version == expired.branch.state_version

        # The in-transit lot is independent of later route authorization. It is
        # delivered at its exact arrival schedule, ten ticks after treaty expiry.
        before_arrival = still_expired
        time_to_arrival = CommandEnvelope(
            actor_id=scope.user_id,
            idempotency_key="advance-to-grain-arrival",
            expected_state_version=before_arrival.branch.state_version,
            command_type="simulation.test_clock",
            payload={"to_tick": ARRIVAL_TICK},
        )
        repository.commit_command(
            scope, world_id, branch_id, time_to_arrival,
            [TimeAdvancedEvent(to_tick=ARRIVAL_TICK)],
        )
        at_arrival = repository.read_world(scope, world_id, branch_id)
        schedule = next(item for item in at_arrival.state.scheduled_events if item.id == dispatch.arrival_schedule_id)
        arrival_context = RuleContext(
            world_id=world_id,
            branch_id=branch_id,
            command_id=uuid4(),
            actor_id=sender_id,
            ruleset_version=at_arrival.world.ruleset_version,
        )
        arrival_resolution = RuleEngine().resolve_scheduled(at_arrival.state, schedule, arrival_context)
        assert arrival_resolution.outcome.status == "succeeded"
        delivered = next(
            event for event in arrival_resolution.events if isinstance(event, ShipmentArrivalDeliveredEvent)
        )
        assert delivered.arrival_tick == ARRIVAL_TICK
        assert sum((row.delta for row in ledger_postings(delivered)), Decimal("0")) == 0
        delivery_command = CommandEnvelope(
            command_id=arrival_context.command_id,
            actor_id=sender_id,
            idempotency_key="deliver-arrived-grain-after-treaty-expiry",
            expected_state_version=at_arrival.branch.state_version,
            command_type="simulation.shipment_arrival",
            payload={"scheduled_event_id": str(schedule.id)},
        )
        delivered_commit = repository.commit_command(
            scope, world_id, branch_id, delivery_command, arrival_resolution.events,
        )

        final = repository.read_world(scope, world_id, branch_id)
        assert final.state.current_tick == ARRIVAL_TICK
        assert final.state.resources[dispatch.transit_account_id].balance == Decimal("0")
        assert final.state.resources[ids["destination_account_id"]].balance == Decimal("1")
        assert final.state.resources[ids["source_account_id"]].balance == Decimal("4")
        final_shipment = ShipmentComponent.model_validate(
            final.state.entities[dispatch.shipment_id].components["shipment"],
        )
        assert final_shipment.status == "delivered"
        assert final_shipment.arrived_tick == ARRIVAL_TICK
        receiver_lots = InventoryComponent.model_validate(
            final.state.entities[receiver_id].components["inventory"],
        ).lots
        assert [(lot.quantity, lot.expiry_tick) for lot in receiver_lots] == [(Decimal("1"), 2_000)]
        assert _ledger_rows(database, delivered.event_id) == [
            (dispatch.transit_account_id, RESOURCE_ID, Decimal("-1")),
            (ids["destination_account_id"], RESOURCE_ID, Decimal("1")),
        ]

        replayed = repository.replay(scope, world_id, branch_id, from_genesis=True)
        assert replayed.state_hash == final.state_hash == state_hash(final.state)
        assert replayed.state.current_tick == ARRIVAL_TICK
        assert delivered_commit.state_hash == final.state_hash
        assert dispatch_commit.last_seq is not None and delivered_commit.last_seq is not None
    finally:
        database.dispose()
