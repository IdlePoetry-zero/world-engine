from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from uuid import uuid4

from sqlalchemy import text

from world_engine.domain.models import AccessScope, NewWorld, WorldState
from world_engine.storage.database import Database
from world_engine.storage.event_rows import append_event_row
from world_engine.storage.models import EventPayloadRow, EventRow
from world_engine.storage.repository import WorldRepository


def _load_probe_runner():
    script = Path(__file__).resolve().parents[2] / "scripts" / "probe-world-advance.py"
    spec = importlib.util.spec_from_file_location("probe_world_advance_storage_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _insert_event(repository, *, world_id, branch_id, sequence, event_type, payload):
    event = EventRow(
        branch_id=branch_id,
        sequence=sequence,
        event_id=uuid4(),
        world_id=world_id,
        tick=sequence,
        event_type=event_type,
        command_id=uuid4(),
        actor_id=uuid4(),
        causation_json=[],
        correlation_id=None,
        ruleset_version="test",
        payload_json=payload,
        visibility_json={},
        state_version_after=sequence,
        commit_boundary=True,
        committed_at=datetime.now(timezone.utc),
    )
    with repository.database.session_factory() as session:
        append_event_row(session, event)
        session.commit()
    return event.event_id


def _create_world(repository, scope, title):
    created = repository.create_world(scope, NewWorld(title=title, state=WorldState()))
    return created.world.world_id, created.branch.branch_id


def _inline_byte_lengths(database, world_id):
    with database.engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT event_type, LENGTH(CAST(payload_json AS BLOB)) AS inline_bytes "
            "FROM world_events WHERE world_id = :world_id"
        ), {"world_id": str(world_id).replace("-", "")}).all()
    return {row.event_type: int(row.inline_bytes) for row in rows}


def test_probe_storage_metrics_count_logical_and_stored_payloads_by_world(tmp_path):
    runner = _load_probe_runner()
    database = Database(f"sqlite:///{(tmp_path / 'payload-metrics.db').as_posix()}")
    database.create_schema()
    try:
        repository = WorldRepository(database)
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        world_id, branch_id = _create_world(repository, scope, "Payload metrics")
        other_world_id, other_branch_id = _create_world(repository, scope, "Other world")

        inline_type = "core.time_advanced.v1"
        compressed_type = "core.background_routine_advanced.v1"
        inline_event_id = _insert_event(
            repository, world_id=world_id, branch_id=branch_id, sequence=1,
            event_type=inline_type, payload={"note": "legacy inline payload"},
        )
        compressed_payload = {"needs": "x" * 100_000}
        compressed_event_id = _insert_event(
            repository, world_id=world_id, branch_id=branch_id, sequence=2,
            event_type=compressed_type, payload=compressed_payload,
        )
        # A sidecar in another world must not leak into this world's totals.
        _insert_event(
            repository, world_id=other_world_id, branch_id=other_branch_id, sequence=1,
            event_type=compressed_type, payload={"needs": "y" * 100_000},
        )

        with database.session_factory() as session:
            sidecar = session.get(EventPayloadRow, compressed_event_id)
        assert sidecar is not None
        inline_lengths = _inline_byte_lengths(database, world_id)
        expected_event_payload_bytes = inline_lengths[inline_type] + sidecar.raw_byte_count
        expected_stored_bytes = inline_lengths[inline_type] + inline_lengths[compressed_type] + len(sidecar.compressed_bytes)

        metrics = runner._storage_metrics(database, str(world_id))

        assert metrics == {
            "event_count": 2,
            "event_payload_bytes": expected_event_payload_bytes,
            "event_payload_stored_bytes": expected_stored_bytes,
            "event_payload_sidecar_count": 1,
            "events_by_type": {
                inline_type: {
                    "count": 1,
                    "payload_bytes": inline_lengths[inline_type],
                    "stored_payload_bytes": inline_lengths[inline_type],
                    "sidecar_count": 0,
                },
                compressed_type: {
                    "count": 1,
                    "payload_bytes": sidecar.raw_byte_count,
                    "stored_payload_bytes": inline_lengths[compressed_type] + len(sidecar.compressed_bytes),
                    "sidecar_count": 1,
                },
            },
        }
        assert inline_event_id != compressed_event_id
    finally:
        database.dispose()


def test_probe_storage_metrics_support_legacy_database_without_sidecar_table(tmp_path):
    runner = _load_probe_runner()
    database = Database(f"sqlite:///{(tmp_path / 'legacy-payload-metrics.db').as_posix()}")
    database.create_schema()
    try:
        repository = WorldRepository(database)
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        world_id, branch_id = _create_world(repository, scope, "Legacy payload metrics")
        _insert_event(
            repository, world_id=world_id, branch_id=branch_id, sequence=1,
            event_type="core.time_advanced.v1", payload={"note": "inline only"},
        )
        inline_bytes = _inline_byte_lengths(database, world_id)["core.time_advanced.v1"]
        with database.engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE world_event_payloads")

        metrics = runner._storage_metrics(database, str(world_id))

        assert metrics == {
            "event_count": 1,
            "event_payload_bytes": inline_bytes,
            "event_payload_stored_bytes": inline_bytes,
            "event_payload_sidecar_count": 0,
            "events_by_type": {
                "core.time_advanced.v1": {
                    "count": 1,
                    "payload_bytes": inline_bytes,
                    "stored_payload_bytes": inline_bytes,
                    "sidecar_count": 0,
                },
            },
        }
    finally:
        database.dispose()
