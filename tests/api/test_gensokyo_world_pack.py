"""Verify the playable package uses the same import path as other worlds."""

from decimal import Decimal
from uuid import UUID

from fastapi.testclient import TestClient

from world_engine.application.quoted_speech_guard import visible_dialogue_targets
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.world_packs import load_world_pack, write_world_pack
from world_engine.content.gensokyo_cast_habitats import HABITATS
from world_engine.content.gensokyo_reference import _id
from world_engine.content.gensokyo_world import CHAPTERS, actor_id, build_archive, build_opening, world_source_index
from world_engine.domain.components import PlaceRoute
from world_engine.domain.models import JobStatus, WorldState
from world_engine.rules.engine import route_access_reason
from world_engine.runtime import build_application, local_identity
from world_engine.simulation.npc_routine_schedule import next_npc_routine_tick, npc_routine_candidate


def _reachable_with_available_permits(state: WorldState) -> set[UUID]:
    player = state.entities[state.player_id]
    grants = set(player.components["capabilities"]["grants"])
    challenges = {challenge.challenge_id: challenge for challenge in state.challenges}
    while True:
        reached = set()
        pending = [player.location_id]
        while pending:
            place = pending.pop()
            if place in reached:
                continue
            reached.add(place)
            actor = player.model_copy(update={"location_id": place, "components": player.components | {
                "capabilities": player.components["capabilities"] | {"grants": sorted(grants)},
            }})
            for raw in state.entities[place].components["place"]["routes"]:
                route = PlaceRoute.model_validate(raw)
                if route_access_reason(state, actor, route) is None:
                    pending.append(route.to_place_id)

        earned = {
            award.capability for reaction in state.story_reactions for award in reaction.node.fallback.awards
            if award.actor_id == player.id and all(
                (gate.kind == "challenge_passed" and challenges[gate.challenge_id].venue_id in reached
                 and gate.actor_id == player.id
                 and Decimal(player.components["skills"]["skills"][challenges[gate.challenge_id].skill_id]["level"])
                 >= challenges[gate.challenge_id].pass_level)
                or (gate.kind == "world_condition" and gate.condition.kind == "entity_status"
                    and state.entities[gate.condition.subject_id].status == gate.condition.status)
                for gate in reaction.when
            )
        }
        additional = earned - grants
        if not additional:
            return reached
        grants.update(additional)


