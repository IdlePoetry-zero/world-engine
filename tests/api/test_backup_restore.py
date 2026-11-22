"""Atomic, side-by-side import of replay-verified backup data."""

from __future__ import annotations

import io
import json
from decimal import Decimal
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import select

from world_engine.application.backups import BackupApplication
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.gameplay_events import InvestigationCaseOpenedEvent
from world_engine.domain.investigation_cases import InvestigationCaseFile, case_definition_digest, case_definition_from_anchor
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    NewWorld,
    ResourceAccount,
    ResourceTransferredEvent,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.rules import RuleContext, RuleEngine
from world_engine.storage.backup_import import BackupImportError, WorldBackupImporter
from world_engine.storage.database import Database
from world_engine.storage.models import (
    BranchLeaseRow,
    CommandRow,
    EventRow,
    JobRow,
    LedgerPostingRow,
    OutboxRow,
    SnapshotRow,
)
from world_engine.storage.repository import WorldRepository


@pytest.fixture
def restore_fixture(tmp_path):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player_id = uuid4()
    receiver_id = uuid4()
    source_account_id = uuid4()
    receiver_account_id = uuid4()
    source_db = Database(f"sqlite:///{(tmp_path / 'source.db').as_posix()}")
    source_db.create_schema()
    source_repository = WorldRepository(source_db)
    created = source_repository.create_world(
        scope,
        NewWorld(
            title="Archive source",
            state=WorldState(
                player_id=player_id,
                entities={
                    player_id: WorldEntity(id=player_id, kind="character", name="Traveler"),
                    receiver_id: WorldEntity(id=receiver_id, kind="character", name="Merchant"),
                },
                resources={
                    source_account_id: ResourceAccount(
                        resource_id="core.silver", balance=Decimal("10"), owner_id=player_id
                    ),
                    receiver_account_id: ResourceAccount(
                        resource_id="core.silver", balance=Decimal("2"), owner_id=receiver_id
                    ),
                },
            ),
        ),
    )
    world_id = created.world.world_id
    root_branch_id = created.branch.branch_id
    first_event_ids = [uuid4(), uuid4()]
    first_commit = source_repository.commit_command(
        scope,
        world_id,
        root_branch_id,
        CommandEnvelope(
            actor_id=player_id,
            command_type="wait",
            idempotency_key="restore-seed-batch",
            expected_state_version=0,
            payload={"private_request": "do not export or invent on restore"},
        ),
        [
            TimeAdvancedEvent(event_id=first_event_ids[0], to_tick=30),
            TimeAdvancedEvent(event_id=first_event_ids[1], to_tick=60),
        ],
    )
    child = source_repository.fork(scope, world_id, root_branch_id, first_commit.last_seq, "Forked copy")
    source_repository.commit_command(
        scope,
        world_id,
        root_branch_id,
        CommandEnvelope(
            actor_id=player_id,
            command_type="wait",
            idempotency_key="restore-root-next",
            expected_state_version=1,
        ),
        [
            ResourceTransferredEvent(
                from_account_id=source_account_id,
                to_account_id=receiver_account_id,
                resource_id="core.silver",
                quantity=Decimal("2"),
            ),
            TimeAdvancedEvent(to_tick=90),
        ],
    )
    source_repository.commit_command(
        scope,
        world_id,
        child.branch_id,
        CommandEnvelope(
            actor_id=player_id,
            command_type="wait",
            idempotency_key="restore-child-next",
            expected_state_version=1,
        ),
        [TimeAdvancedEvent(to_tick=75)],
    )
    backup_app = BackupApplication(source_repository)
    archive_bytes = backup_app.export_world(scope, world_id)
    verified = backup_app.validate_archive(archive_bytes)

    destination_db = Database(f"sqlite:///{(tmp_path / 'restored.db').as_posix()}")
    destination_db.create_schema()
    destination_repository = WorldRepository(destination_db)
    yield {
        "scope": scope,
        "source_db": source_db,
        "source_repository": source_repository,
        "destination_db": destination_db,
        "destination_repository": destination_repository,
        "backup_app": backup_app,
        "archive_bytes": archive_bytes,
        "verified": verified,
        "world_id": world_id,
        "root_branch_id": root_branch_id,
        "child_branch_id": child.branch_id,
        "first_event_ids": first_event_ids,
    }
    destination_db.dispose()
    source_db.dispose()


