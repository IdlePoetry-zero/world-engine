"""Station arrival is a persistent first step; a transport receipt cannot settle an investigation."""

import json
from uuid import UUID, uuid5

from fastapi.testclient import TestClient
import pytest

from world_engine.application.modern_opening_goal import project_modern_opening_goal
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.visibility import project_player_state
from world_engine.domain.models import CommandEnvelope, JobStatus
from world_engine.domain.modern_opening_goal import OPENING_TITLE
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, RuleContext
from world_engine.rules.engine import RuleEngine
from world_engine.runtime import build_application, local_identity

def test_registered_opening_uses_stable_ids_after_titles_or_list_order_change():
    goal = {
        "id": "goal", "owner_id": "player", "title": "Renamed goal", "created_tick": 0,
        "status": "active", "conditions": [
            {"kind": "reach_location", "subject_id": "player", "target_id": "destination"},
        ],
    }
    entities = {
        "origin": {"name": "始发地"},
        "destination": {"name": "车站"},
        "anchor": {"location_id": "origin", "components": {
            "investigation_case": {"case_id": "case", "title": "改名后的调查"},
        }},
    }
    state = {
        "metadata": {
            "pack_id": "modern_city",
            "content_index": {"player_id": "player", "settlement_ids": ["destination", "origin"]},
            "opening_investigation": {"goal_id": "goal", "destination_id": "destination",
                                      "anchor_id": "anchor", "case_id": "case"},
        },
        "entities": entities,
        "investigation_cases": [{"case_id": "case", "anchor_id": "anchor", "owner_id": "player",
                                 "title": "改名后的调查", "status": "closed"}],
    }
    reading = project_modern_opening_goal(state, "player", goal, entities, direct_arrival_tick=42)
    assert reading["arrival_tick"] == "42"
    assert "「改名后的调查」已归档" in reading["description"]


