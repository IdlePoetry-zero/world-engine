"""SQLite coverage for UTC lease writes and legacy naive timestamp reads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event

from world_engine.domain.errors import InvalidLease
from world_engine.domain.models import AccessScope, JobStatus, NewJob, NewWorld, WorldEntity, WorldState, normalize_datetime
from world_engine.storage.database import Database
from world_engine.storage.models import BranchLeaseRow, JobRow
from world_engine.storage.repository import WorldRepository


def _world(repository: WorldRepository):
    tenant_id, owner_id, player_id, place_id = (uuid4() for _ in range(4))
    scope = AccessScope(tenant_id=tenant_id, user_id=owner_id)
    created = repository.create_world(scope, NewWorld(
        title="Lease clock test",
        state=WorldState(entities={
            place_id: WorldEntity(id=place_id, kind="place", name="Harbor"),
            player_id: WorldEntity(id=player_id, kind="character", name="Mira", location_id=place_id),
        }, player_id=player_id),
    ))
    return scope, created


def test_simulation_lease_uses_aware_utc_writes_and_normalizes_sqlite_legacy_values(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'simulation-leases.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    flushed_lease_timestamps: list[tuple[str, bool]] = []
    lease_comparison_parameters: list[datetime] = []

    def inspect_lease_writes(session, _flush_context, _instances):
        for row in (*session.new, *session.dirty):
            if isinstance(row, (JobRow, BranchLeaseRow)) and row.lease_expires_at is not None:
                flushed_lease_timestamps.append((row.__tablename__, row.lease_expires_at.tzinfo is not None))

    def inspect_lease_comparisons(connection, clauseelement, _multiparams, _params, _options):
        if "lease_expires_at" not in str(clauseelement) or "<=" not in str(clauseelement):
            return
        compiled = clauseelement.compile(dialect=connection.dialect)
        lease_comparison_parameters.extend(
            value for name, value in compiled.params.items()
            if "lease_expires_at" in name and isinstance(value, datetime)
        )

    event.listen(database.session_factory, "before_flush", inspect_lease_writes)
    event.listen(database.engine, "before_execute", inspect_lease_comparisons)
    try:
        scope, created = _world(repository)
        job = repository.enqueue_job(scope, NewJob(
            world_id=created.world.world_id,
            branch_id=created.branch.branch_id,
            job_type="simulation.world_advance",
            idempotency_key="utc-lease",
        ))
        local_now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        utc_now = datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc)
        lease = repository.claim_job("first-worker", lease_seconds=10, now=local_now)
        assert lease is not None and lease.job_id == job.job_id
        assert lease.expires_at == utc_now + timedelta(seconds=10)
        assert lease.expires_at.utcoffset() == timedelta(0)

        # SQLite returns DateTime(timezone=True) columns as naive values. This
        # second claim exercises both the SQL eligibility comparison and the
        # Python branch-lease comparison against those legacy-shaped values.
        assert repository.claim_job("early-worker", now=utc_now + timedelta(seconds=9)) is None
        renewed = repository.renew_job(lease, lease_seconds=5, now=utc_now + timedelta(seconds=9))
        assert renewed.expires_at == utc_now + timedelta(seconds=14)
        assert renewed.expires_at.utcoffset() == timedelta(0)

        finished_at = utc_now + timedelta(seconds=13)
        finished = repository.finish_job(renewed, JobStatus.SUCCEEDED, now=finished_at)
        assert finished.status is JobStatus.SUCCEEDED
        with database.session_factory() as session:
            saved_job = session.get(JobRow, job.job_id)
            saved_lease = session.get(BranchLeaseRow, created.branch.branch_id)
            assert saved_job is not None and saved_lease is not None
            assert normalize_datetime(saved_job.lease_expires_at) == finished_at
            assert normalize_datetime(saved_lease.lease_expires_at) == finished_at

        with pytest.raises(InvalidLease):
            repository.renew_job(renewed, now=finished_at + timedelta(seconds=1))

        assert flushed_lease_timestamps
        assert all(is_aware for _table, is_aware in flushed_lease_timestamps)
        assert {table for table, _is_aware in flushed_lease_timestamps} >= {
            "world_jobs", "branch_writer_leases",
        }
        assert lease_comparison_parameters
        assert all(value.utcoffset() == timedelta(0) for value in lease_comparison_parameters)
    finally:
        event.remove(database.session_factory, "before_flush", inspect_lease_writes)
        event.remove(database.engine, "before_execute", inspect_lease_comparisons)
        database.dispose()


def test_media_lease_uses_aware_utc_writes_and_survives_naive_sqlite_reads(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'media-leases.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    flushed_lease_timestamps: list[bool] = []
    lease_comparison_parameters: list[datetime] = []

    def inspect_lease_writes(session, _flush_context, _instances):
        for row in (*session.new, *session.dirty):
            if isinstance(row, JobRow) and row.lease_expires_at is not None:
                flushed_lease_timestamps.append(row.lease_expires_at.tzinfo is not None)

    def inspect_lease_comparisons(connection, clauseelement, _multiparams, _params, _options):
        if "lease_expires_at" not in str(clauseelement) or "<=" not in str(clauseelement):
            return
        compiled = clauseelement.compile(dialect=connection.dialect)
        lease_comparison_parameters.extend(
            value for name, value in compiled.params.items()
            if "lease_expires_at" in name and isinstance(value, datetime)
        )

    event.listen(database.session_factory, "before_flush", inspect_lease_writes)
    event.listen(database.engine, "before_execute", inspect_lease_comparisons)
    try:
        scope, created = _world(repository)
        job = repository.enqueue_job(scope, NewJob(
            world_id=created.world.world_id,
            branch_id=created.branch.branch_id,
            job_type="media.generate",
            idempotency_key="utc-media-lease",
            payload={"submitted_by": str(scope.user_id)},
        ))
        local_now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        utc_now = datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc)
        first = repository.claim_media_job("media-worker-a", lease_seconds=10, now=local_now)
        assert first is not None and first.job.job_id == job.job_id
        assert first.lease.expires_at == utc_now + timedelta(seconds=10)
        assert first.lease.expires_at.utcoffset() == timedelta(0)

        # The stored SQLite value reloads as naive UTC; renewal validates it
        # against a timezone-aware UTC clock before extending the lease.
        renewed = repository.renew_media_job(
            first.lease, lease_seconds=5, now=utc_now + timedelta(seconds=9),
        )
        assert renewed.expires_at == utc_now + timedelta(seconds=14)

        finished_at = utc_now + timedelta(seconds=13)
        finished = repository.finish_media_job(renewed, JobStatus.FAILED, now=finished_at)
        assert finished.status is JobStatus.FAILED
        with database.session_factory() as session:
            saved_job = session.get(JobRow, job.job_id)
            assert saved_job is not None
            assert normalize_datetime(saved_job.lease_expires_at) == finished_at
        assert flushed_lease_timestamps and all(flushed_lease_timestamps)
        assert lease_comparison_parameters
        assert all(value.utcoffset() == timedelta(0) for value in lease_comparison_parameters)
    finally:
        event.remove(database.session_factory, "before_flush", inspect_lease_writes)
        event.remove(database.engine, "before_execute", inspect_lease_comparisons)
        database.dispose()
