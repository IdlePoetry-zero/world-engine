from __future__ import annotations

import io
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event as sqlalchemy_event, select, update

from world_engine.application.backups import BackupApplication
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.content import ContentCatalog
from world_engine.domain.errors import Conflict
from world_engine.domain.events import event_payload, parse_event
from world_engine.domain.gameplay_events import BackgroundNeedsAdvancedEvent
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    EventRecord,
    JobStatus,
    NewWorld,
    WorldEntity,
    WorldState,
)
from world_engine.domain.reducers import state_hash
from world_engine.rules import RuleEngine
from world_engine.rules.contracts import RuleContext
from world_engine.simulation.contracts import WorldAdvancePlan
from world_engine.simulation.engine import SimulationEngine, SimulationPolicy
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.event_payload_codec import (
    CompressedEventPayload,
    EventPayloadTooLarge,
    decode_event_payload,
)
from world_engine.storage import event_payload_codec
from world_engine.storage.event_payload_codec import MIN_COMPRESS_RAW_BYTES
from world_engine.storage.event_rows import prepare_event_payload, read_event_payload
from world_engine.storage.models import EventPayloadRow, EventRow
from world_engine.storage.repository import WorldRepository


COMPRESSED_EVENT_TYPES = {
    "core.background_routine_advanced.v1",
    "core.renewable_resource_harvested.v1",
}


def _advance_opening_month(repository, scope, world_id, branch_id, target_tick):
    view = repository.read_world(scope, world_id, branch_id)
    jobs = SimulationJobs(
        repository,
        SimulationEngine(policy=SimulationPolicy(max_macro_step_seconds=target_tick)),
    )
    job = jobs.enqueue_world_advance(
        scope,
        world_id,
        branch_id,
        WorldAdvancePlan(started_tick=view.state.current_tick, target_tick=target_tick),
        idempotency_key="storage-codec-opening-month",
        speed="balanced",
        depth="deep",
        expected_state_version=view.branch.state_version,
    )
    lease = repository.claim_job("storage-codec-worker", job_type="simulation.world_advance")
    assert lease is not None
    for _ in range(12):
        jobs.advance(scope, lease)
        status = repository.read_job(scope, job.job_id).status
        if status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            break
    result = repository.read_job(scope, job.job_id)
    assert result.status == JobStatus.SUCCEEDED, result.model_dump(mode="json")
    return result


def _event_record_by_id(events: list[EventRecord], event_id):
    return next(event for event in events if event.event_id == event_id)


def _large_background_needs_state() -> WorldState:
    actor_id = UUID(int=1)
    entities = {
        actor_id: WorldEntity(id=actor_id, kind="character", name="Advancing actor"),
    }
    for index in range(1_000):
        entity_id = UUID(int=10_000 + index)
        entities[entity_id] = WorldEntity(
            id=entity_id,
            kind="character",
            name=f"Resident {index:04d}",
            components={"needs": {"water": {"value": 80}}},
        )
    return WorldState(player_id=actor_id, entities=entities)


def _large_background_needs_batch(state, *, world_id, branch_id, ruleset_version):
    command_id = uuid4()
    resolution = RuleEngine().evaluate(
        state,
        {"kind": "wait", "duration_ticks": 3_600},
        RuleContext(
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            actor_id=state.player_id,
            ruleset_version=ruleset_version,
        ),
    )
    background = next(
        event for event in resolution.events if isinstance(event, BackgroundNeedsAdvancedEvent)
    )
    return command_id, resolution, background


