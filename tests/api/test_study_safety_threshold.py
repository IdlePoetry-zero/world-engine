from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from world_engine.api.contracts import LongActionRequest
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.gameplay_events import ComponentReplacedEvent
from world_engine.domain.models import CommandEnvelope, NewWorld, WorldEntity, WorldState
from world_engine.runtime import build_application, local_identity
from world_engine.simulation import LongActionPlan


def _world(repo, scope, *, safety_value: int | None = 80, include_needs: bool = True):
    actor_id = UUID(int=121)
    components = {"skills": {"skills": {"skill.care": {"level": "10"}}}}
    if include_needs:
        components["needs"] = {} if safety_value is None else {"safety": {"value": safety_value}}
    state = WorldState(
        calendar={"day_seconds": 60},
        player_id=actor_id,
        entities={actor_id: WorldEntity(
            id=actor_id,
            kind="character",
            name="Scholar",
            components=components,
        )},
    )
    created = repo.create_world(scope, NewWorld(title="Study threshold", state=state))
    return created, actor_id


def _study_body(branch_id: UUID, *, threshold: int | None = None) -> dict[str, object]:
    body: dict[str, object] = {
        "branch_id": str(branch_id),
        "expected_state_version": "0",
        "duration_seconds": 600,
        "activity": "study",
        "skill_id": "skill.care",
    }
    if threshold is not None:
        body["study_stop_safety_at_or_below"] = threshold
    return body


def test_long_action_study_safety_threshold_contract_is_strict_and_study_only() -> None:
    base = {
        "branch_id": UUID(int=122),
        "expected_state_version": "0",
        "duration_seconds": 60,
    }
    default = LongActionRequest.model_validate({
        **base,
        "activity": "study",
        "skill_id": "skill.care",
    })
    assert default.study_stop_safety_at_or_below is None

    for value in (True, 10.0, "10", -1, 101):
        with pytest.raises(ValidationError):
            LongActionRequest.model_validate({
                **base,
                "activity": "study",
                "skill_id": "skill.care",
                "study_stop_safety_at_or_below": value,
            })

    with pytest.raises(ValidationError):
        LongActionRequest.model_validate({
            **base,
            "activity": "rest",
            "study_stop_safety_at_or_below": 50,
        })


def test_explicit_threshold_is_saved_projected_and_part_of_idempotency(tmp_path) -> None:
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        created, actor_id = _world(repo, scope, safety_value=0)
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        path = f"/api/v1/worlds/{world_id}/long-actions"
        headers = {"Idempotency-Key": "study-safety-zero"}
        body = _study_body(branch_id, threshold=0)

        accepted = client.post(path, json=body, headers=headers)
        assert accepted.status_code == 202
        job_id = UUID(accepted.json()["job_id"])
        job = repo.read_job(scope, job_id)
        assert job.payload["submitted_by"] == str(scope.user_id)
        plan = LongActionPlan.model_validate(job.payload["plan"])
        assert len(plan.stop_conditions) == 1
        condition = plan.stop_conditions[0]
        assert (condition.kind, condition.subject_id, condition.need, condition.threshold) == (
            "need_at_or_below", actor_id, "safety", 0,
        )

        status_path = f"/api/v1/jobs/{job_id}"
        projected = client.get(status_path).raise_for_status().json()
        assert projected["study_stop_safety_at_or_below"] == 0
        assert "stop_conditions" not in projected

        retry = client.post(path, json=body, headers=headers)
        assert retry.status_code == 202 and retry.json()["job_id"] == str(job_id)
        changed = client.post(path, json=_study_body(branch_id, threshold=1), headers=headers)
        assert changed.status_code == 409


