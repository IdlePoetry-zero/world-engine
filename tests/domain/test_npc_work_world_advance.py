"""Normal leased world advancement starts and settles a registered NPC shift."""

from uuid import uuid4

import pytest

from world_engine.application.backups import BackupApplication
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.models import AccessScope, JobStatus, MembershipRole, NewWorld
from world_engine.simulation import WorldAdvancePlan
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository

from test_npc_attendance import _state
from test_work_shift import DURATION, WAGE, WORKER_ID


def _advance(repository, scope, world_id, branch_id, target):
    before = repository.read_world(scope, world_id, branch_id)
    jobs = SimulationJobs(repository)
    job = jobs.enqueue_world_advance(
        scope, world_id, branch_id, WorldAdvancePlan(started_tick=before.state.current_tick, target_tick=target),
        idempotency_key=f"advance:{target}", speed="balanced", depth="standard",
    )
    lease = repository.claim_job(f"work-test:{target}", job_type=job.job_type)
    assert lease.job_id == job.job_id
    for _ in range(8):
        jobs.advance(scope, lease)
        current = repository.read_job(scope, job.job_id)
        if current.status != JobStatus.RUNNING:
            break
    assert current.status == JobStatus.SUCCEEDED
    return repository.read_world(scope, world_id, branch_id)


@pytest.mark.parametrize("balance,paid", [("100", True), ("0", False)])
def test_leased_worker_opens_and_closes_shift_at_exact_targets(tmp_path, balance, paid):
    source = Database(f"sqlite:///{(tmp_path / 'world.db').as_posix()}")
    restored = Database(f"sqlite:///{(tmp_path / 'restored.db').as_posix()}")
    source.create_schema()
    restored.create_schema()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        repository = WorldRepository(source)
        initial = _state(tick=0, employer_balance=balance)
        made = repository.create_world(scope, NewWorld(title="Registered shift", state=initial))
        world, branch = made.world.world_id, made.branch.branch_id
        started = _advance(repository, scope, world, branch, DURATION)
        record, = started.state.npc_attendance
        assert record.actor_id == WORKER_ID and record.ends_at_tick == 2 * DURATION
        assert started.state.resources == initial.resources

        ended = _advance(repository, scope, world, branch, 2 * DURATION)
        assert not ended.state.npc_attendance
        events = repository.list_events(scope, world, branch).events
        assert sum(event.type == "core.npc_attendance_started.v1" for event in events) == 1
        event, = [event for event in events if event.type == "core.npc_attendance_settled.v1"]
        assert (event.payload.get("payment") is not None) is paid
        if paid:
            posting_ids = [row["account_id"] for row in event.payload["payment"]["ledger_entries"]]
            deltas = {str(key): ended.state.resources[key].balance - value.balance
                      for key, value in initial.resources.items() if str(key) in posting_ids}
            assert sorted(deltas.values()) == [-WAGE, WAGE]
        else:
            assert ended.state.resources == initial.resources
        assert repository.replay(scope, world, branch, from_genesis=True).state_hash == ended.state_hash

        backup = BackupApplication(repository, scratch_directory=tmp_path / "backup-scratch")
        verified = backup.validate_archive(backup.export_world(scope, world, format_version="2.0"))
        target = WorldRepository(restored)
        WorldBackupImporter(target).import_verified_backup(scope, verified)
        assert target.replay(scope, world, branch, from_genesis=True).state_hash == ended.state_hash
    finally:
        source.dispose()
        restored.dispose()


def test_world_worker_leaves_externally_controlled_character_off_auto_shift(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'world.db').as_posix()}")
    database.create_schema()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    try:
        repository = WorldRepository(database)
        made = repository.create_world(scope, NewWorld(title="Controlled worker", state=_state(tick=0)))
        world, branch = made.world.world_id, made.branch.branch_id
        controller = uuid4()
        repository.grant_membership(scope, world, controller, MembershipRole.PLAYER)
        repository.grant_actor_control(scope, world, controller, WORKER_ID)
        head = _advance(repository, scope, world, branch, 2 * DURATION)
        assert not head.state.npc_attendance
        assert not any(event.type.startswith("core.npc_attendance_") for event in repository.list_events(scope, world, branch).events)
    finally:
        database.dispose()
