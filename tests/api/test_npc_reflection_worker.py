import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from world_engine.adapters.models.contracts import RoleTask
from world_engine.agents.knowledge import build_agent_knowledge
from world_engine.application.backups import BackupApplication
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.components import GoalRecord
from world_engine.domain.models import AccessScope, NewWorld, WorldEntity, WorldState
from world_engine.domain.npc_plan_review import build_npc_plan_review_schedule
from world_engine.runtime import _invoke_worker_model_decision
from world_engine.simulation.contracts import WorldAdvancePlan
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", ["standard", "deep", "exhaustive"])
async def test_depth_pins_actor_private_reflection_and_survives_worker_reacquisition(tmp_path, depth):
    database = Database(f"sqlite:///{(tmp_path / 'source.sqlite').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    world_id, actor_id, player_id, own_goal_id, private_goal_id = (uuid4() for _ in range(5))
    state = WorldState(
        player_id=player_id,
        entities={
            player_id: WorldEntity(id=player_id, kind="character", name="Player"),
            actor_id: WorldEntity(id=actor_id, kind="character", name="Arin"),
        },
        goals=[
            GoalRecord(id=own_goal_id, owner_id=actor_id, title="Find a safe route"),
            GoalRecord(id=private_goal_id, owner_id=player_id, title="PLAYER_PRIVATE_GOAL"),
        ],
    )
    state = state.model_copy(update={"scheduled_events": [build_npc_plan_review_schedule(state, world_id, actor_id, 0)]})
    view = repository.create_world(scope, NewWorld(world_id=world_id, title="Reflection", state=state))
    branch_id = view.branch.branch_id
    engine = SimulationJobs(repository)
    job = engine.enqueue_world_advance(
        scope, world_id, branch_id, WorldAdvancePlan(started_tick=0, target_tick=120),
        idempotency_key="reflect-on-a-plan-cycle", speed="balanced", depth=depth,
    )
    lease = repository.claim_job("before-reacquisition")
    assert lease is not None and lease.job_id == job.job_id
    model_roles = []

    class RoleAwareModel:
        async def generate(self, _scope, request):
            model_roles.append(request.role_task)
            if request.role_task is RoleTask.NPC_DECISION:
                return SimpleNamespace(
                    normalized_output={"kind": "defer", "reason": "Wait for trustworthy route information."},
                    call_trace=SimpleNamespace(request_id="source-plan-call"),
                )
            assert request.role_task is RoleTask.MEMORY_SUMMARY
            assert request.trace_context.job_id == str(job.job_id)
            body = json.loads(request.messages[-1].content)
            assert str(actor_id) in json.dumps(body)
            assert str(private_goal_id) not in json.dumps(body)
            assert "PLAYER_PRIVATE_GOAL" not in json.dumps(body)
            assert "trustworthy route information" not in json.dumps(body)
            return SimpleNamespace(
                normalized_output={"outlook": "reconsider"},
                call_trace=SimpleNamespace(request_id="private-reflection-call"),
            )

    models = RoleAwareModel()
    try:
        opening = engine.advance(scope, lease)
        source_request, = opening.decision_requests
        source_answer = await _invoke_worker_model_decision(models, repository, scope, lease, job, source_request)
        source_resolution = engine.advance(scope, lease, model_decision=source_answer)
        source_event, = [event for event in source_resolution.events if event.type == "core.npc_plan_review_resolved.v1"]
        after_source = repository.read_world(scope, world_id, branch_id)
        assert after_source.state.current_tick == 0
        assert model_roles == [RoleTask.NPC_DECISION]

        if depth != "standard":
            reflection = engine.advance(scope, lease)
            request, = reflection.decision_requests
            assert request.kind == "npc.reflection"
            assert request.causal_subject_id == source_event.event_id
            await _invoke_worker_model_decision(models, repository, scope, lease, job, request)
            before_commit = repository.read_world(scope, world_id, branch_id)
            assert before_commit.state_hash == after_source.state_hash
            repository.control_job(scope, job.job_id, "pause")
            repository.control_job(scope, job.job_id, "resume")
            reopened = WorldRepository(database)
            engine = SimulationJobs(reopened)
            recovered = reopened.claim_job("after-reacquisition")
            assert recovered is not None and recovered.job_id == lease.job_id
            assert recovered.fencing_token > lease.fencing_token
            repository, lease = reopened, recovered
            committed = engine.advance(scope, lease)
            memory, = committed.events
            assert memory.type == "core.knowledge_acquired.v1"
            assert memory.record.source_event_id == source_event.event_id
            assert memory.record.owner_id == memory.record.holder_id == actor_id
            assert memory.record.visibility.policy == "private"
            assert committed.next_tick == 0
            assert model_roles == [RoleTask.NPC_DECISION, RoleTask.MEMORY_SUMMARY]

        for _ in range(5):
            current = repository.read_job(scope, job.job_id)
            if current.status.value != "running":
                break
            engine.advance(scope, lease)
        current = repository.read_job(scope, job.job_id)
        assert current.status.value == "succeeded"
        final = repository.read_world(scope, world_id, branch_id)
        assert final.state.current_tick == 120
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash
        known = build_agent_knowledge(final.state, actor_id).known_records
        visible_to_player = build_agent_knowledge(final.state, player_id).known_records
        expected_notes = 0 if depth == "standard" else 1
        assert sum(record.source_event_id == source_event.event_id for record in known) == expected_notes
        assert all(record.source_event_id != source_event.event_id for record in visible_to_player)
        assert len(model_roles) == expected_notes + 1

        if depth == "exhaustive":
            backup = BackupApplication(repository)
            archive = backup.export_world(scope, world_id)
            restored_database = Database(f"sqlite:///{(tmp_path / 'restored.sqlite').as_posix()}")
            restored_database.create_schema()
            try:
                restored = WorldRepository(restored_database)
                with backup.validate_archive(archive) as verified:
                    WorldBackupImporter(restored).import_verified_backup(scope, verified)
                assert restored.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash
            finally:
                restored_database.dispose()
    finally:
        database.dispose()
