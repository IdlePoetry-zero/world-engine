"""Archive restore and replay for lease-committed NPC plan transactions."""

from __future__ import annotations

import io
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import select

from world_engine.application.backups import BackupApplication
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.content import ContentCatalog
from world_engine.domain.models import AccessScope, WorldState
from world_engine.domain.npc_plan_review import build_npc_plan_review_schedule
from world_engine.domain.npc_plans import NpcPlan
from world_engine.domain.reducers import state_hash
from world_engine.simulation import WorldAdvancePlan
from world_engine.simulation.contracts import DecisionResponse
from world_engine.simulation.npc_capabilities import preflight_npc_plan_step
from world_engine.rules.contracts import RuleContext
from world_engine.storage.backup_import import (
    WorldBackupImporter,
    _prior_journey_resolution_receipts,
)
from world_engine.storage.database import Database
from world_engine.storage.models import CommandRow, EventRow
from world_engine.storage.repository import WorldRepository


def _leased_route_world():
    draft = ContentCatalog().build_world("modern_city", "NPC archive replay", "Player", seed=23)
    state = draft.state
    for goal in state.goals:
        actor = state.entities[goal.owner_id]
        if actor.kind != "character" or actor.id == state.player_id or actor.location_id is None:
            continue
        place = state.entities[actor.location_id].components.get("place", {})
        for route in place.get("routes", []):
            from world_engine.domain.npc_plans import NpcPlanStep

            # This test covers the successful completion and archive replay
            # path. The generated modern-city routes all carry a nonzero risk,
            # so make the selected fixture route safe instead of relying on a
            # probabilistic roll to avoid an interruption.
            route["risk"] = "0"
            step = NpcPlanStep(capability_id="world.action.move.v1", arguments={
                "destination_id": route["to_place_id"], "route_id": route["route_id"],
            })
            checked = preflight_npc_plan_step(state, actor.id, step, RuleContext(
                world_id=draft.world_id,
                branch_id=draft.branch_id,
                command_id=uuid4(),
                actor_id=actor.id,
                ruleset_version=draft.ruleset_version,
            ))
            if checked.status == "journey_start":
                schedule = build_npc_plan_review_schedule(
                    state, draft.world_id, actor.id, 0, repeat_ticks=3600, max_runs=2,
                )
                payload = state.model_dump(mode="python")
                payload["scheduled_events"] = [schedule, *state.scheduled_events]
                return draft, WorldState.model_validate(payload), actor.id, goal.id, step
    raise AssertionError("The full content world must provide an NPC with a legal route.")


def _run_leased_route(repository, scope):
    draft, state, actor_id, goal_id, step = _leased_route_world()
    raw = state.model_dump(mode="python")
    raw["scheduled_events"][0].update(max_runs=1, repeat_ticks=None)
    initial = WorldState.model_validate(raw)
    route_id = str(step.arguments["route_id"])
    origin_id = initial.entities[actor_id].location_id
    route = next(
        item for item in initial.entities[origin_id].components["place"]["routes"]
        if item["route_id"] == route_id
    )
    target_tick = route["travel_ticks"]
    repository.create_world(scope, draft.model_copy(update={"state": initial}))
    jobs = SimulationJobs(repository)
    job = jobs.enqueue_world_advance(
        scope,
        draft.world_id,
        draft.branch_id,
        WorldAdvancePlan(started_tick=0, target_tick=target_tick),
        idempotency_key="npc-plan-archive-replay",
        speed="balanced",
        depth="standard",
    )
    lease = repository.claim_job("npc-plan-archive", job_type=job.job_type)
    pending = jobs.advance(scope, lease)
    request, = pending.decision_requests
    reviewed = repository.read_world(scope, draft.world_id, draft.branch_id)
    plan = NpcPlan(
        plan_id=uuid5(request.request_id, f"npc.plan.v1:{actor_id}:{request.state_version}"),
        actor_id=actor_id,
        goal_ids=[goal_id],
        revision=1,
        status="ready",
        created_tick=0,
        updated_tick=0,
        next_review_tick=0,
        current_step=0,
        steps=[step],
        reason="Deterministic test policy for the archive replay transaction.",
    )
    response = DecisionResponse(
        request_id=request.request_id,
        state_version=request.state_version,
        at_tick=0,
        source="policy",
        option_id="plan",
        npc_planning_status="ready",
        npc_plan=plan,
        npc_plan_state_hash=state_hash(reviewed.state),
    )
    jobs.advance(scope, lease, model_decision=response)
    jobs.advance(scope, lease)
    for _ in range(30):
        current = repository.read_job(scope, job.job_id)
        if current.status != "running":
            break
        jobs.advance(scope, lease)
    result = repository.read_world(scope, draft.world_id, draft.branch_id)
    assert repository.read_job(scope, job.job_id).status == "succeeded"
    assert result.state.current_tick == target_tick
    journey = result.state.travel_journeys[0]
    assert journey.status == "arrived", (
        f"risk-free archive replay fixture journey ended as {journey.status!r} "
        f"at tick {result.state.current_tick} (scheduled arrival {journey.arrival_tick})"
    )
    assert str(result.state.entities[actor_id].location_id) == step.arguments["destination_id"]
    assert result.state.npc_plans[0].status == "completed"
    assert result.state.npc_plans[0].active_execution is None
    return draft.world_id, draft.branch_id, result