def test_real_opening_month_payloads_roundtrip_through_repository_and_backup(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'event-payloads.db').as_posix()}")
    database.create_schema()
    restored_database = None
    try:
        repository = WorldRepository(database)
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        draft = ContentCatalog().build_world(
            "long_life_cultivation",
            "Compressed event storage",
            "Traveler",
            seed=789,
            population_mode="sustained",
            pack_version="2.1.0",
        )
        created = repository.create_world(scope, draft)
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        month_ticks = 30 * draft.state.calendar.day_seconds

        _advance_opening_month(repository, scope, world_id, branch_id, month_ticks)
        head = repository.read_world(scope, world_id, branch_id)
        assert head.state.current_tick == month_ticks

        listed = repository.list_events(scope, world_id, branch_id, limit=100).events
        compressed_events = [event for event in listed if event.type in COMPRESSED_EVENT_TYPES]
        assert {event.type for event in compressed_events} == COMPRESSED_EVENT_TYPES
        assert len(compressed_events) > 1
        small_inline_event = next(event for event in listed if event.type == "core.time_advanced.v1")
        assert small_inline_event.payload

        with database.session_factory() as session:
            stored_events = session.scalars(
                select(EventRow).where(EventRow.branch_id == branch_id).order_by(EventRow.sequence)
            ).all()
            payload_rows = session.scalars(
                select(EventPayloadRow).order_by(EventPayloadRow.event_id)
            ).all()
        payload_by_event = {row.event_id: row for row in payload_rows}
        assert {row.event_id for row in payload_rows} == {event.event_id for event in compressed_events}
        stored_by_event = {row.event_id: row for row in stored_events}

        for event in compressed_events:
            stored = stored_by_event[event.event_id]
            sidecar = payload_by_event[event.event_id]
            assert stored.payload_json == {"$event_payload": sidecar.codec_version, "sha256": sidecar.sha256}
            assert sidecar.codec_version == 1
            assert sidecar.raw_byte_count > 64 * 1024
            assert len(sidecar.compressed_bytes) < sidecar.raw_byte_count
            decoded = decode_event_payload(
                event.type,
                compressed_payload=CompressedEventPayload(
                    codec_version=sidecar.codec_version,
                    raw_byte_count=sidecar.raw_byte_count,
                    sha256=sidecar.sha256,
                    compressed_bytes=sidecar.compressed_bytes,
                ),
            )
            assert decoded == event.payload

        stored_small = stored_by_event[small_inline_event.event_id]
        assert "$event_payload" not in stored_small.payload_json
        assert small_inline_event.payload == repository.read_branch_event(
            scope, world_id, branch_id, small_inline_event.event_id,
        ).payload

        # List, point read, compact command receipt, and outbox all expose the
        # same logical JSON payload despite the on-disk side-table reference.
        for event in compressed_events:
            point_read = repository.read_branch_event(scope, world_id, branch_id, event.event_id)
            assert point_read.payload == event.payload
            receipt = repository.read_command_receipt(
                scope,
                world_id,
                branch_id,
                event.command_id,
                event.actor_id,
                f"simulation:{event.command_id}",
            )
            assert receipt is not None
            assert _event_record_by_id(receipt.events, event.event_id).payload == event.payload

        pending = repository.pending_outbox(limit=100)
        outbox_by_event = {item.event_id: item for item in pending}
        for event in compressed_events:
            outbox = outbox_by_event[event.event_id]
            assert outbox.payload["payload"] == event.payload
            assert outbox.payload["type"] == event.type

        sidecar_selects = []

        def count_sidecar_select(connection, cursor, statement, parameters, context, executemany):
            if "world_event_payloads" in statement.lower():
                sidecar_selects.append(statement)

        sqlalchemy_event.listen(database.engine, "before_cursor_execute", count_sidecar_select)
        try:
            replay = repository.replay(scope, world_id, branch_id)
        finally:
            sqlalchemy_event.remove(database.engine, "before_cursor_execute", count_sidecar_select)
        assert len(sidecar_selects) == 1
        assert replay.state_hash == head.state_hash
        assert replay.state_hash == state_hash(head.state)
        assert replay.state.model_dump(mode="json") == head.state.model_dump(mode="json")

        backup_app = BackupApplication(repository)
        archive = backup_app.export_world(scope, world_id)
        restored_database = Database(f"sqlite:///{(tmp_path / 'event-payloads-restored.db').as_posix()}")
        restored_database.create_schema()
        with backup_app.validate_archive_file_streaming(io.BytesIO(archive)) as verified:
            imported = WorldBackupImporter(WorldRepository(restored_database)).import_verified_backup(
                scope,
                verified,
            )
        assert imported.branch_count == 1
        assert imported.state_hashes == {branch_id: head.state_hash}
        restored_repository = WorldRepository(restored_database)
        restored_head = restored_repository.read_world(scope, world_id, branch_id)
        assert restored_head.state_hash == head.state_hash
        restored_events = restored_repository.list_events(scope, world_id, branch_id).events
        assert [(event.event_id, event.type, event.payload) for event in restored_events] == [
            (event.event_id, event.type, event.payload) for event in listed
        ]
        with restored_database.session_factory() as session:
            restored_payloads = session.scalars(select(EventPayloadRow)).all()
        assert {row.event_id for row in restored_payloads} == {event.event_id for event in compressed_events}
        assert restored_repository.replay(scope, world_id, branch_id).state_hash == head.state_hash

        # A corrupted side-table blob is surfaced as the storage-domain Conflict
        # by point reads; the repository never returns a marker or raw bytes.
        corrupted_event = compressed_events[0]
        with database.session_factory() as session:
            with session.begin():
                session.execute(
                    update(EventPayloadRow)
                    .where(EventPayloadRow.event_id == corrupted_event.event_id)
                    .values(compressed_bytes=b"tampered")
                )
        with pytest.raises(Conflict, match="Stored compressed event payload failed verification"):
            repository.read_branch_event(scope, world_id, branch_id, corrupted_event.event_id)
        with pytest.raises(Conflict, match="Stored compressed event payload failed verification"):
            repository.replay(scope, world_id, branch_id, from_genesis=True)

        with database.session_factory() as session:
            with session.begin():
                session.execute(delete(EventPayloadRow).where(EventPayloadRow.event_id == corrupted_event.event_id))
            missing_sidecar_row = session.scalar(
                select(EventRow).where(EventRow.event_id == corrupted_event.event_id)
            )
            missing_sidecar_queries = []

            def count_missing_sidecar_query(connection, cursor, statement, parameters, context, executemany):
                if "world_event_payloads" in statement.lower():
                    missing_sidecar_queries.append(statement)

            sqlalchemy_event.listen(database.engine, "before_cursor_execute", count_missing_sidecar_query)
            try:
                with pytest.raises(Conflict, match="Stored event payload reference does not match"):
                    read_event_payload(missing_sidecar_row, prefetched_sidecar=None)
            finally:
                sqlalchemy_event.remove(database.engine, "before_cursor_execute", count_missing_sidecar_query)
            assert missing_sidecar_queries == []
    finally:
        if restored_database is not None:
            restored_database.dispose()
        database.dispose()


