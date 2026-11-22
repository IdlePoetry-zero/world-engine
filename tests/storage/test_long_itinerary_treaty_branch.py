"""A long itinerary pauses at an expired border treaty and branches lawfully."""

from uuid import uuid4

from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.components import (
    GovernanceComponent,
    OfficeRecord,
    PlaceComponent,
    PlaceRoute,
    ScheduledEventRecord,
)
from world_engine.domain.events import parse_event
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    JobStatus,
    NewWorld,
    WorldEntity,
    WorldState,
)
from world_engine.domain.reducers import state_hash
from world_engine.domain.safe_conduct import SAFE_CONDUCT_TAG
from world_engine.domain.safe_conduct_events import SafeConductChangedEvent
from world_engine.domain.safe_conduct_state import (
    PROPOSE_SAFE_CONDUCT_COMMAND,
    RESOLVE_SAFE_CONDUCT_COMMAND,
)
from world_engine.domain.travel_itinerary import (
    TravelItineraryArrivalProof,
    plan_travel_itinerary,
)
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent
from world_engine.domain.travel_journeys import (
    TRAVEL_RESOLUTION_HANDLER_ID,
    TRAVEL_RESOLUTION_HANDLER_VERSION,
    travel_resolution_schedule_id,
)
from world_engine.rules import RuleContext, RuleEngine, parse_action
from world_engine.simulation import LongActionPlan
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


DAY = 86_400
FIRST_LEG_TICKS = 80 * DAY
OLD_TREATY_EXPIRY = 40 * DAY
SHORT_BORDER_TICKS = 2 * DAY
DETOUR_LEG_TICKS = 7 * DAY
ITINERARY_DEADLINE = 130 * DAY


def _route(from_id, to_id, *, ticks, protected=False):
    return PlaceRoute(
        route_id=uuid4(),
        from_place_id=from_id,
        to_place_id=to_id,
        travel_ticks=ticks,
        risk="0",
        access_tags=[SAFE_CONDUCT_TAG] if protected else [],
        travel_modes=["travel.walk"],
    )


def _charter_org(entity_id, name, location_id, leader_id, members, charter):
    governance = GovernanceComponent(
        charter=charter,
        revision=1,
        members=members,
        leader_id=leader_id,
        offices=[OfficeRecord(
            office_id="office.charter_warden",
            title="Charter Warden",
            holder_id=leader_id,
            authority=["organization.lead", "authority.represent"],
        )],
        succession_policy="appointment",
    )
    return WorldEntity(
        id=entity_id,
        kind="organization",
        name=name,
        location_id=location_id,
        components={"governance": governance.model_dump(mode="json")},
    )


