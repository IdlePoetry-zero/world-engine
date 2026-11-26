"""Real authored sample trips through HTTP world creation and advancement."""

from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.adapters.models import RoleTask
from world_engine.application.operations import ModelOperations
from world_engine.domain.components import DailyRoutineComponent, PlaceComponent, RoutineMobilityComponent
from world_engine.domain.models import JobStatus
from world_engine.runtime import build_application, local_identity


@pytest.mark.parametrize(("pack_id", "role", "uses_fallback"), [
    ("low_magic_feudal", "bridge reeve", False),
    ("modern_city", "night rail dispatcher", False),
    ("long_life_cultivation", "sect archivist", True),
])
def test_opt_in_background_commits_routine_trip_and_return(tmp_path, monkeypatch, pack_id, role, uses_fallback):
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

        def advance(seconds, key, expected_version, *, branch=branch_id):
            response = client.post(f"/api/v1/worlds/{world_id}/world-advance", headers={"Idempotency-Key": key}, json={
                "branch_id": str(branch), "duration_seconds": seconds,
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
            return repository.read_world(scope, world_id, branch)

        arrived = advance(visit.start_second, "visit", view.branch.state_version)
        (journey,) = arrived.state.travel_journeys
        assert journey.actor_id == actor.id and journey.route_id == outbound.route_id
        assert journey.status == "arrived" and journey.resolved_tick == visit.start_second
        assert journey.departed_tick == visit.start_second - outbound.travel_ticks
        assert arrived.state.entities[actor.id].location_id == destination
        visible = client.get(view_path).raise_for_status().json()["state"]["entities"]
        assert (str(actor.id) in visible) == (not uses_fallback)

        before_return = arrived
        if pack_id == "modern_city":
            # Two players could use the same window differently. Only the one who
            # chose to meet the dispatcher should acquire their paired memory.
            path = f"/api/v1/worlds/{world_id}"
            quiet = client.post(f"{path}/branches", headers={"Idempotency-Key": "let-the-visit-pass"}, json={
                "branch_id": str(branch_id), "sequence": str(arrived.last_seq), "label": "继续自己的安排",
            }).raise_for_status().json()
            quiet_branch = UUID(quiet["branch_id"])
            line = f"我向{actor.name}说：“昨晚换乘时，哪些记录还要查？”"
            reply = "我还在核对末班到站和实际发车记录。"

            async def generate(_self, _scope, request):
                if request.role_task is RoleTask.INTENT:
                    return SimpleNamespace(normalized_output={"target_id": str(actor.id), "clarification": ""})
                assert request.role_task is RoleTask.SOCIAL_ADJUDICATION
                return SimpleNamespace(normalized_output={"utterance": reply})

            monkeypatch.setattr(ModelOperations, "generate", generate)
            chosen = client.post(f"{path}/messages", json={
                "world_id": str(world_id), "branch_id": str(branch_id), "session_id": str(uuid4()),
                "request_id": str(uuid4()), "actor_id": str(arrived.state.player_id), "mode": "roleplay",
                "expected_state_version": str(arrived.branch.state_version), "text": line,
            }).raise_for_status().json()
            assert chosen["world_effect"] and chosen["references"] == [str(actor.id)]
            before_return = repository.read_world(scope, world_id, branch_id)
            assert before_return.state.current_tick == arrived.state.current_tick
            quiet_final = advance(rest.start_second - visit.start_second, "quiet-return",
                                  arrived.branch.state_version, branch=quiet_branch)
            assert quiet_final.state.entities[actor.id].location_id == home
            assert not any(record.claim == reply for record in quiet_final.state.knowledge)
            assert repository.replay(scope, world_id, quiet_branch, from_genesis=True).state_hash == quiet_final.state_hash

        returned = advance(rest.start_second - visit.start_second, "return", before_return.branch.state_version)
        assert returned.state.entities[actor.id].location_id == home
        assert len(returned.state.travel_journeys) == 2
        if pack_id == "modern_city":
            assert any(record.claim == reply and record.holder_id == returned.state.player_id
                       for record in returned.state.knowledge)
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == returned.state_hash
