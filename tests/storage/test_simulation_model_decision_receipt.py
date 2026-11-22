"""Crash recovery and immutability coverage for simulation decision receipts."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4, uuid5

import pytest

import world_engine.storage.repository as repository_module
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.components import GoalRecord, LifeComponent
from world_engine.domain.errors import Conflict, InvalidLease, StaleVersion
from world_engine.domain.gameplay_events import GovernanceSuccessionEvent, LifeEndedEvent
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    EntityFieldChangedEvent,
    NewWorld,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.domain.npc_plan_review import build_npc_plan_review_schedule
from world_engine.domain.npc_plans import NpcPlan, NpcPlanStep
from world_engine.domain.reducers import state_hash
from world_engine.simulation import LongActionPlan
from world_engine.simulation.contracts import DecisionResponse, WorldAdvancePlan
from world_engine.simulation.npc_capabilities import capability_id_for_action
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def _new_database(path):
    database = Database(f"sqlite:///{path.as_posix()}")
    database.create_schema()
    return database


def _succession_case(path):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database = _new_database(path)
    repository = WorldRepository(database)
    player_id, leader_id, successor_id, organization_id = uuid4(), uuid4(), uuid4(), uuid4()
    created = repository.create_world(scope, NewWorld(
        title="Model succession receipt recovery",
        state=WorldState(
            player_id=player_id,
            entities={
                player_id: WorldEntity(id=player_id, kind="character", name="Observer"),
                leader_id: WorldEntity(
                    id=leader_id, kind="character", name="Former chair",
                    components={"life": LifeComponent(birth_tick=0, lifespan_ticks=60).model_dump(mode="json")},
                ),
                successor_id: WorldEntity(id=successor_id, kind="character", name="Successor"),
                organization_id: WorldEntity(
                    id=organization_id, kind="organization", name="Council",
                    components={"governance": {
                        "members": [str(leader_id), str(successor_id)],
                        "leader_id": str(leader_id), "succession_policy": "election",
                    }},
                ),
            },
        ),
    ))
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    jobs = SimulationJobs(repository)
    job = jobs.enqueue(
        scope, world_id, branch_id,
        LongActionPlan(actor_id=player_id, started_tick=0, target_tick=120, activity="wait"),
        idempotency_key="model-succession-receipt", speed="balanced", depth="deep",
        expected_state_version=0,
    )
    lease = repository.claim_job("succession-first-worker", lease_seconds=120)
    assert lease is not None and lease.job_id == job.job_id

    first = jobs.advance(scope, lease)
    assert first.outcome == "advanced" and first.next_tick == 60
    death_events = [event for event in first.events if isinstance(event, LifeEndedEvent)]
    assert len(death_events) == 1 and death_events[0].entity_id == leader_id
    waiting = jobs.advance(scope, lease)
    assert waiting.outcome == "decision_required" and waiting.events == ()
    assert len(waiting.decision_requests) == 1
    decision, = waiting.decision_requests
    assert decision.kind == "organization.succession"
    assert decision.decision_owner == "model"
    successor_option = next(option for option in decision.options if option.option_id != "leave_vacant")
    return {
        "scope": scope,
        "database": database,
        "path": path,
        "repository": repository,
        "jobs": jobs,
        "lease": lease,
        "job": job,
        "decision": decision,
        "response": DecisionResponse(
            request_id=decision.request_id,
            state_version=decision.state_version,
            at_tick=decision.at_tick,
            source="model",
            option_id=successor_option.option_id,
            explanation="The elected successor is the active member.",
        ),
        "player_id": player_id,
        "leader_id": leader_id,
        "successor_id": successor_id,
        "organization_id": organization_id,
        "death_event": death_events[0],
    }


def test_model_succession_receipt_survives_reopen_and_is_consumed_once(tmp_path, monkeypatch):
    case = _succession_case(tmp_path / "succession-receipt.sqlite")
    database = case["database"]
    repository = case["repository"]
    scope, lease, decision, response = case["scope"], case["lease"], case["decision"], case["response"]
    world_id, branch_id, job_id = case["job"].world_id, case["job"].branch_id, case["job"].job_id
    death_view = repository.read_world(scope, world_id, branch_id)
    assert death_view.state.current_tick == 60
    assert death_view.state.entities[case["leader_id"]].status == "dead"
    pre_record = (death_view.last_seq, death_view.state_hash, death_view.branch.state_version)

    assert repository.record_model_decision(lease, response, decision.state_version) == "pending"
    assert repository.record_model_decision(lease, response, decision.state_version) == "pending"
    with pytest.raises(Conflict):
        repository.record_model_decision(
            lease, response.model_copy(update={"option_id": "leave_vacant"}), decision.state_version,
        )
    other_request = response.model_copy(update={"request_id": uuid4()})
    with pytest.raises(Conflict):
        repository.record_model_decision(lease, other_request, decision.state_version)
    after_record = repository.read_world(scope, world_id, branch_id)
    assert (after_record.last_seq, after_record.state_hash, after_record.branch.state_version) == pre_record
    assert repository.read_job(scope, job_id).payload["pending_decision"] == response.model_dump(mode="json")

    database.dispose()
    reopened = Database(f"sqlite:///{case['path'].as_posix()}")
    repository = WorldRepository(reopened)
    jobs = SimulationJobs(repository)
    try:
        receipt_job = repository.read_job(scope, job_id)
        assert DecisionResponse.model_validate(receipt_job.payload["pending_decision"]) == response
        expired_at = lease.expires_at + timedelta(seconds=1)
        monkeypatch.setattr(repository_module, "utc_now", lambda: expired_at)
        replacement = repository.claim_job("succession-recovery-worker", lease_seconds=120)
        assert replacement is not None and replacement.job_id == job_id
        assert replacement.fencing_token > lease.fencing_token

        with pytest.raises(InvalidLease):
            repository.record_model_decision(lease, response, decision.state_version)
        with pytest.raises(InvalidLease):
            jobs.advance(scope, lease, model_decision=response)
        before_recovery = repository.read_world(scope, world_id, branch_id)
        assert (before_recovery.last_seq, before_recovery.state_hash) == pre_record[:2]

        recovered = jobs.advance(scope, replacement)
        assert recovered.outcome == "advanced" and recovered.next_tick == 60
        succession_events = [event for event in recovered.events if isinstance(event, GovernanceSuccessionEvent)]
        assert len(succession_events) == 1
        succession, = succession_events
        assert succession.predecessor_id == case["leader_id"]
        assert succession.successor_id == case["successor_id"]
        assert succession.source_death_event_id == case["death_event"].event_id
        assert succession.decision_request_id == decision.request_id
        assert succession.decision_option_id == f"leader_holder_{case['successor_id'].hex}"

        after_succession = repository.read_world(scope, world_id, branch_id)
        assert after_succession.state.current_tick == 60
        assert after_succession.state.entities[case["organization_id"]].components["governance"]["leader_id"] == str(
            case["successor_id"]
        )
        assert after_succession.last_seq == pre_record[0] + 1
        assert after_succession.state_hash != pre_record[1]
        stored_job = repository.read_job(scope, job_id)
        assert "pending_decision" not in stored_job.payload
        assert stored_job.payload["last_decision"] == response.model_dump(mode="json")

        assert repository.record_model_decision(
            replacement, response, after_succession.branch.state_version,
        ) == "committed"
        confirmation = jobs.advance(scope, replacement, model_decision=response)
        assert confirmation.status.value == "running"
        unchanged = repository.read_world(scope, world_id, branch_id)
        assert (unchanged.last_seq, unchanged.state_hash, unchanged.state.current_tick) == (
            after_succession.last_seq, after_succession.state_hash, after_succession.state.current_tick,
        )

        completed = jobs.advance(scope, replacement)
        assert completed.outcome == "completed" and completed.next_tick == 120
        final = repository.read_world(scope, world_id, branch_id)
        events = repository.list_events(scope, world_id, branch_id).events
        assert [event.sequence for event in events] == list(range(1, final.last_seq + 1))
        assert sum(event.type == "core.governance_succession.v1" for event in events) == 1
        assert final.state.current_tick == 120
        assert final.state.entities[case["leader_id"]].status == "dead"
        assert final.state.entities[case["organization_id"]].components["governance"]["leader_id"] == str(
            case["successor_id"]
        )
        assert repository.read_job(scope, job_id).payload["last_decision"] == response.model_dump(mode="json")
        replayed = repository.replay(scope, world_id, branch_id, from_genesis=True)
        assert replayed.state_hash == final.state_hash
        assert replayed.state.current_tick == final.state.current_tick
        assert replayed.state.entities[case["organization_id"]].components["governance"]["leader_id"] == str(
            case["successor_id"]
        )
    finally:
        reopened.dispose()


def test_model_decision_receipts_reject_stale_branch_and_clock_without_writing(tmp_path):
    case = _succession_case(tmp_path / "stale-receipt.sqlite")
    repository, database = case["repository"], case["database"]
    scope, lease, decision, response = case["scope"], case["lease"], case["decision"], case["response"]
    world_id, branch_id = case["job"].world_id, case["job"].branch_id
    try:
        before = repository.read_world(scope, world_id, branch_id)
        repository.commit_command(
            scope, world_id, branch_id,
            CommandEnvelope(
                actor_id=case["player_id"], idempotency_key="invalidate-branch-version",
                expected_state_version=before.branch.state_version, command_type="fixture.rename",
            ),
            [EntityFieldChangedEvent(entity_id=case["successor_id"], field_name="name", value="Changed member")],
            lease=lease,
        )
        changed = repository.read_world(scope, world_id, branch_id)
        assert changed.state.current_tick == before.state.current_tick
        with pytest.raises(StaleVersion):
            repository.record_model_decision(lease, response, decision.state_version)
        assert "pending_decision" not in repository.read_job(scope, case["job"].job_id).payload

        repository.commit_command(
            scope, world_id, branch_id,
            CommandEnvelope(
                actor_id=case["player_id"], idempotency_key="advance-clock-outside-job",
                expected_state_version=changed.branch.state_version, command_type="fixture.tick",
            ),
            [TimeAdvancedEvent(to_tick=changed.state.current_tick + 1)],
            lease=lease,
        )
        moved = repository.read_world(scope, world_id, branch_id)
        stale_tick = response.model_copy(update={
            "state_version": moved.branch.state_version,
            "at_tick": moved.state.current_tick - 1,
        })
        with pytest.raises(Conflict):
            repository.record_model_decision(lease, stale_tick, moved.branch.state_version)
        final = repository.read_world(scope, world_id, branch_id)
        assert (final.last_seq, final.state_hash, final.state.current_tick) == (
            moved.last_seq, moved.state_hash, moved.state.current_tick,
        )
        assert "pending_decision" not in repository.read_job(scope, case["job"].job_id).payload
    finally:
        database.dispose()


def test_typed_npc_receipt_optional_fields_round_trip_and_are_immutable(tmp_path):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database = _new_database(tmp_path / "npc-receipt.sqlite")
    repository = WorldRepository(database)
    world_id, player_id, actor_id = uuid4(), uuid4(), uuid4()
    goal = GoalRecord(
        id=uuid4(), owner_id=actor_id, title="Reach the coast", description="Travel safely.",
        created_tick=0, updated_tick=0,
    )
    state = WorldState(
        player_id=player_id,
        entities={
            player_id: WorldEntity(id=player_id, kind="character", name="Player"),
            actor_id: WorldEntity(id=actor_id, kind="character", name="Ari"),
        },
        goals=[goal],
    )
    review = build_npc_plan_review_schedule(state, world_id, actor_id, 0)
    state = state.model_copy(update={"scheduled_events": [review]})
    created = repository.create_world(scope, NewWorld(
        world_id=world_id, title="NPC receipt fields", state=state,
    ))
    jobs = SimulationJobs(repository)
    job = jobs.enqueue_world_advance(
        scope, world_id, created.branch.branch_id, WorldAdvancePlan(started_tick=0, target_tick=60),
        idempotency_key="npc-typed-receipt", speed="balanced", depth="deep", expected_state_version=0,
    )
    lease = repository.claim_job("npc-receipt-worker", lease_seconds=120)
    assert lease is not None
    request_result = jobs.advance(scope, lease)
    assert request_result.outcome == "decision_required"
    request, = request_result.decision_requests
    assert request.kind == "npc.plan_review" and request.options[0].option_id == "plan"
    current = repository.read_world(scope, world_id, created.branch.branch_id)
    plan = NpcPlan(
        plan_id=uuid5(request.request_id, f"npc.plan.v1:{actor_id}:{request.state_version}"),
        actor_id=actor_id, goal_ids=[goal.id], revision=1, status="ready",
        created_tick=request.at_tick, updated_tick=request.at_tick, next_review_tick=request.at_tick,
        steps=[NpcPlanStep(capability_id=capability_id_for_action("explore"), arguments={})],
        current_step=0, reason="Follow the active travel goal.",
    )
    typed = DecisionResponse(
        request_id=request.request_id, state_version=request.state_version, at_tick=request.at_tick,
        source="model", option_id="plan", explanation="Ready after bounded preflight.",
        npc_planning_status="ready", npc_plan=plan, npc_plan_state_hash=state_hash(current.state),
        npc_model_call_id="npc-review-trace-1",
    )
    try:
        assert repository.record_model_decision(lease, typed, request.state_version) == "pending"
        stored = repository.read_job(scope, job.job_id).payload["pending_decision"]
        round_trip = DecisionResponse.model_validate(stored)
        assert round_trip == typed
        assert round_trip.npc_plan == plan
        assert round_trip.npc_plan_state_hash == state_hash(current.state)
        assert round_trip.npc_model_call_id == "npc-review-trace-1"

        with pytest.raises(Conflict):
            repository.record_model_decision(
                lease, typed.model_copy(update={"npc_model_call_id": "npc-review-trace-2"}),
                request.state_version,
            )
        revised_plan = plan.model_copy(update={"reason": "Different plan text."})
        with pytest.raises(Conflict):
            repository.record_model_decision(
                lease, typed.model_copy(update={"npc_plan": revised_plan}), request.state_version,
            )
        still_stored = repository.read_job(scope, job.job_id).payload["pending_decision"]
        assert DecisionResponse.model_validate(still_stored) == typed
        assert repository.read_world(scope, world_id, created.branch.branch_id).state_hash == current.state_hash
    finally:
        database.dispose()