def _treaty_world():
    origin_id, waypoint_id, destination_id, detour_id = (uuid4() for _ in range(4))
    traveler_id, source_leader_id, recipient_leader_id = (uuid4() for _ in range(3))
    source_org_id, recipient_org_id = uuid4(), uuid4()

    first_route = _route(origin_id, waypoint_id, ticks=FIRST_LEG_TICKS)
    border_route = _route(waypoint_id, destination_id, ticks=SHORT_BORDER_TICKS, protected=True)
    detour_first_route = _route(waypoint_id, detour_id, ticks=DETOUR_LEG_TICKS)
    detour_second_route = _route(detour_id, destination_id, ticks=DETOUR_LEG_TICKS)

    origin = WorldEntity(
        id=origin_id,
        kind="place",
        name="Greyhaven Coast",
        components={"place": PlaceComponent(place_type="region", routes=[first_route]).model_dump(mode="json")},
    )
    waypoint = WorldEntity(
        id=waypoint_id,
        kind="place",
        name="Ash March Waypoint",
        components={"place": PlaceComponent(
            place_type="region", routes=[border_route, detour_first_route],
        ).model_dump(mode="json")},
    )
    destination = WorldEntity(
        id=destination_id,
        kind="place",
        name="Northbank Court",
        components={"place": PlaceComponent(place_type="region").model_dump(mode="json")},
    )
    detour = WorldEntity(
        id=detour_id,
        kind="place",
        name="Pinewatch Roadstead",
        components={"place": PlaceComponent(
            place_type="region", routes=[detour_second_route],
        ).model_dump(mode="json")},
    )
    traveler = WorldEntity(
        id=traveler_id,
        kind="character",
        name="Mira Vale",
        location_id=origin_id,
        components={"capabilities": {"grants": ["world.travel"], "restrictions": []}},
    )
    source_leader = WorldEntity(
        id=source_leader_id,
        kind="character",
        name="Tomas Reed",
        location_id=waypoint_id,
    )
    recipient_leader = WorldEntity(
        id=recipient_leader_id,
        kind="character",
        name="Sera North",
        location_id=destination_id,
    )
    source_org = _charter_org(
        source_org_id,
        "Greyhaven March Charter",
        waypoint_id,
        source_leader_id,
        [source_leader_id, traveler_id],
        "The warden may grant timed safe-conduct to charter members on the registered Northbank road.",
    )
    recipient_org = _charter_org(
        recipient_org_id,
        "Northbank Court Charter",
        destination_id,
        recipient_leader_id,
        [recipient_leader_id],
        "The court warden may ratify or decline passage terms for the Northbank road.",
    )
    state = WorldState(
        current_tick=0,
        player_id=traveler_id,
        entities={entity.id: entity for entity in (
            origin, waypoint, destination, detour, traveler,
            source_leader, recipient_leader, source_org, recipient_org,
        )},
    )
    return state, {
        "origin": origin_id,
        "waypoint": waypoint_id,
        "destination": destination_id,
        "detour": detour_id,
        "traveler": traveler_id,
        "source_leader": source_leader_id,
        "recipient_leader": recipient_leader_id,
        "source_org": source_org_id,
        "recipient_org": recipient_org_id,
        "first_route": first_route.route_id,
        "border_route": border_route.route_id,
        "detour_first_route": detour_first_route.route_id,
        "detour_second_route": detour_second_route.route_id,
    }


def _commit_treaty_action(repository, scope, world_id, branch_id, actor_id, payload, *, key):
    view = repository.read_world(scope, world_id, branch_id)
    command_id = uuid4()
    normalized = parse_action(payload).model_dump(mode="json")
    resolution = RuleEngine().evaluate(
        view.state,
        normalized,
        RuleContext(
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            actor_id=actor_id,
            ruleset_version=view.world.ruleset_version,
        ),
    )
    assert resolution.outcome.status == "succeeded", resolution.outcome.model_dump(mode="json")
    command_type = (
        PROPOSE_SAFE_CONDUCT_COMMAND
        if normalized["kind"] == "propose_safe_conduct"
        else RESOLVE_SAFE_CONDUCT_COMMAND
    )
    committed = repository.commit_command(
        scope,
        world_id,
        branch_id,
        CommandEnvelope(
            command_id=command_id,
            actor_id=actor_id,
            idempotency_key=key,
            expected_state_version=view.branch.state_version,
            command_type=command_type,
            payload=normalized,
        ),
        resolution.events,
    )
    return committed, resolution


def _long_action(state, actor_id, route_ids, itinerary_id):
    itinerary = plan_travel_itinerary(
        state,
        itinerary_id=itinerary_id,
        actor_id=actor_id,
        route_ids=route_ids,
        deadline_tick=ITINERARY_DEADLINE,
    )
    return LongActionPlan(
        actor_id=actor_id,
        started_tick=state.current_tick,
        target_tick=ITINERARY_DEADLINE,
        activity="travel",
        travel_itinerary=itinerary,
        depart_immediately=True,
    )