def _import_archive(application, repository, scope, archive: bytes, *, streaming: bool):
    importer = WorldBackupImporter(repository)
    if streaming:
        with application.validate_archive_file_streaming(io.BytesIO(archive)) as verified:
            return importer.import_verified_backup(scope, verified)
    return importer.import_verified_backup(scope, application.validate_archive(archive))


def _event_context_rows(repository, world_id):
    with repository.database.session_factory() as session:
        rows = session.scalars(
            select(EventRow)
            .where(EventRow.world_id == world_id)
            .order_by(EventRow.branch_id, EventRow.sequence)
        ).all()
        return tuple(
            (
                row.event_id,
                row.world_id,
                row.branch_id,
                row.sequence,
                row.command_id,
                row.actor_id,
                row.ruleset_version,
                row.tick,
                row.state_version_after,
                row.commit_boundary,
            )
            for row in rows
        )


@pytest.mark.parametrize("first_restore_streaming", [False, True])
def test_actual_npc_plan_transaction_survives_genesis_and_two_restore_cycles(
    tmp_path, first_restore_streaming,
):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    databases = [
        Database(f"sqlite:///{(tmp_path / f"npc-archive-{index}.db").as_posix()}")
        for index in range(3)
    ]
    for database in databases:
        database.create_schema()
    source, restored_once, restored_twice = [WorldRepository(database) for database in databases]
    try:
        world_id, branch_id, source_head = _run_leased_route(source, scope)
        source_event_contexts = _event_context_rows(source, world_id)
        source_genesis = source.replay(scope, world_id, branch_id, from_genesis=True)
        assert source_genesis.state_hash == source_head.state_hash

        journey = source_head.state.travel_journeys[0]
        arrival, = source.find_journey_resolution_events(scope, world_id, branch_id, {journey.journey_id}).values()
        with source.database.session_factory() as session:
            arrival_row = session.scalar(
                select(EventRow).where(EventRow.event_id == arrival.event_id)
            )
        assert arrival_row is not None
        arrival_sequence = arrival_row.sequence
        pre_arrival = source.replay(
            scope, world_id, branch_id, through_seq=arrival_sequence - 1, from_genesis=True,
        )
        assert pre_arrival.state.npc_plans[0].active_execution.journey_id == journey.journey_id
        root = SimpleNamespace(branch_id=branch_id, parent_branch_id=None, fork_seq=0)
        before_arrival_child = SimpleNamespace(
            branch_id=uuid4(), parent_branch_id=branch_id, fork_seq=arrival_sequence - 1,
        )
        after_arrival_child = SimpleNamespace(
            branch_id=uuid4(), parent_branch_id=branch_id, fork_seq=arrival_sequence,
        )
        lineage = {
            branch_id: root,
            before_arrival_child.branch_id: before_arrival_child,
            after_arrival_child.branch_id: after_arrival_child,
        }
        verified_receipts = {branch_id: [(arrival_sequence, arrival)]}
        # A future parent receipt stays hidden behind an earlier child fork,
        # even if a caller has already scanned and cached the later record.
        assert journey.journey_id not in _prior_journey_resolution_receipts(
            lineage, verified_receipts, before_arrival_child,
            arrival_sequence + 100, pre_arrival.state,
        )
        inherited = _prior_journey_resolution_receipts(
            lineage, verified_receipts, after_arrival_child,
            arrival_sequence + 1, pre_arrival.state,
        )
        assert inherited[journey.journey_id].event_id == arrival.event_id

        source_backups = BackupApplication(source)
        first_archive = source_backups.export_world(scope, world_id)
        _import_archive(
            source_backups, restored_once, scope, first_archive,
            streaming=first_restore_streaming,
        )
        first_replay = restored_once.replay(scope, world_id, branch_id, from_genesis=True)
        assert _event_context_rows(restored_once, world_id) == source_event_contexts
        assert first_replay.state_hash == source_head.state_hash
        assert first_replay.state.current_tick == source_head.state.current_tick
        assert first_replay.state.npc_plans[0].status == "completed"
        assert first_replay.state.travel_journeys[0].status == "arrived"

        second_backups = BackupApplication(restored_once)
        second_archive = second_backups.export_world(scope, world_id)
        _import_archive(
            second_backups, restored_twice, scope, second_archive,
            streaming=not first_restore_streaming,
        )
        second_replay = restored_twice.replay(scope, world_id, branch_id, from_genesis=True)
        assert _event_context_rows(restored_twice, world_id) == source_event_contexts
        assert second_replay.state_hash == source_head.state_hash
        assert second_replay.state.current_tick == source_head.state.current_tick
        assert second_replay.state.npc_plans[0].status == "completed"
        assert second_replay.state.travel_journeys[0].status == "arrived"

        with restored_once.database.session_factory() as session:
            commands = session.scalars(
                select(CommandRow).where(CommandRow.world_id == world_id)
            ).all()
            event_count = len(source_event_contexts)
            assert commands
            assert all(row.command_type == "backup.restored_event_batch" for row in commands)
            assert all(
                row.payload_json["source_request_payload_available"] is False
                and row.payload_json["source_command_id"] == str(row.command_id)
                for row in commands
            )
            assert event_count > 0
    finally:
        for database in databases:
            database.dispose()