@pytest.mark.parametrize("streaming", [False, True])
def test_investigation_case_archive_replays_without_original_request(restore_fixture, streaming):
    fixture = restore_fixture
    scope, source = fixture["scope"], fixture["source_repository"]
    actor_id, place_id, anchor_id, case_id, command_id = (uuid4() for _ in range(5))
    actor = WorldEntity(id=actor_id, kind="character", name="Inspector", location_id=place_id)
    place = WorldEntity(id=place_id, kind="place", name="Depot")
    anchor = WorldEntity(id=anchor_id, kind="item", name="Notice", location_id=place_id, components={
        "historical_evidence": {
            "evidence_id": str(uuid4()), "subject_id": str(place_id), "claim": "The depot had a night shift.",
            "fact": {"predicate": "entity_status", "value": "active", "effective_tick": -60},
            "provenance": [{"kind": "pack", "reference_id": "test.night_shift"}],
        },
        "investigation_case": {
            "case_id": str(case_id), "title": "Depot closure", "brief": "Check the notice.",
            "routes": [{
                "route_id": f"route.{index}", "suspect_id": str(actor_id), "finding": "supports",
                "requirements": [{
                    "subject_id": str(place_id), "predicate": "entity_status", "effective_tick": 0,
                    "finding": "conflict", "observation_provenance": [{"kind": "pack", "reference_id": f"log.{index}"}],
                    "claim_provenance": [{"kind": "pack", "reference_id": f"notice.{index}"}],
                }],
            } for index in range(2)],
        },
    })
    view = source.create_world(scope, NewWorld(title="Investigation restore", state=WorldState(
        player_id=actor_id, entities={entity.id: entity for entity in (actor, place, anchor)},
    )))
    world_id, branch_id, version = view.world.world_id, view.branch.branch_id, view.world.ruleset_version
    definition = case_definition_from_anchor(anchor)
    event = InvestigationCaseOpenedEvent(
        event_id=uuid5(command_id, f"{version}:investigation_case.opened"), world_id=world_id,
        branch_id=branch_id, command_id=command_id, ruleset_version=version, actor_id=actor_id,
        case_file=InvestigationCaseFile(
            id=uuid5(command_id, f"{version}:investigation_case.file"), case_id=case_id, anchor_id=anchor_id,
            owner_id=actor_id, title=definition.title, brief=definition.brief,
            definition_hash=case_definition_digest(definition), opened_tick=0, updated_tick=0,
        ),
    )
    command = CommandEnvelope(
        command_id=command_id, actor_id=actor_id, command_type="action.open_investigation_case",
        idempotency_key="open-case", expected_state_version=0,
        payload={"kind": "open_investigation_case", "anchor_id": str(anchor_id)},
    )
    wrong_command = command.model_copy(update={"payload": {"kind": "open_investigation_case", "anchor_id": str(place_id)}})
    with pytest.raises(InvalidEvent, match="exact authored anchor proposal"):
        source.commit_command(scope, world_id, branch_id, wrong_command, [event])
    result = source.commit_command(scope, world_id, branch_id, command, [event])
    assert source.replay(scope, world_id, branch_id, from_genesis=True).state_hash == result.state_hash

    destruction_command = CommandEnvelope(
        actor_id=actor_id, command_type="action.destroy_historical_evidence", idempotency_key="destroy-notice",
        expected_state_version=1, payload={"kind": "destroy_historical_evidence", "target_id": str(anchor_id)},
    )
    before_destruction = source.read_world(scope, world_id, branch_id)
    destruction = RuleEngine().evaluate(before_destruction.state, destruction_command.payload, RuleContext(
        world_id=world_id, branch_id=branch_id, actor_id=actor_id, ruleset_version=version,
        command_id=destruction_command.command_id,
    ))
    assert destruction.outcome.status == "succeeded"
    destroyed = source.commit_command(scope, world_id, branch_id, destruction_command, destruction.events)

    backups = fixture["backup_app"]
    archive = backups.export_world(scope, world_id)
    with (backups.validate_archive_file_streaming(io.BytesIO(archive)) if streaming
          else backups.validate_archive(archive)) as verified:
        destination = fixture["destination_repository"]
        WorldBackupImporter(destination).import_verified_backup(scope, verified)
    replayed = destination.replay(scope, world_id, branch_id, from_genesis=True)
    assert replayed.state_hash == destroyed.state_hash
    assert replayed.state.investigation_cases == [event.case_file]
    assert replayed.state.entities[anchor_id].status == "destroyed"


