"""Backup codec, authorization, archive safety, and replay-contract tests."""

from __future__ import annotations

import io
import json
import warnings
import zipfile
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import delete

from world_engine.application.backups import (
    BackupApplication,
    BackupArchiveError,
    BackupLimits,
)
from world_engine.domain.errors import AccessDenied
from world_engine.domain.models import (
    AccessScope,
    CommandEnvelope,
    MembershipRole,
    NewWorld,
    ResourceAccount,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.domain.reducers import state_hash
from world_engine.storage.database import Database
from world_engine.storage.backup_import import WorldBackupImporter
from world_engine.storage.models import BranchRow, SnapshotRow
from world_engine.storage.repository import WorldRepository


@pytest.fixture
def backup_fixture(tmp_path):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player_id = uuid4()
    database = Database(f"sqlite:///{(tmp_path / 'worlds.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    created = repository.create_world(
        scope,
        NewWorld(
            title="Branching world",
            state=WorldState(
                player_id=player_id,
                entities={
                    player_id: WorldEntity(id=player_id, kind="character", name="Traveler")
                },
                metadata={"pack_id": "fixture"},
            ),
        ),
    )
    world_id = created.world.world_id
    root_branch_id = created.branch.branch_id
    first_ids = [uuid4(), uuid4()]
    first = repository.commit_command(
        scope,
        world_id,
        root_branch_id,
        CommandEnvelope(
            actor_id=player_id,
            command_type="wait",
            idempotency_key="first-batch",
            expected_state_version=0,
            payload={"private_command_text": "not world history"},
        ),
        [
            TimeAdvancedEvent(event_id=first_ids[0], to_tick=10),
            TimeAdvancedEvent(event_id=first_ids[1], to_tick=20),
        ],
    )
    child = repository.fork(scope, world_id, root_branch_id, first.last_seq, "Before the second day")
    root_second = repository.commit_command(
        scope,
        world_id,
        root_branch_id,
        CommandEnvelope(
            actor_id=player_id,
            command_type="wait",
            idempotency_key="root-second",
            expected_state_version=1,
            causation_ids=[first_ids[0]],
        ),
        [TimeAdvancedEvent(to_tick=30)],
    )
    child_event_id = uuid4()
    child_result = repository.commit_command(
        scope,
        world_id,
        child.branch_id,
        CommandEnvelope(
            actor_id=player_id,
            command_type="wait",
            idempotency_key="child-second",
            expected_state_version=1,
            causation_ids=[first_ids[1]],
        ),
        [TimeAdvancedEvent(event_id=child_event_id, to_tick=25)],
    )
    application = BackupApplication(repository)
    yield {
        "scope": scope,
        "database": database,
        "repository": repository,
        "application": application,
        "world_id": world_id,
        "root_branch_id": root_branch_id,
        "child_branch_id": child.branch_id,
        "first_ids": first_ids,
        "root_second_event_id": root_second.events[0].event_id,
        "child_event_id": child_result.events[0].event_id,
    }
    database.dispose()


def test_export_is_world_scoped_deterministic_and_preserves_branch_batches(backup_fixture):
    fixture = backup_fixture
    repository = fixture["repository"]
    application = fixture["application"]
    scope = fixture["scope"]
    world_id = fixture["world_id"]

    archive_bytes = application.export_world(scope, world_id)
    assert archive_bytes == application.export_world(scope, world_id)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert "world.json" in names
        assert "branches.json" in names
        assert "providers.json" not in names
        assert "settings.json" not in names
        assert "credentials.json" not in names
        assert b"private_command_text" not in b"".join(archive.read(name) for name in names)
        world_header = json.loads(archive.read("world.json"))
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format_version"] == "1.0"
        assert "owner_id" not in world_header and "tenant_id" not in world_header

    verified = application.validate_archive(archive_bytes)
    assert verified.world.world_id == world_id
    assert verified.world.active_branch_id == repository.read_world(scope, world_id).branch.branch_id
    assert {branch.branch_id for branch in verified.branches} == {
        fixture["root_branch_id"], fixture["child_branch_id"]
    }
    root = next(branch for branch in verified.branches if branch.branch_id == fixture["root_branch_id"])
    child = next(branch for branch in verified.branches if branch.branch_id == fixture["child_branch_id"])
    assert [event.event_id for event in root.events] == [
        *fixture["first_ids"], fixture["root_second_event_id"]
    ]
    assert root.events[0].commit_boundary is False
    assert root.events[1].commit_boundary is True
    assert root.events[0].state_version_after == root.events[1].state_version_after == 1
    assert root.events[2].commit_boundary is True
    assert child.parent_branch_id == root.branch_id and child.fork_seq == 2
    assert child.initial_state_version == 1
    assert child.events[0].event_id == fixture["child_event_id"]
    assert child.events[0].causation_ids == [fixture["first_ids"][1]]
    assert root.state_hash == repository.read_world(scope, world_id, root.branch_id).state_hash
    assert child.state_hash == repository.read_world(scope, world_id, child.branch_id).state_hash
    assert verified.summary()["event_count"] == 4


def test_archive_writes_and_streams_zip64_when_configured_limit_allows_it(backup_fixture, monkeypatch):
    fixture = backup_fixture
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1024)
    application = fixture["application"]
    archive = application.export_world(fixture["scope"], fixture["world_id"], format_version="2.0")
    assert b"PK\x06\x06" in archive
    with application.validate_archive_file_streaming(io.BytesIO(archive)) as verified:
        assert verified.summary()["event_count"] == 4
        assert verified.world.world_id == fixture["world_id"]


def test_archive_file_validation_hashes_in_chunks_and_restores_stream_position(backup_fixture):
    class BoundedReadStream(io.BytesIO):
        requested_sizes: list[int]

        def __init__(self, payload: bytes) -> None:
            super().__init__(payload)
            self.requested_sizes = []

        def read(self, size: int = -1) -> bytes:
            self.requested_sizes.append(size)
            if size < 0:
                raise AssertionError("validation requested an unbounded read")
            return super().read(size)

    fixture = backup_fixture
    payload = fixture["application"].export_world(fixture["scope"], fixture["world_id"])
    stream = BoundedReadStream(payload)
    stream.seek(7)
    verified = fixture["application"].validate_archive_file(stream)

    assert verified.archive_sha256 == sha256(payload).hexdigest()
    assert stream.tell() == 7
    assert max(stream.requested_sizes) <= 1024 * 1024


def test_large_event_history_uses_segmented_v2_and_restores_flattened_history(backup_fixture, tmp_path):
    fixture = backup_fixture
    repository = fixture["repository"]
    scope = fixture["scope"]
    world_id = fixture["world_id"]
    branch_id = fixture["root_branch_id"]
    initial = repository.read_world(scope, world_id, branch_id)

    # Many small committed waits force the legacy single event member beyond
    # this test's deliberately small member cap while each commit still fits.
    for offset in range(1, 21):
        current = repository.read_world(scope, world_id, branch_id)
        repository.commit_command(
            scope,
            world_id,
            branch_id,
            CommandEnvelope(
                actor_id=initial.state.player_id,
                command_type="wait",
                idempotency_key=f"segmented-wait-{offset}",
                expected_state_version=current.branch.state_version,
            ),
            [TimeAdvancedEvent(to_tick=30 + offset)],
        )

    limits = BackupLimits(member_bytes=4096)
    application = BackupApplication(repository, limits=limits)
    archive_bytes = application.export_world(scope, world_id)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        members = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        branch_rows = json.loads(archive.read("branches.json"))
        assert manifest["format_version"] == "2.0"
        assert all("events_path" not in row for row in branch_rows)
        assert any(path.startswith(f"events/{branch_id}/") for path in members)
        root_row = next(row for row in branch_rows if row["branch_id"] == str(branch_id))
        segments = root_row["event_segments"]
        assert len(segments) > 1
        assert all(segment["event_count"] == segment["last_seq"] - segment["first_seq"] + 1 for segment in segments)
        assert all(segment["path"] in members for segment in segments)

    verified = application.validate_archive(archive_bytes)
    assert verified.format_version == "2.0"
    restored_events = next(branch for branch in verified.branches if branch.branch_id == branch_id).events
    assert [event.sequence for event in restored_events] == list(range(1, len(restored_events) + 1))
    assert restored_events[-1].commit_boundary is True

    destination = Database(f"sqlite:///{(tmp_path / 'restored.db').as_posix()}")
    destination.create_schema()
    try:
        restored_repository = WorldRepository(destination)
        result = WorldBackupImporter(restored_repository).import_verified_backup(scope, verified)
        restored_view = restored_repository.read_world(scope, world_id, branch_id)
        source_view = repository.read_world(scope, world_id, branch_id)
        assert result.event_count == sum(len(branch.events) for branch in verified.branches)
        assert restored_view.state_hash == source_view.state_hash
        assert restored_view.last_seq == source_view.last_seq
    finally:
        destination.dispose()


def test_segmented_v2_rejects_rehashed_invalid_sequence_bounds(backup_fixture):
    fixture = backup_fixture
    application = fixture["application"]
    archive_bytes = application.export_world(
        fixture["scope"], fixture["world_id"], format_version="2.0"
    )
    _, members = _read_archive(archive_bytes)
    branches = json.loads(members["branches.json"])
    segment = branches[0]["event_segments"][0]
    segment["first_seq"] += 1
    members["branches.json"] = json.dumps(
        branches, separators=(",", ":"), sort_keys=True
    ).encode()
    manifest = json.loads(members["manifest.json"])
    for row in manifest["members"]:
        payload = members[row["path"]]
        row["sha256"] = sha256(payload).hexdigest()
        row["size"] = len(payload)
    members["manifest.json"] = json.dumps(
        manifest, separators=(",", ":"), sort_keys=True
    ).encode()

    with pytest.raises(BackupArchiveError, match="sequence bounds or path"):
        application.validate_archive(_repack(members))


def test_backup_preserves_pinned_branch_head_when_a_job_commits_during_export(backup_fixture, monkeypatch):
    fixture = backup_fixture
    repository = fixture["repository"]
    scope, world_id = fixture["scope"], fixture["world_id"]
    root_id = fixture["root_branch_id"]
    before = repository.read_world(scope, world_id, root_id)
    list_branches = repository.list_branches

    def list_before_another_commit(*args):
        branches = list_branches(*args)
        repository.commit_command(
            scope, world_id, root_id,
            CommandEnvelope(
                actor_id=before.state.player_id,
                command_type="wait",
                idempotency_key="simultaneous-commit",
                expected_state_version=before.branch.state_version,
            ),
            [TimeAdvancedEvent(to_tick=40)],
        )
        return branches

    monkeypatch.setattr(repository, "list_branches", list_before_another_commit)
    application = fixture["application"]
    verified = application.validate_archive(application.export_world(scope, world_id))
    root = next(branch for branch in verified.branches if branch.branch_id == root_id)
    assert root.head_seq == before.last_seq
    assert root.state_hash == before.state_hash
    assert root.events[-1].event_id == fixture["root_second_event_id"]
    assert repository.read_world(scope, world_id, root_id).state.current_tick == 40


def test_backup_export_requires_owner_or_editor_membership(backup_fixture):
    fixture = backup_fixture
    scope = fixture["scope"]
    player_id = fixture["repository"].read_world(scope, fixture["world_id"]).state.player_id
    other = AccessScope(tenant_id=scope.tenant_id, user_id=uuid4())
    fixture["repository"].grant_membership(
        scope,
        fixture["world_id"],
        other.user_id,
        MembershipRole.PLAYER,
        controlled_actor_ids=[player_id],
    )
    with pytest.raises(AccessDenied):
        fixture["application"].export_world(other, fixture["world_id"])


