"""Real authored sample trips through HTTP world creation and advancement."""

from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.components import DailyRoutineComponent, PlaceComponent, RoutineMobilityComponent
from world_engine.domain.models import JobStatus
from world_engine.runtime import build_application, local_identity


@pytest.mark.parametrize(("pack_id", "role", "uses_fallback"), [
    ("low_magic_feudal", "bridge reeve", False),
    ("modern_city", "night rail dispatcher", False),
    ("long_life_cultivation", "sect archivist", True),
])
def test_opt_in_background_commits_routine_trip_and_return(tmp_path, pack_id, role, uses_fallback):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repository = app.state.worlds.repository
        world = client.post("/api/v1/worlds", headers={"Idempotency-Key": "routine-opening"}, json={
            "pack_id": pack_id, "pack_version": "2.12.0", "title": "Routine sample",
            "player_name": "Traveler", "seed": 20261123,
        }).raise_for_status().json()
        world_id = UUID(world["world_id"])
        branch_id = UUID(world["active_branch_id"])
        view_path = f"/api/v1/worlds/{world_id}/view"
        view = repository.read_world(scope, world_id, branch_id)
        (actor,) = (entity for entity in view.state.entities.values() if "routine_mobility" in entity.components)
        assert actor.components["public_role"] == role
        assert str(actor.id) not in client.get(view_path).raise_for_status().json()["state"]["entities"]
        routine = DailyRoutineComponent.model_validate(actor.components["daily_routine"])
        mobility = RoutineMobilityComponent.model_validate(actor.components["routine_mobility"])
        visit, rest = routine.windows
        (fallback,) = mobility.alternatives[visit.window_id]
        destination = fallback if uses_fallback else visit.place_id
        home = actor.location_id
        assert rest.place_id == home
        home_routes = PlaceComponent.model_validate(view.state.entities[home].components["place"]).routes
        (outbound,) = (route for route in home_routes if route.to_place_id == destination)

        def advance(seconds, key, expected_version):
            response = client.post(f"/api/v1/worlds/{world_id}/world-advance", headers={"Idempotency-Key": key}, json={
                "branch_id": str(branch_id), "duration_seconds": seconds,
                "expected_state_version": str(expected_version),
            }).raise_for_status().json()
            job_id = UUID(response["job_id"])
            lease = repository.claim_job("authored-routine-test")
            assert lease is not None and lease.job_id == job_id
            jobs = SimulationJobs(repository)
            for _ in range(12):
                jobs.advance(scope, lease)
                job = repository.read_job(scope, job_id)
                if job.status != JobStatus.RUNNING:
                    break
            assert job.status == JobStatus.SUCCEEDED, job.result
            return repository.read_world(scope, world_id, branch_id)

        arrived = advance(visit.start_second, "visit", view.branch.state_version)
        (journey,) = arrived.state.travel_journeys
        assert journey.actor_id == actor.id and journey.route_id == outbound.route_id
        assert journey.status == "arrived" and journey.resolved_tick == visit.start_second
        assert journey.departed_tick == visit.start_second - outbound.travel_ticks
        assert arrived.state.entities[actor.id].location_id == destination
        visible = client.get(view_path).raise_for_status().json()["state"]["entities"]
        assert (str(actor.id) in visible) == (not uses_fallback)

        returned = advance(rest.start_second - visit.start_second, "return", arrived.branch.state_version)
        assert returned.state.entities[actor.id].location_id == home
        assert len(returned.state.travel_journeys) == 2
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == returned.state_hash
