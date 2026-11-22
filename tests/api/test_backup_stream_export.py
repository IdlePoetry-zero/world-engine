"""Out-of-core export coverage using a synthetic paged repository."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
from uuid import UUID, uuid5
import zipfile

import pytest

from world_engine.application.backups import BackupApplication, BackupArchiveError, BackupLimits
from world_engine.application import backups as backups_module
from world_engine.domain.models import (
    AccessScope,
    BranchRecord,
    EventPage,
    EventRecord,
    MembershipRole,
    WorldEntity,
    WorldRecord,
    WorldState,
    WorldView,
)
from world_engine.domain.reducers import state_hash


class SyntheticPagedRepository:
    """Produce deterministic event pages without storing the full history."""

    def __init__(self, event_count: int) -> None:
        self.event_count = event_count
        self.world_id = uuid5(UUID("41b7c63d-815e-4c75-9009-19a99585081b"), f"world-{event_count}")
        self.branch_id = uuid5(self.world_id, "main")
        self.owner_id = uuid5(self.world_id, "owner")
        self.player_id = uuid5(self.world_id, "player")
        self.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.initial_state = WorldState(
            player_id=self.player_id,
            entities={self.player_id: WorldEntity(id=self.player_id, kind="character", name="Traveler")},
        )
        self.head_state = self.initial_state.model_copy(update={"current_tick": event_count})
        self.branch = BranchRecord(
            branch_id=self.branch_id,
            world_id=self.world_id,
            name="main",
            parent_branch_id=None,
            fork_seq=0,
            head_seq=event_count,
            state_version=event_count,
            current_tick=event_count,
            state_hash=state_hash(self.head_state),
            created_at=self.created_at,
        )
        self.world = WorldRecord(
            world_id=self.world_id,
            tenant_id=uuid5(self.world_id, "tenant"),
            owner_id=self.owner_id,
            title="Synthetic paged world",
            active_branch_id=self.branch_id,
            ruleset_version="1.0.0",
            pack_version="1.0.0",
            created_at=self.created_at,
        )
        self.requested_limits: list[int] = []
        self.max_page_size = 0

    def authorize_role(self, _scope, _world_id, roles):
        assert roles == {MembershipRole.OWNER, MembershipRole.EDITOR}

    def read_world(self, _scope, _world_id, _branch_id=None):
        return WorldView(
            world=self.world,
            branch=self.branch,
            state=self.head_state,
            last_seq=self.event_count,
            state_hash=self.branch.state_hash,
        )

    def list_branches(self, _scope, _world_id):
        return [self.branch]

    def replay(self, _scope, _world_id, _branch_id, through_seq=None, **_kwargs):
        sequence = self.event_count if through_seq is None else through_seq
        state = self.initial_state if sequence == 0 else self.head_state
        return WorldView(
            world=self.world,
            branch=self.branch,
            state=state,
            last_seq=sequence,
            state_hash=state_hash(state),
        )

    def list_events(self, _scope, _world_id, branch_id, *, after_seq, limit, include_ancestors):
        assert branch_id == self.branch_id
        assert include_ancestors is False
        self.requested_limits.append(limit)
        first = after_seq + 1
        last = min(self.event_count, after_seq + limit)
        events = [
            EventRecord(
                event_id=uuid5(self.branch_id, str(sequence)),
                world_id=self.world_id,
                branch_id=self.branch_id,
                sequence=sequence,
                tick=sequence,
                type="core.time_advanced.v1",
                command_id=uuid5(self.branch_id, f"command-{sequence}"),
                actor_id=self.player_id,
                ruleset_version="1.0.0",
                payload={"to_tick": sequence},
                visibility={"policy": "public"},
                state_version_after=sequence,
                commit_boundary=True,
                committed_at=self.created_at,
            )
            for sequence in range(first, last + 1)
        ]
        self.max_page_size = max(self.max_page_size, len(events))
        return EventPage(
            branch_id=self.branch_id,
            events=events,
            next_cursor=last if events else after_seq,
            has_more=last < self.event_count,
        )


def _stream_sha256(stream) -> str:
    stream.seek(0)
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def test_v2_export_pages_and_spools_large_history_deterministically():
    event_count = 10_000
    repository = SyntheticPagedRepository(event_count)
    scope = AccessScope(tenant_id=uuid5(repository.world_id, "tenant"), user_id=repository.owner_id)
    limits = BackupLimits(
        compressed_bytes=16 * 1024 * 1024,
        uncompressed_bytes=32 * 1024 * 1024,
        members=5_000,
        member_bytes=64 * 1024,
        compression_ratio=200,
        events=event_count,
    )
    application = BackupApplication(repository, limits=limits)

    with tempfile.TemporaryFile(mode="w+b") as first, tempfile.TemporaryFile(mode="w+b") as second:
        first_size = application.export_world_to(
            scope, repository.world_id, first, format_version="2.0"
        )
        second_size = application.export_world_to(
            scope, repository.world_id, second, format_version="2.0"
        )
        assert first_size == second_size
        assert _stream_sha256(first) == _stream_sha256(second)
        assert max(repository.requested_limits) <= 32
        assert repository.max_page_size <= 32

        first.seek(0)
        with zipfile.ZipFile(first) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            branches = json.loads(archive.read("branches.json"))
            assert manifest["format_version"] == "2.0"
            branch = branches[0]
            segments = branch["event_segments"]
            assert len(segments) > 1
            assert segments[0]["first_seq"] == 1
            assert segments[-1]["last_seq"] == event_count
            assert all(item["event_count"] == item["last_seq"] - item["first_seq"] + 1 for item in segments)
            assert all(item["path"] in archive.namelist() for item in segments)
            assert all(archive.getinfo(item["path"]).file_size <= limits.member_bytes for item in segments)


def test_spooled_export_failure_leaves_caller_output_empty():
    repository = SyntheticPagedRepository(1)
    scope = AccessScope(tenant_id=uuid5(repository.world_id, "tenant"), user_id=repository.owner_id)
    application = BackupApplication(repository, limits=BackupLimits(compressed_bytes=1))
    output = io.BytesIO()
    with pytest.raises(BackupArchiveError, match="compressed size limit"):
        application.export_world_to(scope, repository.world_id, output)
    assert output.getvalue() == b""


def _replace_archive_member(archive_bytes: bytes, path: str, payload: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r") as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members[path] = payload
    manifest = json.loads(members["manifest.json"])
    row = next(item for item in manifest["members"] if item["path"] == path)
    row["size"] = len(payload)
    row["sha256"] = hashlib.sha256(payload).hexdigest()
    members["manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(members.items()):
            archive.writestr(name, data)
    return output.getvalue()


def _stream_archive(repository: SyntheticPagedRepository, *, limits: BackupLimits | None = None) -> bytes:
    scope = AccessScope(tenant_id=uuid5(repository.world_id, "tenant"), user_id=repository.owner_id)
    output = io.BytesIO()
    BackupApplication(repository, limits=limits or BackupLimits()).export_world_to(
        scope, repository.world_id, output, format_version="2.0"
    )
    return output.getvalue()


def test_streaming_file_validation_owns_lazy_events_and_checkpoints():
    repository = SyntheticPagedRepository(12)
    scope = AccessScope(tenant_id=uuid5(repository.world_id, "tenant"), user_id=repository.owner_id)
    application = BackupApplication(repository)
    archive_bytes = _stream_archive(repository)
    source = io.BytesIO(archive_bytes)
    source.seek(7)

    verified = application.validate_archive_file_streaming(source)
    spool_path = Path(verified._spool._directory.name)
    try:
        branch = verified.branches[0]
        assert branch.initial_state is None and branch.events is None
        assert source.tell() == 7
        assert verified.summary()["event_count"] == 12
        initial = verified.load_initial_state(repository.branch_id)
        head = verified.load_head_state(repository.branch_id)
        events = list(verified.iter_branch_events(repository.branch_id))
        assert initial.current_tick == 0
        assert head.current_tick == 12
        assert [row.sequence for row in events] == list(range(1, 13))
        assert events[-1].commit_boundary is True

        iterator = verified.iter_branch_events(repository.branch_id)
        assert next(iterator).sequence == 1
        verified.close()
        iterator.close()
    finally:
        verified.close()
    assert not spool_path.exists()


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (b'[{"event_id":"x","event_id":"y"}]', "duplicate keys"),
        (b"<truncate>", "complete JSON array"),
        (b"<trailing-comma>", "without trailing commas"),
    ],
)
def test_streaming_file_validation_rejects_duplicate_keys_and_truncation(replacement, message):
    repository = SyntheticPagedRepository(1)
    archive_bytes = _stream_archive(repository)
    with zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r") as archive:
        branch = json.loads(archive.read("branches.json"))[0]
        event_path = branch["event_segments"][0]["path"]
        original_events = archive.read(event_path)
    if replacement == b"<truncate>":
        assert original_events.endswith(b"]")
        replacement = original_events[:-1]
    elif replacement == b"<trailing-comma>":
        assert original_events.endswith(b"]")
        replacement = original_events[:-1] + b",]"
    corrupted = _replace_archive_member(archive_bytes, event_path, replacement)

    application = BackupApplication(repository)
    spool_type = backups_module._VerifiedBackupSpool
    spools = []

    class TrackingSpool(spool_type):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.path_for_test = Path(self._directory.name)
            spools.append(self)

    backups_module._VerifiedBackupSpool = TrackingSpool
    try:
        with pytest.raises(BackupArchiveError, match=message):
            application.validate_archive_file_streaming(io.BytesIO(corrupted))
    finally:
        backups_module._VerifiedBackupSpool = spool_type
    assert spools and spools[0]._closed
    assert not spools[0].path_for_test.exists()


def test_streaming_file_validation_enforces_event_byte_limit():
    repository = SyntheticPagedRepository(1)
    archive_bytes = _stream_archive(repository)
    with pytest.raises(BackupArchiveError, match="per-event byte limit"):
        BackupApplication(repository, limits=BackupLimits(event_bytes=32)).validate_archive_file_streaming(
            io.BytesIO(archive_bytes)
        )


def test_streaming_file_validation_reads_legacy_v1_and_empty_event_arrays():
    for event_count in (0, 3):
        repository = SyntheticPagedRepository(event_count)
        scope = AccessScope(tenant_id=uuid5(repository.world_id, "tenant"), user_id=repository.owner_id)
        output = io.BytesIO()
        BackupApplication(repository).export_world_to(
            scope, repository.world_id, output, format_version="1.0"
        )
        output.seek(0)
        with BackupApplication(repository).validate_archive_file_streaming(output) as verified:
            assert verified.format_version == "1.0"
            assert verified.summary()["event_count"] == event_count
            assert [row.sequence for row in verified.iter_branch_events(repository.branch_id)] == list(
                range(1, event_count + 1)
            )
            assert verified.load_initial_state(repository.branch_id).current_tick == 0
            assert verified.load_head_state(repository.branch_id).current_tick == event_count