def _enqueue_itinerary(repository, scope, jobs, world_id, branch_id, actor_id, route_ids, *, key):
    view = repository.read_world(scope, world_id, branch_id)
    plan = _long_action(view.state, actor_id, route_ids, uuid4())
    job = jobs.enqueue(
        scope,
        world_id,
        branch_id,
        plan,
        idempotency_key=key,
        speed="balanced",
        depth="standard",
        expected_state_version=view.branch.state_version,
    )
    lease = repository.claim_job(f"treaty-itinerary:{key}", job_type="simulation.long_action")
    assert lease is not None and lease.job_id == job.job_id
    return job, lease


def _advance_until(repository, scope, jobs, lease, job_id, *, predicate):
    for _ in range(512):
        jobs.advance(scope, lease)
        record = repository.read_job(scope, job_id)
        if predicate(record):
            return record
        if record.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            return record
    raise AssertionError("The itinerary worker exceeded its bounded advancement loop.")


def _typed_events(repository, scope, world_id, branch_id, event_type):
    records = repository.list_events(scope, world_id, branch_id, limit=500).events
    return [
        parse_event({
            "type": record.type,
            "event_id": str(record.event_id),
            "visibility": record.visibility,
            **record.payload,
        })
        for record in records
        if record.type == event_type
    ]


def _timeline(events):
    operation_order = {"depart": 0, "arrive": 1, "interrupt": 2, "delay": 3}
    ordered = sorted(
        events,
        key=lambda event: (
            event.journey.departed_tick,
            event.journey.arrival_tick,
            operation_order[event.operation],
        ),
    )
    return [
        (event.operation, event.journey.route_id, event.journey.departed_tick, event.journey.arrival_tick)
        for event in ordered
    ]