def test_restore_preserves_history_and_hashes_in_empty_isolated_namespace(restore_fixture):
    fixture = restore_fixture
    source_before = fixture["source_repository"].read_world(
        fixture["scope"], fixture["world_id"]
    )
    result = WorldBackupImporter(fixture["destination_repository"]).import_verified_backup(
        fixture["scope"], fixture["verified"]
    )
    destination = fixture["destination_repository"]
    assert result.world_id == fixture["world_id"]
    assert result.active_branch_id == source_before.world.active_branch_id
    assert result.branch_count == 2
    assert result.event_count == 5

    for branch in fixture["verified"].branches:
        restored = destination.read_world(fixture["scope"], fixture["world_id"], branch.branch_id)
        replayed = destination.replay(fixture["scope"], fixture["world_id"], branch.branch_id)
        assert restored.branch.branch_id == branch.branch_id
        assert restored.branch.parent_branch_id == branch.parent_branch_id
        assert restored.branch.fork_seq == branch.fork_seq
        assert restored.last_seq == branch.head_seq
        assert restored.state_hash == branch.state_hash
        assert replayed.state_hash == branch.state_hash

    with destination.database.session_factory() as session:
        restored_events = session.scalars(
            select(EventRow).where(EventRow.world_id == fixture["world_id"])
        ).all()
        restored_commands = session.scalars(
            select(CommandRow).where(CommandRow.world_id == fixture["world_id"])
        ).all()
        assert {event.event_id for event in restored_events} == {
            event.event_id
            for branch in fixture["verified"].branches
            for event in branch.events
        }
        assert {event.command_id for event in restored_events} == {
            command.command_id for command in restored_commands
        }
        assert len(restored_commands) == 3
        assert all(command.command_type == "backup.restored_event_batch" for command in restored_commands)
        assert all(command.payload_json["source_request_payload_available"] is False for command in restored_commands)
        assert all("private_request" not in command.payload_json for command in restored_commands)
        assert session.scalar(
            select(OutboxRow.outbox_id).where(OutboxRow.world_id == fixture["world_id"]).limit(1)
        ) is None
        assert session.scalar(
            select(JobRow.job_id).where(JobRow.world_id == fixture["world_id"]).limit(1)
        ) is None
        assert session.scalar(
            select(BranchLeaseRow.branch_id)
            .where(BranchLeaseRow.branch_id.in_([fixture["root_branch_id"], fixture["child_branch_id"]]))
            .limit(1)
        ) is None
        assert session.scalar(
            select(SnapshotRow.snapshot_id).where(SnapshotRow.world_id == fixture["world_id"])
        ) is not None
        postings = session.scalars(
            select(LedgerPostingRow).where(
                LedgerPostingRow.event_id.in_([event.event_id for event in restored_events])
            )
        ).all()
        assert {row.delta for row in postings} == {"-2", "2"}
        assert session.scalar(
            select(LedgerPostingRow.posting_id).where(LedgerPostingRow.event_id.in_([event.event_id for event in restored_events]))
        ) is not None

    source_after = fixture["source_repository"].read_world(fixture["scope"], fixture["world_id"])
    assert source_after.state_hash == source_before.state_hash
    assert fixture["backup_app"].export_world(fixture["scope"], fixture["world_id"]) == fixture["archive_bytes"]