def test_large_typed_background_needs_payload_roundtrips_as_mixed_sidecar_ledger(tmp_path):
    database = Database(f"sqlite:///{(tmp_path / 'background-needs-payloads.db').as_posix()}")
    database.create_schema()
    restored_database = None
    try:
        repository = WorldRepository(database)
        scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
        created = repository.create_world(
            scope,
            NewWorld(title="Large passive needs batch", state=_large_background_needs_state()),
        )
        world_id, branch_id = created.world.world_id, created.branch.branch_id
        view = repository.read_world(scope, world_id, branch_id)
        command_id, resolution, background = _large_background_needs_batch(
            view.state,
            world_id=world_id,
            branch_id=branch_id,
            ruleset_version=created.world.ruleset_version,
        )
        assert resolution.outcome.status == "succeeded"
        assert [event.type for event in resolution.events] == [
            "core.time_advanced.v1", "core.background_needs_advanced.v1",
        ]
        assert len(background.expected_agents) == 1_000

        idempotency_key = "large-background-needs-sidecar"
        committed = repository.commit_command(
            scope,
            world_id,
            branch_id,
            CommandEnvelope(
                command_id=command_id,
                actor_id=view.state.player_id,
                idempotency_key=idempotency_key,
                expected_state_version=view.branch.state_version,
                command_type="action.wait",
                payload={"kind": "wait", "duration_ticks": 3_600},
            ),
            resolution.events,
        )
        head = repository.read_world(scope, world_id, branch_id)
        assert head.state.current_tick == 3_600

        listed = repository.list_events(scope, world_id, branch_id, limit=10).events
        inline_event = next(event for event in listed if event.type == "core.time_advanced.v1")
        large_event = next(event for event in listed if event.type == "core.background_needs_advanced.v1")
        assert inline_event.payload == {"to_tick": 3_600}
        assert len(large_event.payload["expected_agents"]) == 1_000

        with database.session_factory() as session:
            stored_events = session.scalars(
                select(EventRow).where(EventRow.branch_id == branch_id).order_by(EventRow.sequence)
            ).all()
            payload_rows = session.scalars(select(EventPayloadRow)).all()
        stored_by_event = {row.event_id: row for row in stored_events}
        payload_by_event = {row.event_id: row for row in payload_rows}
        assert set(payload_by_event) == {large_event.event_id}
        stored_inline = stored_by_event[inline_event.event_id]
        assert "$event_payload" not in stored_inline.payload_json
        assert stored_inline.payload_json == inline_event.payload

        stored_large = stored_by_event[large_event.event_id]
        sidecar = payload_by_event[large_event.event_id]
        assert stored_large.payload_json == {
            "$event_payload": sidecar.codec_version,
            "sha256": sidecar.sha256,
        }
        assert sidecar.codec_version == 1
        assert sidecar.raw_byte_count > MIN_COMPRESS_RAW_BYTES
        assert len(sidecar.compressed_bytes) < sidecar.raw_byte_count
        decoded = decode_event_payload(
            large_event.type,
            compressed_payload=CompressedEventPayload(
                codec_version=sidecar.codec_version,
                raw_byte_count=sidecar.raw_byte_count,
                sha256=sidecar.sha256,
                compressed_bytes=sidecar.compressed_bytes,
            ),
        )
        assert decoded == large_event.payload
        assert set(decoded) == {
            "actor_id", "include_actor", "from_tick", "to_tick", "expected_agents",
            "expected_accounts", "provenance",
        }
        assert len(decoded["expected_agents"]) == 1_000
        assert read_event_payload(stored_large, prefetched_sidecar=sidecar) == decoded

        restored_event = parse_event({
            "type": large_event.type,
            "event_id": str(large_event.event_id),
            "visibility": large_event.visibility,
            **decoded,
        })
        assert isinstance(restored_event, BackgroundNeedsAdvancedEvent)
        assert restored_event.event_id == background.event_id == large_event.event_id
        assert restored_event.actor_id == background.actor_id == view.state.player_id
        assert restored_event.model_dump(mode="json") == background.model_dump(mode="json")

        point_read = repository.read_branch_event(scope, world_id, branch_id, large_event.event_id)
        assert point_read.payload == decoded
        receipt = repository.read_command_receipt(
            scope, world_id, branch_id, command_id, view.state.player_id, idempotency_key,
        )
        assert receipt is not None
        assert _event_record_by_id(receipt.events, large_event.event_id).payload == decoded

        replay = repository.replay(scope, world_id, branch_id, from_genesis=True)
        assert replay.state_hash == committed.state_hash == head.state_hash
        first_resident = replay.state.entities[UUID(int=10_000)]
        assert first_resident.components["needs"]["water"]["value"] == 79
        assert replay.state.model_dump(mode="json") == head.state.model_dump(mode="json")

        backup_app = BackupApplication(repository)
        archive = backup_app.export_world(scope, world_id)
        restored_database = Database(
            f"sqlite:///{(tmp_path / 'background-needs-payloads-restored.db').as_posix()}"
        )
        restored_database.create_schema()
        with backup_app.validate_archive_file_streaming(io.BytesIO(archive)) as verified:
            imported = WorldBackupImporter(WorldRepository(restored_database)).import_verified_backup(
                scope, verified,
            )
        assert imported.branch_count == 1
        assert imported.state_hashes == {branch_id: committed.state_hash}
        restored_repository = WorldRepository(restored_database)
        restored_listed = restored_repository.list_events(scope, world_id, branch_id, limit=10).events
        assert [(event.event_id, event.type, event.payload) for event in restored_listed] == [
            (event.event_id, event.type, event.payload) for event in listed
        ]
        with restored_database.session_factory() as session:
            restored_rows = session.scalars(select(EventPayloadRow)).all()
            restored_inline = session.scalar(
                select(EventRow).where(EventRow.event_id == inline_event.event_id)
            )
        assert {row.event_id for row in restored_rows} == {large_event.event_id}
        assert "$event_payload" not in restored_inline.payload_json
        assert restored_repository.replay(scope, world_id, branch_id).state_hash == committed.state_hash
    finally:
        if restored_database is not None:
            restored_database.dispose()
        database.dispose()


def test_oversized_background_needs_payload_falls_back_inline_without_weakening_other_caps(
    monkeypatch,
):
    state = _large_background_needs_state()
    _command_id, _resolution, background = _large_background_needs_batch(
        state,
        world_id=UUID(int=90_001),
        branch_id=UUID(int=90_002),
        ruleset_version="1.0.0",
    )
    payload = event_payload(background)
    monkeypatch.setattr(event_payload_codec, "MAX_RAW_PAYLOAD_BYTES", 128 * 1024)

    inline_payload, sidecar = prepare_event_payload(background.event_id, background.type, payload)
    assert inline_payload == payload
    assert sidecar is None

    # The fallback is specific to background needs; other registered sidecar
    # types continue to reject payloads outside the same codec bound.
    with pytest.raises(EventPayloadTooLarge, match="configured raw byte limit"):
        prepare_event_payload(
            uuid4(), "core.background_routine_advanced.v1", payload,
        )
