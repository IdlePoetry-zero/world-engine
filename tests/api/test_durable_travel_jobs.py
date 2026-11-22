from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.backups import BackupApplication
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.travel_chat import recent_resolved_travel, travel_job_readback
from world_engine.application.worlds import WorldApplication
from world_engine.content import ContentCatalog
from world_engine.domain.components import (
    InventoryComponent,
    InventoryLot,
    NeedsComponent,
    PlaceComponent,
    PlaceRoute,
    ProductionComponent,
    ProductionQuotaPolicy,
    ScheduledEventRecord,
    GoalRecord,
)
from world_engine.domain.errors import AccessDenied, Conflict, InvalidEvent
from world_engine.domain.gameplay_events import GoalCreatedEvent
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    JobStatus,
    NewWorld,
    ResourceAccount,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.domain.travel_journeys import (
    TRAVEL_RESOLUTION_HANDLER_ID,
    TRAVEL_RESOLUTION_HANDLER_VERSION,
)
from world_engine.rules import RuleContext, RuleEngine
from world_engine.rules.contracts import WaitIntent
from world_engine.simulation import LongActionPlan, SimulationEngine
from world_engine.simulation.engine import SimulationPolicy, _is_ordinary_itinerary_transit_batch
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository
import world_engine.storage.repository as repository_module


def _id(value: int) -> UUID:
    return UUID(int=value)


def _travel_world() -> tuple[WorldState, dict[str, UUID]]:
    actor_id = _id(1)
    origin_id, destination_id, route_id = _id(2), _id(3), _id(4)
    producer_id, source_account_id, quota_schedule_id = _id(5), _id(6), _id(7)
    route = PlaceRoute(
        route_id=route_id,
        from_place_id=origin_id,
        to_place_id=destination_id,
        travel_ticks=20,
        risk=Decimal("0"),
        is_open=True,
    )
    quota = ProductionQuotaPolicy(
        quota_id="fixture.travel_boundary",
        resource_id="core.fixture_supply",
        source_account_id=source_account_id,
        period_ticks=10,
        per_period=Decimal("1"),
        maximum_renewal=Decimal("2"),
        base_credit_limit=Decimal("1"),
    )
    state = WorldState(
        current_tick=0,
        player_id=actor_id,
        entities={
            actor_id: WorldEntity(
                id=actor_id,
                kind="character",
                name="Traveler",
                location_id=origin_id,
                components={"capabilities": {"grants": ["world.travel"], "restrictions": []}},
            ),
            origin_id: WorldEntity(
                id=origin_id,
                kind="place",
                name="Departure Shore",
                components={"place": PlaceComponent(place_type="settlement", routes=[route]).model_dump(mode="json")},
            ),
            destination_id: WorldEntity(
                id=destination_id,
                kind="place",
                name="Arrival Harbor",
                components={"place": PlaceComponent(place_type="settlement").model_dump(mode="json")},
            ),
            producer_id: WorldEntity(
                id=producer_id,
                kind="organization",
                name="Boundary Producer",
                components={"production": ProductionComponent(quotas=[quota]).model_dump(mode="json")},
            ),
        },
        resources={
            source_account_id: ResourceAccount(
                resource_id="core.fixture_supply",
                balance=Decimal("0"),
                owner_id=producer_id,
                account_type="production_source",
                credit_limit=Decimal("1"),
            )
        },
        scheduled_events=[
            ScheduledEventRecord(
                id=quota_schedule_id,
                due_tick=10,
                phase="environment",
                handler_id="production.quota_renewal",
                handler_version="1.0.0",
                payload={"producer_id": str(producer_id), "quota_id": quota.quota_id},
            )
        ],
    )
    return state, {
        "actor": actor_id,
        "origin": origin_id,
        "destination": destination_id,
        "route": route_id,
        "producer": producer_id,
        "source_account": source_account_id,
        "quota_schedule": quota_schedule_id,
    }


def _enqueue_travel(repository, scope, world_id, branch_id, actor_id, route_id, *, key):
    view = repository.read_world(scope, world_id, branch_id)
    jobs = SimulationJobs(repository)
    job = jobs.enqueue(
        scope,
        world_id,
        branch_id,
        LongActionPlan(
            actor_id=actor_id,
            started_tick=view.state.current_tick,
            target_tick=30,
            activity="travel",
            route_id=route_id,
        ),
        idempotency_key=key,
        speed="balanced",
        depth="standard",
        expected_state_version=view.branch.state_version,
    )
    lease = repository.claim_job(f"travel-worker:{key}", job_type="simulation.long_action")
    assert lease is not None and lease.job_id == job.job_id
    return jobs, job, lease


