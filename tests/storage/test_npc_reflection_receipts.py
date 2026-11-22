"""Durable, leased storage substrate for bounded NPC reflection receipts."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4, uuid5

import pytest

import world_engine.storage.repository as repository_module
from world_engine.application.visibility import project_event, project_player_state
from world_engine.domain.components import GoalRecord
from world_engine.domain.errors import Conflict, InvalidEvent
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    JobStatus,
    MembershipRole,
    NewJob,
    NewWorld,
    WorldEntity,
    WorldState,
)
from world_engine.domain.npc_plan_review import build_npc_plan_review_schedule
from world_engine.domain.reducers import state_hash
from world_engine.simulation import DecisionResponse, SimulationContext, SimulationEngine, WorldAdvancePlan
from world_engine.storage.database import Database
from world_engine.storage.models import ActorControlRow
from world_engine.storage.repository import WorldRepository


def _case(path, *, mode: str | None):
    database_url = f"sqlite:///{path.as_posix()}"
    database = Database(database_url)
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    world_id, branch_id, player_id, actor_id, goal_id = (uuid4() for _ in range(5))
    state = WorldState(
        player_id=player_id,
        entities={
            player_id: WorldEntity(id=player_id, kind="character", name="Player"),
            actor_id: WorldEntity(id=actor_id, kind="character", name="Ari"),
        },
        goals=[GoalRecord(
            id=goal_id, owner_id=actor_id, title="Find a safe road",
            created_tick=0, updated_tick=0,
        )],
    )
    schedule = build_npc_plan_review_schedule(state, world_id, actor_id, 0)
    state = state.model_copy(update={"scheduled_events": [schedule]})
    created = repository.create_world(scope, NewWorld(
        world_id=world_id,
        branch_id=branch_id,
        title="Reflection storage test",
        state=state,
    ))
    plan = WorldAdvancePlan(started_tick=0, target_tick=60)
    job_payload = {
        "plan": plan.model_dump(mode="json"),
        "speed": "balanced",
        "depth": "deep",
    }
    if mode is not None:
        job_payload["npc_reflection_mode"] = mode
    job = repository.enqueue_job(scope, NewJob(
        world_id=world_id,
        branch_id=branch_id,
        job_type="simulation.world_advance",
        idempotency_key=f"reflection-{uuid4()}",
        payload=job_payload,
    ), expected_state_version=0)
    lease = repository.claim_job("reflection-storage-worker", lease_seconds=120)
    assert lease is not None and lease.job_id == job.job_id

    current = repository.read_world(scope, world_id, branch_id)
    command_id = uuid4()
    context = SimulationContext(
        world_id=world_id,
        branch_id=branch_id,
        command_id=command_id,
        command_actor_id=scope.user_id,
        world_seed=str(world_id),
        state_version=0,
        ruleset_version=created.world.ruleset_version,
        speed="balanced",
        depth="deep",
    )
    engine = SimulationEngine()
    probe = engine.plan_window(current.state, plan, context)
    assert probe.outcome == "decision_required" and len(probe.decision_requests) == 1
    request, = probe.decision_requests
    response = DecisionResponse(
        request_id=request.request_id,
        state_version=request.state_version,
        at_tick=request.at_tick,
        source="model",
        option_id="plan",
        explanation="The stored choice is a typed test receipt.",
        npc_planning_status="defer",
        npc_plan_state_hash=state_hash(current.state),
        npc_model_call_id="reflection-test-call",
    )
    repository.record_model_decision(lease, response, request.state_version)
    resolution = engine.plan_window(
        current.state, plan, context.model_copy(update={"decision_responses": (response,)}),
    )
    assert resolution.outcome == "advanced"
    assert len(resolution.events) == 1
    assert resolution.events[0].type == "core.npc_plan_review_resolved.v1"
    command = CommandEnvelope(
        command_id=command_id,
        actor_id=scope.user_id,
        idempotency_key=f"reflection-window-{job.job_id}",
        expected_state_version=0,
        command_type="simulation.window",
        payload={"job_id": str(job.job_id), "next_tick": "0"},
    )
    return {
        "database": database,
        "database_url": database_url,
        "repository": repository,
        "scope": scope,
        "world_id": world_id,
        "branch_id": branch_id,
        "player_id": player_id,
        "actor_id": actor_id,
        "job": job,
        "lease": lease,
        "command": command,
        "events": resolution.events,
    }


def _commit_trigger(case, *, capture: bool = True):
    return case["repository"].commit_command(
        case["scope"], case["world_id"], case["branch_id"], case["command"], case["events"],
        lease=case["lease"], capture_npc_reflection=capture,
    )


def test_pinned_trigger_survives_repository_restart_and_commits_one_private_memory(tmp_path, monkeypatch):
    case = _case(tmp_path / "npc-reflection.sqlite", mode="salient_events_and_plans")
    repository = case["repository"]
    scope, lease = case["scope"], case["lease"]
    world_id, branch_id, actor_id = case["world_id"], case["branch_id"], case["actor_id"]
    try:
        committed = _commit_trigger(case)
        assert committed.state_version == 1
        assert committed.events[0].type == "core.npc_plan_review_resolved.v1"
        saved = repository.read_job(scope, case["job"].job_id)
        pending = saved.payload["pending_npc_reflection"]
        source_event_id = UUID(pending["source_event_id"])
        request_id = UUID(pending["request_id"])
        assert request_id == uuid5(case["job"].job_id, str(source_event_id))
        assert pending["source_kind"] == "npc_plan_review"
        assert UUID(pending["actor_id"]) == actor_id
        assert pending["tick"] == 0
        assert pending["source_state_version"] == committed.state_version
        assert pending["source_head_seq"] == committed.last_seq
        assert pending["source_sequence"] == committed.first_seq
        assert pending["result"] is None
        assert "reason" not in pending["source_evidence"]
        assert "claim" not in pending["source_evidence"]

        read = repository.read_pending_npc_reflection_source(scope, lease)
        assert read is not None
        assert read["evidence"] == {
            "status": "defer", "title": None, "predicate": None, "effective_tick": None,
        }
        assert read["result"] is None
        head_before_model_result = repository.read_world(scope, world_id, branch_id)
        assert (head_before_model_result.last_seq, head_before_model_result.state_hash) == (
            committed.last_seq, committed.state_hash,
        )

        assert repository.persist_npc_reflection_result(
            scope, lease,
            request_id=request_id,
            source_event_id=source_event_id,
            outcome="proposed",
            outlook="reconsider",
            model_call_id="reflection-readback-1",
        ) == "pending"
        assert repository.persist_npc_reflection_result(
            scope, lease,
            request_id=request_id,
            source_event_id=source_event_id,
            outcome="proposed",
            outlook="reconsider",
            model_call_id="reflection-readback-1",
        ) == "pending"
        with pytest.raises(Conflict):
            repository.persist_npc_reflection_result(
                scope, lease,
                request_id=request_id,
                source_event_id=source_event_id,
                outcome="proposed",
                outlook="carry_forward",
                model_call_id="reflection-readback-1",
            )
        accepted = repository.read_pending_npc_reflection_source(scope, lease)
        assert accepted is not None and accepted["result"] == {
            "outcome": "proposed", "outlook": "reconsider", "model_call_id": "reflection-readback-1",
        }

        database_url = case["database_url"]
        database = case["database"]
        database.dispose()
        reopened_database = Database(database_url)
        repository = WorldRepository(reopened_database)
        try:
            monkeypatch.setattr(repository_module, "utc_now", lambda: lease.expires_at + timedelta(seconds=1))
            recovered_lease = repository.claim_job("reflection-recovery-worker", lease_seconds=120)
            assert recovered_lease is not None and recovered_lease.job_id == case["job"].job_id
            assert recovered_lease.fencing_token > lease.fencing_token
            recovered = repository.read_pending_npc_reflection_source(scope, recovered_lease)
            assert recovered is not None
            assert recovered["request_id"] == str(request_id)
            assert recovered["source_event_id"] == str(source_event_id)
            assert recovered["result"]["outlook"] == "reconsider"

            memory_commit = repository.commit_pending_npc_reflection(scope, recovered_lease)
            assert memory_commit is not None and memory_commit.state_version == 2
            assert len(memory_commit.events) == 1
            memory_event, = memory_commit.events
            assert memory_event.type == "core.knowledge_acquired.v1"
            assert memory_event.visibility == {"policy": "author_only"}
            assert memory_event.causation_ids == [source_event_id]
            assert memory_event.payload["record"]["claim"] == "我会重新考虑原方法。"
            assert project_event(memory_event.model_dump(mode="json"), str(case["player_id"])) is None

            final = repository.read_world(scope, world_id, branch_id)
            assert final.state.current_tick == 0
            memory, = final.state.knowledge
            assert memory.owner_id == memory.holder_id == memory.subject_id == actor_id
            assert memory.kind == "memory"
            assert memory.visibility.policy == "private"
            assert memory.source_event_id == source_event_id
            assert memory.source_refs == [source_event_id]
            assert [(item.kind, item.reference_id) for item in memory.provenance] == [
                ("event", str(source_event_id)), ("rule", "npc.reflection.v1"),
            ]
            final_job = repository.read_job(scope, case["job"].job_id)
            assert "pending_npc_reflection" not in final_job.payload
            receipt = final_job.payload["last_npc_reflection"]
            assert receipt["request_id"] == str(request_id)
            assert receipt["source_event_id"] == str(source_event_id)
            assert receipt["outcome"] == "proposed" and receipt["outlook"] == "reconsider"
            assert receipt["memory_event_id"] == str(memory_event.event_id)
            assert "source_evidence" not in receipt and "claim" not in receipt
            assert repository.commit_pending_npc_reflection(scope, recovered_lease) is None
            assert repository.persist_npc_reflection_result(
                scope, recovered_lease,
                request_id=request_id,
                source_event_id=source_event_id,
                outcome="proposed",
                outlook="reconsider",
                model_call_id="reflection-readback-1",
            ) == "duplicate"

            player_view = project_player_state(final.state.model_dump(mode="json"), str(case["player_id"]))
            assert all(item.get("claim") != memory.claim for item in player_view["knowledge"])
            replayed = repository.replay(scope, world_id, branch_id, from_genesis=True)
            assert replayed.state_hash == final.state_hash
            assert replayed.state.knowledge == final.state.knowledge
            assert repository.read_job(scope, case["job"].job_id).status is JobStatus.RUNNING
        finally:
            reopened_database.dispose()
    finally:
        try:
            case["database"].dispose()
        except Exception:
            pass


@pytest.mark.parametrize(
    ("outcome", "outlook"),
    [("deferred", "defer"), ("unavailable", None)],
)
def test_deferred_or_unavailable_result_is_durable_and_world_noop(tmp_path, outcome, outlook):
    case = _case(tmp_path / f"npc-reflection-{outcome}.sqlite", mode="salient_events_and_plans")
    repository = case["repository"]
    try:
        _commit_trigger(case)
        pending = repository.read_job(case["scope"], case["job"].job_id).payload["pending_npc_reflection"]
        before = repository.read_world(case["scope"], case["world_id"], case["branch_id"])
        result = {
            "scope": case["scope"], "lease": case["lease"],
            "request_id": UUID(pending["request_id"]),
            "source_event_id": UUID(pending["source_event_id"]),
        }
        assert repository.persist_npc_reflection_result(
            result["scope"], result["lease"],
            request_id=result["request_id"], source_event_id=result["source_event_id"],
            outcome=outcome, outlook=outlook, model_call_id=None,
        ) == "cleared"
        assert repository.persist_npc_reflection_result(
            result["scope"], result["lease"],
            request_id=result["request_id"], source_event_id=result["source_event_id"],
            outcome=outcome, outlook=outlook, model_call_id=None,
        ) == "duplicate"
        job = repository.read_job(result["scope"], case["job"].job_id)
        assert "pending_npc_reflection" not in job.payload
        assert job.payload["last_npc_reflection"]["outcome"] == outcome
        after = repository.read_world(result["scope"], case["world_id"], case["branch_id"])
        assert (after.last_seq, after.branch.state_version, after.state_hash) == (
            before.last_seq, before.branch.state_version, before.state_hash,
        )
        assert after.state.knowledge == []
    finally:
        case["database"].dispose()


@pytest.mark.parametrize(
    ("mode", "capture"),
    [(None, True), ("salient_events_and_plans", False)],
)
def test_unpinned_or_unrequested_window_never_runs_reflection_selector(tmp_path, monkeypatch, mode, capture):
    case = _case(tmp_path / f"npc-reflection-gated-{mode}-{capture}.sqlite", mode=mode)
    try:
        import world_engine.domain.npc_reflection as reflection_module

        def forbidden(*_args, **_kwargs):
            raise AssertionError("an unattended or unrequested window invoked the reflection selector")

        monkeypatch.setattr(reflection_module, "select_npc_reflection_trigger", forbidden)
        _commit_trigger(case, capture=capture)
        payload = case["repository"].read_job(case["scope"], case["job"].job_id).payload
        assert "pending_npc_reflection" not in payload
    finally:
        case["database"].dispose()


def test_malformed_unpinned_mode_fails_closed_without_selector(tmp_path, monkeypatch):
    case = _case(tmp_path / "npc-reflection-malformed-mode.sqlite", mode=None)
    repository = case["repository"]
    try:
        with repository._transaction(write=True) as session:
            row = session.get(repository_module.JobRow, case["job"].job_id)
            row.payload_json = {**row.payload_json, "npc_reflection_mode": ["invalid"]}
        import world_engine.domain.npc_reflection as reflection_module

        def forbidden(*_args, **_kwargs):
            raise AssertionError("a malformed mode must fail closed before reflection selection")

        monkeypatch.setattr(reflection_module, "select_npc_reflection_trigger", forbidden)
        _commit_trigger(case, capture=True)
        assert "pending_npc_reflection" not in repository.read_job(
            case["scope"], case["job"].job_id,
        ).payload
    finally:
        case["database"].dispose()


def test_controlled_actor_cannot_receive_approved_memory(tmp_path):
    case = _case(tmp_path / "npc-reflection-controlled.sqlite", mode="salient_events_and_plans")
    repository = case["repository"]
    try:
        _commit_trigger(case)
        pending = repository.read_job(case["scope"], case["job"].job_id).payload["pending_npc_reflection"]
        request_id, source_event_id = UUID(pending["request_id"]), UUID(pending["source_event_id"])
        assert repository.persist_npc_reflection_result(
            case["scope"], case["lease"], request_id=request_id,
            source_event_id=source_event_id, outcome="proposed", outlook="carry_forward",
            model_call_id="reflection-readback-controlled",
        ) == "pending"
        next_user = uuid4()
        repository.grant_membership(
            case["scope"], case["world_id"], next_user, MembershipRole.PLAYER,
            controlled_actor_ids=[case["actor_id"]],
        )
        assert repository.commit_pending_npc_reflection(case["scope"], case["lease"]) is None
        job = repository.read_job(case["scope"], case["job"].job_id)
        assert "pending_npc_reflection" not in job.payload
        assert job.payload["last_npc_reflection"]["outcome"] == "unavailable"
        head = repository.read_world(case["scope"], case["world_id"], case["branch_id"])
        assert head.branch.state_version == 1 and head.state.knowledge == []
    finally:
        case["database"].dispose()


def test_unavailable_response_downgrades_a_saved_proposal_after_control_changes(tmp_path):
    case = _case(tmp_path / "npc-reflection-control-downgrade.sqlite", mode="salient_events_and_plans")
    repository = case["repository"]
    try:
        _commit_trigger(case)
        pending = repository.read_job(case["scope"], case["job"].job_id).payload["pending_npc_reflection"]
        request_id, source_event_id = UUID(pending["request_id"]), UUID(pending["source_event_id"])
        call_id = "reflection-readback-before-control-change"
        assert repository.persist_npc_reflection_result(
            case["scope"], case["lease"], request_id=request_id,
            source_event_id=source_event_id, outcome="proposed", outlook="carry_forward",
            model_call_id=call_id,
        ) == "pending"
        next_user = uuid4()
        repository.grant_membership(
            case["scope"], case["world_id"], next_user, MembershipRole.PLAYER,
            controlled_actor_ids=[case["actor_id"]],
        )
        source = repository.read_pending_npc_reflection_source(case["scope"], case["lease"])
        assert source is not None and source["evidence"] is None
        assert repository.persist_npc_reflection_result(
            case["scope"], case["lease"], request_id=request_id,
            source_event_id=source_event_id, outcome="unavailable", outlook=None,
            model_call_id=call_id,
        ) == "cleared"
        stored = repository.read_job(case["scope"], case["job"].job_id)
        assert "pending_npc_reflection" not in stored.payload
        assert stored.payload["last_npc_reflection"]["outcome"] == "unavailable"
        assert stored.payload["last_npc_reflection"]["response"]["outlook"] == "carry_forward"
        head = repository.read_world(case["scope"], case["world_id"], case["branch_id"])
        assert head.branch.state_version == 1 and head.state.knowledge == []
    finally:
        case["database"].dispose()