def test_expired_treaty_pauses_itinerary_then_branches_to_renewal_or_safe_detour(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'long-itinerary-treaty-branch.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        state, ids = _treaty_world()
        created = repository.create_world(
            scope,
            NewWorld(title="The expired Northbank charter", ruleset_version="treaty-itinerary.v1", state=state),
        )
        world_id, diplomacy_branch = created.world.world_id, created.branch.branch_id

        # The registered border route is treaty-gated. Its original timed grant
        # is ratified through the ordinary bilateral rules before departure.
        _, proposal = _commit_treaty_action(
            repository,
            scope,
            world_id,
            diplomacy_branch,
            ids["source_leader"],
            {
                "kind": "propose_safe_conduct",
                "proposer_organization_id": str(ids["source_org"]),
                "recipient_organization_id": str(ids["recipient_org"]),
                "from_place_id": str(ids["waypoint"]),
                "route_id": str(ids["border_route"]),
                "expires_at_tick": OLD_TREATY_EXPIRY,
            },
            key="propose-original-northbank-safe-conduct",
        )
        original_treaty_id = proposal.events[0].resulting_treaty.treaty_id
        _, initial_ratification = _commit_treaty_action(
            repository,
            scope,
            world_id,
            diplomacy_branch,
            ids["recipient_leader"],
            {
                "kind": "resolve_safe_conduct",
                "treaty_id": str(original_treaty_id),
                "expected_revision": 1,
                "operation": "ratify",
            },
            key="ratify-original-northbank-safe-conduct",
        )
        assert initial_ratification.events[0].resulting_treaty.status == "active"

        jobs = SimulationJobs(repository)
        initial_job, initial_lease = _enqueue_itinerary(
            repository,
            scope,
            jobs,
            world_id,
            diplomacy_branch,
            ids["traveler"],
            [ids["first_route"], ids["border_route"]],
            key="long-chartered-crossing",
        )

        # Record the actual scheduled travel resolution before advancing the
        # 80-day first leg. The treaty expires during that harmless crossing.
        departure = jobs.advance(scope, initial_lease)
        assert departure.outcome == "advanced"
        first_departure = next(
            event for event in departure.events
            if isinstance(event, TravelJourneyChangedEvent) and event.operation == "depart"
        )
        first_journey_id = first_departure.journey.journey_id
        after_departure = repository.read_world(scope, world_id, diplomacy_branch)
        scheduled_first_leg = next(
            event for event in after_departure.state.scheduled_events
            if event.id == travel_resolution_schedule_id(first_journey_id)
        )
        assert isinstance(scheduled_first_leg, ScheduledEventRecord)
        assert (
            scheduled_first_leg.handler_id,
            scheduled_first_leg.handler_version,
            scheduled_first_leg.due_tick,
        ) == (TRAVEL_RESOLUTION_HANDLER_ID, TRAVEL_RESOLUTION_HANDLER_VERSION, FIRST_LEG_TICKS)

        first_arrival_job = _advance_until(
            repository,
            scope,
            jobs,
            initial_lease,
            initial_job.job_id,
            predicate=lambda record: record.payload.get("itinerary_cursor") is not None,
        )
        assert first_arrival_job.status is JobStatus.RUNNING
        first_proof = TravelItineraryArrivalProof.model_validate(
            first_arrival_job.payload["itinerary_cursor"],
        )
        assert first_proof.leg_index == 0
        assert first_proof.journey_id == first_journey_id
        assert (first_proof.departed_tick, first_proof.arrived_tick) == (0, FIRST_LEG_TICKS)
        arrival_head = repository.read_world(scope, world_id, diplomacy_branch)

        # The next worker iteration tries the fixed border leg at the waypoint.
        # It must pause at the exact route_access_denied boundary without a
        # departure, clock advance, or second-leg cursor proof.
        paused = _advance_until(
            repository,
            scope,
            jobs,
            initial_lease,
            initial_job.job_id,
            predicate=lambda record: record.status is not JobStatus.RUNNING,
        )
        assert paused.status is JobStatus.PAUSED
        assert paused.result["interruption"]["reason_code"] == "route_access_denied"
        waypoint_head = repository.read_world(scope, world_id, diplomacy_branch)
        assert waypoint_head.state.current_tick == FIRST_LEG_TICKS
        assert waypoint_head.state.entities[ids["traveler"]].location_id == ids["waypoint"]
        assert [journey.route_id for journey in waypoint_head.state.travel_journeys] == [ids["first_route"]]
        assert paused.payload["itinerary_cursor"] == first_proof.model_dump(mode="json")
        assert paused.committed_through_tick == FIRST_LEG_TICKS
        assert waypoint_head.last_seq == arrival_head.last_seq
        assert waypoint_head.state_hash == arrival_head.state_hash

        # Fork the exact saved waypoint before either response. Repository forks
        # carry the committed journey and receipt history, not the live job lease.
        detour_branch = repository.fork(
            scope,
            world_id,
            diplomacy_branch,
            waypoint_head.last_seq,
            "Take the longer open road",
        ).branch_id

        # Renew the expired treaty on the original branch with the same
        # authenticated proposal and ratification rules used for the first grant.
        renewal_expiry = FIRST_LEG_TICKS + 30 * DAY
        _, renewal_proposal = _commit_treaty_action(
            repository,
            scope,
            world_id,
            diplomacy_branch,
            ids["source_leader"],
            {
                "kind": "propose_safe_conduct",
                "proposer_organization_id": str(ids["source_org"]),
                "recipient_organization_id": str(ids["recipient_org"]),
                "from_place_id": str(ids["waypoint"]),
                "route_id": str(ids["border_route"]),
                "expires_at_tick": renewal_expiry,
            },
            key="propose-renewed-northbank-safe-conduct",
        )
        renewed_treaty = renewal_proposal.events[0].resulting_treaty
        assert renewed_treaty.treaty_id != original_treaty_id
        _, renewal_ratification = _commit_treaty_action(
            repository,
            scope,
            world_id,
            diplomacy_branch,
            ids["recipient_leader"],
            {
                "kind": "resolve_safe_conduct",
                "treaty_id": str(renewed_treaty.treaty_id),
                "expected_revision": 1,
                "operation": "ratify",
            },
            key="ratify-renewed-northbank-safe-conduct",
        )
        assert renewal_ratification.events[0].resulting_treaty.status == "active"
        assert renewal_ratification.events[0].resulting_treaty.expires_at_tick == renewal_expiry

        # The original durable itinerary can now resume at its saved cursor.
        resumed = repository.control_job(scope, initial_job.job_id, "resume")
        assert resumed.status is JobStatus.QUEUED
        resumed_lease = repository.claim_job("renewed-charter-worker", job_type="simulation.long_action")
        assert resumed_lease is not None and resumed_lease.job_id == initial_job.job_id
        direct_departure = jobs.advance(scope, resumed_lease)
        direct_trip = next(
            event.journey for event in direct_departure.events
            if isinstance(event, TravelJourneyChangedEvent) and event.operation == "depart"
        )
        direct_in_transit = repository.read_world(scope, world_id, diplomacy_branch)
        direct_schedule = next(
            event for event in direct_in_transit.state.scheduled_events
            if event.id == travel_resolution_schedule_id(direct_trip.journey_id)
        )
        assert direct_trip.route_id == ids["border_route"]
        assert direct_schedule.due_tick == FIRST_LEG_TICKS + SHORT_BORDER_TICKS
        direct_finished = _advance_until(
            repository,
            scope,
            jobs,
            resumed_lease,
            initial_job.job_id,
            predicate=lambda record: record.status not in {JobStatus.QUEUED, JobStatus.RUNNING},
        )
        assert direct_finished.status is JobStatus.SUCCEEDED
        direct_head = repository.read_world(scope, world_id, diplomacy_branch)
        assert direct_head.state.current_tick == FIRST_LEG_TICKS + SHORT_BORDER_TICKS
        assert direct_head.state.entities[ids["traveler"]].location_id == ids["destination"]

        # The fork chooses the longer route, with a distinct itinerary identity.
        detour_job, detour_lease = _enqueue_itinerary(
            repository,
            scope,
            jobs,
            world_id,
            detour_branch,
            ids["traveler"],
            [ids["detour_first_route"], ids["detour_second_route"]],
            key="long-safe-detour-from-waypoint",
        )
        assert detour_job.job_id != initial_job.job_id
        detour_departure = jobs.advance(scope, detour_lease)
        assert detour_departure.outcome == "advanced"
        first_detour_trip = next(
            event.journey for event in detour_departure.events
            if isinstance(event, TravelJourneyChangedEvent) and event.operation == "depart"
        )
        detour_midway = repository.read_world(scope, world_id, detour_branch)
        detour_schedule = next(
            event for event in detour_midway.state.scheduled_events
            if event.id == travel_resolution_schedule_id(first_detour_trip.journey_id)
        )
        assert detour_schedule.due_tick == FIRST_LEG_TICKS + DETOUR_LEG_TICKS
        detour_first_arrival = _advance_until(
            repository,
            scope,
            jobs,
            detour_lease,
            detour_job.job_id,
            predicate=lambda record: record.payload.get("itinerary_cursor") is not None,
        )
        assert detour_first_arrival.status is JobStatus.RUNNING
        second_detour_departure = jobs.advance(scope, detour_lease)
        second_detour_trip = next(
            event.journey for event in second_detour_departure.events
            if isinstance(event, TravelJourneyChangedEvent) and event.operation == "depart"
        )
        second_detour_in_transit = repository.read_world(scope, world_id, detour_branch)
        second_detour_schedule = next(
            event for event in second_detour_in_transit.state.scheduled_events
            if event.id == travel_resolution_schedule_id(second_detour_trip.journey_id)
        )
        assert second_detour_trip.route_id == ids["detour_second_route"]
        assert second_detour_schedule.due_tick == FIRST_LEG_TICKS + 2 * DETOUR_LEG_TICKS
        detour_finished = _advance_until(
            repository,
            scope,
            jobs,
            detour_lease,
            detour_job.job_id,
            predicate=lambda record: record.status not in {JobStatus.QUEUED, JobStatus.RUNNING},
        )
        assert detour_finished.status is JobStatus.SUCCEEDED
        detour_head = repository.read_world(scope, world_id, detour_branch)
        assert detour_head.state.current_tick == FIRST_LEG_TICKS + (2 * DETOUR_LEG_TICKS)
        assert detour_head.state.entities[ids["traveler"]].location_id == ids["destination"]
        assert detour_head.state.current_tick > direct_head.state.current_tick

        # The original first-leg arrival remains the same committed receipt on
        # both branches, while each branch settles only its own chosen continuation.
        for branch_head in (direct_head, detour_head):
            inherited = next(
                journey for journey in branch_head.state.travel_journeys
                if journey.journey_id == first_journey_id
            )
            assert (inherited.route_id, inherited.status, inherited.arrival_tick) == (
                ids["first_route"], "arrived", FIRST_LEG_TICKS,
            )

        direct_timeline = _timeline(_typed_events(
            repository, scope, world_id, diplomacy_branch, "core.travel_journey_changed.v1",
        ))
        assert direct_timeline == [
            ("depart", ids["first_route"], 0, FIRST_LEG_TICKS),
            ("arrive", ids["first_route"], 0, FIRST_LEG_TICKS),
            ("depart", ids["border_route"], FIRST_LEG_TICKS, FIRST_LEG_TICKS + SHORT_BORDER_TICKS),
            ("arrive", ids["border_route"], FIRST_LEG_TICKS, FIRST_LEG_TICKS + SHORT_BORDER_TICKS),
        ]
        detour_timeline = _timeline(_typed_events(
            repository, scope, world_id, detour_branch, "core.travel_journey_changed.v1",
        ))
        assert detour_timeline == [
            ("depart", ids["first_route"], 0, FIRST_LEG_TICKS),
            ("arrive", ids["first_route"], 0, FIRST_LEG_TICKS),
            ("depart", ids["detour_first_route"], FIRST_LEG_TICKS, FIRST_LEG_TICKS + DETOUR_LEG_TICKS),
            ("arrive", ids["detour_first_route"], FIRST_LEG_TICKS, FIRST_LEG_TICKS + DETOUR_LEG_TICKS),
            (
                "depart", ids["detour_second_route"], FIRST_LEG_TICKS + DETOUR_LEG_TICKS,
                FIRST_LEG_TICKS + 2 * DETOUR_LEG_TICKS,
            ),
            (
                "arrive", ids["detour_second_route"], FIRST_LEG_TICKS + DETOUR_LEG_TICKS,
                FIRST_LEG_TICKS + 2 * DETOUR_LEG_TICKS,
            ),
        ]

        direct_treaties = [
            event for event in _typed_events(
                repository, scope, world_id, diplomacy_branch, "core.safe_conduct_changed.v1",
            ) if isinstance(event, SafeConductChangedEvent)
        ]
        assert [(event.operation, event.resulting_treaty.status, event.resulting_treaty.expires_at_tick)
                for event in direct_treaties] == [
            ("propose", "pending", OLD_TREATY_EXPIRY),
            ("ratify", "active", OLD_TREATY_EXPIRY),
            ("propose", "pending", renewal_expiry),
            ("ratify", "active", renewal_expiry),
        ]
        detour_treaties = [
            event for event in _typed_events(
                repository, scope, world_id, detour_branch, "core.safe_conduct_changed.v1",
            ) if isinstance(event, SafeConductChangedEvent)
        ]
        assert [(event.operation, event.resulting_treaty.status) for event in detour_treaties] == [
            ("propose", "pending"), ("ratify", "active"),
        ]

        for branch_id, head in ((diplomacy_branch, direct_head), (detour_branch, detour_head)):
            replay = repository.replay(scope, world_id, branch_id, from_genesis=True)
            assert replay.state_hash == head.state_hash == state_hash(head.state)
    finally:
        database.dispose()