def test_safety_threshold_pauses_worker_and_is_retained_on_resume(tmp_path) -> None:
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository
        created, actor_id = _world(repo, scope, safety_value=0)
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        accepted = client.post(
            f"/api/v1/worlds/{world_id}/long-actions",
            json=_study_body(branch_id, threshold=0),
            headers={"Idempotency-Key": "study-safety-pause"},
        )
        assert accepted.status_code == 202
        job_id = UUID(accepted.json()["job_id"])

        lease = repo.claim_job("study-safety-worker", job_type="simulation.long_action")
        assert lease is not None and lease.job_id == job_id
        resolution = SimulationJobs(repo).advance(scope, lease)
        assert resolution.outcome == "stopped"
        paused = repo.read_job(scope, job_id)
        assert paused.status.value == "paused"
        assert paused.committed_through_tick == 0
        assert repo.read_world(scope, world_id, branch_id).state.current_tick == 0
        assert paused.payload["plan"]["stop_conditions"][0]["threshold"] == 0

        # Model an independent, committed change that puts safety above the
        # threshold. The job must then resume from this exact saved checkpoint.
        checkpoint = repo.read_world(scope, world_id, branch_id)
        needs = checkpoint.state.entities[actor_id].components["needs"]
        replacement_needs = {
            **needs,
            "revision": needs.get("revision", 1) + 1,
            "safety": {
                **needs["safety"],
                "value": 80,
                "revision": needs["safety"].get("revision", 1) + 1,
            },
        }
        repo.commit_command(
            scope,
            world_id,
            branch_id,
            CommandEnvelope(
                actor_id=actor_id,
                idempotency_key="test-safety-recovered",
                expected_state_version=checkpoint.branch.state_version,
                command_type="test.safety_recovered",
            ),
            [ComponentReplacedEvent(
                entity_id=actor_id,
                component_name="needs",
                expected_revision=needs.get("revision", 1),
                replacement=replacement_needs,
            )],
        )

        resumed = client.post(f"/api/v1/jobs/{job_id}/control", json={"action": "resume"})
        assert resumed.status_code == 200 and resumed.json()["status"] == "queued"
        replacement = repo.claim_job("study-safety-resumed", job_type="simulation.long_action")
        assert replacement is not None and replacement.job_id == job_id
        resumed_jobs = SimulationJobs(repo)
        completed = resumed_jobs.advance(scope, replacement)
        for _ in range(20):
            if completed.outcome == "completed":
                break
            assert completed.outcome == "advanced"
            completed = resumed_jobs.advance(scope, replacement)
        assert completed.outcome == "completed"
        after_resume = repo.read_job(scope, job_id)
        assert after_resume.status.value == "succeeded"
        assert after_resume.committed_through_tick == 600
        assert after_resume.payload["plan"]["stop_conditions"][0]["threshold"] == 0
        assert repo.read_world(scope, world_id, branch_id).state.current_tick == 600


def test_missing_safety_metric_is_rejected_and_omitted_or_other_activity_stays_absent(tmp_path) -> None:
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        scope = local_identity(tmp_path)
        repo = app.state.worlds.repository

        for index, include_needs in enumerate((False, True)):
            missing, _actor_id = _world(repo, scope, safety_value=None, include_needs=include_needs)
            missing_path = f"/api/v1/worlds/{missing.world.world_id}/long-actions"
            safety_error = client.post(
                missing_path,
                json=_study_body(missing.branch.branch_id, threshold=50),
                headers={"Idempotency-Key": f"study-safety-missing-{index}"},
            )
            assert safety_error.status_code == 422
            assert safety_error.json()["error"]["code"] == "STUDY_SAFETY_UNAVAILABLE"

        created, _actor_id = _world(repo, scope, safety_value=80)
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        study = client.post(
            f"/api/v1/worlds/{world_id}/long-actions",
            json=_study_body(branch_id),
            headers={"Idempotency-Key": "study-safety-omitted"},
        )
        assert study.status_code == 202
        study_job = UUID(study.json()["job_id"])
        study_payload = client.get(f"/api/v1/jobs/{study_job}").raise_for_status().json()
        assert "study_stop_safety_at_or_below" not in study_payload
        assert repo.read_job(scope, study_job).payload["plan"]["stop_conditions"] == []

        rest = client.post(
            f"/api/v1/worlds/{world_id}/long-actions",
            json={
                "branch_id": str(branch_id),
                "expected_state_version": "0",
                "duration_seconds": 60,
                "activity": "rest",
            },
            headers={"Idempotency-Key": "rest-no-study-safety"},
        )
        assert rest.status_code == 202
        rest_job = UUID(rest.json()["job_id"])
        rest_payload = client.get(f"/api/v1/jobs/{rest_job}").raise_for_status().json()
        assert "study_stop_safety_at_or_below" not in rest_payload
        invalid_rest = client.post(
            f"/api/v1/worlds/{world_id}/long-actions",
            json={
                "branch_id": str(branch_id),
                "expected_state_version": "0",
                "duration_seconds": 60,
                "activity": "rest",
                "study_stop_safety_at_or_below": 50,
            },
            headers={"Idempotency-Key": "rest-with-study-safety"},
        )
        assert invalid_rest.status_code == 422
