from fastapi.testclient import TestClient
import pytest

from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.backups import BackupApplication
from world_engine.content import ContentCatalog
from world_engine.domain.components import PlaceComponent, PlaceRoute
from world_engine.domain.errors import InvalidLease
from world_engine.domain.models import AccessScope, CommandEnvelope, EntityFieldChangedEvent, NewWorld, TimeAdvancedEvent, WorldEntity, WorldState
from world_engine.domain.models import JobStatus
from world_engine.storage.database import Database
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.repository import WorldRepository
from uuid import UUID, uuid4
from world_engine.runtime import build_application, local_identity
from world_engine.rules import RuleContext, RuleEngine
from world_engine.simulation import LongActionPlan
from world_engine.simulation.contracts import WorldAdvancePlan
from world_engine.simulation.engine import SimulationEngine, SimulationPolicy


@pytest.mark.parametrize(("job_activity", "weather", "choose_alternative", "phase_day"), [
    ("world-advance", "clear", False, 0), ("world-advance", "storm", True, 0),
    ("rest", "storm", True, 0), ("wait", "clear", False, 0), ("travel", "storm", True, 0),
    ("wait", "clear", False, 1),
])
def test_simulation_commits_actual_routine_route_and_replays(tmp_path, job_activity, weather, choose_alternative, phase_day):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        player, npc, origin, outside, inside, window, trail_end = (uuid4() for _ in range(7))
        routes = [PlaceRoute(route_id=uuid4(), from_place_id=origin, to_place_id=place, travel_ticks=30)
                  for place in (outside, inside)]
        player_route = PlaceRoute(route_id=uuid4(), from_place_id=origin, to_place_id=trail_end, travel_ticks=180)
        routes.append(player_route)
        day_seconds = 1200
        mobility = {"alternatives": {str(window): [str(inside)]}, "avoid_conditions": ["storm"]}
        if phase_day:
            mobility["cadences"] = {str(window): {"every_days": 2, "phase_day": phase_day}}
        state = WorldState(player_id=player, calendar={"day_seconds": day_seconds}, entities={
            origin: WorldEntity(id=origin, kind="place", components={
                "place": PlaceComponent(place_type="settlement", routes=routes).model_dump(mode="json"),
            }),
            outside: WorldEntity(id=outside, kind="place", components={
                "place": {"place_type": "settlement", "environment": {"weather": weather}},
            }),
            inside: WorldEntity(id=inside, kind="place", components={"place": {"place_type": "settlement"}}),
            trail_end: WorldEntity(id=trail_end, kind="place", components={"place": {"place_type": "settlement"}}),
            player: WorldEntity(id=player, kind="character", location_id=origin, components={
                "capabilities": {"grants": ["world.travel"]}, "needs": {"rest": {"value": 40}},
            }),
            npc: WorldEntity(id=npc, kind="character", location_id=origin, components={
                "capabilities": {"grants": ["world.travel"]},
                "daily_routine": {"windows": [{"window_id": str(window), "start_second": 120,
                                               "duration_seconds": 600, "activity": "study", "place_id": str(outside)}]},
                "routine_mobility": mobility,
            }),
        })
        created = repo.create_world(scope, NewWorld(title="Routine routes", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        player_view = f"/api/v1/worlds/{world_id}/view?branch_id={branch_id}"
        before_projection = client.get(player_view).raise_for_status().json()["state"]["entities"]
        assert str(npc) in before_projection
        assert "routine_mobility" not in before_projection[str(npc)]["components"]
        endpoint = "world-advance" if job_activity == "world-advance" else "long-actions"
        occurrence = day_seconds * phase_day + 120
        body = {"branch_id": str(branch_id), "expected_state_version": "0", "duration_seconds": occurrence}
        if job_activity != "world-advance":
            body["activity"] = job_activity
        if job_activity == "travel":
            body.update(duration_seconds=180, route_id=str(player_route.route_id))
        response = client.post(f"/api/v1/worlds/{world_id}/{endpoint}", headers={"Idempotency-Key": "routine"}, json=body)
        job_id = UUID(response.raise_for_status().json()["job_id"])
        lease = repo.claim_job("routine-worker")
        assert lease is not None and lease.job_id == job_id
        jobs = SimulationJobs(repo)
        for _ in range(10):
            jobs.advance(scope, lease)
            if repo.read_job(scope, job_id).status == JobStatus.SUCCEEDED:
                break
        else:
            pytest.fail("Routine world advance did not finish")
        final = repo.read_world(scope, world_id, branch_id)
        journey, = [entry for entry in final.state.travel_journeys if entry.actor_id == npc]
        assert journey.departed_tick == occurrence - 30 and journey.resolved_tick == occurrence
        assert journey.status == "arrived" and journey.actor_id == npc
        assert final.state.entities[npc].location_id == (inside if choose_alternative else outside)
        details, = [event.payload["routine_choice"] for event in repo.list_events(scope, world_id, branch_id).events
                    if event.type == "core.travel_journey_changed.v1" and event.payload.get("routine_choice")]
        assert details["window_id"] == str(window)
        assert details["reason"] == ("weather" if choose_alternative else "scheduled_destination")
        player_history = client.get(f"/api/v1/worlds/{world_id}/history?branch_id={branch_id}&mode=player").raise_for_status().text
        assert "routine_choice" not in player_history
        assert final.state.current_tick == body["duration_seconds"]
        if job_activity == "travel":
            assert final.state.entities[player].location_id == trail_end
        final_projection = client.get(player_view).raise_for_status().json()["state"]["entities"]
        assert str(npc) not in final_projection
        assert repo.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash
        if job_activity == "world-advance" and choose_alternative:
            backups = BackupApplication(repo)
            archive = backups.validate_archive(backups.export_world(scope, world_id, format_version="2.0"))
            restored_db = Database(f"sqlite:///{(tmp_path / 'routine-restore.db').as_posix()}")
            try:
                restored_db.create_schema()
                restored = WorldRepository(restored_db)
                WorldBackupImporter(restored).import_verified_backup(scope, archive)
                assert restored.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash
                assert any(event.payload.get("routine_choice") == details
                           for event in restored.list_events(scope, world_id, branch_id).events)
            finally:
                restored_db.dispose()


def test_real_simulation_window_commits_with_job_progress_and_replays(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        draft = ContentCatalog().build_world("long_life_cultivation", "Job QA", "Traveler")
        world = repo.create_world(scope, draft)
        jobs = SimulationJobs(repo, SimulationEngine(policy=SimulationPolicy(max_macro_step_seconds=1800)))
        job = jobs.enqueue(scope, world.world.world_id, world.branch.branch_id, LongActionPlan(
            actor_id=draft.state.player_id, started_tick=0, target_tick=3600, activity="rest",
        ), idempotency_key="qa-rest", speed="balanced", depth="deep")
        lease = repo.claim_job("qa")
        assert lease is not None
        first = jobs.advance(scope, lease)
        assert first.outcome == "advanced"
        assert repo.read_job(scope, job.job_id).committed_through_tick == 1800
        control = f"/api/v1/jobs/{job.job_id}/control"
        paused = client.post(control, json={"action": "pause"}).raise_for_status().json()
        assert paused["status"] == "paused" and paused["committed_through_tick"] == "1800"
        assert repo.claim_job("while-paused") is None
        with pytest.raises(InvalidLease):
            jobs.advance(scope, lease)
        resumed = client.post(control, json={"action": "resume"}).raise_for_status().json()
        assert resumed["status"] == "queued"
        replacement = repo.claim_job("replacement")
        assert replacement is not None and replacement.job_id == job.job_id
        assert replacement.fencing_token > lease.fencing_token
        with pytest.raises(InvalidLease):
            jobs.advance(scope, lease)
        assert repo.read_job(scope, job.job_id).committed_through_tick == 1800
        outcome = SimulationJobs(repo).advance(scope, replacement)
        assert outcome.outcome == "completed"
        assert len(outcome.events) >= 2
        persisted = repo.read_job(scope, job.job_id)
        assert persisted.status.value == "succeeded" and persisted.committed_through_tick == 3600
        live = repo.read_world(scope, job.world_id, job.branch_id)
        assert live.state.current_tick == 3600
        assert live.state.entities[draft.state.player_id].components["needs"] != draft.state.entities[draft.state.player_id].components["needs"]
        assert repo.replay(scope, job.world_id, job.branch_id).state_hash == live.state_hash
        npc = next(entity for entity in draft.state.entities.values()
                   if entity.kind == "character" and entity.id != draft.state.player_id)
        assert live.state.entities[npc.id].components["needs"] != npc.components["needs"]
        events = repo.list_events(scope, job.world_id, job.branch_id).events
        assert sum(event.type == "core.background_needs_advanced.v1" for event in events) == 2

        stale = jobs.enqueue(scope, job.world_id, job.branch_id, LongActionPlan(
            actor_id=draft.state.player_id, started_tick=3600, target_tick=7200, activity="rest",
        ), idempotency_key="qa-stale", speed="balanced", depth="deep")
        repo.commit_command(scope, job.world_id, job.branch_id, CommandEnvelope(
            actor_id=draft.state.player_id, idempotency_key="other-command",
            expected_state_version=live.branch.state_version, command_type="core.tick",
        ), [TimeAdvancedEvent(to_tick=3601)])
        before_stale = repo.read_world(scope, job.world_id, job.branch_id)
        stale_lease = repo.claim_job("qa")
        assert stale_lease is not None and stale_lease.job_id == stale.job_id
        failed = jobs.advance(scope, stale_lease)
        assert failed.status.value == "failed" and failed.result["error"] == "BRANCH_CLOCK_CHANGED"
        assert repo.read_world(scope, job.world_id, job.branch_id).last_seq == before_stale.last_seq

    # Reopening the same directory proves the application released its lock.
    with TestClient(build_application(data_directory=tmp_path), base_url="http://127.0.0.1:8000") as restarted:
        assert restarted.get("/health").status_code == 200


def test_http_world_advance_runs_under_owner_without_freezing_player(tmp_path):
    class SeedCheckingEngine(SimulationEngine):
        def plan_window(self, state, plan, context):
            assert context.world_seed == "0"
            return super().plan_window(state, plan, context)

    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        player, npc = uuid4(), uuid4()
        created = repo.create_world(scope, NewWorld(title="World advance", state=WorldState(
            calendar={"day_seconds": 60}, player_id=player, metadata={"generation_seed": 0},
            entities={entity_id: WorldEntity(id=entity_id, kind="character", components={"needs": {"food": {"value": 90}}})
                      for entity_id in (player, npc)},
        )))
        wid, bid = created.world.world_id, created.branch.branch_id
        body = {"branch_id": str(bid), "expected_state_version": "0", "duration_seconds": 60}
        endpoint = f"/api/v1/worlds/{wid}/world-advance"
        headers = {"Idempotency-Key": "advance"}
        accepted = client.post(endpoint, json=body, headers=headers).raise_for_status().json()
        assert accepted["job_type"] == "simulation.world_advance"
        assert accepted["started_tick"] == "0" and accepted["target_tick"] == "60"
        lease = repo.claim_job("advance", job_type="simulation.world_advance")
        SimulationJobs(repo, SeedCheckingEngine()).advance(scope, lease)
        persisted = repo.read_world(scope, wid, bid)
        assert persisted.state.current_tick == 60 and persisted.state.player_id == player
        assert all(persisted.state.entities[entity_id].components["needs"]["food"]["value"] < 90
                   for entity_id in (player, npc))
        assert repo.replay(scope, wid, bid).state_hash == persisted.state_hash
        duplicate = client.post(endpoint, json=body, headers=headers).raise_for_status().json()
        assert duplicate["job_id"] == accepted["job_id"]
        changed = client.post(endpoint, json={**body, "duration_seconds": 61}, headers=headers)
        assert changed.status_code == 409


def test_interrupted_itinerary_preserves_arrivals_and_accepts_a_new_route(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        actor, origin, waypoint, goal = (uuid4() for _ in range(4))
        first = PlaceRoute(route_id=uuid4(), from_place_id=origin, to_place_id=waypoint, travel_ticks=120)
        risky = PlaceRoute(route_id=uuid4(), from_place_id=waypoint, to_place_id=goal, travel_ticks=180, risk="1")
        detour = PlaceRoute(route_id=uuid4(), from_place_id=waypoint, to_place_id=goal, travel_ticks=240)
        state = WorldState(player_id=actor, entities={
            actor: WorldEntity(
                id=actor, kind="character", name="Traveler", location_id=origin,
                components={
                    "capabilities": {"grants": ["world.travel"]},
                    "known_places": [str(place) for place in (origin, waypoint, goal)],
                },
            ),
            **{
                place: WorldEntity(
                    id=place, kind="place", name=name,
                    components={"place": PlaceComponent(routes=routes).model_dump(mode="json")},
                )
                for place, name, routes in (
                    (origin, "Home Port", [first]),
                    (waypoint, "Waystation", [risky, detour]),
                    (goal, "Far Harbor", []),
                )
            },
        })
        created = repo.create_world(scope, NewWorld(title="Caravan to Far Harbor", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        endpoint = f"/api/v1/worlds/{world_id}/long-actions"
        body = {"branch_id": str(branch_id), "duration_seconds": 900, "activity": "travel"}
        accepted = client.post(
            endpoint,
            headers={"Idempotency-Key": "outbound"},
            json=body | {"expected_state_version": "0", "route_ids": [str(first.route_id), str(risky.route_id)]},
        ).raise_for_status().json()
        assert "itinerary_requires_replan" not in accepted
        job_path = f"/api/v1/jobs/{accepted['job_id']}"
        jobs = SimulationJobs(repo)
        lease = repo.claim_job("travel-regression")
        assert lease is not None and lease.job_id == UUID(accepted["job_id"])
        for _ in range(8):
            resolution = jobs.advance(scope, lease)
            if resolution.outcome == "stopped":
                break
        assert resolution.outcome == "stopped"
        paused = client.get(job_path).raise_for_status().json()
        assert paused["status"] == "paused" and paused["reason_code"] == "route_hazard"
        assert paused["itinerary_requires_replan"] is True
        assert paused["itinerary_progress"] == {"legs_arrived": 1, "legs_total": 2}
        head = repo.read_world(scope, world_id, branch_id)
        interrupted_journey = next(
            journey for journey in head.state.travel_journeys
            if journey.route_id == risky.route_id
        )
        assert interrupted_journey.status == "interrupted"
        assert interrupted_journey.departed_tick < interrupted_journey.resolved_tick < interrupted_journey.arrival_tick
        assert head.state.current_tick == interrupted_journey.resolved_tick
        assert head.state.entities[actor].location_id == waypoint
        assert client.post(job_path + "/control", json={"action": "resume"}).status_code == 409
        assert client.get(job_path).raise_for_status().json() == paused
        assert repo.read_world(scope, world_id, branch_id).state_hash == head.state_hash

        replacement = client.post(
            endpoint,
            headers={"Idempotency-Key": "detour"},
            json=body | {
                "expected_state_version": str(head.branch.state_version), "route_ids": [str(detour.route_id)],
            },
        ).raise_for_status().json()
        lease = repo.claim_job("travel-regression-detour")
        assert lease is not None and lease.job_id == UUID(replacement["job_id"])
        for _ in range(4):
            resolution = jobs.advance(scope, lease)
            if resolution.outcome == "completed":
                break
        assert resolution.outcome == "completed"
        final = repo.read_world(scope, world_id, branch_id)
        detour_journey = next(
            journey for journey in final.state.travel_journeys
            if journey.route_id == detour.route_id
        )
        assert detour_journey.status == "arrived"
        assert detour_journey.departed_tick == interrupted_journey.resolved_tick
        assert final.state.current_tick == interrupted_journey.resolved_tick + detour.travel_ticks
        assert final.state.current_tick == detour_journey.arrival_tick
        assert final.state.entities[actor].location_id == goal
        assert client.get(job_path).raise_for_status().json() == paused
        assert repo.replay(scope, world_id, branch_id).state_hash == final.state_hash


def test_worker_records_planner_failure_without_advancing_world(tmp_path):
    class FailingPlanner:
        def plan_window(self, state, plan, context):
            raise RuntimeError("fixture failure")

    database = Database(f"sqlite:///{(tmp_path / 'worker.db').as_posix()}")
    database.create_schema()
    try:
        repo = WorldRepository(database)
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        actor = uuid4()
        world = repo.create_world(scope, NewWorld(title="Worker failure", state=WorldState(
            player_id=actor, entities={actor: WorldEntity(id=actor, kind="character", name="Traveler")},
        )))
        jobs = SimulationJobs(repo, FailingPlanner())
        job = jobs.enqueue(scope, world.world.world_id, world.branch.branch_id, LongActionPlan(
            actor_id=actor, started_tick=0, target_tick=60, activity="rest",
        ), idempotency_key="fail", speed="balanced", depth="deep")
        lease = repo.claim_job("worker")
        with pytest.raises(RuntimeError, match="fixture failure"):
            jobs.advance(scope, lease)
        after = repo.read_job(scope, job.job_id)
        assert after.status.value == "failed"
        assert after.committed_through_tick == 0
        assert after.result == {"error": "SIMULATION_WINDOW_FAILED"}
        assert repo.read_world(scope, job.world_id, job.branch_id).last_seq == 0
    finally:
        database.dispose()


def test_same_tick_branch_edit_invalidates_resumed_simulation(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000"):
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        draft = ContentCatalog().build_world("long_life_cultivation", "Job QA", "Traveler")
        world = repo.create_world(scope, draft)
        world_id, branch_id = world.world.world_id, world.branch.branch_id
        jobs = SimulationJobs(repo, SimulationEngine(policy=SimulationPolicy(max_macro_step_seconds=1800)))
        job = jobs.enqueue(scope, world_id, branch_id, LongActionPlan(
            actor_id=draft.state.player_id, started_tick=0, target_tick=3600, activity="rest",
        ), idempotency_key="rest", speed="balanced", depth="deep")
        assert jobs.advance(scope, repo.claim_job("first")).outcome == "advanced"
        repo.control_job(scope, job.job_id, "pause")
        view = repo.read_world(scope, world_id, branch_id)
        repo.commit_command(scope, world_id, branch_id, CommandEnvelope(
            actor_id=draft.state.player_id, idempotency_key="rename",
            expected_state_version=view.branch.state_version, command_type="core.rename",
        ), [EntityFieldChangedEvent(entity_id=draft.state.player_id, field_name="name", value="Archivist")])
        repo.control_job(scope, job.job_id, "resume")
        failed = jobs.advance(scope, repo.claim_job("second"))
        assert failed.status.value == "failed" and failed.result["error"] == "STALE_START_VERSION"
        assert repo.read_world(scope, world_id, branch_id).state.current_tick == 1800


def test_waiting_job_only_accepts_an_offered_user_option(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        actor = uuid4()
        world = repo.create_world(scope, NewWorld(title="Decision", state=WorldState(
            player_id=actor, entities={actor: WorldEntity(id=actor, kind="character", name="Traveler")},
        )))
        job = SimulationJobs(repo).enqueue(scope, world.world.world_id, world.branch.branch_id, LongActionPlan(
            actor_id=actor, started_tick=0, target_tick=60, activity="rest",
        ), idempotency_key="decision", speed="balanced", depth="deep")
        request_id = str(uuid4())
        offered = {"request_id": request_id, "title": "择一方案", "options": [
            {"option_id": "wait", "label": "等候", "effect_summary": ""},
        ]}
        repo.finish_job(repo.claim_job("worker"), JobStatus.WAITING_INPUT, result={
            "decision_requests": [offered | {
                "decision_owner": "user", "at_tick": 0, "state_version": 0, "evidence_ids": [str(uuid4())],
            }],
        })
        path = f"/api/v1/jobs/{job.job_id}/decision"
        assert client.get(path).raise_for_status().json() == {"items": [offered]}
        assert client.post(path, json={"request_id": request_id, "option_id": "other"}).status_code == 409
        body = {"request_id": request_id, "option_id": "wait"}
        assert client.post(path, json=body).raise_for_status().json()["status"] == "queued"
        assert client.post(path, json=body).raise_for_status().json()["status"] == "queued"
        assert client.get(path).raise_for_status().json() == {"items": []}
        assert repo.read_job(scope, job.job_id).payload["pending_decision"]["source"] == "user"


def test_player_succession_resumes_after_zero_tick_commit(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        actor, leader, organization = uuid4(), uuid4(), uuid4()
        world = repo.create_world(scope, NewWorld(title="Succession decision", state=WorldState(
            player_id=actor, entities={
                actor: WorldEntity(id=actor, kind="character", name="Player"),
                leader: WorldEntity(id=leader, kind="character", name="Leader"),
                organization: WorldEntity(id=organization, kind="organization", name="Council", components={
                    "governance": {"leader_id": str(leader), "members": [str(actor), str(leader)],
                                   "succession_policy": "election"},
                }),
            },
        )))
        world_id, branch_id = world.world.world_id, world.branch.branch_id
        death = EntityFieldChangedEvent(entity_id=leader, field_name="status", value="dead")
        repo.commit_command(scope, world_id, branch_id, CommandEnvelope(
            actor_id=actor, idempotency_key="death", expected_state_version=0, command_type="fixture.death",
        ), [death])
        jobs = SimulationJobs(repo)
        job = jobs.enqueue(scope, world_id, branch_id, LongActionPlan(
            actor_id=actor, started_tick=0, target_tick=60, activity="wait",
        ), idempotency_key="succession", speed="balanced", depth="deep")
        waiting = jobs.advance(scope, repo.claim_job("first"))
        assert waiting.outcome == "decision_required"
        path = f"/api/v1/jobs/{job.job_id}/decision"
        offered, = client.get(path).raise_for_status().json()["items"]
        selected = next(option for option in offered["options"] if option["option_id"] != "leave_vacant")
        body = {"request_id": offered["request_id"], "option_id": selected["option_id"]}
        assert client.post(path, json=body).raise_for_status().json()["status"] == "queued"
        lease = repo.claim_job("second")
        result = jobs.advance(scope, lease)
        assert result.outcome == "advanced" and result.next_tick == 0
        event, = result.events
        assert event.source_death_event_id == death.event_id
        assert client.post(path, json=body).raise_for_status().json()["status"] == "running"
        assert jobs.advance(scope, lease).outcome == "completed"
        after = repo.read_world(scope, world_id, branch_id)
        assert after.state.entities[organization].components["governance"]["leader_id"] == str(actor)
        assert after.state.current_tick == 60
        assert repo.replay(scope, world_id, branch_id).state_hash == after.state_hash


def test_npc_expiry_continues_leased_job_to_player_succession_gate(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        actor, leader, organization = uuid4(), uuid4(), uuid4()
        world = repo.create_world(scope, NewWorld(title="Finite leadership", state=WorldState(
            player_id=actor, entities={
                actor: WorldEntity(id=actor, kind="character", name="Player"),
                leader: WorldEntity(id=leader, kind="character", name="Leader", components={
                    "life": {"birth_tick": 0, "lifespan_ticks": 60},
                }),
                organization: WorldEntity(id=organization, kind="organization", name="Council", components={
                    "governance": {"leader_id": str(leader), "members": [str(actor), str(leader)],
                                   "succession_policy": "election"},
                }),
            },
        )))
        world_id, branch_id = world.world.world_id, world.branch.branch_id
        jobs = SimulationJobs(repo)
        job = jobs.enqueue(scope, world_id, branch_id, LongActionPlan(
            actor_id=actor, started_tick=0, target_tick=120, activity="wait",
        ), idempotency_key="finite-leadership", speed="balanced", depth="deep")
        lease = repo.claim_job("first")
        expiry = jobs.advance(scope, lease)
        assert expiry.outcome == "advanced" and expiry.next_tick == 60
        death = next(event for event in expiry.events if event.type == "core.life_ended.v1")
        assert death.entity_id == leader
        waiting = jobs.advance(scope, lease)
        assert waiting.outcome == "decision_required"
        path = f"/api/v1/jobs/{job.job_id}/decision"
        offered, = client.get(path).raise_for_status().json()["items"]
        body = {"request_id": offered["request_id"], "option_id": "leave_vacant"}
        client.post(path, json=body).raise_for_status()
        lease = repo.claim_job("second")
        succession = jobs.advance(scope, lease)
        event, = succession.events
        assert event.source_death_event_id == death.event_id and succession.next_tick == 60
        assert jobs.advance(scope, lease).outcome == "completed"
        after = repo.read_world(scope, world_id, branch_id)
        assert after.state.current_tick == 120 and after.state.entities[leader].status == "dead"
        assert repo.replay(scope, world_id, branch_id).state_hash == after.state_hash


def test_player_lifespan_ends_job_at_committed_tick(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        actor = uuid4()
        world = repo.create_world(scope, NewWorld(title="Finite lifespan", state=WorldState(
            player_id=actor, entities={actor: WorldEntity(
                id=actor, kind="character", name="Traveler",
                components={"life": {"birth_tick": 0, "lifespan_ticks": 60}},
            )},
        )))
        jobs = SimulationJobs(repo)
        job = jobs.enqueue(scope, world.world.world_id, world.branch.branch_id, LongActionPlan(
            actor_id=actor, started_tick=0, target_tick=120, activity="wait",
        ), idempotency_key="finite", speed="balanced", depth="deep")
        outcome = jobs.advance(scope, repo.claim_job("lifespan"))
        assert outcome.next_tick == 60
        job_path = f"/api/v1/jobs/{job.job_id}"
        public = client.get(job_path).raise_for_status().json()
        assert (public["status"], public["reason_code"], public["committed_through_tick"]) == (
            "failed", "actor_lifespan_expired", "60",
        )
        assert client.post(job_path + "/control", json={"action": "resume"}).status_code == 409
        enqueue_path = f"/api/v1/worlds/{world.world.world_id}/long-actions"
        response = client.post(enqueue_path, headers={"Idempotency-Key": "after-death"}, json={
            "branch_id": str(world.branch.branch_id), "expected_state_version": "1",
            "duration_seconds": 60, "activity": "wait",
        })
        assert response.status_code == 409


def test_study_long_action_api_validates_skill_matches_idempotency_and_commits_training(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repository = app.state.worlds.repository
        draft = ContentCatalog().build_world("long_life_cultivation", "Study API", "Scholar")
        actor = draft.state.player_id
        assert actor is not None
        world = repository.create_world(scope, draft)
        world_id, branch_id = world.world.world_id, world.branch.branch_id
        before = repository.read_world(scope, world_id, branch_id)
        skill_id = "skill.local_knowledge"
        original_skill = before.state.entities[actor].components["skills"]["skills"][skill_id]
        path = f"/api/v1/worlds/{world_id}/long-actions"
        headers = {"Idempotency-Key": "study-local-knowledge"}
        body = {
            "branch_id": str(branch_id),
            "expected_state_version": str(before.branch.state_version),
            "duration_seconds": 300,
            "activity": "study",
            "skill_id": skill_id,
        }

        accepted = client.post(path, headers=headers, json=body)
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        assert repository.read_job(scope, UUID(job_id)).payload["plan"]["skill_id"] == skill_id

        retried = client.post(path, headers=headers, json=body)
        assert retried.status_code == 202 and retried.json()["job_id"] == job_id
        mismatched = client.post(path, headers=headers, json={**body, "skill_id": "skill.care"})
        assert mismatched.status_code == 409

        unheld = client.post(
            path,
            headers={"Idempotency-Key": "study-unheld-skill"},
            json={**body, "skill_id": "skill.not_registered"},
        )
        assert unheld.status_code == 422
        assert unheld.json()["error"]["code"] == "SKILL_NOT_REGISTERED"

        lease = repository.claim_job("study-api-worker", job_type="simulation.long_action")
        assert lease is not None and str(lease.job_id) == job_id
        outcome = SimulationJobs(repository).advance(scope, lease)
        assert outcome.outcome == "completed"
        after = repository.read_world(scope, world_id, branch_id)
        trained_skill = after.state.entities[actor].components["skills"]["skills"][skill_id]
        assert after.state.current_tick == 300
        assert trained_skill["trained_ticks"] == original_skill["trained_ticks"] + 300
        assert trained_skill["experience"] == original_skill["experience"] + 1
        assert repository.replay(scope, world_id, branch_id).state_hash == after.state_hash


def test_study_counts_training_before_world_boundaries_without_duplicate_experience(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    scope, repository = local_identity(tmp_path), app.state.worlds.repository
    actor, first_npc, second_npc = uuid4(), uuid4(), uuid4()
    skill_id = "skill.local_knowledge"
    created = repository.create_world(scope, NewWorld(title="Interrupted study", state=WorldState(
        player_id=actor,
        entities={
            actor: WorldEntity(id=actor, kind="character", components={"skills": {"skills": {skill_id: {"level": "1"}}}}),
            first_npc: WorldEntity(id=first_npc, kind="character", components={"life": {"birth_tick": 0, "lifespan_ticks": 11}}),
            second_npc: WorldEntity(id=second_npc, kind="character", components={"life": {"birth_tick": 0, "lifespan_ticks": 302}}),
        },
    )))
    world, branch = created.world.world_id, created.branch.branch_id
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        accepted = client.post(f"/api/v1/worlds/{world}/long-actions", headers={"Idempotency-Key": "study-through-npc-events"}, json={
            "branch_id": str(branch), "expected_state_version": "0",
            "duration_seconds": 900, "activity": "study", "skill_id": skill_id,
        }).raise_for_status()
        job_id = UUID(accepted.json()["job_id"])
        lease = repository.claim_job("study-through-npc-events")
        assert lease and lease.job_id == job_id
        for tick, experience in ((11, 0), (302, 1), (900, 3)):
            assert SimulationJobs(repository).advance(scope, lease).next_tick == tick
            after = repository.read_world(scope, world, branch)
            trained = after.state.entities[actor].components["skills"]["skills"][skill_id]
            assert (trained["trained_ticks"], trained["experience"]) == (tick, experience)
        assert repository.read_job(scope, job_id).status == JobStatus.SUCCEEDED
        assert repository.replay(scope, world, branch, from_genesis=True).state_hash == after.state_hash


def test_quiet_calendar_year_batches_wait_and_preserves_sparse_world_boundaries(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    scope, repository = local_identity(tmp_path), app.state.worlds.repository
    actor, resident = uuid4(), uuid4()
    day_seconds, year_days = 108_000, 360
    expiry = 84 * day_seconds + 17
    deadline = day_seconds * year_days
    created = repository.create_world(scope, NewWorld(title="Quiet year", state=WorldState(
        player_id=actor, calendar={"day_seconds": day_seconds, "year_days": year_days},
        entities={
            actor: WorldEntity(id=actor, kind="character"),
            resident: WorldEntity(id=resident, kind="character", components={"life": {
                "birth_tick": 0, "lifespan_ticks": expiry,
            }}),
        },
    )))
    world, branch = created.world.world_id, created.branch.branch_id
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        result = client.post(f"/api/v1/worlds/{world}/long-actions", headers={"Idempotency-Key": "quiet-year"}, json={
            "branch_id": str(branch), "expected_state_version": "0", "duration_seconds": deadline, "activity": "wait",
        }).raise_for_status().json()
        job_id = UUID(result["job_id"])
        lease = repository.claim_job("quiet-calendar-year")
        assert lease and lease.job_id == job_id
        jobs = SimulationJobs(repository)
        for _ in range(16):
            jobs.advance(scope, lease)
            status = repository.read_job(scope, job_id).status
            if status == JobStatus.SUCCEEDED:
                break
            assert status == JobStatus.RUNNING
        else:
            pytest.fail("The quiet calendar year exceeded its expected window budget")
        events = repository.list_events(scope, world, branch).events
        ticks = [0, *(event.tick for event in events if event.type == "core.time_advanced.v1")]
        assert ticks[-1] == deadline and expiry in ticks
        assert 12 <= len(ticks) - 1 <= 14
        assert all(0 < end - start <= 30 * day_seconds for start, end in zip(ticks, ticks[1:]))
        death, = [event for event in events if event.type == "core.life_ended.v1"]
        assert (death.tick, death.payload["entity_id"]) == (expiry, str(resident))
        final = repository.read_world(scope, world, branch)
        assert repository.replay(scope, world, branch, from_genesis=True).state_hash == final.state_hash


@pytest.mark.parametrize("world_advance", [False, True])
def test_background_deprivation_job_resolves_at_exact_tick_and_continues(tmp_path, world_advance):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        actor, neighbor, council = uuid4(), uuid4(), uuid4()
        state = WorldState.model_validate({
            "calendar": {"day_seconds": 60},
            "player_id": actor,
            "entities": {
                actor: {"id": actor, "kind": "character", "name": "Traveler"},
                neighbor: {"id": neighbor, "kind": "character", "name": "Neighbor", "components": {
                    "health": {"current": 1, "maximum": 10}, "needs": {"food": {"value": 0}},
                }},
                council: {"id": council, "kind": "organization", "name": "Council", "components": {
                    "governance": {"members": [str(actor), str(neighbor)], "leader_id": str(neighbor),
                                   "succession_policy": "election"},
                }},
            },
        })
        world = repo.create_world(scope, NewWorld(title="Food shortage", state=state))
        wid, bid = world.world.world_id, world.branch.branch_id
        jobs = SimulationJobs(repo)
        target = 60 if world_advance else 120
        plan = (WorldAdvancePlan(started_tick=0, target_tick=target) if world_advance else
                LongActionPlan(actor_id=actor, started_tick=0, target_tick=target, activity="wait"))
        job = jobs.enqueue(scope, wid, bid, plan, idempotency_key="food-shortage", speed="balanced", depth="deep")
        lease = repo.claim_job("food-shortage-worker")
        first = jobs.advance(scope, lease)
        assert first.outcome == "advanced" and first.next_tick == 60
        resolved = jobs.advance(scope, lease)
        assert resolved.outcome == "advanced" and resolved.next_tick == 60
        death, = resolved.events
        assert death.type == "core.background_deprivation_resolved.v1"
        assert repo.find_death_events(scope, wid, bid, {neighbor}) == {neighbor: death.event_id}
        assert jobs.advance(scope, lease).outcome == "decision_required"
        path = f"/api/v1/jobs/{job.job_id}/decision"
        decision, = client.get(path).raise_for_status().json()["items"]
        option = next(option for option in decision["options"] if option["option_id"] != "leave_vacant")
        client.post(path, json={"request_id": decision["request_id"],
                                "option_id": option["option_id"]}).raise_for_status()
        lease = repo.claim_job("succession-worker")
        succession = jobs.advance(scope, lease)
        assert succession.next_tick == 60
        assert succession.events[0].source_death_event_id == death.event_id
        assert jobs.advance(scope, lease).outcome == "completed"
        after = repo.read_world(scope, wid, bid)
        assert after.state.current_tick == target and after.state.entities[neighbor].status == "dead"
        assert after.state.entities[council].components["governance"]["leader_id"] == str(actor)
        assert repo.read_job(scope, job.job_id).status is JobStatus.SUCCEEDED
        assert repo.replay(scope, wid, bid).state_hash == after.state_hash
        backups = BackupApplication(repo)
        archive = backups.validate_archive(backups.export_world(scope, wid))
        assert archive.summary()["state_hashes"][str(bid)] == after.state_hash
        restored_db = Database(f"sqlite:///{(tmp_path / 'restored.db').as_posix()}")
        try:
            restored_db.create_schema()
            restored = WorldRepository(restored_db)
            WorldBackupImporter(restored).import_verified_backup(scope, archive)
            assert restored.replay(scope, wid, bid).state_hash == after.state_hash
            assert restored.find_death_events(scope, wid, bid, {neighbor}) == {neighbor: death.event_id}
        finally:
            restored_db.dispose()


def test_background_drinking_commits_stock_and_need_together(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000"):
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        actor, npc, holder, sink, lot = (uuid4() for _ in range(5))
        water = "cultivation.spirit_water"
        state = WorldState.model_validate({
            "player_id": actor,
            "metadata": {"resource_roles": {"water_resource_id": water}},
            "entities": {
                actor: {"id": actor, "kind": "character", "name": "Traveler"},
                npc: {"id": npc, "kind": "character", "name": "Neighbor", "components": {
                    "needs": {"water": {"value": 20}},
                    "inventory": {"lots": [{"lot_id": lot, "account_id": holder,
                                            "resource_id": water, "quantity": "2"}]},
                }},
            },
            "resources": {
                holder: {"resource_id": water, "balance": "2", "owner_id": npc},
                sink: {"resource_id": water, "balance": "0", "account_type": "consumption_sink"},
            },
        })
        world = repo.create_world(scope, NewWorld(title="Background drinking", state=state))
        wid, bid = world.world.world_id, world.branch.branch_id
        command = CommandEnvelope(actor_id=actor, idempotency_key="drink", expected_state_version=0,
                                  command_type="simulation.window")
        context = RuleContext(world_id=wid, branch_id=bid, command_id=command.command_id,
                              actor_id=actor, ruleset_version=world.world.ruleset_version)
        resolution = RuleEngine().evaluate_world_action(state, {"kind": "wait", "duration_ticks": 60},
            context, [{"entity_id": npc, "action": "drink", "resource_id": water, "quantity": "1"}])
        assert resolution.outcome.status == "succeeded"
        assert resolution.events[1].expected_agents[0].action == "drink"
        result = repo.commit_command(scope, wid, bid, command, resolution.events)
        after = repo.read_world(scope, wid, bid)
        assert after.state.resources[holder].balance == after.state.resources[sink].balance == 1
        assert after.state.entities[npc].components["inventory"]["lots"][0]["quantity"] == "1"
        assert after.state.entities[npc].components["needs"]["water"]["value"] == 30
        assert repo.replay(scope, wid, bid).state_hash == result.state_hash


def test_recurring_production_quota_rolls_through_leased_windows(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000"):
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        actor, place, account, schedule, resident = (uuid4() for _ in range(5))
        state = WorldState.model_validate({
            "player_id": actor,
            "entities": {
                actor: {"id": actor, "kind": "character", "name": "Traveler"},
                resident: {"id": resident, "kind": "character", "name": "Resident",
                           "components": {"life": {"birth_tick": 0, "lifespan_ticks": 10}}},
                place: {"id": place, "kind": "place", "name": "Farm", "components": {"production": {
                    "quotas": [{"quota_id": "grain", "resource_id": "core.grain",
                                "source_account_id": str(account), "period_ticks": 10,
                                "per_period": "2", "maximum_renewal": "3", "base_credit_limit": "1"}],
                }}},
            },
            "resources": {account: {"resource_id": "core.grain", "balance": "0", "owner_id": place,
                                     "account_type": "production_source", "credit_limit": "1"}},
            "scheduled_events": [{"id": schedule, "due_tick": 10, "phase": "environment",
                                  "handler_id": "production.quota_renewal", "handler_version": "1.0.0",
                                  "repeat_ticks": 10, "max_runs": 3,
                                  "payload": {"producer_id": str(place), "quota_id": "grain"}}],
        })
        world = repo.create_world(scope, NewWorld(title="Recurring quota", state=state))
        world_id, branch_id = world.world.world_id, world.branch.branch_id
        jobs = SimulationJobs(repo)
        job = jobs.enqueue(scope, world_id, branch_id, LongActionPlan(
            actor_id=actor, started_tick=0, target_tick=40, activity="wait",
        ), idempotency_key="quota", speed="balanced", depth="deep")
        lease = repo.claim_job("quota-worker")
        clock = []
        for _ in range(8):
            outcome = jobs.advance(scope, lease)
            clock.append(outcome.next_tick)
        assert clock == [10, 10, 10, 20, 20, 30, 30, 40]
        assert repo.read_job(scope, job.job_id).status is JobStatus.SUCCEEDED
        final = repo.read_world(scope, world_id, branch_id)
        assert final.state.resources[account].credit_limit == 4
        assert final.state.scheduled_events[0].status == "completed"
        assert final.state.scheduled_events[0].completed_runs == 3
        at_boundary = [event.type for event in repo.list_events(scope, world_id, branch_id).events
                       if event.tick == 10]
        assert at_boundary.index("core.production_quota_renewed.v1") < at_boundary.index("core.life_ended.v1")
        assert final.state.entities[resident].status == "dead"
        assert repo.replay(scope, world_id, branch_id).state_hash == final.state_hash
        backups = BackupApplication(repo)
        archive = backups.validate_archive(backups.export_world(scope, world_id))
        assert archive.summary()["state_hashes"][str(branch_id)] == final.state_hash
