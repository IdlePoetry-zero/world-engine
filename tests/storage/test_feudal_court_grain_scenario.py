"""A court's safe-conduct decision determines whether a real grain convoy arrives."""

from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.components import (
    CapabilitiesComponent,
    GovernanceComponent,
    InventoryComponent,
    InventoryLot,
    OfficeRecord,
    PlaceComponent,
    PlaceRoute,
    ScheduledEventRecord,
)
from world_engine.domain.events import parse_event
from world_engine.domain.gameplay_events import (
    GovernanceLeadershipHandedOverEvent,
    ShipmentArrivalDeliveredEvent,
    ShipmentDispatchedEvent,
)
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    JobStatus,
    NewWorld,
    ResourceAccount,
    WorldEntity,
    WorldState,
)
from world_engine.domain.reducers import ledger_postings, state_hash
from world_engine.domain.safe_conduct import SAFE_CONDUCT_TAG
from world_engine.domain.safe_conduct_events import SafeConductChangedEvent
from world_engine.domain.safe_conduct_state import (
    PROPOSE_SAFE_CONDUCT_COMMAND,
    RESOLVE_SAFE_CONDUCT_COMMAND,
)
from world_engine.domain.shipment import ShipmentComponent
from world_engine.rules import RuleContext, RuleEngine, parse_action
from world_engine.simulation import SimulationEngine, WorldAdvancePlan
from world_engine.storage.database import Database
from world_engine.storage.models import LedgerPostingRow
from world_engine.storage.repository import WorldRepository


START_TICK = 1_000
ARRIVAL_TICK = 1_060
SAFE_CONDUCT_EXPIRY_TICK = 2_000
GRAIN = "core.grain"


