"""Recover a leased simulation window after SQLite reaches its page limit."""

from datetime import timedelta
from sqlite3 import SQLITE_FULL
from uuid import uuid4

import pytest
from sqlalchemy import event

from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.domain.errors import InsufficientStorage, InvalidLease
from world_engine.domain.models import AccessScope, JobStatus, NewWorld, WorldEntity, WorldState
from world_engine.simulation import LongActionPlan
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


def test_sqlite_full_pauses_and_resumes_the_same_leased_simulation_window(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'simulation-job-full.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        actor_id, place_id = uuid4(), uuid4()
        # Rewriting the branch checkpoint needs transient overflow pages. The
        # starting world is valid; this makes the connection's real SQLite
        # page ceiling reliably reject the next committed window.
        state = WorldState(
            entities={
                place_id: WorldEntity(id=place_id, kind="place", name="Harbor"),
                actor_id: WorldEntity(
                    id=actor_id, kind="character", name="Mira", location_id=place_id,
                ),
            },
            player_id=actor_id,
            metadata={"capacity_probe": "x" * (64 * 1024)},
        )
        created = repository.create_world(scope, NewWorld(title="Capacity recovery", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        jobs = SimulationJobs(repository)
        plan = LongActionPlan(
            actor_id=actor_id, started_tick=0, target_tick=60, activity="wait",
        )
        queued = jobs.enqueue(
            scope, world_id, branch_id, plan,
            idempotency_key="retry-same-simulation-window", speed="balanced", depth="deep",
        )
        lease = repository.claim_job("capacity-limited-worker", job_type=queued.job_type)
        assert lease is not None and lease.job_id == queued.job_id
        leased_job = repository.read_job(scope, queued.job_id)
        before = repository.read_world(scope, world_id, branch_id)

        def cap_database_pages(connection, _record):
            cursor = connection.cursor()
            try:
                pages = cursor.execute("PRAGMA page_count").fetchone()[0]
                cap = cursor.execute(f"PRAGMA max_page_count={pages}").fetchone()[0]
                assert cap == pages
            finally:
                cursor.close()

        sqlite_errors = []

        def record_sqlite_error(exception_context):
            sqlite_errors.append(getattr(exception_context.original_exception, "sqlite_errorcode", None))

        database.dispose()
        event.listen(database.engine, "connect", cap_database_pages)
        event.listen(database.engine, "handle_error", record_sqlite_error)
        try:
            # This is a real SimulationJobs window backed by SQLite's actual
            # max_page_count limit, rather than a synthetic repository fault.
            paused = jobs.advance(scope, lease)

            assert SQLITE_FULL in sqlite_errors
            assert paused.status is JobStatus.PAUSED
            assert paused.result == {"reason_code": "insufficient_storage"}

            after_failure = repository.read_world(scope, world_id, branch_id)
            assert after_failure.state_hash == before.state_hash
            assert after_failure.last_seq == before.last_seq == 0
            assert after_failure.branch.head_seq == before.branch.head_seq == 0
            assert after_failure.branch.state_version == before.branch.state_version == 0
            assert after_failure.state.current_tick == before.state.current_tick == 0
            assert repository.list_events(
                scope, world_id, branch_id, include_ancestors=False,
            ).events == []
            assert repository.pending_outbox() == []

            saved_pause = repository.read_job(scope, queued.job_id)
            assert saved_pause.status is JobStatus.PAUSED
            assert saved_pause.result == {"reason_code": "insufficient_storage"}
            assert saved_pause.committed_through_tick == leased_job.committed_through_tick == 0
            assert saved_pause.fencing_token == lease.fencing_token
            assert saved_pause.payload == leased_job.payload
            assert saved_pause.payload["plan"] == plan.model_dump(mode="json")
            assert "committed_state_version" not in saved_pause.payload
            assert "itinerary_cursor" not in saved_pause.payload
            assert "work_progress" not in saved_pause.payload
        finally:
            event.remove(database.engine, "handle_error", record_sqlite_error)
            event.remove(database.engine, "connect", cap_database_pages)
            # max_page_count belongs to each SQLite connection; disposing the
            # capped pool ensures the resumed lease gets unconstrained handles.
            database.dispose()

        repository.control_job(scope, queued.job_id, "resume")
        resumed_lease = repository.claim_job("capacity-restored-worker", job_type=queued.job_type)
        assert resumed_lease is not None and resumed_lease.job_id == queued.job_id
        assert resumed_lease.fencing_token > lease.fencing_token

        completed = jobs.advance(scope, resumed_lease)
        assert completed.outcome == "completed"
        assert completed.next_tick == 60
        assert len(completed.events) == 1

        final = repository.read_world(scope, world_id, branch_id)
        assert final.state.current_tick == 60
        assert final.last_seq == 1
        assert final.branch.head_seq == 1
        rows = repository.list_events(scope, world_id, branch_id, limit=20).events
        assert len(rows) == 1
        assert rows[0].event_id == completed.events[0].event_id
        assert rows[0].type == "core.time_advanced.v1"

        saved = repository.read_job(scope, queued.job_id)
        assert saved.status is JobStatus.SUCCEEDED
        assert saved.payload["plan"] == plan.model_dump(mode="json")
        assert saved.committed_through_tick == 60
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash
        outbox = repository.pending_outbox()
        assert len(outbox) == 1
        assert outbox[0].event_id == rows[0].event_id
    finally:
        database.dispose()


def test_sqlite_full_preserves_running_lease_when_pause_status_write_fails(tmp_path, monkeypatch):
    database = Database(f"sqlite:///{(tmp_path / 'simulation-job-pause-full.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        actor_id, place_id = uuid4(), uuid4()
        state = WorldState(
            entities={
                place_id: WorldEntity(id=place_id, kind="place", name="Harbor"),
                actor_id: WorldEntity(
                    id=actor_id, kind="character", name="Mira", location_id=place_id,
                ),
            },
            player_id=actor_id,
            metadata={"capacity_probe": "x" * (64 * 1024)},
        )
        created = repository.create_world(scope, NewWorld(title="Capacity pause recovery", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        jobs = SimulationJobs(repository)
        plan = LongActionPlan(
            actor_id=actor_id, started_tick=0, target_tick=60, activity="wait",
        )
        queued = jobs.enqueue(
            scope, world_id, branch_id, plan,
            idempotency_key="retry-after-pause-status-full", speed="balanced", depth="deep",
        )
        lease = repository.claim_job("capacity-limited-worker", job_type=queued.job_type)
        assert lease is not None and lease.job_id == queued.job_id
        leased_job = repository.read_job(scope, queued.job_id)
        before = repository.read_world(scope, world_id, branch_id)

        def cap_database_pages(connection, _record):
            cursor = connection.cursor()
            try:
                pages = cursor.execute("PRAGMA page_count").fetchone()[0]
                cap = cursor.execute(f"PRAGMA max_page_count={pages}").fetchone()[0]
                assert cap == pages
            finally:
                cursor.close()

        actual_finish_job = repository.finish_job
        pause_attempts = []

        def fail_only_storage_pause(lease_arg, status, **kwargs):
            if status is JobStatus.PAUSED:
                pause_attempts.append((lease_arg, kwargs))
                raise InsufficientStorage("simulated full page limit on pause status write")
            return actual_finish_job(lease_arg, status, **kwargs)

        monkeypatch.setattr(repository, "finish_job", fail_only_storage_pause)
        database.dispose()
        event.listen(database.engine, "connect", cap_database_pages)
        try:
            with pytest.raises(InsufficientStorage) as failure:
                jobs.advance(scope, lease)

            # The window itself reached SQLite and failed at its real page cap;
            # only the follow-up PAUSED status write is injected here.
            assert failure.value.__cause__.orig.sqlite_errorcode == SQLITE_FULL
            assert len(pause_attempts) == 1
            assert pause_attempts[0][0] == lease
            assert pause_attempts[0][1]["result"] == {"reason_code": "insufficient_storage"}

            after_failure = repository.read_world(scope, world_id, branch_id)
            assert after_failure.state_hash == before.state_hash
            assert after_failure.last_seq == before.last_seq == 0
            assert after_failure.branch.head_seq == before.branch.head_seq == 0
            assert after_failure.branch.state_version == before.branch.state_version == 0
            assert after_failure.state.current_tick == before.state.current_tick == 0
            assert repository.list_events(
                scope, world_id, branch_id, include_ancestors=False,
            ).events == []
            assert repository.pending_outbox() == []

            still_running = repository.read_job(scope, queued.job_id)
            assert still_running.status is JobStatus.RUNNING
            assert still_running.result is None
            assert still_running.committed_through_tick == leased_job.committed_through_tick == 0
            assert still_running.fencing_token == lease.fencing_token
            assert still_running.payload == leased_job.payload
            assert still_running.payload["plan"] == plan.model_dump(mode="json")
            assert "committed_state_version" not in still_running.payload
            assert "itinerary_cursor" not in still_running.payload
            assert "work_progress" not in still_running.payload
        finally:
            event.remove(database.engine, "connect", cap_database_pages)
            # The page cap is connection-local; dispose capped handles before
            # the recovery worker uses SQLite again.
            database.dispose()

        expired_at = lease.expires_at + timedelta(seconds=1)
        recovered_lease = repository.claim_job(
            "capacity-recovery-worker", job_type=queued.job_type, now=expired_at,
        )
        assert recovered_lease is not None and recovered_lease.job_id == queued.job_id
        assert recovered_lease.fencing_token > lease.fencing_token

        # A takeover fences the old worker even though it never wrote its pause
        # marker. That worker cannot commit the same window after recovery.
        with pytest.raises(InvalidLease):
            jobs.advance(scope, lease)
        after_fenced_attempt = repository.read_world(scope, world_id, branch_id)
        assert after_fenced_attempt.state_hash == before.state_hash
        assert repository.read_job(scope, queued.job_id).status is JobStatus.RUNNING

        completed = jobs.advance(scope, recovered_lease)
        assert completed.outcome == "completed"
        assert completed.next_tick == 60
        assert len(completed.events) == 1

        final = repository.read_world(scope, world_id, branch_id)
        assert final.state.current_tick == 60
        assert final.last_seq == 1
        assert final.branch.head_seq == 1
        rows = repository.list_events(scope, world_id, branch_id, limit=20).events
        assert len(rows) == 1
        assert rows[0].event_id == completed.events[0].event_id
        assert rows[0].type == "core.time_advanced.v1"

        saved = repository.read_job(scope, queued.job_id)
        assert saved.status is JobStatus.SUCCEEDED
        assert saved.payload["plan"] == plan.model_dump(mode="json")
        assert saved.committed_through_tick == 60
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash
        outbox = repository.pending_outbox()
        assert len(outbox) == 1
        assert outbox[0].event_id == rows[0].event_id
    finally:
        database.dispose()


def test_post_commit_success_status_storage_fault_resumes_from_saved_checkpoint(tmp_path, monkeypatch):
    """Post-commit fault injected: a lost success marker must not replay a window."""
    database = Database(f"sqlite:///{(tmp_path / 'simulation-job-post-commit.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        actor_id, place_id = uuid4(), uuid4()
        state = WorldState(
            entities={
                place_id: WorldEntity(id=place_id, kind="place", name="Harbor"),
                actor_id: WorldEntity(
                    id=actor_id, kind="character", name="Mira", location_id=place_id,
                ),
            },
            player_id=actor_id,
        )
        created = repository.create_world(scope, NewWorld(title="Post-commit recovery", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        jobs = SimulationJobs(repository)
        plan = LongActionPlan(
            actor_id=actor_id, started_tick=0, target_tick=60, activity="wait",
        )
        queued = jobs.enqueue(
            scope, world_id, branch_id, plan,
            idempotency_key="retry-after-success-status-full", speed="balanced", depth="deep",
        )
        lease = repository.claim_job("post-commit-capacity-worker", job_type=queued.job_type)
        assert lease is not None and lease.job_id == queued.job_id

        actual_finish_job = repository.finish_job
        injected_success_failures = []
        pause_attempts = []

        def fail_succeeded_status_once(lease_arg, status, **kwargs):
            if status is JobStatus.SUCCEEDED and not injected_success_failures:
                committed_view = repository.read_world(scope, world_id, branch_id)
                committed_rows = repository.list_events(
                    scope, world_id, branch_id, include_ancestors=False,
                ).events
                committed_job = repository.read_job(scope, queued.job_id)
                # Prove the fault is after the real window transaction has
                # committed and advanced this exact leased job's checkpoint.
                assert committed_view.state.current_tick == 60
                assert committed_view.last_seq == 1
                assert len(committed_rows) == 1
                assert committed_job.committed_through_tick == 60
                injected_success_failures.append((lease_arg, status, committed_rows[0].event_id))
                raise InsufficientStorage("injected full condition while saving success status")
            if status is JobStatus.PAUSED:
                pause_attempts.append((lease_arg, kwargs))
            return actual_finish_job(lease_arg, status, **kwargs)

        monkeypatch.setattr(repository, "finish_job", fail_succeeded_status_once)
        paused = jobs.advance(scope, lease)

        assert paused.status is JobStatus.PAUSED
        assert paused.result == {"reason_code": "insufficient_storage"}
        assert len(injected_success_failures) == 1
        assert injected_success_failures[0][0] == lease
        assert injected_success_failures[0][1] is JobStatus.SUCCEEDED
        assert len(pause_attempts) == 1
        assert pause_attempts[0][0] == lease
        assert pause_attempts[0][1]["result"] == {"reason_code": "insufficient_storage"}

        committed = repository.read_world(scope, world_id, branch_id)
        assert committed.state.current_tick == 60
        assert committed.last_seq == 1
        assert committed.branch.head_seq == 1
        assert committed.branch.state_version == 1
        committed_rows = repository.list_events(
            scope, world_id, branch_id, include_ancestors=False,
        ).events
        assert len(committed_rows) == 1
        committed_event_id = committed_rows[0].event_id
        assert committed_event_id == injected_success_failures[0][2]
        assert committed_rows[0].type == "core.time_advanced.v1"

        saved_pause = repository.read_job(scope, queued.job_id)
        assert saved_pause.job_id == queued.job_id
        assert saved_pause.status is JobStatus.PAUSED
        assert saved_pause.result == {"reason_code": "insufficient_storage"}
        assert saved_pause.committed_through_tick == committed.state.current_tick == 60
        assert saved_pause.fencing_token == lease.fencing_token
        assert saved_pause.payload["plan"] == plan.model_dump(mode="json")
        assert saved_pause.payload["committed_state_version"] == committed.branch.state_version

        committed_outbox = repository.pending_outbox()
        assert len(committed_outbox) == 1
        assert committed_outbox[0].event_id == committed_event_id

        repository.control_job(scope, queued.job_id, "resume")
        resumed_lease = repository.claim_job("post-commit-recovery-worker", job_type=queued.job_type)
        assert resumed_lease is not None and resumed_lease.job_id == queued.job_id
        assert resumed_lease.fencing_token > lease.fencing_token
        completed = jobs.advance(scope, resumed_lease)
        assert completed.outcome == "completed"
        assert completed.next_tick == 60
        assert completed.events == ()

        final = repository.read_world(scope, world_id, branch_id)
        assert final.state_hash == committed.state_hash
        assert final.state.current_tick == 60
        assert final.last_seq == 1
        assert final.branch.head_seq == 1
        final_rows = repository.list_events(scope, world_id, branch_id, limit=20).events
        assert len(final_rows) == 1
        assert final_rows[0].event_id == committed_event_id
        saved = repository.read_job(scope, queued.job_id)
        assert saved.job_id == queued.job_id
        assert saved.status is JobStatus.SUCCEEDED
        assert saved.payload["plan"] == plan.model_dump(mode="json")
        assert saved.committed_through_tick == 60
        assert repository.pending_outbox() == committed_outbox
        assert repository.replay(scope, world_id, branch_id, from_genesis=True).state_hash == final.state_hash
    finally:
        database.dispose()
