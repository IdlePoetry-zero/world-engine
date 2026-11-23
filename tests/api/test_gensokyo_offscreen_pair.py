"""Exercise offscreen social decisions in the published pack with deterministic model outputs."""

import json
from types import SimpleNamespace
from uuid import UUID, uuid5

from fastapi.testclient import TestClient
import pytest

from world_engine.adapters.models.contracts import RoleTask
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.content.gensokyo_reference import _id
from world_engine.content.gensokyo_world import actor_id, build_archive
from world_engine.domain.models import JobStatus
from world_engine.runtime import _invoke_worker_model_decision, build_application, local_identity


@pytest.mark.asyncio
async def test_scarlet_mansion_npcs_exchange_private_words_while_player_is_away(tmp_path):
    sakuya, remilia, flandre = map(actor_id, ("sakuya", "remilia", "flandre"))
    mansion = _id("place:mansion")
    pair = {sakuya, remilia}
    proposal = "大小姐，可否在晚饭前到会客室核对来客名单？"
    reply = "可以，晚饭前到会客室来。先把湖边来访的几位列出来。"
    reviewed = []
    proposal_memory_ids = []

    async def generate(_scope, request):
        if request.role_task == RoleTask.NPC_PLAN_REVIEW:
            return SimpleNamespace(normalized_output={"verdict": "approve"})
        if request.role_task == RoleTask.MEMORY_SUMMARY:
            return SimpleNamespace(normalized_output={"outlook": "defer"})
        assert request.role_task == RoleTask.NPC_DECISION
        context = json.loads(request.messages[-1].content)
        actor = UUID(context["actor"]["id"])
        reviewed.append(actor)
        if actor not in pair:
            assert all(text not in request.messages[-1].content for text in (proposal, reply))
            return SimpleNamespace(normalized_output={"kind": "defer", "reason": "稍后再回顾当前目标。"})

        counterpart, = pair - {actor}
        assert str(counterpart) in context["known_view"]["entities"]
        assert str(player) not in context["known_view"]["entities"]
        assert any(row["to_id"] == str(counterpart) and row["dimensions"]["familiarity"] == "12"
                   for row in context["known_view"]["own_local_relationships"])
        actor_key = "sakuya" if actor == sakuya else "remilia"
        goal, = [row for row in context["active_goals"] if row["id"] == str(_id(f"world:social-goal:{actor_key}"))]
        arguments = {"target_ids": [str(counterpart)], "utterance": proposal if actor == sakuya else reply}
        if actor == remilia:
            memory, = [row for row in context["known_records"] if proposal in row["claim"]]
            assert memory["kind"] == "memory"
            assert {"memory_id": memory["id"], "speaker_id": str(counterpart)} in context["private_reply_options"]
            proposal_memory_ids.append(UUID(memory["id"]))
            arguments["reply_to_memory_id"] = memory["id"]
        return SimpleNamespace(normalized_output={
            "kind": "plan", "goal_ids": [goal["id"]], "reason": "与在场的馆方人员核对近期待客安排。",
            "steps": [{"capability_id": "world.action.speak.v1", "arguments": arguments}],
        })

    def pair_relations(state):
        return {(row.from_id, row.to_id): row for row in state.relations if {row.from_id, row.to_id} == pair}

    app = build_application(data_directory=tmp_path, run_simulation_worker=False)
    scope = local_identity(tmp_path)
    repository = app.state.worlds.repository
    jobs = SimulationJobs(repository)
    origin = "http://127.0.0.1:8000"
    archive = build_archive()
    upload = {"Origin": origin, "Content-Type": "application/zip"}

    with TestClient(app, base_url=origin) as client:
        client.get("/api/v1/session").raise_for_status()
        preview = client.post("/api/v1/packs/import/preview", headers=upload, content=archive).raise_for_status().json()
        created = client.post("/api/v1/packs/import", headers=upload | {
            "Idempotency-Key": "offscreen-mansion", "X-World-Pack-Sha256": preview["archive_sha256"],
            "X-Story-Opening": "scarlet-mist",
        }, content=archive).raise_for_status().json()
        world_id, branch_id = map(UUID, (created["world_id"], created["active_branch_id"]))
        path = f"/api/v1/worlds/{world_id}"
        before = repository.read_world(scope, world_id, branch_id)
        player = before.state.player_id
        player_place = before.state.entities[player].location_id
        assert player_place != mansion
        assert all(before.state.entities[actor].location_id == mansion for actor in (*pair, flandre))
        assert not any(proposal in row.claim or reply in row.claim for row in before.state.knowledge)
        assert set(pair_relations(before.state)) == {(sakuya, remilia), (remilia, sakuya)}
        review_ticks = {UUID(row.payload["actor_id"]): row.due_tick for row in before.state.scheduled_events
                        if row.handler_id == "npc.goal_review"}
        assert review_ticks[sakuya] < review_ticks[remilia] < review_ticks[flandre]
        target = review_ticks[flandre] + 1
        body = {"branch_id": str(branch_id), "expected_state_version": str(before.branch.state_version),
                "duration_seconds": target - before.state.current_tick}
        headers = {"Idempotency-Key": "mansion-household-reviews"}
        queued = client.post(f"{path}/world-advance", headers=headers, json=body).raise_for_status().json()
        job_id = UUID(queued["job_id"])
        lease = repository.claim_job("offscreen-mansion-worker", lease_seconds=120)
        assert lease and lease.job_id == job_id

        decision_models = SimpleNamespace(generate=generate)
        response = None
        for _ in range(96):
            window = jobs.advance(scope, lease, model_decision=response)
            response = None
            current = repository.read_job(scope, job_id)
            if current.status == JobStatus.SUCCEEDED:
                break
            if window.decision_requests:
                decision, = window.decision_requests
                assert decision.decision_owner == "model"
                response = await _invoke_worker_model_decision(
                    decision_models, repository, scope, lease, current, decision,
                )
                if response is not None:
                    assert response.npc_planning_status != "invalid", response.npc_planning_rejection
        assert current.status == JobStatus.SUCCEEDED
        assert [actor for actor in reviewed if actor in pair | {flandre}] == [sakuya, remilia, flandre]

        final = repository.read_world(scope, world_id, branch_id)
        assert final.state.current_tick == target
        assert final.state.entities[player].location_id == player_place
        assert all(final.state.entities[actor].location_id == mansion for actor in pair)
        plans = {row.actor_id: row for row in final.state.npc_plans if row.actor_id in pair}
        assert set(plans) == pair
        events = repository.list_events(scope, world_id, branch_id, after_seq=before.last_seq, limit=100).events
        visibility = {"policy": "subject_ids", "subject_ids": sorted(map(str, pair))}
        speeches = {}
        for speaker, listener, words in ((sakuya, remilia, proposal), (remilia, sakuya, reply)):
            memory, = [row for row in final.state.knowledge if words in row.claim]
            assert (memory.owner_id, memory.holder_id, memory.subject_id) == (speaker, listener, speaker)
            assert memory.kind == "memory" and memory.fact is None
            assert memory.received_tick == review_ticks[speaker]
            if speaker == sakuya:
                assert proposal_memory_ids == [memory.id]
            else:
                assert memory.speech_reply_to == proposal_memory_ids[0]
            event, = [row for row in events if row.type == "core.knowledge_acquired.v1"
                      and row.payload["record"]["id"] == str(memory.id)]
            assert event.visibility == visibility
            assert event.command_id == uuid5(job_id, f"window:{event.state_version_after - 1}")
            assert plans[speaker].status == "completed" and event.event_id in plans[speaker].evidence_refs
            speeches[speaker] = event

        assert speeches[sakuya].sequence < speeches[remilia].sequence
        final_relations = pair_relations(final.state)
        for direction, previous in pair_relations(before.state).items():
            updated = final_relations[direction]
            assert updated.dimensions["familiarity"] == previous.dimensions["familiarity"] + 1
            assert updated.evidence_refs == [*previous.evidence_refs, speeches[remilia].event_id]
            change, = [row for row in events if row.type == "core.relation_dimension_changed.v1"
                       and row.payload["relation_id"] == str(updated.id)]
            assert change.visibility == visibility and change.command_id == speeches[remilia].command_id

        player_view = client.get(f"{path}/view", params={"branch_id": str(branch_id), "mode": "player"})
        player_history = client.get(f"{path}/history", params={"branch_id": str(branch_id), "mode": "player",
                                                            "after": before.last_seq})
        for response in (player_view, player_history):
            projected = json.dumps(response.raise_for_status().json(), ensure_ascii=False)
            assert all(words not in projected for words in (proposal, reply))
        duplicate = client.post(f"{path}/world-advance", headers=headers, json=body).raise_for_status().json()
        assert duplicate["job_id"] == str(job_id) and duplicate["status"] == JobStatus.SUCCEEDED.value
        assert repository.read_world(scope, world_id, branch_id).state_hash == final.state_hash
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash
