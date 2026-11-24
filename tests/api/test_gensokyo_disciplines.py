"""Play the published Gensokyo disciplines through the registered HTTP actions."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from fastapi.testclient import TestClient
import pytest

from world_engine.adapters.models.contracts import RoleTask
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.content.gensokyo_reference import _id
from world_engine.content.gensokyo_world import actor_id, build_archive
from world_engine.domain.models import JobStatus
from world_engine.runtime import _invoke_worker_model_decision, build_application, local_identity


@pytest.mark.asyncio
async def test_scarlet_mist_training_unlocks_four_exercises_and_a_consented_spell_card_contest(tmp_path):
    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    repository = app.state.worlds.repository
    scope = local_identity(tmp_path)
    jobs = SimulationJobs(repository)
    origin = "http://127.0.0.1:8000"
    archive = build_archive()
    reimu = actor_id("reimu")

    async def generate(_scope, request):
        # Keep work on the player disciplines by deferring unrelated NPC model decisions.
        if request.role_task == RoleTask.NPC_DECISION:
            return SimpleNamespace(normalized_output={"kind": "defer", "reason": "稍后再回顾当前目标。"})
        assert request.role_task == RoleTask.MEMORY_SUMMARY
        return SimpleNamespace(normalized_output={"outlook": "defer"})

    decision_models = SimpleNamespace(generate=generate)
    upload = {"Content-Type": "application/zip", "Origin": origin}
    with TestClient(app, base_url=origin) as client:
        client.get("/api/v1/session").raise_for_status()
        preview = client.post("/api/v1/packs/import/preview", headers=upload, content=archive).raise_for_status().json()
        created = client.post("/api/v1/packs/import", headers=upload | {
            "X-World-Pack-Sha256": preview["archive_sha256"], "X-Story-Opening": "scarlet-mist",
            "Idempotency-Key": "scarlet-mist-disciplines",
        }, content=archive).raise_for_status().json()
        world_id, branch_id = map(UUID, (created["world_id"], created["active_branch_id"]))
        path = f"/api/v1/worlds/{world_id}"

        def read(branch=branch_id):
            return repository.read_world(scope, world_id, branch)

        def player_view():
            return client.get(f"{path}/view", params={"branch_id": str(branch_id)}).raise_for_status().json()["state"]

        player = read().state.player_id

        async def long_action(activity, duration, **arguments):
            before = read()
            payload = {"branch_id": str(branch_id), "expected_state_version": str(before.branch.state_version),
                       "duration_seconds": duration, "activity": activity, **arguments}
            queued = client.post(f"{path}/long-actions", headers={"Idempotency-Key": str(uuid4())},
                                 json=payload).raise_for_status().json()
            job_id = UUID(queued["job_id"])
            lease = repository.claim_job("gensokyo-discipline-worker", lease_seconds=120)
            assert lease and lease.job_id == job_id
            response = None
            for _ in range(256):
                window = jobs.advance(scope, lease, model_decision=response)
                response = None
                current = repository.read_job(scope, job_id)
                if current.status == JobStatus.SUCCEEDED:
                    break
                assert current.status not in {JobStatus.FAILED, JobStatus.CANCELLED}, current.result
                if window.decision_requests:
                    decision, = window.decision_requests
                    assert decision.decision_owner == "model"
                    response = await _invoke_worker_model_decision(
                        decision_models, repository, scope, lease, current, decision,
                    )
            assert current.status == JobStatus.SUCCEEDED, current.result
            assert read().state.current_tick == before.state.current_tick + duration

        async def travel(destination):
            ident = _id(f"place:{destination}")
            name = read().state.entities[ident].name
            route, = [row for row in player_view()["travel_options"] if row["destination_name"] == name]
            await long_action("travel", int(route["travel_ticks"]), route_id=route["route_id"])
            assert read().state.entities[player].location_id == ident

        def player_action(endpoint, *, branch=branch_id, expected=200, **arguments):
            response = client.post(f"{path}/{endpoint}", headers={"Idempotency-Key": str(uuid4())}, json={
                "branch_id": str(branch), "expected_state_version": str(read(branch).branch.state_version),
                "actor_id": str(player), **arguments,
            })
            assert response.status_code == expected, response.text
            return response.json()

        def challenge(discipline, **params):
            ident = _id(f"world:challenge:{discipline}")
            return player_action(f"challenges/{ident}/attempt", **params)

        async def train_and_pass(discipline):
            ident = _id(f"world:challenge:{discipline}")
            option, = [row for row in player_view()["local_challenges"] if row["challenge_id"] == str(ident)]
            assert option["passed"] is None and Decimal(option["pass_level"]) == 12
            assert Decimal(option["current_level"]) == 10 and not option["can_attempt_skill"]
            skill_id = f"gensokyo.{discipline}"
            before = read().state.entities[player].components["skills"]["skills"][skill_id]
            assert Decimal(before["level"]) == 10
            await long_action("study", 60_000, skill_id=skill_id)
            trained = read().state.entities[player].components["skills"]["skills"][skill_id]
            assert Decimal(trained["level"]) == 12
            assert trained["experience"] - before["experience"] == 200
            assert trained["trained_ticks"] - before["trained_ticks"] == 60_000
            assert challenge(discipline)["passed"] is True
            completed, = [row for row in player_view()["local_challenges"] if row["challenge_id"] == str(ident)]
            assert completed["passed"] is True

        await travel("shrine")
        untrained = read()
        fork = client.post(f"{path}/branches", headers={"Idempotency-Key": "before-declaration-study"}, json={
            "branch_id": str(branch_id), "sequence": str(untrained.last_seq), "label": "Attempt before studying",
        }).raise_for_status().json()
        control = UUID(fork["branch_id"])
        assert challenge("declaration", branch=control, expected=409)["error"]["code"] == "CHALLENGE_UNAVAILABLE"
        assert read(control).state_hash == untrained.state_hash
        assert not read(control).state.challenge_attempts

        contest_id = _id("world:contest:shrine")
        offer = {"operation": "invite", "definition_id": str(contest_id), "recipient_id": str(reimu),
                 "routine": "boundary-line"}
        assert not player_view()["local_contests"][0]["can_invite"]
        player_action("contests", action=offer, expected=409)
        await train_and_pass("declaration")
        contest, = player_view()["local_contests"]
        assert contest["definition_id"] == str(contest_id) and contest["can_invite"]
        assert str(reimu) in {row["id"] for row in contest["opponents"]}
        invite = player_action("contests", action=offer)
        assert invite["status"] == "invited"
        change = {"contest_id": invite["contest_id"]}
        player_action("contests", action=change | {"operation": "settle"}, expected=409)
        assert player_action("contests", action=change | {"operation": "ask_npc"})["status"] == "accepted"
        score = player_action("contests", action=change | {"operation": "settle"})
        assert (score["status"], score["rounds"], score["winner_id"]) == ("settled", 3, str(reimu))
        assert (score["proposer_wins"], score["recipient_wins"]) == (0, 3)

        await travel("forest")
        await train_and_pass("magic")
        await travel("kourindou")
        await train_and_pass("technology")
        await travel("human-village")
        await travel("bamboo")
        await train_and_pass("youkai")

        final = read()
        assert {row.challenge_id for row in final.state.challenge_attempts if row.actor_id == player and row.passed} == {
            _id(f"world:challenge:{discipline}") for discipline in ("declaration", "magic", "technology", "youkai")
        }
        settled, = final.state.contests
        assert (settled.recipient_id, settled.recipient_routine, settled.status) == (reimu, "boundary-line", "settled")
        assert [(row.proposer_skill, row.recipient_skill) for row in settled.rounds] == [(12, 18), (10, 17), (10, 14)]
        note_id = uuid5(_id("world:challenge:declaration"), f"note:{player}")
        note, = [row for row in final.state.knowledge if row.id == note_id]
        assert note.source_event_id is not None and note.visibility.subject_ids == [player]
        for branch in (branch_id, control):
            assert repository.replay(scope, world_id, branch, from_genesis=True).state_hash == read(branch).state_hash