def _court_world() -> tuple[WorldState, dict[str, object]]:
    origin_id, destination_id = uuid4(), uuid4()
    sender_id, warden_id, heir_id = uuid4(), uuid4(), uuid4()
    source_court_id, recipient_court_id = uuid4(), uuid4()
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
        name="Ash March Granary",
        components={"place": PlaceComponent(place_type="region", routes=[route]).model_dump(mode="json")},
    )
    destination = WorldEntity(
        id=destination_id,
        kind="place",
        name="Northbank Court",
        components={"place": PlaceComponent(place_type="region").model_dump(mode="json")},
    )
    sender = WorldEntity(
        id=sender_id,
        kind="character",
        name="Marshal Mira Vale",
        location_id=origin_id,
        components={
            "capabilities": CapabilitiesComponent(grants=["world.travel"]).model_dump(mode="json"),
            "inventory": InventoryComponent(lots=[InventoryLot(
                lot_id=source_lot_id,
                account_id=source_account_id,
                resource_id=GRAIN,
                quantity=Decimal("5"),
                expiry_tick=4_000,
            )]).model_dump(mode="json"),
        },
    )
    warden = WorldEntity(
        id=warden_id,
        kind="character",
        name="Warden Sera North",
        location_id=destination_id,
        components={"inventory": InventoryComponent().model_dump(mode="json")},
    )
    heir = WorldEntity(
        id=heir_id,
        kind="character",
        name="Heir Tomas North",
        location_id=destination_id,
    )

    def court(org_id, name, location_id, leader_id, members):
        governance = GovernanceComponent(
            revision=1,
            members=members,
            leader_id=leader_id,
            offices=[OfficeRecord(
                office_id="office.leader",
                title="Warden",
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

    source_court = court(source_court_id, "Ash March Court", origin_id, sender_id, [sender_id])
    recipient_court = court(
        recipient_court_id, "Northbank Court", destination_id, warden_id, [warden_id, heir_id],
    )
    state = WorldState(
        current_tick=START_TICK,
        player_id=sender_id,
        entities={entity.id: entity for entity in (
            origin, destination, sender, warden, heir, source_court, recipient_court,
        )},
        resources={
            source_account_id: ResourceAccount(resource_id=GRAIN, balance="5", owner_id=sender_id),
            destination_account_id: ResourceAccount(resource_id=GRAIN, balance="0", owner_id=warden_id),
        },
    )
    return state, {
        "origin_id": origin_id,
        "destination_id": destination_id,
        "sender_id": sender_id,
        "warden_id": warden_id,
        "heir_id": heir_id,
        "source_court_id": source_court_id,
        "recipient_court_id": recipient_court_id,
        "route_id": route_id,
        "source_account_id": source_account_id,
        "destination_account_id": destination_account_id,
        "source_lot_id": source_lot_id,
    }


def _commit_action(repository, scope, world_id, branch_id, actor_id, payload, *, idempotency_key):
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
    assert resolution.outcome.status == "succeeded", resolution.outcome.model_dump(mode="json")
    if normalized["kind"] == "propose_safe_conduct":
        command_type = PROPOSE_SAFE_CONDUCT_COMMAND
    elif normalized["kind"] == "resolve_safe_conduct":
        command_type = RESOLVE_SAFE_CONDUCT_COMMAND
    elif normalized["kind"] == "handover_leadership":
        command_type = "player.action"
    else:
        command_type = f"action.{normalized['kind']}"
    command = CommandEnvelope(
        command_id=command_id,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        expected_state_version=view.branch.state_version,
        command_type=command_type,
        payload={} if normalized["kind"] == "handover_leadership" else normalized,
    )
    committed = repository.commit_command(scope, world_id, branch_id, command, resolution.events)
    return committed, resolution


def _safe_conduct_events(repository, scope, world_id, branch_id):
    records = repository.list_events(scope, world_id, branch_id, limit=100).events
    return [
        parse_event({"type": record.type, "event_id": str(record.event_id), **record.payload})
        for record in records
        if record.type == "core.safe_conduct_changed.v1"
    ]


def _ledger_rows(database, event_id):
    with database.session_factory() as session:
        rows = session.scalars(
            select(LedgerPostingRow)
            .where(LedgerPostingRow.event_id == event_id)
            .order_by(LedgerPostingRow.posting_index)
        ).all()
    return [(row.account_id, row.resource_id, Decimal(row.delta)) for row in rows]


def test_feudal_court_treaty_branch_controls_grain_convoy_and_replays(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'feudal-court-grain.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        state, ids = _court_world()
        created = repository.create_world(
            scope,
            NewWorld(title="Northbank grain passage", ruleset_version="feudal-court-grain.v1", state=state),
        )
        world_id, accepted_branch = created.world.world_id, created.branch.branch_id

        # The marshal proposes a dated crossing under her court's representational office.
        proposal_commit, proposal = _commit_action(
            repository, scope, world_id, accepted_branch, ids["sender_id"],
            {
                "kind": "propose_safe_conduct",
                "proposer_organization_id": str(ids["source_court_id"]),
                "recipient_organization_id": str(ids["recipient_court_id"]),
                "from_place_id": str(ids["origin_id"]),
                "route_id": str(ids["route_id"]),
                "expires_at_tick": SAFE_CONDUCT_EXPIRY_TICK,
            },
            idempotency_key="propose-ash-march-grain-passage",
        )
        treaty_id = proposal.events[0].resulting_treaty.treaty_id

        # Fork before the receiving court's decision: one warden ratifies, while the
        # other history appoints the heir, who declines in the office's lawful name.
        declined_branch = repository.fork(
            scope, world_id, accepted_branch, proposal_commit.last_seq, "Northbank refuses the grain road",
        ).branch_id
        _, ratification = _commit_action(
            repository, scope, world_id, accepted_branch, ids["warden_id"],
            {"kind": "resolve_safe_conduct", "treaty_id": str(treaty_id),
             "expected_revision": 1, "operation": "ratify"},
            idempotency_key="warden-ratifies-grain-passage",
        )
        active_treaty = ratification.events[0].resulting_treaty
        assert active_treaty.status == "active"

        _, handover = _commit_action(
            repository, scope, world_id, declined_branch, ids["warden_id"],
            {"kind": "handover_leadership", "organization_id": str(ids["recipient_court_id"]),
             "successor_id": str(ids["heir_id"])},
            idempotency_key="warden-appoints-heir-before-court-ruling",
        )
        handed_over = next(event for event in handover.events if isinstance(event, GovernanceLeadershipHandedOverEvent))
        assert handed_over.successor_id == ids["heir_id"]
        refused_commit, refusal = _commit_action(
            repository, scope, world_id, declined_branch, ids["heir_id"],
            {"kind": "resolve_safe_conduct", "treaty_id": str(treaty_id),
             "expected_revision": 1, "operation": "decline"},
            idempotency_key="new-warden-refuses-grain-passage",
        )
        refused_treaty = refusal.events[0].resulting_treaty
        assert refused_treaty.status == "declined"
        assert isinstance(refusal.events[0], SafeConductChangedEvent)
        assert refusal.events[0].actor_id == ids["heir_id"]
        refused_head = repository.read_world(scope, world_id, declined_branch)
        assert refused_head.state.entities[ids["recipient_court_id"]].components["governance"]["leader_id"] == str(ids["heir_id"])
        assert refused_head.state.safe_conduct_treaties[0].status == "declined"

        dispatch_payload = {
            "kind": "dispatch_shipment",
            "route_id": str(ids["route_id"]),
            "source_owner_id": str(ids["sender_id"]),
            "destination_owner_id": str(ids["warden_id"]),
            "resource_id": GRAIN,
            "quantity": "1",
            "source_account_id": str(ids["source_account_id"]),
            "destination_account_id": str(ids["destination_account_id"]),
        }
        rejected_view = repository.read_world(scope, world_id, declined_branch)
        rejected_context = RuleContext(
            world_id=world_id,
            branch_id=declined_branch,
            command_id=uuid4(),
            actor_id=ids["sender_id"],
            ruleset_version=refused_head.world.ruleset_version,
        )
        rejected_dispatch = RuleEngine().evaluate(
            refused_head.state, parse_action(dispatch_payload).model_dump(mode="json"), rejected_context,
        )
        assert rejected_dispatch.outcome.status == "rejected"
        assert rejected_dispatch.outcome.code == "route_access_denied"
        assert rejected_dispatch.events == []
        assert repository.read_world(scope, world_id, declined_branch).state_hash == rejected_view.state_hash
        assert refused_head.state.resources[ids["source_account_id"]].balance == Decimal("5")

        # Ratification is the cause of passage on the accepted branch. Dispatch uses
        # the real source lot and creates the registered arrival schedule and transit account.
        dispatch_commit, dispatch_resolution = _commit_action(
            repository, scope, world_id, accepted_branch, ids["sender_id"], dispatch_payload,
            idempotency_key="dispatch-one-bushel-under-safe-conduct",
        )
        dispatch = next(event for event in dispatch_resolution.events if isinstance(event, ShipmentDispatchedEvent))
        assert dispatch.departure_tick == START_TICK
        assert dispatch.arrival_tick == ARRIVAL_TICK
        assert sum((posting.delta for posting in ledger_postings(dispatch)), Decimal("0")) == 0
        assert _ledger_rows(database, dispatch.event_id) == [
            (ids["source_account_id"], GRAIN, Decimal("-1")),
            (dispatch.transit_account_id, GRAIN, Decimal("1")),
        ]
        after_departure = repository.read_world(scope, world_id, accepted_branch)
        assert after_departure.state.resources[ids["source_account_id"]].balance == Decimal("4")
        assert after_departure.state.resources[dispatch.transit_account_id].balance == Decimal("1")
        assert after_departure.state.resources[ids["destination_account_id"]].balance == Decimal("0")
        arrival_schedule = next(
            event for event in after_departure.state.scheduled_events if event.id == dispatch.arrival_schedule_id
        )
        assert isinstance(arrival_schedule, ScheduledEventRecord)
        assert (arrival_schedule.handler_id, arrival_schedule.due_tick) == ("shipment.arrival", ARRIVAL_TICK)

        # Run the registered shipment arrival through the persisted world-advance
        # scheduler job, committing every boundary before checking settlement.
        jobs = SimulationJobs(repository, SimulationEngine())
        job = jobs.enqueue_world_advance(
            scope,
            world_id,
            accepted_branch,
            WorldAdvancePlan(started_tick=START_TICK, target_tick=ARRIVAL_TICK),
            idempotency_key="advance-court-grain-to-arrival",
            speed="balanced",
            depth="deep",
            expected_state_version=after_departure.branch.state_version,
        )
        lease = repository.claim_job("feudal-grain-scheduler", job_type="simulation.world_advance")
        assert lease is not None
        scheduled_deliveries = []
        for _ in range(8):
            resolution = jobs.advance(scope, lease)
            if hasattr(resolution, "events"):
                scheduled_deliveries.extend(
                    event for event in resolution.events if isinstance(event, ShipmentArrivalDeliveredEvent)
                )
            status = repository.read_job(scope, job.job_id).status
            if status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
                break
        assert status == JobStatus.SUCCEEDED
        assert len(scheduled_deliveries) == 1
        delivered = scheduled_deliveries[0]
        assert delivered.shipment_id == dispatch.shipment_id

        delivered_head = repository.read_world(scope, world_id, accepted_branch)
        assert delivered_head.state.current_tick == ARRIVAL_TICK
        assert delivered.arrival_tick == ARRIVAL_TICK
        assert sum((posting.delta for posting in ledger_postings(delivered)), Decimal("0")) == 0
        assert _ledger_rows(database, delivered.event_id) == [
            (dispatch.transit_account_id, GRAIN, Decimal("-1")),
            (ids["destination_account_id"], GRAIN, Decimal("1")),
        ]
        assert delivered_head.state.resources[dispatch.transit_account_id].balance == Decimal("0")
        assert delivered_head.state.resources[ids["destination_account_id"]].balance == Decimal("1")
        assert delivered_head.state.resources[ids["source_account_id"]].balance == Decimal("4")
        final_shipment = ShipmentComponent.model_validate(
            delivered_head.state.entities[dispatch.shipment_id].components["shipment"],
        )
        assert final_shipment.status == "delivered"
        assert final_shipment.arrived_tick == ARRIVAL_TICK
        received_lots = InventoryComponent.model_validate(
            delivered_head.state.entities[ids["warden_id"]].components["inventory"],
        ).lots
        assert [(lot.quantity, lot.expiry_tick) for lot in received_lots] == [(Decimal("1"), 4_000)]

        # The branches preserve different political causes and downstream cargo outcomes.
        accepted_treaties = _safe_conduct_events(repository, scope, world_id, accepted_branch)
        refused_treaties = _safe_conduct_events(repository, scope, world_id, declined_branch)
        assert [(event.operation, event.resulting_treaty.status) for event in accepted_treaties] == [
            ("propose", "pending"), ("ratify", "active"),
        ]
        assert [(event.operation, event.resulting_treaty.status) for event in refused_treaties] == [
            ("propose", "pending"), ("decline", "declined"),
        ]
        assert any(isinstance(event, SafeConductChangedEvent) and event.operation == "decline"
                   for event in refusal.events)
        assert dispatch_commit.last_seq is not None and refused_commit.last_seq is not None

        accepted_replay = repository.replay(scope, world_id, accepted_branch, from_genesis=True)
        refused_replay = repository.replay(scope, world_id, declined_branch, from_genesis=True)
        assert accepted_replay.state_hash == delivered_head.state_hash == state_hash(delivered_head.state)
        assert refused_replay.state_hash == refused_head.state_hash == state_hash(refused_head.state)
        assert accepted_replay.state_hash != refused_replay.state_hash
    finally:
        database.dispose()
