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
from world_engine.content.gensokyo_chronology import MORIYA_SURVEY_ID, MORIYA_SURVEY_PERMIT
from world_engine.content.gensokyo_reference import _id
from world_engine.content.gensokyo_mist_reports import CHECK_ID, RUMOR_ID, SOURCES, report_id as mist_report_id
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
    player_words = "灵梦，今天只是来问好，不登记线索。"
    denial = "我没有目击北面的雾，也没有可以提交的报告。"
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
            assert any(words in request.messages[-1].content for words in (player_words, denial))
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
        inactive = fork(view(main), "Stay in the village and let the incident play out")
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
        without_note = fork(view(main), "Leave without inspecting the village")
        def register(branch, challenge, key):
            body = {"branch_id": branch, "actor_id": player, "expected_state_version": view(branch)["branch"]["state_version"]}
            return client.post(f"{path}/challenges/{challenge}/attempt", headers={"Idempotency-Key": key}, json=body)

        register(main, CHECK_ID, "check-shadow").raise_for_status()
        route = next(row for row in start["travel_options"] if row["destination_name"] == shrine_name)
        run_job(main, "long-actions", int(route["travel_ticks"]), activity="travel", route_id=route["route_id"])
        run_job(without_note, "long-actions", int(route["travel_ticks"]), activity="travel", route_id=route["route_id"])
        assert all(row["challenge_id"] != report_id for row in view(without_note)["state"]["local_challenges"])
        assert register(without_note, report_id, "unearned-observation").status_code == 409

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
        denied = fork(local, "Explicitly deny having a report")
        client.post(f"{path}/messages", headers={"Origin": origin}, json={
            "world_id": str(world_id), "branch_id": denied, "request_id": str(uuid4()), "session_id": str(uuid4()),
            "actor_id": player, "expected_state_version": view(denied)["branch"]["state_version"],
            "mode": "roleplay", "text": denial,
        }).raise_for_status()
        other_reports = []
        for kind in ("hearsay", "self_report", "suggestion"):
            branch = fork(local, kind)
            register(branch, mist_report_id(kind), kind).raise_for_status()
            receipt = view(branch, "author")["state"]["challenge_attempts"][-1]["report"]
            assert receipt["source_kind"] == kind and receipt["recipient_id"] == contact
            assert receipt["source_record_id"] == (str(RUMOR_ID) if kind == "hearsay" else None)
            other_reports.append((branch, SOURCES[kind][2]))

        assert any(row["challenge_id"] == report_id for row in view(main)["state"]["local_challenges"])
        register(main, mist_report_id("hearsay"), "earlier-rumor").raise_for_status()
        registration_version = view(main)["branch"]["state_version"]
        report = client.post(f"{path}/challenges/{report_id}/attempt", headers={"Idempotency-Key": "report-red-mist"}, json={
            "branch_id": main, "actor_id": player, "expected_state_version": registration_version,
        }).raise_for_status().json()
        assert report["passed"]
        duplicate = client.post(f"{path}/challenges/{report_id}/attempt", headers={"Idempotency-Key": "report-red-mist"}, json={
            "branch_id": main, "actor_id": player, "expected_state_version": registration_version,
        }).raise_for_status().json()
        assert duplicate == report
        records = view(main, "author")["state"]["challenge_attempts"]
        observation, = [row["report"] for row in records if row["challenge_id"] == report_id]
        assert observation["source_record_id"] == str(uuid5(CHECK_ID, f"note:{player}"))

        incident_tick = chapter.hours * 3600
        relief = local_aid(chapter.key).transfer
        branches = [(main, "player-report"), (dialogue_only, "independent"), (denied, "independent"),
                    (no_contact, "independent"), (inactive, "independent"), *other_reports]
        summaries = {key: summary for _, _, key, summary in SOURCES.values()} | {"independent": chapter.independent}
        for branch, outcome in branches:
            before_advance = view(branch)
            duration = incident_tick + 3600 - int(before_advance["branch"]["current_tick"])
            job_id = run_job(branch, "world-advance", duration)
            final = view(branch, "author")
            state = final["state"]
            result = next(row for row in state["story_milestones"] if row["schedule_id"] == incident)
            assert result["outcome"] == outcome and int(result["resolved_tick"]) == incident_tick
            public = next(row for row in view(branch)["state"]["story_milestones"] if row["record_id"] == incident)
            assert public["summary"] == summaries[outcome]
            own_reports = view(branch)["state"].get("own_report_receipts", [])
            assert len(own_reports) == (2 if branch == main else 0 if outcome == "independent" else 1)
            if branch == main:
                assert own_reports[0]["source_record_id"] == str(uuid5(CHECK_ID, f"note:{player}"))
                assert own_reports[1]["source_record_id"] == str(RUMOR_ID)
            assert state["entities"][player]["location_id"] == (village if branch == inactive else shrine)
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
            cursor = before_advance["last_seq"]
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

        assert planning_branches == [branch for branch, _ in branches]
        previous = view(main)["state"]
        village_name = previous["entities"][village]["name"]
        return_route = next(row for row in previous["travel_options"] if row["destination_name"] == village_name)
        run_job(main, "long-actions", int(return_route["travel_ticks"]), activity="travel", route_id=return_route["route_id"])
        back_home = view(main)["state"]
        assert back_home["entities"][player]["location_id"] == village
        assert back_home["own_report_receipts"] == previous["own_report_receipts"]