def test_backup_roundtrip_preserves_absent_and_explicit_null_credit_limit(tmp_path):
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player_id = uuid4()
    absent_account_id, null_account_id = uuid4(), uuid4()
    database = Database(f"sqlite:///{(tmp_path / 'credit-limits.db').as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    try:
        created = repository.create_world(
            scope,
            NewWorld(
                title="Credit limit field presence",
                state=WorldState(
                    player_id=player_id,
                    entities={player_id: WorldEntity(id=player_id, kind="character", name="Traveler")},
                    resources={
                        absent_account_id: ResourceAccount(
                            resource_id="core.coin", balance="0", account_type="production_source"
                        ),
                        null_account_id: ResourceAccount(
                            resource_id="core.coin", balance="0", account_type="production_source",
                            credit_limit=None,
                        ),
                    },
                    metadata={"pack_id": "fixture"},
                ),
            ),
        )
            # Recreate the raw field-presence shape of an older save directly.
        with database.session_factory.begin() as session:
            branch_row = session.get(BranchRow, created.branch.branch_id)
            assert branch_row is not None
            legacy_state_json = deepcopy(branch_row.initial_state_json)
            legacy_state_json["resources"][str(absent_account_id)].pop("credit_limit", None)
            legacy_state = WorldState.model_validate(legacy_state_json)
            legacy_hash = state_hash(legacy_state)
            branch_row.initial_state_json = legacy_state_json
            branch_row.state_json = deepcopy(legacy_state_json)
            branch_row.initial_state_hash = legacy_hash
            branch_row.state_hash = legacy_hash
            session.execute(delete(SnapshotRow).where(SnapshotRow.branch_id == created.branch.branch_id))
        original = repository.read_world(scope, created.world.world_id)
        application = BackupApplication(repository)
        archive_bytes = application.export_world(scope, created.world.world_id)

        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            branch_rows = json.loads(archive.read("branches.json"))
            checkpoint = json.loads(archive.read(branch_rows[0]["checkpoint_path"]))
        raw_resources = checkpoint["resources"]
        assert "credit_limit" not in raw_resources[str(absent_account_id)]
        assert raw_resources[str(null_account_id)]["credit_limit"] is None

        verified = application.validate_archive(archive_bytes)
        restored = verified.branches[0].initial_state
        assert "credit_limit" not in restored.resources[absent_account_id].model_fields_set
        assert "credit_limit" in restored.resources[null_account_id].model_fields_set
        assert state_hash(restored) == original.state_hash
        assert verified.branches[0].initial_state_hash == original.state_hash
    finally:
        database.dispose()


def test_archive_rejects_tampered_member_even_when_zip_itself_is_valid(backup_fixture):
    fixture = backup_fixture
    archive_bytes = fixture["application"].export_world(fixture["scope"], fixture["world_id"])
    names, members = _read_archive(archive_bytes)
    event_path = f"events/{fixture['root_branch_id']}.json"
    events = json.loads(members[event_path])
    events[0]["tick"] = 999
    members[event_path] = json.dumps(events, separators=(",", ":")).encode()
    manifest = json.loads(members["manifest.json"])
    for row in manifest["members"]:
        if row["path"] == event_path:
            # Keep the declared length accurate so validation reaches the
            # content digest check instead of rejecting metadata first.
            row["size"] = len(members[event_path])
    members["manifest.json"] = json.dumps(
        manifest, separators=(",", ":"), sort_keys=True
    ).encode()
    with pytest.raises(BackupArchiveError, match="SHA-256"):
        fixture["application"].validate_archive(_repack(members))


def test_archive_rejects_hash_valid_event_history_that_does_not_replay(backup_fixture):
    fixture = backup_fixture
    archive_bytes = fixture["application"].export_world(fixture["scope"], fixture["world_id"])
    _, members = _read_archive(archive_bytes)
    event_path = f"events/{fixture['root_branch_id']}.json"
    events = json.loads(members[event_path])
    events[0]["payload"]["to_tick"] = 12
    members[event_path] = json.dumps(events, separators=(",", ":"), sort_keys=True).encode()
    manifest = json.loads(members["manifest.json"])
    for row in manifest["members"]:
        data = members[row["path"]]
        row["sha256"] = sha256(data).hexdigest()
        row["size"] = len(data)
    members["manifest.json"] = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    with pytest.raises(BackupArchiveError, match="replay|hash|tick"):
        fixture["application"].validate_archive(_repack(members))


def test_archive_rejects_traversal_duplicate_names_and_duplicate_json_keys(backup_fixture):
    application = backup_fixture["application"]
    traversal = io.BytesIO()
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../outside.json", b"{}")
    with pytest.raises(BackupArchiveError, match="unsafe member path"):
        application.validate_archive(traversal.getvalue())

    duplicate = io.BytesIO()
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("manifest.json", b'{"format_id":"world-engine.world-backup","format_id":"bad"}')
    with pytest.raises(BackupArchiveError, match="duplicate keys"):
        application.validate_archive(duplicate.getvalue())

    duplicate_names = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(duplicate_names, "w") as archive:
            first = zipfile.ZipInfo("manifest.json")
            second = zipfile.ZipInfo("manifest.json")
            archive.writestr(first, b"{}")
            archive.writestr(second, b"{}")
    with pytest.raises(BackupArchiveError, match="duplicate member names"):
        application.validate_archive(duplicate_names.getvalue())


def test_archive_rejects_unexpected_members_and_provider_configuration_data(backup_fixture):
    fixture = backup_fixture
    application = fixture["application"]
    good_archive = application.export_world(fixture["scope"], fixture["world_id"])
    _, members = _read_archive(good_archive)
    members["providers.json"] = b"{}"
    with pytest.raises(BackupArchiveError, match="manifest does not describe"):
        application.validate_archive(_repack(members))

    # A new checksum does not make credential/configuration data admissible.
    _, clean_members = _read_archive(good_archive)
    branches = json.loads(clean_members["branches.json"])
    checkpoint_path = branches[0]["checkpoint_path"]
    checkpoint = json.loads(clean_members[checkpoint_path])
    checkpoint["metadata"]["credential"] = "synthetic-never-exported"
    clean_members[checkpoint_path] = json.dumps(checkpoint, separators=(",", ":"), sort_keys=True).encode()
    manifest = json.loads(clean_members["manifest.json"])
    for row in manifest["members"]:
        data = clean_members[row["path"]]
        row["sha256"] = sha256(data).hexdigest()
        row["size"] = len(data)
    clean_members["manifest.json"] = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    with pytest.raises(BackupArchiveError, match="credential/configuration"):
        application.validate_archive(_repack(clean_members))


def _read_archive(archive_bytes: bytes) -> tuple[set[str], dict[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
        return names, {name: archive.read(name) for name in names}


def _repack(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100600) << 16
            archive.writestr(info, data)
    return output.getvalue()
