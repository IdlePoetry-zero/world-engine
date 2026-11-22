from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.content import ContentCatalog
from world_engine.domain.models import (
    AccessScope,
    CalendarConfig,
    CommandEnvelope,
    EntityFieldChangedEvent,
    JobStatus,
    NewWorld,
    WorldEntity,
    WorldState,
)
from world_engine.simulation import SimulationEngine, WorldAdvancePlan
from world_engine.simulation.engine import SimulationPolicy
from world_engine.storage.database import Database
from world_engine.storage.models import SnapshotRow
from world_engine.storage.repository import WorldRepository


def _load_probe_runner():
    script = Path(__file__).resolve().parents[2] / "scripts" / "probe-world-advance.py"
    spec = importlib.util.spec_from_file_location("probe_world_advance_handoff_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("pause", ["storage_maintenance", "model_decision_failed"])
def test_resumable_handoff_releases_lock_without_replay_or_archive(tmp_path, monkeypatch, pause):
    runner = _load_probe_runner()
    parser = runner._build_parser(["modern_city"])
    args = parser.parse_args([
        "--pack", "modern_city", "--seed", "0", "--years", "0", "--days", "60", "--max-windows", "1",
        "--data-directory", str(tmp_path),
    ])
    catalog = ContentCatalog()
    runner.run(args, catalog)
    path = tmp_path / ".probe-world-advance.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["status"] == "paused"
    if pause == "storage_maintenance":
        runner._atomic_json(tmp_path / runner.MAINTENANCE_REQUEST_NAME, {"schema": 1, "job_id": manifest["job_id"]})
    else:
        connection = sqlite3.connect(tmp_path / "world-engine.db")
        try:
            connection.execute("UPDATE world_jobs SET result_json = ?", (json.dumps({"probe_pause": pause}),))
            connection.commit()
        finally:
            connection.close()

    def unexpected(*args, **kwargs):
        raise AssertionError("Paused job must release the probe lock before final verification.")

    monkeypatch.setattr(runner, "_storage_metrics", unexpected)
    monkeypatch.setattr(runner, "_sqlite_integrity", unexpected)
    monkeypatch.setattr(runner, "_archive_metrics", unexpected)
    monkeypatch.setattr(runner.WorldRepository, "replay", unexpected)
    args.audit_from_genesis = True
    report, code = runner.run(args, catalog)

    assert code == 2 and report["status"] == pause
    assert report["job_id"] == manifest["job_id"]
    assert report["committed_tick"] == manifest["last_tick"]
    assert report["state_hash"] == manifest["last_state_hash"]
    assert not report["simulation_completed"]
    with runner._directory_lock(tmp_path / ".probe-world-advance.lock"):
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["last_state_hash"] == report["state_hash"]


def test_probe_persists_the_expired_decision_boundary_without_advancing_the_world(tmp_path, monkeypatch):
    runner = _load_probe_runner()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    database = Database(f"sqlite:///{(tmp_path / 'deadline.db').as_posix()}")
    database.create_schema()
    try:
        repository = WorldRepository(database)
        created = repository.create_world(scope, NewWorld(title="Deadline", state=WorldState()))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        jobs = SimulationJobs(repository)
        job = jobs.enqueue_world_advance(
            scope, world_id, branch_id, WorldAdvancePlan(started_tick=0, target_tick=60),
            idempotency_key="deadline", speed="balanced", depth="deep", expected_state_version=0,
        )
        decision = SimpleNamespace(
            kind="governance.succession", decision_owner="model", options=("candidate",),
            request_id=uuid4(), state_version=0, at_tick=0,
        )
        boundary = SimpleNamespace(outcome="decision_required", decision_requests=(decision,))

        async def delayed_provider(*args, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(runner, "invoke_model_decision", delayed_provider)
        monkeypatch.setattr(runner, "MODEL_DECISION_TIMEOUT_SECONDS", 0.01)
        before = repository.read_world(scope, world_id, branch_id)
        result, outcomes, reasons, windows = runner._advance_job(
            repository=repository, scope=scope, jobs=SimpleNamespace(advance=lambda *args: boundary),
            job=job, world_id=world_id, branch_id=branch_id, start_tick=0, target_tick=60,
            day_seconds=1, year_days=3, max_windows=None, lease_wait_seconds=0,
            manifest={"checkpoints": []}, manifest_path=tmp_path / "manifest.json",
            decision_mode="configured_local_model", model_operations=object(), model_scope=scope,
        )
        assert result.status is JobStatus.PAUSED and result.committed_through_tick == 0
        durable = repository.read_job(scope, job.job_id)
        assert durable.result == {
            "probe_pause": "model_decision_failed",
            "model_host_deadline": {"request_id": str(decision.request_id), "state_version": 0, "tick": 0},
        }
        assert outcomes["model_host_deadline_expirations"] == 1
        assert reasons == ["model_decision_failed"] and windows == 1
        assert repository.read_world(scope, world_id, branch_id).state_hash == before.state_hash
    finally:
        database.dispose()


def test_probe_persists_exact_decade_handoffs_without_early_manifest_registration(tmp_path):
    runner = _load_probe_runner()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player_id, witness_id = uuid4(), uuid4()
    state = WorldState(
        player_id=player_id,
        entities={
            player_id: WorldEntity(
                id=player_id,
                kind="character",
                name="Boundary witness",
                components={"life": {"birth_tick": 0, "lifespan_ticks": 30}},
            ),
            witness_id: WorldEntity(
                id=witness_id,
                kind="character",
                name="Living witness",
            ),
        },
        calendar=CalendarConfig(day_seconds=1),
    )
    database = Database(f"sqlite:///{(tmp_path / 'handoff.db').as_posix()}")
    database.create_schema()
    try:
        repository = WorldRepository(database)
        created = repository.create_world(scope, NewWorld(title="Checkpoint handoff", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        jobs = SimulationJobs(
            repository,
            SimulationEngine(policy=SimulationPolicy(max_macro_step_seconds=60)),
        )
        job = jobs.enqueue_world_advance(
            scope,
            world_id,
            branch_id,
            WorldAdvancePlan(started_tick=0, target_tick=60),
            idempotency_key="decade-handoff",
            speed="balanced",
            depth="deep",
            expected_state_version=0,
        )

        manifest = {"checkpoints": []}
        manifest_path = tmp_path / "manifest.json"
        original_advance = jobs.advance
        observations = []

        def snapshot_rows():
            with database.session_factory() as session:
                return list(session.scalars(
                    select(SnapshotRow)
                    .where(SnapshotRow.branch_id == branch_id)
                    .order_by(SnapshotRow.last_seq)
                ))

        def observe_then_advance(scope_arg, lease, **kwargs):
            live = repository.read_world(scope_arg, world_id, branch_id)
            if live.state.current_tick == 30:
                observations.append({
                    "snapshots": [
                        (row.current_tick, row.state_version, row.last_seq)
                        for row in snapshot_rows()
                    ],
                    "checkpoints": json.loads(manifest_path.read_text(encoding="utf-8"))[
                        "checkpoints"
                    ],
                })
                # Hold the worker on the exact decade boundary for one
                # same-tick observation, then commit a same-tick rename before
                # allowing the clock to proceed. Key the injection to boundary
                # observations rather than total macro-window calls: deep mode
                # can now use more windows before year 10.
                if len(observations) == 1:
                    return repository.read_job(scope_arg, job.job_id)
                if len(observations) == 2:
                    live = repository.read_world(scope_arg, world_id, branch_id)
                    repository.commit_command(
                        scope_arg,
                        world_id,
                        branch_id,
                        CommandEnvelope(
                            actor_id=scope_arg.user_id,
                            idempotency_key="same-tick-event",
                            expected_state_version=live.branch.state_version,
                            command_type="core.rename",
                        ),
                        [EntityFieldChangedEvent(
                            entity_id=witness_id,
                            field_name="name",
                            value="Still living witness",
                        )],
                        lease=lease,
                    )
                    return repository.read_job(scope_arg, job.job_id)
            return original_advance(scope_arg, lease, **kwargs)

        jobs.advance = observe_then_advance
        original_snapshot = repository.snapshot
        handoff_snapshot_requests = []

        def count_handoff_snapshot(scope_arg, world_arg, branch_arg, *, at_tick=None):
            if at_tick is None:
                handoff_snapshot_requests.append((world_arg, branch_arg))
            return original_snapshot(scope_arg, world_arg, branch_arg, at_tick=at_tick)

        repository.snapshot = count_handoff_snapshot
        completed, _outcomes, _stops, _windows = runner._advance_job(
            repository=repository,
            scope=scope,
            jobs=jobs,
            job=job,
            world_id=world_id,
            branch_id=branch_id,
            start_tick=0,
            target_tick=60,
            day_seconds=1,
            year_days=3,
            max_windows=None,
            lease_wait_seconds=0,
            manifest=manifest,
            manifest_path=manifest_path,
        )

        assert completed.status is JobStatus.SUCCEEDED
        assert [len(item["snapshots"]) for item in observations] == [1, 1, 2]
        first_boundary_version = observations[0]["snapshots"][0][1]
        assert [
            [(tick, version) for tick, version, _sequence in item["snapshots"]]
            for item in observations
        ] == [
            [(30, first_boundary_version)],
            [(30, first_boundary_version)],
            [(30, first_boundary_version), (30, first_boundary_version + 1)],
        ]
        assert all(item["checkpoints"] == [] for item in observations)
        assert len(handoff_snapshot_requests) == 2

        checkpoints = manifest["checkpoints"]
        assert [(item["scheduled_year"], item["tick"]) for item in checkpoints] == [
            (10, "30"),
            (20, "60"),
        ]
        boundary_checkpoint = checkpoints[0]
        exact_boundary = repository.snapshot(scope, world_id, branch_id, at_tick=30)
        assert boundary_checkpoint["sequence"] == str(exact_boundary.last_seq)
        settled_boundary_version = first_boundary_version + 1
        assert boundary_checkpoint["state_version"] == str(exact_boundary.state_version) == str(
            settled_boundary_version
        )
        assert boundary_checkpoint["state_hash"] == exact_boundary.state_hash

        stored_at_boundary = [row for row in snapshot_rows() if row.current_tick == 30]
        assert [(row.state_version, row.last_seq) for row in stored_at_boundary] == [
            (first_boundary_version, observations[0]["snapshots"][0][2]),
            (settled_boundary_version, observations[2]["snapshots"][1][2]),
        ]
    finally:
        database.dispose()