def test_gensokyo_cast_and_access_across_imported_openings(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    repository = app.state.worlds.repository
    scope = local_identity(tmp_path)
    origin = "http://127.0.0.1:8000"
    archive = build_archive()
    headers = {"Content-Type": "application/zip", "Origin": origin}
    registry = world_source_index()
    cast = {actor_id(slug) for slug in HABITATS}
    accessible = set()

    with TestClient(app, base_url=origin) as client:
        client.get("/api/v1/session").raise_for_status()
        preview = client.post("/api/v1/packs/import/preview", content=archive, headers=headers).raise_for_status().json()
        assert not preview["blockers"]
        assert [row["opening_id"] for row in preview["story_openings"]] == [chapter.key for chapter in CHAPTERS]

        previous = set()
        for chapter in CHAPTERS:
            created = client.post("/api/v1/packs/import", content=archive, headers=headers | {
                "Idempotency-Key": chapter.key,
                "X-World-Pack-Sha256": preview["archive_sha256"],
                "X-Story-Opening": chapter.key,
            }).raise_for_status().json()
            world_id, branch_id = map(UUID, (created["world_id"], created["active_branch_id"]))
            view = repository.read_world(scope, world_id, branch_id)
            state = view.state
            assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == view.state_hash
            assert all(citation.source_id in registry for row in state.world_book_entries
                       for citation in row.provenance.citations)
            present = cast.intersection(state.entities)
            assert previous <= present
            assert present == {goal.owner_id for goal in state.goals}
            assert present == {UUID(schedule.payload["actor_id"]) for schedule in state.scheduled_events
                               if schedule.handler_id == "npc.goal_review"}
            assert actor_id(chapter.contact) in present
            previous = present

            reached = _reachable_with_available_permits(state)
            accessible.update(actor for actor in present if state.entities[actor].location_id in reached)

    assert len(cast) == 116
    assert present == cast
    assert accessible == cast


def test_red_mist_save_opens_the_full_timeline_with_private_knowledge(tmp_path):
    source = load_world_pack(build_archive())
    opening = source.template["story_openings"][0]
    scenario = opening["scenario"]
    trigger = next(row for row in scenario["story_followups"] if row["node"]["title"] == "迟来的春天")
    arrival = next(row for row in scenario["story_arrivals"] if row["actor"]["id"] == str(actor_id("yukari")))
    assert arrival["trigger_id"] == trigger["followup_id"]
    assert trigger["delay_ticks"] > 24 * 3600
    assert next(row for row in scenario["world_book_entries"] if row["source_key"] == "yukari")[
        "visibility"
    ] == "author_only"
    assert len(scenario["story_arrivals"]) == 107

    # Run the published opening and its actual arrival definitions with a short
    # author-configured clock. The release's multi-month interval is checked above.
    main = next(row for row in scenario["scheduled_events"] if row["id"] == trigger["after_id"])
    main["due_tick"] = 2
    for followup in scenario["story_followups"]:
        followup["delay_ticks"] = 3
    player_spec = scenario["entities"][scenario["player_id"]]
    player_spec["location_id"] = str(_id("place:shrine"))
    archive = write_world_pack(manifest=source.manifest, template={"story_openings": [opening]})
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    scope = local_identity(tmp_path)
    repository = app.state.worlds.repository
    jobs = SimulationJobs(repository)
    origin = "http://127.0.0.1:8000"
    headers = {"Origin": origin, "Content-Type": "application/zip"}
    yukari = actor_id("yukari")
    with TestClient(app, base_url=origin) as client:
        client.get("/api/v1/session").raise_for_status()
        preview = client.post("/api/v1/packs/import/preview", content=archive, headers=headers).raise_for_status().json()
        created = client.post("/api/v1/packs/import", content=archive, headers=headers | {
            "Idempotency-Key": "red-mist-with-continuation", "X-World-Pack-Sha256": preview["archive_sha256"],
            "X-Story-Opening": "scarlet-mist",
        }).raise_for_status().json()
        world_id, branch_id = map(UUID, (created["world_id"], created["active_branch_id"]))
        before = repository.read_world(scope, world_id, branch_id)
        assert yukari not in before.state.entities
        assert all(record.holder_id != yukari for record in before.state.knowledge)
        path = f"/api/v1/worlds/{world_id}"
        netherworld = _id("place:netherworld")
        assert before.state.entities[netherworld].status == "closed"
        route_options = client.get(f"{path}/view").raise_for_status().json()["state"]["travel_options"]
        assert all(row["destination_name"] != "冥界" for row in route_options)
        permit_id = _id("world:passage:gensokyo.netherworld_passage")
        filed = client.post(f"{path}/challenges/{permit_id}/attempt", headers={"Idempotency-Key": "netherworld-permit"}, json={
            "branch_id": str(branch_id), "actor_id": scenario["player_id"], "expected_state_version": str(before.branch.state_version),
        }).raise_for_status().json()
        assert filed["passed"]
        before = repository.read_world(scope, world_id, branch_id)
        assert "gensokyo.netherworld_passage" not in before.state.entities[before.state.player_id].components[
            "capabilities"
        ]["grants"]
        def advance_until(target):
            now = repository.read_world(scope, world_id, branch_id)
            job = client.post(f"{path}/world-advance", headers={"Idempotency-Key": f"continue-to-{target}"}, json={
                "branch_id": str(branch_id), "expected_state_version": str(now.branch.state_version),
                "duration_seconds": target - now.state.current_tick,
            }).raise_for_status().json()
            lease = repository.claim_job("gensokyo-continuation")
            assert lease is not None and lease.job_id == UUID(job["job_id"])
            for _ in range(96):
                jobs.advance(scope, lease)
                if repository.read_job(scope, lease.job_id).status == JobStatus.SUCCEEDED:
                    break
            assert repository.read_job(scope, lease.job_id).status == JobStatus.SUCCEEDED

        late_report = str(_id("world:incident:unclaimed-land:report"))

        def view():
            return client.get(f"{path}/view").raise_for_status().json()["state"]

        assert all(row["challenge_id"] != late_report for row in view()["local_challenges"])
        advance_until(65)
        assert any(row["challenge_id"] == late_report for row in view()["local_challenges"])
        middle = repository.read_world(scope, world_id, branch_id)
        report = client.post(f"{path}/challenges/{late_report}/attempt", headers={"Idempotency-Key": "later-report"}, json={
            "branch_id": str(branch_id), "actor_id": scenario["player_id"],
            "expected_state_version": str(middle.branch.state_version),
        }).raise_for_status().json()
        assert report["passed"]
        advance_until(90)
        assert all(row["challenge_id"] != late_report for row in view()["local_challenges"])
        after = repository.read_world(scope, world_id, branch_id)
        assert after.state.entities[yukari].name == "八云紫"
        assert {goal.owner_id for goal in after.state.goals} == {actor_id(key) for key in HABITATS}
        reachable = _reachable_with_available_permits(after.state)
        assert all(after.state.entities[actor_id(key)].location_id in reachable for key in HABITATS)
        assert all(entity.status == "active" for entity in after.state.entities.values())
        assert next(entry for entry in after.state.world_book_entries if entry.source_key == "yukari").visibility == "public"
        assert str(actor_id("sanae")) in after.state.entities[_id("organization:moriya-faith")].components[
            "governance"
        ]["members"]
        route_options = client.get(f"{path}/view").raise_for_status().json()["state"]["travel_options"]
        assert any(row["destination_name"] == "冥界" for row in route_options)
        established, = [record for record in after.state.knowledge if record.holder_id == yukari]
        assert established.received_tick == 5
        assert established.visibility.policy == "private"
        assert established.source_event_id is not None and established.provenance[0].kind == "pack"
        assert any(row.handler_id == "npc.goal_review" and row.payload["actor_id"] == str(yukari)
                   for row in after.state.scheduled_events)
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == after.state_hash


def test_reimu_has_recurring_patrol_with_weather_fallback():
    state = WorldState.model_validate(build_opening(CHAPTERS[0])["scenario"])
    reimu = actor_id("reimu")
    excluded = set(state.entities) - {reimu}
    day = state.calendar.day_seconds
    due = next_npc_routine_tick(state, 2 * day, excluded_actor_ids=excluded)
    assert due is not None and due > day
    travel = npc_routine_candidate(state.model_copy(update={"current_tick": due}), excluded_actor_ids=excluded)
    assert travel is not None and travel.actor_id == reimu
    assert travel.reason == "primary"

    village = state.entities[_id("place:human-village")]
    village_place = village.components["place"] | {"environment": {"weather": "storm"}}
    changed = village.model_copy(update={"components": village.components | {"place": village_place}})
    storm = state.model_copy(update={"entities": state.entities | {village.id: changed}, "current_tick": due})
    assert npc_routine_candidate(storm, excluded_actor_ids=excluded) is None


def test_every_original_actor_is_an_eligible_dialogue_target_at_their_opening_location():
    state = WorldState.model_validate(build_opening(CHAPTERS[-1])["scenario"])
    player = state.entities[state.player_id]
    actors = {actor_id(key) for key in HABITATS}
    venues = {state.entities[ident].location_id for ident in actors}
    eligible = set()
    for venue in venues:
        visitor = player.model_copy(update={"location_id": venue})
        current = state.model_copy(update={"entities": state.entities | {player.id: visitor}})
        eligible.update(UUID(ident) for ident in visible_dialogue_targets(current, player.id))
    assert actors <= eligible
