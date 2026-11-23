"""Exercise the published Gensokyo scenario through HTTP with deterministic model outputs."""

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from fastapi.testclient import TestClient
import pytest

from world_engine.adapters.models.contracts import RoleTask
from world_engine.application.operations import ModelOperations
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.content.gensokyo_reference import _id
from world_engine.content.gensokyo_village_life import local_aid
from world_engine.content.gensokyo_world import CHAPTERS, actor_id, build_archive
from world_engine.domain.models import JobStatus
from world_engine.domain.npc_plan_events import NPC_PLAN_CREATED_EVENT_TYPE
from world_engine.domain.travel_journey_events import TRAVEL_JOURNEY_CHANGED_EVENT_TYPE
from world_engine.runtime import _invoke_worker_model_decision, build_application, local_identity


def test_scarlet_mist_dialogue_changes_the_chapter_with_autonomous_npc_travel(tmp_path, monkeypatch):
    chapter = next(row for row in CHAPTERS if row.key == "scarlet-mist")
    contact = str(actor_id(chapter.contact))
    shrine = str(_id(f"place:{chapter.meeting}"))
    village = str(_id(f"place:{chapter.start}"))
    incident = str(_id(f"world:incident:{chapter.key}"))
    report_id = str(_id(f"world:incident:{chapter.key}:report"))
    player_words = "灵梦，我在村口看到红雾越过屋顶，想把目击情况告诉你。"
    buy_words = "我购买一份米粮。"
    npc_words = "我会去人里核对红雾最先出现的位置。"
    planning_branches = []

    async def generate(_scope, request):
        role = request.role_task
        if role == RoleTask.INTENT:
            if buy_words in request.messages[-1].content:
                output = {"action": {
                    "kind": "trade", "quantity": "1",
                    "listing_id": str(_id("world:listing:human-village:gensokyo.rice")),
                }, "clarification": ""}
            else:
                output = {"target_id": contact, "clarification": ""}
        elif role == RoleTask.SOCIAL_ADJUDICATION:
            assert player_words in request.messages[-1].content
            output = {"utterance": npc_words}
        elif role == RoleTask.NPC_DECISION:
            context = json.loads(request.messages[-1].content)
            if context["actor"]["id"] == contact:
                planning_branches.append(request.trace_context.branch_id)
                goal, = context["active_goals"]
                route = next(row for row in context["known_routes"] if row["destination_id"] == village)
                assert route["access_allowed_now"]
                output = {
                    "kind": "plan", "goal_ids": [goal["id"]], "reason": "去人里核对红雾目击情况。",
                    "steps": [{"capability_id": "world.action.move.v1", "arguments": {
                        "destination_id": village, "route_id": route["route_id"],
                    }}],
                }
            else:
                output = {"kind": "defer", "reason": "稍后再回顾当前目标。"}
        elif role == RoleTask.NPC_PLAN_REVIEW:
            output = {"verdict": "approve"}
        elif role == RoleTask.MEMORY_SUMMARY:
            output = {"outlook": "defer"}
        else:
            pytest.fail(f"Unexpected model role {role}")
        return SimpleNamespace(normalized_output=output)

    monkeypatch.setattr(ModelOperations, "generate", staticmethod(generate))
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    repository = app.state.worlds.repository
    scope = local_identity(tmp_path)
    jobs = SimulationJobs(repository)
    decision_models = SimpleNamespace(generate=generate)
    origin = "http://127.0.0.1:8000"

    with TestClient(app, base_url=origin) as client:
        client.get("/api/v1/session").raise_for_status()
        archive = build_archive()
        upload = {"Content-Type": "application/zip", "Origin": origin}
        preview = client.post("/api/v1/packs/import/preview", headers=upload, content=archive).raise_for_status().json()
        created = client.post("/api/v1/packs/import", headers=upload | {
            "Idempotency-Key": chapter.key, "X-World-Pack-Sha256": preview["archive_sha256"],
            "X-Story-Opening": chapter.key,
        }, content=archive).raise_for_status().json()
        world_id = UUID(created["world_id"])
        main = created["active_branch_id"]
        path = f"/api/v1/worlds/{world_id}"

        def view(branch, mode="player"):
            return client.get(f"{path}/view", params={"branch_id": branch, "mode": mode}).raise_for_status().json()

        def fork(parent_view, label):
            return client.post(f"{path}/branches", headers={"Idempotency-Key": label}, json={
                "branch_id": main, "sequence": parent_view["last_seq"], "label": label,
            }).raise_for_status().json()["branch_id"]

        def run_job(branch, endpoint, duration, **params):
            job = client.post(f"{path}/{endpoint}", headers={"Idempotency-Key": str(uuid4())}, json={
                "branch_id": branch, "expected_state_version": view(branch)["branch"]["state_version"],
                "duration_seconds": duration, **params,
            }).raise_for_status().json()
            job_id = UUID(job["job_id"])
            lease = repository.claim_job("gensokyo-acceptance-worker", lease_seconds=120)
            assert lease and lease.job_id == job_id and str(lease.branch_id) == branch

            async def advance():
                response = None
                for _ in range(128):
                    window = jobs.advance(scope, lease, model_decision=response)
                    response = None
                    current = repository.read_job(scope, job_id)
                    if current.status == JobStatus.SUCCEEDED:
                        return
                    if window.decision_requests:
                        decision, = window.decision_requests
                        assert decision.decision_owner == "model"
                        response = await _invoke_worker_model_decision(
                            decision_models, repository, scope, lease, current, decision,
                        )
                        if response is not None:
                            assert response.npc_planning_status != "invalid", response.npc_planning_rejection
                pytest.fail(f"Simulation job {job_id} did not reach its target")

            asyncio.run(advance())
            status = client.get(f"/api/v1/jobs/{job_id}").raise_for_status().json()
            assert status["status"] == JobStatus.SUCCEEDED.value
            return job_id

        def familiarity(state):
            return next((row["dimensions"]["familiarity"] for row in state["relations"]
                         if (row["from_id"], row["to_id"]) == (player, contact)), None)

        start = view(main)["state"]
        player = start["player_id"]
        assert start["entities"][player]["location_id"] == village
        assert contact not in start["entities"]
        assert any(row["resource_id"] == "gensokyo.rice" for row in start["market_offers"]["offers"])
        purchase = client.post(f"{path}/messages", headers={"Origin": origin}, json={
            "world_id": str(world_id), "branch_id": main, "request_id": str(uuid4()),
            "session_id": str(uuid4()), "actor_id": player, "expected_state_version": view(main)["branch"]["state_version"],
            "mode": "action", "text": buy_words,
        }).raise_for_status().json()
        assert purchase["kind"] == "action" and purchase["world_effect"] is True
        after_buy = view(main, "author")["state"]
        player_balances = {item["resource_id"]: item["balance"] for item in after_buy["resources"].values()
                           if item["owner_id"] == player}
        assert player_balances == {"gensokyo.copper": "29", "gensokyo.rice": "1"}
        shrine_name = view(main, "author")["state"]["entities"][shrine]["name"]
        route = next(row for row in start["travel_options"] if row["destination_name"] == shrine_name)
        run_job(main, "long-actions", int(route["travel_ticks"]), activity="travel", route_id=route["route_id"])

        local = view(main)
        assert local["state"]["entities"][player]["location_id"] == shrine
        assert local["state"]["entities"][contact]["location_id"] == shrine
        no_contact = fork(local, "Visit without making contact")
        baseline = view(main, "author")
        assert baseline["state_hash"] == view(no_contact, "author")["state_hash"]
        assert familiarity(baseline["state"]) is None

        reply = client.post(f"{path}/messages", headers={"Origin": origin}, json={
            "world_id": str(world_id), "branch_id": main, "request_id": str(uuid4()),
            "session_id": str(uuid4()), "actor_id": player, "expected_state_version": local["branch"]["state_version"],
            "mode": "roleplay", "text": player_words,
        }).raise_for_status().json()
        assert reply["kind"] == "dialogue" and reply["world_effect"] is True
        assert reply["references"] == [contact]
        after_dialogue = view(main, "author")
        assert familiarity(after_dialogue["state"]) == "1"
        assert any(row["holder_id"] == contact and row["claim"] == player_words
                   for row in after_dialogue["state"]["knowledge"])
        assert view(no_contact, "author")["state_hash"] == baseline["state_hash"]
        dialogue_only = fork(after_dialogue, "Talk without registering a report")
        assert view(dialogue_only, "author")["state_hash"] == after_dialogue["state_hash"]

        assert any(row["challenge_id"] == report_id for row in view(main)["state"]["local_challenges"])
        report = client.post(f"{path}/challenges/{report_id}/attempt", headers={"Idempotency-Key": "report-red-mist"}, json={
            "branch_id": main, "actor_id": player, "expected_state_version": after_dialogue["branch"]["state_version"],
        }).raise_for_status().json()
        assert report["passed"]

        incident_tick = chapter.hours * 3600
        duration = incident_tick + 3600 - int(after_dialogue["state"]["current_tick"])
        relief = local_aid(chapter.key).transfer
        for branch, outcome in ((main, "player-report"), (dialogue_only, "independent"), (no_contact, "independent")):
            job_id = run_job(branch, "world-advance", duration)
            final = view(branch, "author")
            state = final["state"]
            result = next(row for row in state["story_milestones"] if row["schedule_id"] == incident)
            assert result["outcome"] == outcome and int(result["resolved_tick"]) == incident_tick
            public = next(row for row in view(branch)["state"]["story_milestones"] if row["record_id"] == incident)
            assert public["summary"] == (chapter.joined if branch == main else chapter.independent)
            assert state["entities"][player]["location_id"] == shrine
            assert state["entities"][contact]["location_id"] == village
            followup = next(row for row in state["story_milestones"] if row["schedule_id"] == str(
                _id(f"world:incident:{chapter.key}:{outcome}:followup")
            ))
            assert followup["outcome"] == ("aid-delivered" if branch == main else "recorded")
            assert state["resources"][str(relief.from_account_id)]["balance"] == ("12" if branch == main else "18")
            assert state["resources"][str(relief.to_account_id)]["balance"] == ("6" if branch == main else "0")
            plan, = [row for row in state["npc_plans"] if row["actor_id"] == contact]
            assert plan["status"] == "completed" and len(plan["evidence_refs"]) >= 2
            assert repository.replay(scope, world_id, UUID(branch), from_genesis=True).state_hash == final["state_hash"]

            events = []
            cursor = local["last_seq"]
            while cursor is not None:
                page = client.get(f"{path}/history", params={"branch_id": branch, "after": cursor}).raise_for_status().json()
                events.extend(page["items"])
                cursor = page["next_cursor"]
            created_plan, = [row for row in events if row["type"] == NPC_PLAN_CREATED_EVENT_TYPE
                             and row["payload"]["plan"]["plan_id"] == plan["plan_id"]]
            departure, = [row for row in events if row["type"] == TRAVEL_JOURNEY_CHANGED_EVENT_TYPE
                          and row["payload"]["actor_id"] == contact and row["payload"]["operation"] == "depart"]
            assert int(created_plan["sequence"]) < int(departure["sequence"])
            assert departure["event_id"] in plan["evidence_refs"]
            departure_window = int(departure["state_version_after"]) - 1
            assert departure["command_id"] == str(uuid5(job_id, f"window:{departure_window}"))
            assert departure["payload"]["journey"]["to_place_id"] == village

        assert planning_branches == [main, dialogue_only, no_contact]