def test_moriya_survey_report_and_inactivity_have_distinct_world_consequences(tmp_path, monkeypatch):
    # Import the published opening and use the real rules, HTTP API, and leased
    # simulation. The model double keeps Sanae on site and other NPC plans deferred.
    opening = "mountain-shrine"
    sanae = str(actor_id("sanae"))
    incident = str(_id(f"world:incident:{opening}"))
    report_id = str(_id(f"world:incident:{opening}:report"))
    shrines = {key: str(_id(f"place:{key}")) for key in ("moriya-shrine", "shrine")}
    player_words = "早苗，我想把来客对山路通行的顾虑告诉你。"

    async def generate(_scope, request):
        if request.role_task == RoleTask.INTENT:
            assert player_words in request.messages[-1].content
            output = {"target_id": sanae, "clarification": ""}
        elif request.role_task == RoleTask.SOCIAL_ADJUDICATION:
            assert player_words in request.messages[-1].content
            output = {"utterance": "我愿意听听来客对这段山路的顾虑。"}
        elif request.role_task == RoleTask.NPC_DECISION:
            output = {"kind": "defer", "reason": "暂时留在原地，稍后再回顾目标。"}
        elif request.role_task == RoleTask.MEMORY_SUMMARY:
            output = {"outlook": "defer"}
        else:
            pytest.fail(f"Unexpected model role {request.role_task}")
        return SimpleNamespace(normalized_output=output)

    monkeypatch.setattr(ModelOperations, "generate", staticmethod(generate))
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    repository = app.state.worlds.repository
    scope = local_identity(tmp_path)
    jobs = SimulationJobs(repository)
    models = SimpleNamespace(generate=generate)
    origin = "http://127.0.0.1:8000"

    with TestClient(app, base_url=origin) as client:
        client.get("/api/v1/session").raise_for_status()
        archive = build_archive()
        upload = {"Content-Type": "application/zip", "Origin": origin}
        preview = client.post("/api/v1/packs/import/preview", headers=upload, content=archive).raise_for_status().json()
        created = client.post("/api/v1/packs/import", headers=upload | {
            "Idempotency-Key": opening, "X-World-Pack-Sha256": preview["archive_sha256"],
            "X-Story-Opening": opening,
        }, content=archive).raise_for_status().json()
        world_id = UUID(created["world_id"])
        survey_branch = created["active_branch_id"]
        path = f"/api/v1/worlds/{world_id}"

        def view(branch, mode="player"):
            return client.get(f"{path}/view", params={"branch_id": branch, "mode": mode}).raise_for_status().json()

        def fork(label):
            return client.post(f"{path}/branches", headers={"Idempotency-Key": label}, json={
                "branch_id": survey_branch, "sequence": view(survey_branch)["last_seq"], "label": label,
            }).raise_for_status().json()["branch_id"]

        def run_job(branch, endpoint, duration, **params):
            submitted = client.post(f"{path}/{endpoint}", headers={"Idempotency-Key": str(uuid4())}, json={
                "branch_id": branch, "expected_state_version": view(branch)["branch"]["state_version"],
                "duration_seconds": duration, **params,
            }).raise_for_status().json()
            job_id = UUID(submitted["job_id"])
            lease = repository.claim_job("moriya-acceptance-worker", lease_seconds=120)
            assert lease and lease.job_id == job_id and str(lease.branch_id) == branch

            async def advance():
                response = None
                for _ in range(128):
                    window = jobs.advance(scope, lease, model_decision=response)
                    current = repository.read_job(scope, job_id)
                    if current.status == JobStatus.SUCCEEDED:
                        return
                    assert current.status == JobStatus.RUNNING, current.result
                    response = None
                    if window.decision_requests:
                        decision, = window.decision_requests
                        assert decision.decision_owner == "model"
                        response = await _invoke_worker_model_decision(models, repository, scope, lease, current, decision)
                        if response is not None:
                            assert response.npc_planning_status != "invalid", response.npc_planning_rejection
                pytest.fail(f"Simulation job {job_id} did not reach its target")

            asyncio.run(advance())

        def advance_to(branch, tick):
            delta = tick - int(view(branch)["branch"]["current_tick"])
            assert delta > 0
            run_job(branch, "world-advance", delta)

        def travel(branch, destination):
            destination_name = view(branch, "author")["state"]["entities"][destination]["name"]
            route, = [item for item in view(branch)["state"]["travel_options"] if item["destination_name"] == destination_name]
            run_job(branch, "long-actions", int(route["travel_ticks"]), activity="travel", route_id=route["route_id"])

        def attempt(branch, challenge_id):
            current = view(branch)
            assert any(item["challenge_id"] == challenge_id for item in current["state"]["local_challenges"])
            result = client.post(f"{path}/challenges/{challenge_id}/attempt", headers={
                "Idempotency-Key": f"moriya-{challenge_id}",
            }, json={
                "branch_id": branch, "actor_id": player, "expected_state_version": current["branch"]["state_version"],
            }).raise_for_status().json()
            assert result["passed"]

        def relief_balances(state):
            return tuple(state["resources"][str(account)]["balance"]
                         for account in (relief.from_account_id, relief.to_account_id))

        player = view(survey_branch)["state"]["player_id"]
        aid = local_aid(opening)
        relief = aid.transfer
        incident_tick = 8 * 3600
        aid_tick = incident_tick + 255 * 60

        # Give each branch the same approach to the mountain. All three later
        # reach Moriya via the public route so only the outcome changes shortcut access.
        for destination in (shrines["shrine"], str(_id("place:forest")), str(_id("place:youkai-mountain"))):
            travel(survey_branch, destination)
        report_branch = fork("Report to Sanae")
        inactive_branch = fork("Travel without intervening")
        attempt(survey_branch, str(MORIYA_SURVEY_ID))
        branches = {
            survey_branch: "survey-passage",
            report_branch: "player-report",
            inactive_branch: "independent",
        }
        shortcut = str(_id(f"world-path:{shrines['moriya-shrine']}:{shrines['shrine']}"))
        for branch in branches:
            travel(branch, shrines["moriya-shrine"])
            assert not any(item["route_id"] == shortcut for item in view(branch)["state"]["travel_options"])

        report_view = view(report_branch)
        result = client.post(f"{path}/messages", headers={"Origin": origin}, json={
            "world_id": str(world_id), "branch_id": report_branch, "request_id": str(uuid4()),
            "session_id": str(uuid4()), "actor_id": player, "expected_state_version": report_view["branch"]["state_version"],
            "mode": "roleplay", "text": player_words,
        }).raise_for_status().json()
        assert result["kind"] == "dialogue" and result["world_effect"]
        assert result["references"] == [sanae]
        attempt(report_branch, report_id)

        for branch, expected in branches.items():
            advance_to(branch, incident_tick)
            head = view(branch, "author")
            state = head["state"]
            milestone, = [row for row in state["story_milestones"] if row["schedule_id"] == incident]
            assert (milestone["outcome"], int(milestone["resolved_tick"])) == (expected, incident_tick)
            permits = state["entities"][player]["components"]["capabilities"]["grants"]
            assert (MORIYA_SURVEY_PERMIT in permits) == (branch == survey_branch)
            assert relief_balances(state) == ("18", "0")
            assert any(row["route_id"] == shortcut for row in view(branch)["state"]["travel_options"]) == (
                branch == survey_branch
            )
            assert repository.replay(scope, world_id, UUID(branch), from_genesis=True).state_hash == head["state_hash"]

            advance_to(branch, aid_tick - 1)
            assert relief_balances(view(branch, "author")["state"]) == ("18", "0")
            advance_to(branch, aid_tick)
            after = view(branch, "author")
            state = after["state"]
            followup_id = str(_id(f"world:incident:{opening}:{expected}:followup"))
            followup, = [row for row in state["story_milestones"] if row["schedule_id"] == followup_id]
            if branch == report_branch:
                assert (followup["outcome"], int(followup["resolved_tick"])) == ("aid-delivered", aid_tick)
                assert relief_balances(state) == ("14", "4")
            else:
                assert (followup["outcome"], int(followup["resolved_tick"])) == ("recorded", incident_tick + 3600)
                assert relief_balances(state) == ("18", "0")
            assert (MORIYA_SURVEY_PERMIT in state["entities"][player]["components"]["capabilities"]["grants"]) == (
                branch == survey_branch
            )
            assert repository.replay(scope, world_id, UUID(branch), from_genesis=True).state_hash == after["state_hash"]

        shortcut_route, = [row for row in view(survey_branch)["state"]["travel_options"] if row["route_id"] == shortcut]
        assert int(shortcut_route["travel_ticks"]) == 75 * 60
        run_job(survey_branch, "long-actions", 75 * 60, activity="travel", route_id=shortcut)
        arrived = view(survey_branch, "author")
        assert arrived["state"]["entities"][player]["location_id"] == shrines["shrine"]
        assert int(arrived["branch"]["current_tick"]) == aid_tick + 75 * 60
        assert repository.replay(scope, world_id, UUID(survey_branch), from_genesis=True).state_hash == arrived["state_hash"]