@pytest.mark.parametrize(("seed", "entry"), [(4, "single"), (7, "itinerary"), (4, "old_direct"), (4, "direct")])
def test_modern_station_goal_survives_replay_and_forks(tmp_path, seed, entry):
    # Stable identities keep the default travel hazard rolls reproducible.
    (tmp_path / "local-identity.json").write_text(json.dumps({
        "tenant_id": str(UUID(int=410)), "user_id": str(UUID(int=411)),
    }))
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    repository = app.state.worlds.repository
    scope = local_identity(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get("/api/v1/session").raise_for_status()
        created = client.post("/api/v1/worlds", headers={"Idempotency-Key": "modern-case"}, json={
            "pack_id": "modern_city", "title": "夜班", "player_name": "林晚", "seed": seed,
        }).raise_for_status().json()
        world_id = UUID(created["world_id"])
        branch = UUID(created["active_branch_id"])
        path = f"/api/v1/worlds/{world_id}"

        def head():
            return repository.read_world(scope, world_id, branch)

        def player(branch_id=branch):
            return client.get(f"{path}/view", params={"branch_id": str(branch_id)}).raise_for_status().json()["state"]

        def goal(state):
            return next(item for item in state["goals"] if item["id"] == str(opening.id))

        def fork(version, key):
            return UUID(client.post(f"{path}/branches", headers={"Idempotency-Key": key}, json={
                "branch_id": str(branch), "sequence": str(version), "label": key,
            }).raise_for_status().json()["branch_id"])

        view = head()
        actor = view.state.player_id
        opening = next(item for item in view.state.goals if item.owner_id == actor and item.title == OPENING_TITLE)
        target = opening.conditions[0].target_id
        home = view.state.entities[actor].location_id
        assert "前往" in goal(player())["description"]
        before = fork(view.last_seq, "before-arrival")
        waypoints = [target]
        if entry == "itinerary":
            waypoints.insert(0, UUID(view.state.metadata["content_index"]["region_ids"][0]))
        legs = []
        origin = home
        for waypoint in waypoints:
            route = next(r for r in view.state.entities[origin].components["place"]["routes"]
                         if r["to_place_id"] == str(waypoint))
            legs.append(route)
            origin = waypoint
        duration = sum(int(r["travel_ticks"]) for r in legs)

        def commit_action(action, *, legacy=False):
            current = head()
            for attempt in range(100):
                # Choose an uneventful direct trip for the arrival positive control.
                key = f'{action["kind"]}:{current.branch.state_version}:{attempt}'
                command = CommandEnvelope(
                    command_id=uuid5(branch, key), actor_id=actor, idempotency_key=key,
                    command_type=f'action.{action["kind"]}', expected_state_version=current.branch.state_version,
                    payload=ACTION_INTENT_ADAPTER.validate_python(action).model_dump(mode="json"),
                )
                context = RuleContext(world_id=world_id, branch_id=branch, actor_id=actor,
                                      command_id=command.command_id, ruleset_version=current.world.ruleset_version)
                if action["kind"] != "move" or RuleEngine._roll(context, "movement.route_risk", 100).value > 12:
                    break
            resolution = RuleEngine().evaluate(current.state, action, context)
            assert resolution.outcome.status == "succeeded", resolution.outcome
            events = list(resolution.events)
            if legacy:
                # Preserve exactly the pre-feature direct movement batch and its replay semantics.
                events = [event for event in events if event.type != "core.goal_updated.v1"]
            return repository.commit_command(scope, world_id, branch, command, events)

        if entry == "direct":
            anchor = player()["available_case_anchors"][0]
            commit_action({"kind": "open_investigation_case", "anchor_id": anchor["anchor_id"]})
        if entry in {"direct", "old_direct"}:
            commit_action({"kind": "move", "destination_id": target}, legacy=entry == "old_direct")
        else:
            body = {"activity": "travel", "branch_id": str(branch), "expected_state_version": "0",
                    "duration_seconds": duration}
            if entry == "single":
                body["route_id"] = legs[0]["route_id"]
            else:
                body["route_ids"] = [leg["route_id"] for leg in legs]
            job = client.post(f"{path}/long-actions", headers={"Idempotency-Key": "route"}, json=body).raise_for_status().json()
            lease = repository.claim_job("modern-goal-test")
            assert lease.job_id == UUID(job["job_id"])
            worker = SimulationJobs(repository)
            for _ in range(20):
                worker.advance(scope, lease)
                status = repository.read_job(scope, lease.job_id).status
                if status != JobStatus.RUNNING:
                    break
                assert goal(player())["progress"] == (50 if head().state.entities[actor].location_id == target else 0)
            assert status == JobStatus.SUCCEEDED

        reached = player()
        assert reached["entities"][str(actor)]["location_id"] == str(target)
        stage = goal(reached)
        assert stage["status"] == "active" and stage["progress"] == 50
        assert stage["arrival_tick"] == str(duration)
        if entry == "direct":
            assert "独立记录" in stage["description"] and reached["case_files"][0]["status"] == "open"
        else:
            assert "建立私人案卷" in stage["description"] and not reached["case_files"]
        assert goal(player(before))["progress"] == 0
        after = fork(head().last_seq, "after-arrival")
        assert goal(player(after)) == stage
        raw = next(item for item in head().state.goals if item.id == opening.id)
        assert raw.progress == (0 if entry == "old_direct" else 50)
        if entry in {"single", "itinerary"}:
            legacy_state = head().state.model_dump(mode="json")
            old_goal = next(row for row in legacy_state["goals"] if row["id"] == str(opening.id))
            old_goal.update(progress=0, evidence_refs=[], revision=1, updated_tick=0)
            assert goal(project_player_state(legacy_state, str(actor))) == stage
        if entry in {"direct", "old_direct"}:
            # Leave and return, including more than the four direct moves in the ordinary history readback.
            region = UUID(view.state.metadata["content_index"]["region_ids"][0])
            for _ in range(3 if entry == "old_direct" else 1):
                commit_action({"kind": "move", "destination_id": region}, legacy=entry == "old_direct")
                commit_action({"kind": "move", "destination_id": target}, legacy=entry == "old_direct")
            assert goal(player()) == stage
        if entry == "direct":
            commit_action({"kind": "close_investigation_case", "case_file_id": reached["case_files"][0]["id"]})
            closed = player()
            assert closed["case_files"][0]["outcome"] == "unresolved"
            assert "已归档" in goal(closed)["description"]
            assert "已归档" not in goal(player(after))["description"]
        assert repository.replay(scope, world_id, branch, from_genesis=True).state_hash == head().state_hash

    if entry == "old_direct":
        reopened = build_application(data_directory=tmp_path, run_simulation_worker=False)
        with TestClient(reopened, base_url="http://127.0.0.1:8000") as client:
            client.get("/api/v1/session").raise_for_status()
            assert goal(player()) == stage == goal(player(after))
            assert goal(player(before))["progress"] == 0
