from uuid import uuid4, uuid5

import pytest

from world_engine.application.backups import BackupApplication
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.content import ContentCatalog
from world_engine.domain.models import AccessScope, MembershipRole
from world_engine.domain.npc_plan_review import build_npc_plan_review_schedule
from world_engine.domain.npc_plans import (
    NPC_SHARED_EXPLORATION_POLICY, NPC_SHARED_TRAINING_POLICY, NpcPlan, NpcPlanStep, occupied_npc_ids,
)
from world_engine.domain.reducers import state_hash
from world_engine.simulation import WorldAdvancePlan
from world_engine.simulation.contracts import DecisionResponse
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


@pytest.mark.parametrize(
    "action,policy,result_field,result_code", [
        ("explore", NPC_SHARED_EXPLORATION_POLICY, "exploration_result", "nothing_hidden_found"),
        ("train", NPC_SHARED_TRAINING_POLICY, "training_result", "training_completed"),
    ],
)
def test_two_leased_npc_actions_share_one_clock_and_restore(tmp_path, action, policy, result_field, result_code):
    draft = ContentCatalog().build_world("modern_city", "Two concurrent searches", "Player", seed=0, pack_version="2.0.0")
    state = draft.state
    hidden_sites = {entity.location_id for entity in state.entities.values() if entity.components.get("hidden") is True}
    goals = {
        goal.owner_id: goal.id for goal in state.goals
        if goal.status == "active" and state.entities[goal.owner_id].status == "active"
        and state.entities[goal.owner_id].location_id not in hidden_sites | {None}
        and goal.owner_id != state.player_id
    }
    actors = sorted(goals, key=str)[:2]
    assert len(actors) == 2
    skills = {actor: min(state.entities[actor].components["skills"]["skills"]) for actor in actors}

    def skill_values(candidate_state, actor):
        return candidate_state.entities[actor].components["skills"]["skills"][skills[actor]]

    schedules = [build_npc_plan_review_schedule(state, draft.world_id, actor, 0) for actor in actors]
    initial = state.model_copy(update={
        "metadata": {**state.metadata, policy: True},
        "scheduled_events": [*state.scheduled_events, *schedules],
    })
    database = Database(f"sqlite:///{(tmp_path / 'search.db').as_posix()}")
    restored_database = Database(f"sqlite:///{(tmp_path / 'restored.db').as_posix()}")
    database.create_schema()
    restored_database.create_schema()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        repository = WorldRepository(database)
        repository.create_world(scope, draft.model_copy(update={"state": initial}))
        jobs = SimulationJobs(repository)
        job = jobs.enqueue_world_advance(
            scope, draft.world_id, draft.branch_id, WorldAdvancePlan(started_tick=0, target_tick=600),
            idempotency_key="shared-search", speed="balanced", depth="standard",
        )
        lease = repository.claim_job("shared-search", job_type=job.job_type, lease_seconds=180)
        assert lease is not None
        observed = []
        events = []
        export = BackupApplication(repository)
        active_archive = None
        active_hash = None
        for _ in range(16):
            view = repository.read_world(scope, draft.world_id, draft.branch_id)
            busy = occupied_npc_ids(view.state, view.state.current_tick)
            observed.append((view.state.current_tick, busy))
            if active_archive is None and busy == frozenset(actors):
                assert all(skill_values(view.state, actor) == skill_values(state, actor) for actor in actors)
                active_archive = export.export_world(scope, draft.world_id)
                active_hash = view.state_hash
            if repository.read_job(scope, job.job_id).status.value == "succeeded":
                break
            result = jobs.advance(scope, lease)
            if result.decision_requests:
                request, = result.decision_requests
                actor = request.actor_id
                arguments = (
                    {"place_id": str(state.entities[actor].location_id), "focus": "general"}
                    if action == "explore" else {"skill_id": skills[actor], "duration_ticks": 600}
                )
                plan = NpcPlan(
                    plan_id=uuid5(request.request_id, f"npc.plan.v1:{actor}:{request.state_version}"),
                    actor_id=actor, goal_ids=[goals[actor]], revision=1, status="ready",
                    created_tick=0, updated_tick=0, next_review_tick=0, current_step=0,
                    steps=[NpcPlanStep(capability_id=f"world.action.{action}.v1", arguments=arguments)],
                    reason="A deterministic policy for testing concurrent rule execution.",
                )
                result = jobs.advance(scope, lease, model_decision=DecisionResponse(
                    request_id=request.request_id, state_version=request.state_version, at_tick=0,
                    source="policy", option_id="plan", npc_planning_status="ready", npc_plan=plan,
                    npc_plan_state_hash=state_hash(view.state),
                ))
            events.extend(result.events)
        assert repository.read_job(scope, job.job_id).status.value == "succeeded"
        assert (0, frozenset(actors)) in observed
        assert [event.to_tick for event in events if event.type == "core.time_advanced.v1"] == [600]
        receipts = [event for event in events if getattr(event, result_field, None)]
        assert len(receipts) == 2
        assert all(getattr(event, result_field) == result_code and event.plan.updated_tick == 600 for event in receipts)
        final = repository.read_world(scope, draft.world_id, draft.branch_id)
        assert set(plan.status for plan in final.state.npc_plans) == {"completed"}
        evidence = {ref for plan in final.state.npc_plans for ref in plan.evidence_refs}
        assert {event.event_id for event in receipts} <= evidence
        if action == "train":
            for actor in actors:
                before, after = skill_values(state, actor), skill_values(final.state, actor)
                assert after["experience"] - before["experience"] == 2
                assert after["trained_ticks"] - before["trained_ticks"] == 600
                assert after["last_practiced_tick"] == 600
            assert len(evidence) == 6
        else:
            assert len(evidence) == 2
        assert repository.replay(scope, draft.world_id, draft.branch_id, from_genesis=True).state_hash == final.state_hash
        assert active_archive is not None
        restored = WorldRepository(restored_database)
        with export.validate_archive(active_archive) as verified:
            WorldBackupImporter(restored).import_verified_backup(scope, verified)
        assert restored.replay(scope, draft.world_id, draft.branch_id, from_genesis=True).state_hash == active_hash
        new_player = uuid4()
        restored.grant_membership(scope, draft.world_id, new_player, MembershipRole.PLAYER)
        restored.grant_actor_control(scope, draft.world_id, new_player, actors[0])
        restored_jobs = SimulationJobs(restored)
        followup = restored_jobs.enqueue_world_advance(
            scope, draft.world_id, draft.branch_id, WorldAdvancePlan(started_tick=0, target_tick=600),
            idempotency_key="restored-search", speed="balanced", depth="standard",
        )
        restore_lease = restored.claim_job("restored-search", job_type=followup.job_type, lease_seconds=180)
        assert restore_lease is not None
        interruption = restored_jobs.advance(scope, restore_lease)
        interrupted, = interruption.events
        assert interrupted.plan.actor_id == actors[0] and interrupted.control_interruption == "account_control"
        assert interrupted.plan.updated_tick == 0
        continued_events = []
        for _ in range(8):
            if restored.read_job(scope, followup.job_id).status.value == "succeeded":
                break
            continued_events.extend(restored_jobs.advance(scope, restore_lease).events)
        assert restored.read_job(scope, followup.job_id).status.value == "succeeded"
        continued_receipt, = [e for e in continued_events if getattr(e, result_field, None)]
        assert continued_receipt.plan.actor_id == actors[1] and continued_receipt.plan.updated_tick == 600
        continued = restored.read_world(scope, draft.world_id, draft.branch_id)
        assert skill_values(continued.state, actors[0]) == skill_values(state, actors[0])
        assert skill_values(continued.state, actors[1]) == skill_values(final.state, actors[1])
        assert restored.replay(scope, draft.world_id, draft.branch_id, from_genesis=True).state_hash == continued.state_hash
    finally:
        database.dispose()
        restored_database.dispose()