def test_restore_refuses_nonempty_destination_without_modifying_it(restore_fixture):
    fixture = restore_fixture
    destination = fixture["destination_repository"]
    separate_world = destination.create_world(
        fixture["scope"],
        NewWorld(title="Existing destination", state=WorldState()),
    )
    before = destination.read_world(fixture["scope"], separate_world.world.world_id)
    with pytest.raises(BackupImportError, match="empty, isolated"):
        WorldBackupImporter(destination).import_verified_backup(fixture["scope"], fixture["verified"])
    after = destination.read_world(fixture["scope"], separate_world.world.world_id)
    assert after.state_hash == before.state_hash
    assert len(destination.list_worlds(fixture["scope"])) == 1


@pytest.mark.parametrize("format_version", ["1.0", "2.0"])
def test_streamed_restore_replays_and_imports_without_materializing_history(restore_fixture, format_version):
    fixture = restore_fixture
    archive_bytes = fixture["backup_app"].export_world(
        fixture["scope"], fixture["world_id"], format_version=format_version
    )
    verified = fixture["backup_app"].validate_archive_file_streaming(io.BytesIO(archive_bytes))
    try:
        assert verified.is_spooled
        assert all(branch.initial_state is None and branch.events is None for branch in verified.branches)
        result = WorldBackupImporter(fixture["destination_repository"]).import_verified_backup(
            fixture["scope"], verified
        )
        assert result.event_count == 5
        for branch in verified.branches:
            destination = fixture["destination_repository"].read_world(
                fixture["scope"], fixture["world_id"], branch.branch_id
            )
            source = fixture["source_repository"].read_world(
                fixture["scope"], fixture["world_id"], branch.branch_id
            )
            assert destination.state_hash == source.state_hash == branch.state_hash
            assert destination.last_seq == branch.head_seq
        with fixture["destination_db"].session_factory() as session:
            assert session.scalar(
                select(EventRow.event_id).where(EventRow.world_id == fixture["world_id"]).limit(1)
            ) is not None
            assert session.scalar(
                select(CommandRow.command_id).where(CommandRow.world_id == fixture["world_id"]).limit(1)
            ) is not None
            assert session.scalar(
                select(LedgerPostingRow.posting_id).where(LedgerPostingRow.event_id.in_(
                    select(EventRow.event_id).where(EventRow.world_id == fixture["world_id"])
                )).limit(1)
            ) is not None
    finally:
        verified.close()


def test_streamed_restore_rechecks_fork_head_and_rolls_back_before_commit(restore_fixture):
    fixture = restore_fixture
    verified = fixture["backup_app"].validate_archive_file_streaming(io.BytesIO(fixture["archive_bytes"]))
    try:
        root = next(branch for branch in verified.branches if branch.parent_branch_id is None)
        changed_root = root.model_copy(update={"state_hash": "0" * 64})
        changed = verified.model_copy(update={
            "branches": tuple(changed_root if row.branch_id == root.branch_id else row for row in verified.branches)
        })
        if not changed.is_spooled:
            changed._spool = verified._spool
        with pytest.raises(BackupImportError, match="head state|state hash"):
            WorldBackupImporter(fixture["destination_repository"]).import_verified_backup(
                fixture["scope"], changed
            )
        assert fixture["destination_repository"].list_worlds(fixture["scope"]) == []

        importer = WorldBackupImporter(fixture["destination_repository"])
        def fail_verification(*_args, **_kwargs):
            raise BackupImportError("injected verification fault")

        importer._verify_spooled_rows = fail_verification
        with pytest.raises(BackupImportError, match="injected verification fault"):
            importer.import_verified_backup(fixture["scope"], verified)
        assert fixture["destination_repository"].list_worlds(fixture["scope"]) == []
    finally:
        verified.close()