def _advance_to_completion(repository, scope, jobs, job, lease, world_id, branch_id):
    for _ in range(12):
        jobs.advance(scope, lease)
        record = repository.read_job(scope, job.job_id)
        if record.status != JobStatus.RUNNING:
            break
    assert record.status == JobStatus.SUCCEEDED
    view = repository.read_world(scope, world_id, branch_id)
    assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == view.state_hash
    return view


@pytest.mark.parametrize("interrupted", [False, True])
def test_itinerary_resumes_from_committed_first_leg_without_player_route_reentry(tmp_path, interrupted):
    state, ids = _travel_world()
    final_place, next_route = _id(8), _id(9)
    middle_id = ids["destination"]
    middle = state.entities[middle_id].model_copy(update={"components": {
        "place": PlaceComponent(place_type="settlement", routes=[PlaceRoute(
            route_id=next_route, from_place_id=middle_id, to_place_id=final_place, travel_ticks=30,
            risk=Decimal("1") if interrupted else Decimal("0"),
        )]).model_dump(mode="json"),
    }})
    state = state.model_copy(update={"entities": state.entities | {
        middle_id: middle, final_place: WorldEntity(id=final_place, kind="place", name="Hill Gate"),
    }})
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database = Database(f"sqlite:///{(tmp_path / 'itinerary-source.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        created = repository.create_world(scope, NewWorld(title="Ordered itinerary", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
        app = create_app(WorldApplication(repository, ContentCatalog()), session)
        with TestClient(app) as client:
            client.get("/api/v1/session").raise_for_status()
            path = f"/api/v1/worlds/{world_id}/long-actions"
            body = {
                "branch_id": str(branch_id), "expected_state_version": "0", "activity": "travel",
                "duration_seconds": 60, "route_ids": [str(ids["route"]), str(next_route)],
            }
            headers = {"Idempotency-Key": "ordered-itinerary"}
            reversed_routes = body | {"route_ids": list(reversed(body["route_ids"]))}
            assert client.post(path, headers=headers, json=reversed_routes).status_code == 422
            queued = client.post(path, headers=headers, json=body).raise_for_status().json()
            job_id = UUID(queued["job_id"])
            assert client.post(path, headers=headers, json=reversed_routes).status_code == 409
            status_path = f"/api/v1/jobs/{job_id}"
            jobs = SimulationJobs(repository)
            lease = repository.claim_job("itinerary-first-worker")
            assert lease is not None and lease.job_id == job_id
            for _ in range(10):
                outcome = jobs.advance(scope, lease)
                assert outcome.outcome == "advanced"
                record = repository.read_job(scope, job_id)
                if record.payload.get("itinerary_cursor"):
                    break
            else:
                pytest.fail("First itinerary arrival did not write its durable cursor.")
            assert record.status == JobStatus.RUNNING
            assert record.committed_through_tick == 20
            assert record.payload["itinerary_cursor"]["leg_index"] == 0
            assert client.get(status_path).raise_for_status().json()["itinerary_progress"] == {
                "legs_total": 2, "legs_arrived": 1,
            }
            assert client.post(status_path + "/control", json={"action": "pause"}).raise_for_status().json()["status"] == "paused"
            assert client.post(status_path + "/control", json={"action": "resume"}).raise_for_status().json()["status"] == "queued"
            resumed = repository.claim_job("itinerary-second-worker")
            assert resumed is not None and resumed.job_id == job_id
            if interrupted:
                for _ in range(10):
                    jobs.advance(scope, resumed)
                    saved = repository.read_job(scope, job_id)
                    if saved.status != JobStatus.RUNNING:
                        break
                assert saved.status == JobStatus.PAUSED
                assert saved.payload["itinerary_cursor"]["leg_index"] == 0
                assert saved.result["interruption"]["reason_code"] == "route_hazard"
                stopped = repository.read_world(scope, world_id, branch_id)
                interrupted_journey = next(
                    journey for journey in stopped.state.travel_journeys
                    if journey.route_id == next_route
                )
                assert interrupted_journey.status == "interrupted"
                assert interrupted_journey.departed_tick < interrupted_journey.resolved_tick < interrupted_journey.arrival_tick
                assert saved.committed_through_tick == interrupted_journey.resolved_tick
                assert [(journey.route_id, journey.status) for journey in stopped.state.travel_journeys] == [
                    (ids["route"], "arrived"), (next_route, "interrupted"),
                ]
                assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == stopped.state_hash
                with pytest.raises(Conflict, match="Plan a new itinerary from the current place"):
                    repository.control_job(scope, job_id, "resume")
                still_paused = repository.read_job(scope, job_id)
                still_stopped = repository.read_world(scope, world_id, branch_id)
                assert still_paused.status is JobStatus.PAUSED
                assert still_paused.payload == saved.payload and still_paused.result == saved.result
                assert still_stopped.state_hash == stopped.state_hash
                return
            after = _advance_to_completion(repository, scope, jobs, record, resumed, world_id, branch_id)
            assert after.state.current_tick == 50
            assert after.state.entities[ids["actor"]].location_id == final_place
            source_account = after.state.resources[ids["source_account"]]
            assert (source_account.balance, source_account.credit_limit) == (Decimal("0"), Decimal("2"))
            assert len(after.state.travel_journeys) == 2
            assert client.get(status_path).raise_for_status().json()["itinerary_progress"] == {
                "legs_total": 2, "legs_arrived": 2,
            }
    finally:
        database.dispose()


@pytest.mark.parametrize("mutate_projection", [False, True])
def test_authorized_itinerary_care_reuses_only_its_intact_projection(tmp_path, monkeypatch, mutate_projection):
    state, ids = _travel_world()
    actor_id, origin_id = ids["actor"], ids["origin"]
    owner, sink, source, lot = (_id(index) for index in range(21, 25))
    raw = state.model_dump(mode="python")
    raw["calendar"]["day_seconds"] = 3600
    raw["metadata"]["resource_roles"] = {"water_resource_id": "core.water"}
    raw["entities"][origin_id]["components"]["place"]["routes"][0]["travel_ticks"] = 3600
    raw["entities"][actor_id]["components"]["needs"] = NeedsComponent(water={"value": 26}).model_dump(mode="json")
    raw["entities"][actor_id]["components"]["inventory"] = InventoryComponent(lots=[InventoryLot(
        lot_id=lot, account_id=owner, resource_id="core.water", quantity="10",
    )]).model_dump(mode="json")
    raw["resources"].update({
        owner: ResourceAccount(resource_id="core.water", balance="10", owner_id=actor_id),
        sink: ResourceAccount(resource_id="core.water", balance="0", account_type="consumption_sink"),
        source: ResourceAccount(resource_id="core.water", balance="-10", account_type="issuance_source", credit_limit="10"),
    })
    state = WorldState.model_validate(raw)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database = Database(f"sqlite:///{(tmp_path / 'itinerary-care.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        reducer_calls = 0
        care_commit_reductions: list[tuple[int, int]] = []
        projection_mutated = False
        original_apply_event = repository_module.apply_event
        original_commit_command = repository.commit_command

        def counted_apply_event(state, event):
            nonlocal reducer_calls
            reducer_calls += 1
            return original_apply_event(state, event)

        def record_commit(scope, world_id, branch_id, command, events, **kwargs):
            nonlocal projection_mutated
            receipt = kwargs.get("itinerary_care_receipt")
            if mutate_projection and receipt is not None and not projection_mutated:
                assert receipt.projected_state is not None
                resources = dict(receipt.projected_state.resources)
                receipt.projected_state.resources.clear()
                receipt.projected_state.resources.update(reversed(list(resources.items())))
                projection_mutated = True
            before = reducer_calls
            result = original_commit_command(scope, world_id, branch_id, command, events, **kwargs)
            if receipt is not None:
                care_commit_reductions.append((len(events), reducer_calls - before))
            return result

        monkeypatch.setattr(repository_module, "apply_event", counted_apply_event)
        monkeypatch.setattr(repository, "commit_command", record_commit)
        created = repository.create_world(scope, NewWorld(title="Caravan ration test", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
        app = create_app(WorldApplication(repository, ContentCatalog()), session)
        with TestClient(app) as client:
            client.get("/api/v1/session").raise_for_status()
            body = {"branch_id": str(branch_id), "expected_state_version": "0", "activity": "travel",
                    "duration_seconds": 7200, "route_ids": [str(ids["route"])], "itinerary_auto_care": True}
            path = f"/api/v1/worlds/{world_id}/long-actions"
            headers = {"Idempotency-Key": "itinerary-owned-water"}
            queued = client.post(path, headers=headers, json=body).raise_for_status().json()
            assert client.post(path, headers=headers, json=body | {"itinerary_auto_care": False}).status_code == 409
            job_id = UUID(queued["job_id"])

        jobs = SimulationJobs(repository)
        lease = repository.claim_job("itinerary-care-worker")
        assert lease is not None and lease.job_id == job_id
        for _ in range(40):
            jobs.advance(scope, lease)
            saved = repository.read_job(scope, job_id)
            if saved.status != JobStatus.RUNNING:
                break
        assert saved.status == JobStatus.SUCCEEDED
        assert care_commit_reductions
        if mutate_projection:
            assert projection_mutated
            assert care_commit_reductions[0][1] == care_commit_reductions[0][0]
        else:
            assert all(reduced == 0 for _event_count, reduced in care_commit_reductions)
        after = repository.read_world(scope, world_id, branch_id)
        journey, = after.state.travel_journeys
        water_used = after.state.resources[sink].balance
        assert journey.status == "arrived" and after.state.entities[actor_id].location_id == ids["destination"]
        assert 0 < water_used < 10
        assert after.state.resources[owner].balance == Decimal("10") - water_used
        assert journey.stationary_ticks == 60 * water_used
        assert journey.resolved_tick == 3600 + journey.stationary_ticks
        assert after.state.resources[source].balance == Decimal("-10")
        persisted_events = repository.list_events(scope, world_id, branch_id, limit=500).events
        for delay in (
            event for event in persisted_events
            if event.type == "core.travel_journey_changed.v1" and event.payload.get("operation") == "delay"
        ):
            batch = [event for event in persisted_events if event.command_id == delay.command_id]
            from_tick = int(delay.payload["stationary_from_tick"])
            until_tick = int(delay.payload["stationary_until_tick"])
            assert batch[0].event_id == delay.event_id and delay.tick == from_tick
            assert batch[1].type == "core.time_advanced.v1" and batch[1].tick == until_tick
            assert all(event.tick == until_tick for event in batch[1:])
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == after.state_hash
    finally:
        database.dispose()


@pytest.mark.parametrize(
    "path", ["native_reuse", "care_threshold", "mutated_projection", "unexpected_event", "injected_rule"],
)
def test_itinerary_transit_projection_is_exact_and_fail_closed(tmp_path, monkeypatch, path):
    state, ids = _travel_world()
    actor_water = 21 if path == "care_threshold" else 50
    actor = state.entities[ids["actor"]].model_copy(update={
        "components": state.entities[ids["actor"]].components | {
            "needs": NeedsComponent(water={"value": actor_water}).model_dump(mode="json"),
        },
    })
    resident = WorldEntity(
        id=_id(8), kind="character", name="Local Resident", location_id=ids["origin"],
        components={"needs": NeedsComponent(water={"value": 50}).model_dump(mode="json")},
    )
    state = state.model_copy(update={
        "entities": state.entities | {actor.id: actor, resident.id: resident},
        "scheduled_events": [],
    })
    if path == "care_threshold":
        state = state.model_copy(update={
            "calendar": state.calendar.model_copy(update={"day_seconds": 60}),
        })
    origin = state.entities[ids["origin"]]
    place = PlaceComponent.model_validate(origin.components["place"])
    long_route = place.routes[0].model_copy(update={"travel_ticks": 200})
    long_place = place.model_copy(update={"routes": [long_route]})
    origin = origin.model_copy(update={
        "components": {**origin.components, "place": long_place.model_dump(mode="json")},
    })
    state = state.model_copy(update={"entities": state.entities | {origin.id: origin}})
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database_path = tmp_path / "itinerary-transit-projection.db"
    database = Database(f"sqlite:///{database_path.as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        created = repository.create_world(scope, NewWorld(title="Itinerary projection test", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        session = LocalSession(str(scope.user_id), str(scope.tenant_id), frozenset({"http://testserver"}))
        app = create_app(WorldApplication(repository, ContentCatalog()), session)
        with TestClient(app) as client:
            client.get("/api/v1/session").raise_for_status()
            request = {
                "branch_id": str(branch_id), "expected_state_version": "0", "activity": "travel",
                "duration_seconds": 600, "route_ids": [str(ids["route"])],
            }
            if path == "care_threshold":
                request["itinerary_auto_care"] = True
            queued = client.post(
                f"/api/v1/worlds/{world_id}/long-actions",
                headers={"Idempotency-Key": f"itinerary-transit-projection-{path}"},
                json=request,
            ).raise_for_status().json()
        job_id = UUID(queued["job_id"])

        class InjectedRuleEngine(RuleEngine):
            pass

        jobs = SimulationJobs(repository, engine=SimulationEngine(
            rule_engine=InjectedRuleEngine() if path == "injected_rule" else RuleEngine(),
            policy=SimulationPolicy(max_macro_step_seconds=60),
        ))
        lease = repository.claim_job(f"transit-projection-worker:{path}")
        assert lease is not None and lease.job_id == job_id
        reducer_calls = 0
        projection_mutated = False
        unexpected_event_added = False
        focal_needs_shape_observed = False
        timed_transit_commits: list[tuple[bool, int, int, tuple[str, ...]]] = []
        original_apply_event = repository_module.apply_event
        original_commit_command = repository.commit_command

        def counted_apply_event(current_state, event):
            nonlocal reducer_calls
            reducer_calls += 1
            return original_apply_event(current_state, event)

        def record_commit(current_scope, current_world, current_branch, command, events, **kwargs):
            nonlocal projection_mutated, unexpected_event_added, focal_needs_shape_observed
            receipt = kwargs.get("itinerary_transit_projection_receipt")
            event_batch = tuple(events)
            if receipt is not None:
                assert kwargs.get("itinerary_care_receipt") is None
                if (
                    len(event_batch) == 3
                    and event_batch[1].type == "core.background_needs_advanced.v1"
                    and event_batch[2].type == "core.component_replaced.v1"
                ):
                    assert _is_ordinary_itinerary_transit_batch(
                        event_batch, actor_id=ids["actor"], to_tick=event_batch[0].to_tick,
                    )
                    non_focal_needs = event_batch[2].model_copy(update={"entity_id": resident.id})
                    assert not _is_ordinary_itinerary_transit_batch(
                        (event_batch[0], event_batch[1], non_focal_needs),
                        actor_id=ids["actor"], to_tick=event_batch[0].to_tick,
                    )
                if path == "mutated_projection" and not projection_mutated:
                    receipt.projected_payload["current_tick"] = -1
                    projection_mutated = True
                if path == "unexpected_event" and not unexpected_event_added and receipt is not None:
                    unexpected_event_added = True
                    at_tick = event_batch[0].to_tick
                    event_batch = (*event_batch, GoalCreatedEvent(goal=GoalRecord(
                        id=_id(999), owner_id=ids["actor"], title="Fail-closed receipt probe",
                        created_tick=at_tick, updated_tick=at_tick,
                    )))
                focal_needs_shape_observed = focal_needs_shape_observed or (
                    len(event_batch) >= 3
                    and event_batch[0].type == "core.time_advanced.v1"
                    and event_batch[1].type == "core.background_needs_advanced.v1"
                    and event_batch[2].type == "core.component_replaced.v1"
                    and getattr(event_batch[2], "component_name", None) == "needs"
                    and getattr(event_batch[2], "entity_id", None) == ids["actor"]
                )
            before = reducer_calls
            try:
                result = original_commit_command(
                    current_scope, current_world, current_branch, command, event_batch, **kwargs,
                )
            except Exception:
                reduced = reducer_calls - before
                if any(event.type == "core.time_advanced.v1" for event in event_batch):
                    timed_transit_commits.append((receipt is not None, len(event_batch), reduced,
                                                  tuple(event.type for event in event_batch)))
                raise
            reduced = reducer_calls - before
            if any(event.type == "core.time_advanced.v1" for event in event_batch):
                timed_transit_commits.append((receipt is not None, len(event_batch), reduced,
                                              tuple(event.type for event in event_batch)))
            return result

        monkeypatch.setattr(repository_module, "apply_event", counted_apply_event)
        monkeypatch.setattr(repository, "commit_command", record_commit)
        failed_on_unexpected_event = False
        window_debug = []
        max_windows = 2 if path == "care_threshold" else 20
        for _ in range(max_windows):
            try:
                resolution = jobs.advance(scope, lease)
                window_debug.append((resolution.metrics.integration_boundary, resolution.next_tick,
                                     resolution.rule_resolution.outcome.status if resolution.rule_resolution else None,
                                     resolution.rule_resolution.outcome.code if resolution.rule_resolution else None,
                                     tuple(event.type for event in resolution.events),
                                     resolution._itinerary_transit_projection_receipt is not None))
            except InvalidEvent:
                failed_on_unexpected_event = True
                break
            saved = repository.read_job(scope, job_id)
            if saved.status != JobStatus.RUNNING:
                break

        allowlisted_batches = [
            row for row in timed_transit_commits
            if row[3]
            and row[3][0] == "core.time_advanced.v1"
            and set(row[3]).issubset({
                    "core.time_advanced.v1", "core.background_needs_advanced.v1", "core.component_replaced.v1",
            })
        ]
        assert allowlisted_batches, timed_transit_commits
        if path == "care_threshold":
            assert len(window_debug) == 2
            assert window_debug[-1][0] == "itinerary_auto_care_threshold"
            assert window_debug[-1][-1]
            assert any(
                has_receipt and reduce_count == 0 and event_types == (
                    "core.time_advanced.v1", "core.background_needs_advanced.v1", "core.component_replaced.v1",
                )
                for has_receipt, _count, reduce_count, event_types in timed_transit_commits
            )
            assert saved.status == JobStatus.RUNNING
        elif path == "native_reuse":
            assert focal_needs_shape_observed
            assert any(has_receipt and reduce_count == 0 for has_receipt, _count, reduce_count, _types in allowlisted_batches), (timed_transit_commits, window_debug)
            assert saved.status == JobStatus.SUCCEEDED
        elif path == "mutated_projection":
            assert projection_mutated
            assert any(has_receipt and reduce_count == count
                       for has_receipt, count, reduce_count, _types in allowlisted_batches), timed_transit_commits
            assert saved.status == JobStatus.SUCCEEDED
        elif path == "unexpected_event":
            assert unexpected_event_added
            assert not failed_on_unexpected_event
            assert any(has_receipt and "core.goal_created.v1" in types and reduce_count == count
                       for has_receipt, count, reduce_count, types in timed_transit_commits)
            assert saved.status == JobStatus.SUCCEEDED
        else:
            assert saved.status == JobStatus.SUCCEEDED
            assert all(not has_receipt for has_receipt, _count, _reduce_count, _types in timed_transit_commits)
            assert any(reduce_count == count for _has_receipt, count, reduce_count, _types in allowlisted_batches)

        after = repository.read_world(scope, world_id, branch_id)
        assert after.state.current_tick == (2 if path == "care_threshold" else 200), window_debug
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == after.state_hash
    finally:
        database.dispose()


def test_leased_travel_crosses_scheduled_boundary_and_survives_midjourney_backup(tmp_path):
    state, ids = _travel_world()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    source_database = Database(f"sqlite:///{(tmp_path / 'travel-source.db').as_posix()}")
    restored_database = Database(f"sqlite:///{(tmp_path / 'travel-restored.db').as_posix()}")
    source_database.create_schema()
    restored_database.create_schema()
    repository = WorldRepository(source_database)
    restored_repository = WorldRepository(restored_database)
    try:
        created = repository.create_world(scope, NewWorld(title="Leased travel", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id

        # A structurally valid departure event still cannot bypass the worker
        # lease and the persisted long-action route plan.
        unleased_command_id = uuid4()
        unleased_context = RuleContext(
            world_id=world_id,
            branch_id=branch_id,
            command_id=unleased_command_id,
            actor_id=ids["actor"],
            ruleset_version=created.world.ruleset_version,
        )
        unleased_resolution = RuleEngine().evaluate_travel_departure(state, ids["route"], unleased_context)
        departure, = unleased_resolution.events
        with pytest.raises(AccessDenied, match="simulation worker lease"):
            repository.commit_command(
                scope,
                world_id,
                branch_id,
                CommandEnvelope(
                    command_id=unleased_command_id,
                    actor_id=ids["actor"],
                    idempotency_key="unleased-travel-departure",
                    expected_state_version=created.branch.state_version,
                    command_type="simulation.window",
                ),
                [departure],
            )
        unchanged = repository.read_world(scope, world_id, branch_id)
        assert unchanged.state.current_tick == 0
        assert unchanged.state.travel_journeys == []
        assert unchanged.state.entities[ids["actor"]].location_id == ids["origin"]

        jobs, job, lease = _enqueue_travel(
            repository, scope, world_id, branch_id, ids["actor"], ids["route"], key="durable-travel",
        )
        first = jobs.advance(scope, lease)
        assert first.outcome == "advanced"
        departed = repository.read_world(scope, world_id, branch_id)
        journey, = departed.state.travel_journeys
        travel_schedule = next(
            item for item in departed.state.scheduled_events if item.handler_id == TRAVEL_RESOLUTION_HANDLER_ID
        )
        assert departed.state.current_tick == 0
        assert departed.state.entities[ids["actor"]].location_id is None
        assert journey.status == "in_transit" and journey.arrival_tick == 20 and journey.hazard_tick is None
        active_job = repository.read_job(scope, job.job_id)
        journey_context = journey.model_dump(mode="json")
        assert travel_job_readback(journey_context, [active_job], 0) == {
            "status": "running", "committed_through_tick": 0, "survival_pressure": False,
        }
        assert travel_job_readback(journey_context | {"actor_id": str(uuid4())}, [active_job], 0) is None
        assert travel_schedule.status == "queued"
        assert travel_schedule.handler_version == TRAVEL_RESOLUTION_HANDLER_VERSION
        assert travel_schedule.phase == "resolution" and travel_schedule.due_tick == 20

        # A direct clock edit cannot commit while the simulation owns the
        # branch lease; only the leased job can advance to the next boundary.
        with pytest.raises(Conflict, match="active writer lease"):
            repository.commit_command(
                scope,
                world_id,
                branch_id,
                CommandEnvelope(
                    actor_id=ids["actor"],
                    idempotency_key="unleased-clock-advance",
                    expected_state_version=departed.branch.state_version,
                    command_type="fixture.unleased_tick",
                ),
                [TimeAdvancedEvent(to_tick=1)],
            )

        # Export and restore a valid checkpoint while the traveler has no
        # local presence and the one-shot resolution remains queued.
        backup = BackupApplication(repository, scratch_directory=tmp_path / "travel-backup-scratch")
        archive = backup.validate_archive(backup.export_world(scope, world_id, format_version="2.0"))
        WorldBackupImporter(restored_repository).import_verified_backup(scope, archive)
        checkpoint = restored_repository.read_world(scope, world_id, branch_id)
        assert checkpoint.state.current_tick == 0
        assert checkpoint.state.entities[ids["actor"]].location_id is None
        assert checkpoint.state.travel_journeys[0].status == "in_transit"
        assert next(item for item in checkpoint.state.scheduled_events if item.id == travel_schedule.id).status == "queued"
        assert restored_repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == checkpoint.state_hash

        restored_jobs, restored_job, restored_lease = _enqueue_travel(
            restored_repository, scope, world_id, branch_id, ids["actor"], ids["route"], key="restored-travel",
        )
        restored_final = _advance_to_completion(
            restored_repository, scope, restored_jobs, restored_job, restored_lease, world_id, branch_id,
        )
        assert restored_final.state.current_tick == 20
        assert restored_final.state.entities[ids["actor"]].location_id == ids["destination"]
        assert restored_final.state.travel_journeys[0].status == "arrived"

        # A committed action during a pause can advance the clock. Continue
        # the same journey under a fresh fence from the resulting checkpoint.
        paused = repository.control_job(scope, job.job_id, "pause")
        assert paused.committed_through_tick == 0
        turn = repository.commit_command(
            scope, world_id, branch_id,
            CommandEnvelope(
                actor_id=ids["actor"], idempotency_key="paused-travel-turn",
                expected_state_version=departed.branch.state_version,
                command_type="fixture.paused_travel_turn",
            ),
            [TimeAdvancedEvent(to_tick=1)],
        )
        resumed = repository.control_job(scope, job.job_id, "resume")
        assert resumed.status == JobStatus.QUEUED and resumed.committed_through_tick == 1
        assert resumed.payload["plan"] == paused.payload["plan"]
        assert resumed.payload["committed_state_version"] == turn.state_version
        assert repository.control_job(scope, job.job_id, "resume") == resumed
        continued_lease = repository.claim_job("travel-worker:resumed", job_type=job.job_type)
        assert continued_lease is not None and continued_lease.job_id == job.job_id
        assert continued_lease.fencing_token > lease.fencing_token
        lease = continued_lease

        final = _advance_to_completion(repository, scope, jobs, job, lease, world_id, branch_id)
        assert final.state.current_tick == 20
        assert final.state.entities[ids["actor"]].location_id == ids["destination"]
        journey, = final.state.travel_journeys
        assert journey.status == "arrived" and journey.resolved_tick == journey.arrival_tick == 20
        assert next(item for item in final.state.scheduled_events if item.id == travel_schedule.id).status == "completed"
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash
        visible_places = {str(ids["destination"]): {"name": "Destination"}}
        assert recent_resolved_travel(final.state, ids["actor"], visible_places) == [{
            "journey_id": str(journey.journey_id), "status": "arrived", "departed_tick": 0,
            "resolved_tick": 20, "stationary_ticks": 0, "from_place": None, "to_place": "Destination",
        }]
        assert recent_resolved_travel(final.state, uuid4(), visible_places) == []

        events = repository.list_events(scope, world_id, branch_id, limit=100).events
        event_types = [item.type for item in events]
        quota_index = event_types.index("core.production_quota_renewed.v1")
        travel_events = [
            (index, event) for index, event in enumerate(events)
            if event.type == "core.travel_journey_changed.v1"
        ]
        depart_index, depart_record = next((index, event) for index, event in travel_events
                                           if event.payload["operation"] == "depart")
        arrive_index, arrive_record = next((index, event) for index, event in travel_events
                                           if event.payload["operation"] == "arrive")
        assert depart_index < quota_index < arrive_index
        assert depart_record.tick == 0 and events[quota_index].tick == 10 and arrive_record.tick == 20
        source_account = final.state.resources[ids["source_account"]]
        assert source_account.balance == Decimal("0")
        assert source_account.credit_limit == Decimal("2")
        production = final.state.entities[ids["producer"]].components["production"]
        assert production["quotas"][0]["last_issued_period"] == 1
    finally:
        restored_database.dispose()
        source_database.dispose()


def test_stationary_actions_push_arrival_past_the_old_due_tick(tmp_path):
    state, ids = _travel_world()
    resident = WorldEntity(
        id=_id(8), kind="character", name="Resident", location_id=ids["origin"],
        components={"needs": NeedsComponent(water={"value": 50}).model_dump(mode="json")},
    )
    state = state.model_copy(update={"entities": {**state.entities, resident.id: resident}})
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database = Database(f"sqlite:///{(tmp_path / 'stationary.db').as_posix()}")
    database.create_schema()
    repo = WorldRepository(database)
    try:
        created = repo.create_world(scope, NewWorld(title="Stationary route", state=state))
        world, branch = created.world.world_id, created.branch.branch_id
        jobs, travel, lease = _enqueue_travel(repo, scope, world, branch, ids["actor"], ids["route"], key="route")
        jobs.advance(scope, lease)
        repo.control_job(scope, travel.job_id, "pause")
        departed = repo.read_world(scope, world, branch)
        journey_before, = departed.state.travel_journeys

        # The same action path serves chat, including a command that does not
        # cross another world's still-due schedule at tick 10.
        command = uuid4()
        wait = RuleEngine().evaluate(departed.state, WaitIntent(duration_ticks=5), RuleContext(
            world_id=world, branch_id=branch, command_id=command, actor_id=ids["actor"],
            ruleset_version=created.world.ruleset_version,
        ))
        assert wait.events[0].operation == "delay"
        assert [event.type for event in wait.events[:3]] == [
            "core.travel_journey_changed.v1", "core.time_advanced.v1", "core.background_needs_advanced.v1",
        ]
        envelope = CommandEnvelope(command_id=command, actor_id=ids["actor"], idempotency_key="wait-while-traveling",
                                   expected_state_version=departed.branch.state_version, command_type="action.wait",
                                   payload={"kind": "wait", "duration_ticks": 5})
        with pytest.raises(InvalidEvent, match="matching clock advance"):
            repo.commit_command(scope, world, branch, envelope, wait.events[:1])
        assert repo.read_world(scope, world, branch).state_hash == departed.state_hash
        repo.commit_command(scope, world, branch, envelope, wait.events)

        rest = jobs.enqueue(
            scope, world, branch,
            LongActionPlan(actor_id=ids["actor"], started_tick=5, target_tick=125, activity="rest"),
            idempotency_key="rest-while-traveling", speed="balanced", depth="standard",
        )
        rest_lease = repo.claim_job("rest-while-traveling", job_type=rest.job_type)
        assert rest_lease and rest_lease.job_id == rest.job_id
        rested = _advance_to_completion(repo, scope, jobs, rest, rest_lease, world, branch)
        delayed, = rested.state.travel_journeys
        assert delayed.journey_id == journey_before.journey_id
        assert delayed.stationary_ticks == 125 and delayed.arrival_tick == 145
        assert rested.state.current_tick == 125 and rested.state.entities[ids["actor"]].location_id is None
        quota, = [event for event in repo.list_events(scope, world, branch).events
                   if event.type == "core.production_quota_renewed.v1"]
        assert quota.tick == 10
        assert repo.replay(scope, world, branch, from_genesis=True).state_hash == rested.state_hash

        backup = BackupApplication(repo, scratch_directory=tmp_path / "stationary-backup-scratch")
        archive = backup.validate_archive(backup.export_world(scope, world, format_version="2.0"))
        restored_db = Database(f"sqlite:///{(tmp_path / 'stationary-restored.db').as_posix()}")
        try:
            restored_db.create_schema()
            restored_repo = WorldRepository(restored_db)
            WorldBackupImporter(restored_repo).import_verified_backup(scope, archive)
            restored = restored_repo.read_world(scope, world, branch)
            assert restored.state.travel_journeys == [delayed]
            assert restored.state_hash == rested.state_hash
            restored_jobs = SimulationJobs(restored_repo)
            restored_travel = restored_jobs.enqueue(
                scope, world, branch,
                LongActionPlan(actor_id=ids["actor"], route_id=ids["route"], activity="travel",
                               started_tick=125, target_tick=delayed.arrival_tick),
                idempotency_key="continue-restored-route", speed="balanced", depth="standard",
            )
            restored_lease = restored_repo.claim_job("restored-route", job_type=restored_travel.job_type)
            assert restored_lease and restored_lease.job_id == restored_travel.job_id
            restored_arrival = _advance_to_completion(
                restored_repo, scope, restored_jobs, restored_travel, restored_lease, world, branch,
            )
            assert restored_arrival.state.current_tick == 145
            assert restored_arrival.state.entities[ids["actor"]].location_id == ids["destination"]
        finally:
            restored_db.dispose()

        resumed = repo.control_job(scope, travel.job_id, "resume")
        assert resumed.payload["plan"] == travel.payload["plan"]
        assert resumed.payload["effective_travel_target_tick"] == 145
        continuation = repo.claim_job("continue-delayed-travel", job_type=travel.job_type)
        assert continuation and continuation.fencing_token > lease.fencing_token
        arrived = _advance_to_completion(repo, scope, jobs, resumed, continuation, world, branch)
        assert arrived.state.current_tick == 145
        assert arrived.state.entities[ids["actor"]].location_id == ids["destination"]
        assert repo.replay(scope, world, branch, from_genesis=True).state_hash == arrived.state_hash
    finally:
        database.dispose()