def test_restore_rechecks_replay_hashes_before_any_write(restore_fixture):
    fixture = restore_fixture
    backup = fixture["verified"]
    branch = next(item for item in backup.branches if item.branch_id == fixture["root_branch_id"])
    changed_event = branch.events[0].model_copy(update={"payload": {"to_tick": 31}})
    changed_branch = branch.model_copy(update={"events": (changed_event, *branch.events[1:])})
    changed_branches = tuple(
        changed_branch if item.branch_id == branch.branch_id else item for item in backup.branches
    )
    tampered = backup.model_copy(update={"branches": changed_branches})

    with pytest.raises(BackupImportError, match="replay|tick|hash"):
        WorldBackupImporter(fixture["destination_repository"]).import_verified_backup(
            fixture["scope"], tampered
        )
    assert fixture["destination_repository"].list_worlds(fixture["scope"]) == []


def test_restore_rejects_credential_fields_even_on_typed_verified_object(restore_fixture):
    fixture = restore_fixture
    backup = fixture["verified"]
    branch = next(item for item in backup.branches if item.branch_id == fixture["root_branch_id"])
    poisoned_state = branch.initial_state.model_copy(
        update={"metadata": {**branch.initial_state.metadata, "providerSettings": {"apiKey": "never"}}}
    )
    poisoned_branch = branch.model_copy(update={"initial_state": poisoned_state})
    poisoned = backup.model_copy(
        update={
            "branches": tuple(
                poisoned_branch if item.branch_id == branch.branch_id else item for item in backup.branches
            )
        }
    )
    with pytest.raises(BackupImportError, match="credentials/configuration"):
        WorldBackupImporter(fixture["destination_repository"]).import_verified_backup(
            fixture["scope"], poisoned
        )
    assert fixture["destination_repository"].list_worlds(fixture["scope"]) == []


def test_restore_rolls_back_all_rows_if_transaction_verification_fails(restore_fixture, monkeypatch):
    fixture = restore_fixture

    def reject_write(*_args, **_kwargs):
        raise BackupImportError("forced verification failure")

    monkeypatch.setattr(WorldBackupImporter, "_verify_written_rows", staticmethod(reject_write))
    with pytest.raises(BackupImportError, match="forced verification"):
        WorldBackupImporter(fixture["destination_repository"]).import_verified_backup(
            fixture["scope"], fixture["verified"]
        )
    assert fixture["destination_repository"].list_worlds(fixture["scope"]) == []


def test_restore_preserves_source_and_imported_command_payload_is_explicitly_absent(restore_fixture):
    fixture = restore_fixture
    result = WorldBackupImporter(fixture["destination_repository"]).import_verified_backup(
        fixture["scope"], fixture["verified"]
    )
    assert result.archive_sha256 == fixture["verified"].archive_sha256
    with fixture["destination_repository"].database.session_factory() as session:
        rows = session.scalars(
            select(CommandRow).where(CommandRow.world_id == fixture["world_id"])
        ).all()
        assert rows
        for row in rows:
            receipt = fixture["destination_repository"].read_command_receipt(
                fixture["scope"], row.world_id, row.branch_id, row.command_id, row.actor_id, row.idempotency_key,
            )
            assert receipt is not None and receipt.events and not row.result_json["events"]
            assert row.payload_json == {
                "kind": "verified_backup_event_batch",
                "source_request_payload_available": False,
                "source_archive_sha256": result.archive_sha256,
                "source_command_id": str(row.command_id),
                "source_event_count": len(receipt.events),
            }
            assert "private_request" not in json.dumps(row.payload_json)
