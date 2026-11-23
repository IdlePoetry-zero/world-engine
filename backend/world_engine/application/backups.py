"""World-scoped, replay-verified backup archives.

This module owns the portable archive codec and validation boundary. It exports
the world projection, branch checkpoints, committed event batches, and an
optional original world package. Provider settings, credentials, commands,
jobs, conversations, and operation traces live outside this archive contract.
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import stat
import tempfile
import threading
import zipfile
import zlib
import codecs
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Literal
from uuid import UUID

from pydantic import Field, PrivateAttr, ValidationError

from world_engine.domain.errors import Conflict
from world_engine.domain.combat_validation import validate_combat_batch
from world_engine.domain.treatment_validation import validate_treatment_batch
from world_engine.domain.events import (
    parse_event, validate_adjudication_batch, validate_background_clock_batch,
    validate_historical_evidence_destruction_batch, validate_investigation_case_batch,
)
from world_engine.domain.family_events import validate_kinship_batch
from world_engine.domain.occupation_events import validate_occupation_batch
from world_engine.domain.occupation_death_validation import validate_persisted_death_vacancies
from world_engine.domain.relationship_invitation_state import validate_relationship_invitation_batch
from world_engine.domain.institutional_finance_batch import validate_institutional_finance_batch
from world_engine.domain.private_gift_validation import validate_private_gift_batch
from world_engine.domain.leadership_ballot_state import validate_leadership_ballot_batch
from world_engine.domain.safe_conduct_state import validate_safe_conduct_batch, validate_safe_conduct_travel_batch
from world_engine.rules.work_shift_validation import validate_completed_work_shift
from world_engine.domain.shipment_validation import validate_shipment_spoilage_batch
from world_engine.domain.market_pricing_validation import validate_market_price_review_batch
from world_engine.domain.events import validate_npc_attendance_scope, validate_population_membership_batch
from world_engine.domain.npc_meeting_attendance import (
    NpcMeetingAttendanceDecisionRecordedEvent,
    validate_npc_meeting_attendance_decision_batch,
)
from world_engine.domain.errors import InvalidEvent
from world_engine.domain.models import (
    AccessScope,
    BranchRecord,
    EventRecord,
    MembershipRole,
    WorldState,
    WorldView,
    ContractModel,
)
from world_engine.domain.reducers import apply_event, state_hash, state_json
from world_engine.storage.npc_plan_replay import validate_archived_promise_batch

from .world_packs import MAX_WORLD_PACK_BYTES, WorldPackArchiveError, load_world_pack


FORMAT_ID = "world-engine.world-backup"
FORMAT_VERSION = "1.0"
SEGMENTED_FORMAT_VERSION = "2.0"
WORLD_PACK_ARCHIVE_MEMBER = "world-pack.zip"
MAX_COMPRESSED_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_UNCOMPRESSED_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 50_000
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_ARCHIVE_EVENTS = 1_000_000
MAX_BRANCHES = (MAX_ARCHIVE_MEMBERS - 3) // 2

_SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "credential",
        "credentials",
        "credential_ref",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "secret_ref",
        "secret_reference",
        "secret_value",
        "client_secret",
        "bearer_token",
        "provider_key",
        "provider_secret",
        "provider_id",
        "provider_url",
        "base_url",
        "default_model",
        "provider_config",
        "provider_configuration",
        "provider_settings",
        "model_config",
        "model_settings",
    }
)


class BackupArchiveError(ValueError):
    """The archive is malformed, incompatible, too large, or fails replay checks."""


class ArchiveMember(ContractModel):
    path: str = Field(min_length=1, max_length=240)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0, le=MAX_MEMBER_BYTES)


class BackupManifest(ContractModel):
    format_id: Literal["world-engine.world-backup"]
    format_version: Literal["1.0", "2.0"]
    world_id: UUID
    members: tuple[ArchiveMember, ...]


class BackupWorld(ContractModel):
    """World metadata intentionally excludes tenant and owner account identifiers."""

    world_id: UUID
    title: str = Field(min_length=1, max_length=240)
    active_branch_id: UUID
    ruleset_version: str = Field(min_length=1, max_length=100)
    pack_version: str = Field(min_length=1, max_length=100)
    created_at: datetime


class BackupBranch(ContractModel):
    branch_id: UUID
    name: str = Field(min_length=1, max_length=160)
    parent_branch_id: UUID | None
    fork_seq: int = Field(ge=0)
    head_seq: int = Field(ge=0)
    initial_state_version: int = Field(ge=0)
    state_version: int = Field(ge=0)
    current_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    initial_state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    checkpoint_path: str = Field(min_length=1, max_length=240)
    events_path: str = Field(min_length=1, max_length=240)


class ArchiveEventSegment(ContractModel):
    """A contiguous, complete-commit event range stored as one archive member."""

    path: str = Field(min_length=1, max_length=240)
    first_seq: int = Field(ge=1)
    last_seq: int = Field(ge=1)
    event_count: int = Field(ge=1)


class BackupBranchV2(ContractModel):
    """A branch header for segmented 2.0 event storage."""

    branch_id: UUID
    name: str = Field(min_length=1, max_length=160)
    parent_branch_id: UUID | None
    fork_seq: int = Field(ge=0)
    head_seq: int = Field(ge=0)
    initial_state_version: int = Field(ge=0)
    state_version: int = Field(ge=0)
    current_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    initial_state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    checkpoint_path: str = Field(min_length=1, max_length=240)
    event_segments: tuple[ArchiveEventSegment, ...]


class VerifiedBranch(ContractModel):
    """Typed import row: source IDs and exact state hashes are preserved."""

    branch_id: UUID
    name: str = Field(min_length=1, max_length=160)
    parent_branch_id: UUID | None
    fork_seq: int = Field(ge=0)
    head_seq: int = Field(ge=0)
    initial_state_version: int = Field(ge=0)
    state_version: int = Field(ge=0)
    current_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    initial_state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    # File validation can use a disk-backed event/checkpoint store. In that
    # mode these values are None and callers use VerifiedWorldBackup's accessors.
    initial_state: WorldState | None
    events: tuple[EventRecord, ...] | None


class VerifiedWorldBackup(ContractModel):
    """A validated, replay-checked dataset ready for a staged storage importer."""

    archive_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    format_version: Literal["1.0", "2.0"]
    world: BackupWorld
    branches: tuple[VerifiedBranch, ...]
    world_pack_archive: bytes | None = Field(default=None, exclude=True, repr=False)
    _spool: Any = PrivateAttr(default=None)

    @property
    def is_spooled(self) -> bool:
        return self._spool is not None

    def summary(self) -> dict[str, Any]:
        """Return metadata suitable for a restore preview without exposing state."""
        return {
            "world_id": str(self.world.world_id),
            "title": self.world.title,
            "active_branch_id": str(self.world.active_branch_id),
            "branch_count": len(self.branches),
            "event_count": sum(branch.head_seq - branch.fork_seq for branch in self.branches),
            "archive_sha256": self.archive_sha256,
            "format_version": self.format_version,
            "state_hashes": {
                str(branch.branch_id): branch.state_hash for branch in self.branches
            },
        }

    def iter_branch_events(self, branch_id: UUID) -> Iterator[EventRecord]:
        branch = self._branch(branch_id)
        if branch.events is not None:
            return iter(branch.events)
        if self._spool is None:
            raise ValueError("This verified branch has no available event source.")
        return self._spool.iter_events(branch_id, branch.fork_seq, branch.head_seq)

    def load_initial_state(self, branch_id: UUID) -> WorldState:
        branch = self._branch(branch_id)
        if branch.initial_state is not None:
            return branch.initial_state
        if self._spool is None:
            raise ValueError("This verified branch has no available checkpoint source.")
        return self._spool.load_state(branch_id, "initial")

    def load_head_state(self, branch_id: UUID) -> WorldState:
        branch = self._branch(branch_id)
        if self._spool is not None:
            return self._spool.load_state(branch_id, "head")
        state = self.load_initial_state(branch_id)
        for record in self.iter_branch_events(branch_id):
            typed = parse_event({
                "type": record.type,
                "event_id": str(record.event_id),
                "visibility": record.visibility,
                **record.payload,
            })
            state = apply_event(state, typed)
        return state

    def validate_world_pack_source(self, root_state: WorldState) -> bytes | None:
        """Recheck package data before an isolated restore writes it."""
        archive = self.world_pack_archive
        if archive is not None:
            _validate_world_pack_source(archive)
        _check_world_pack_source_digest(root_state, archive)
        return archive

    def _branch(self, branch_id: UUID) -> VerifiedBranch:
        for branch in self.branches:
            if branch.branch_id == branch_id:
                return branch
        raise KeyError(branch_id)

    def close(self) -> None:
        if self._spool is not None:
            self._spool.close()
            self._spool = None

    def __enter__(self) -> VerifiedWorldBackup:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class BackupLimits:
    compressed_bytes: int = MAX_COMPRESSED_ARCHIVE_BYTES
    uncompressed_bytes: int = MAX_UNCOMPRESSED_ARCHIVE_BYTES
    members: int = MAX_ARCHIVE_MEMBERS
    member_bytes: int = MAX_MEMBER_BYTES
    compression_ratio: int = MAX_COMPRESSION_RATIO
    events: int = MAX_ARCHIVE_EVENTS
    event_bytes: int = MAX_MEMBER_BYTES


def _ensure_scratch_directory(directory: Path | None) -> Path | None:
    if directory is None:
        return None
    path = Path(directory).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True, slots=True)
class _VerifiedMemberRange:
    offset: int
    size: int


@dataclass(frozen=True, slots=True)
class _VerifiedEventRange:
    path: str
    offset: int
    size: int
    first_seq: int | None
    last_seq: int | None
    event_count: int


class _RangeReader:
    """Read one exact member range from a shared disk spool."""

    def __init__(self, stream: BinaryIO, size: int) -> None:
        self.stream = stream
        self.remaining = size

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if size is None or size < 0:
            size = self.remaining
        data = self.stream.read(min(size, self.remaining))
        self.remaining -= len(data)
        return data


class _VerifiedBackupSpool:
    """Owned temporary disk storage for lazy, already-verified archive data."""

    def __init__(
        self, *, snapshot_limit: int, member_limit: int, event_limit: int,
        scratch_directory: Path | None = None,
    ) -> None:
        self._directory = tempfile.TemporaryDirectory(
            prefix="world-engine-verified-backup-", dir=scratch_directory,
        )
        root = Path(self._directory.name)
        self._events_path = root / "events.bin"
        self._states_path = root / "states.bin"
        self._index_path = root / "event-index.sqlite3"
        self._events_writer = self._events_path.open("w+b")
        self._states_writer = self._states_path.open("w+b")
        self._snapshot_limit = snapshot_limit
        self._member_limit = member_limit
        self._event_limit = min(event_limit, member_limit)
        self._event_size = 0
        self._snapshot_size = 0
        self._state_ranges: dict[tuple[UUID, str], _VerifiedMemberRange] = {}
        self._event_ranges: dict[UUID, list[_VerifiedEventRange]] = {}
        self._active_event_path: str | None = None
        self._active_event_offset = 0
        self._active_event_size = 0
        self._closed = False
        self._reader_lock = threading.RLock()
        self._readers: set[BinaryIO] = set()

    @property
    def index_path(self) -> str:
        return str(self._index_path)

    def begin_event_member(self, path: str) -> None:
        self._ensure_open()
        if self._active_event_path is not None:
            raise BackupArchiveError("The verified event spool received overlapping members.")
        self._events_writer.seek(0, io.SEEK_END)
        self._active_event_offset = self._events_writer.tell()
        self._active_event_size = 0
        self._active_event_path = path

    def write_event_chunk(self, chunk: bytes) -> None:
        self._ensure_open()
        if self._active_event_path is None:
            raise BackupArchiveError("The verified event spool has no active member.")
        self._active_event_size += len(chunk)
        self._event_size += len(chunk)
        if self._active_event_size > self._member_limit:
            raise BackupArchiveError("An event member exceeds the configured size limit.")
        if self._event_size > self._snapshot_limit:
            raise BackupArchiveError("Verified event data exceeds the configured scratch limit.")
        self._events_writer.write(chunk)

    def finish_event_member(
        self,
        branch_id: UUID,
        *,
        first_seq: int | None,
        last_seq: int | None,
        event_count: int,
    ) -> None:
        self._ensure_open()
        if self._active_event_path is None:
            raise BackupArchiveError("The verified event spool has no active member.")
        if event_count:
            if first_seq is None or last_seq is None:
                raise BackupArchiveError("A nonempty event member has incomplete sequence bounds.")
            self._event_ranges.setdefault(branch_id, []).append(
                _VerifiedEventRange(
                    path=self._active_event_path,
                    offset=self._active_event_offset,
                    size=self._active_event_size,
                    first_seq=first_seq,
                    last_seq=last_seq,
                    event_count=event_count,
                )
            )
        self._active_event_path = None
        self._active_event_size = 0

    def abort_event_member(self) -> None:
        self._active_event_path = None
        self._active_event_size = 0

    def store_state(self, branch_id: UUID, kind: str, data: bytes) -> None:
        self._ensure_open()
        if kind not in {"initial", "head"}:
            raise ValueError("kind must be initial or head")
        if len(data) > self._snapshot_limit - self._snapshot_size:
            raise BackupArchiveError("Verified branch snapshots exceed the configured scratch limit.")
        self._states_writer.seek(0, io.SEEK_END)
        offset = self._states_writer.tell()
        self._states_writer.write(data)
        self._state_ranges[(branch_id, kind)] = _VerifiedMemberRange(offset, len(data))
        self._snapshot_size += len(data)

    def finish_writing(self) -> None:
        if self._events_writer.closed:
            return
        self._events_writer.flush()
        self._events_writer.close()
        self._states_writer.flush()
        self._states_writer.close()

    def load_state(self, branch_id: UUID, kind: str) -> WorldState:
        self._ensure_open()
        self.finish_writing()
        try:
            item = self._state_ranges[(branch_id, kind)]
        except KeyError:
            raise KeyError((branch_id, kind)) from None
        with self._states_path.open("rb") as stream:
            stream.seek(item.offset)
            raw = stream.read(item.size)
        if len(raw) != item.size:
            raise BackupArchiveError("The verified checkpoint spool is incomplete.")
        return _validate_model(WorldState, _parse_json(raw))

    def iter_events(self, branch_id: UUID, fork_seq: int, head_seq: int) -> Iterator[EventRecord]:
        with self._reader_lock:
            self._ensure_open()
            self.finish_writing()
        expected = fork_seq + 1
        for item in self._event_ranges.get(branch_id, []):
            if item.first_seq is None or item.last_seq is None or item.first_seq != expected:
                raise BackupArchiveError("The verified event spool has a sequence gap.")
            with self._reader_lock:
                self._ensure_open()
                stream = self._events_path.open("rb")
                self._readers.add(stream)
            try:
                stream.seek(item.offset)
                reader = _RangeReader(stream, item.size)
                count = 0
                last_record: EventRecord | None = None
                for raw in _iter_json_array_values(
                    reader,
                    max_bytes=self._member_limit,
                    max_event_bytes=self._event_limit,
                ):
                    record = _validate_model(EventRecord, raw)
                    if record.sequence != expected:
                        raise BackupArchiveError("The verified event spool has a sequence gap.")
                    count += 1
                    expected += 1
                    last_record = record
                    yield record
                if count != item.event_count:
                    raise BackupArchiveError("The verified event spool range has an invalid event count.")
                if expected - 1 != item.last_seq:
                    raise BackupArchiveError("The verified event spool range has invalid sequence bounds.")
                if last_record is None or not last_record.commit_boundary:
                    raise BackupArchiveError("A verified event range ends inside a command batch.")
            finally:
                with self._reader_lock:
                    self._readers.discard(stream)
                stream.close()
        if expected != head_seq + 1:
            raise BackupArchiveError("The verified event spool does not cover its branch head.")

    def remove_index(self) -> None:
        try:
            self._index_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValueError("This verified backup has been closed.")

    def close(self) -> None:
        with self._reader_lock:
            if self._closed:
                return
            self._closed = True
            readers = tuple(self._readers)
            self._readers.clear()
            for stream in (*readers, self._events_writer, self._states_writer):
                try:
                    if not stream.closed:
                        stream.close()
                except OSError:
                    pass
            self._directory.cleanup()


DEFAULT_LIMITS = BackupLimits()


class _BytesViewStream:
    """Seekable ZIP input over the caller's bytes without copying the archive."""

    __slots__ = ("_view", "_position")

    def __init__(self, value: bytes) -> None:
        self._view = memoryview(value)
        self._position = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._view) - self._position
        start = self._position
        end = min(len(self._view), start + size)
        self._position = end
        return self._view[start:end].tobytes()

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self._position + offset
        elif whence == io.SEEK_END:
            position = len(self._view) + offset
        else:
            raise ValueError("invalid seek mode")
        if position < 0:
            raise ValueError("negative seek position")
        self._position = position
        return self._position

    def tell(self) -> int:
        return self._position

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class _SpooledMember:
    path: str
    offset: int
    size: int
    digest: str
    use_deflate: bool


@dataclass(frozen=True, slots=True)
class _SpooledEventSegment:
    first_seq: int
    last_seq: int
    event_count: int
    member: _SpooledMember


class _ExportSpool:
    """Disk-backed archive member store with bounded in-memory metadata."""

    def __init__(self, limits: BackupLimits, *, scratch_directory: Path | None = None) -> None:
        self.limits = limits
        self.file = tempfile.TemporaryFile(mode="w+b", dir=scratch_directory)
        self.members: dict[str, _SpooledMember] = {}
        self.uncompressed_bytes = 0

    def __enter__(self) -> _ExportSpool:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.file.close()

    def add_bytes(self, path: str, data: bytes) -> _SpooledMember:
        if len(data) > self.limits.member_bytes:
            raise BackupArchiveError("An archive member exceeds the configured size limit.")
        self.file.seek(0, io.SEEK_END)
        offset = self.file.tell()
        self.file.write(data)
        return self.add_range(path, offset, len(data))

    def add_range(self, path: str, offset: int, size: int) -> _SpooledMember:
        if size > self.limits.member_bytes:
            raise BackupArchiveError("An archive member exceeds the configured size limit.")
        member = _measure_spooled_member(self.file, path, offset, size, self.limits)
        self.register(member)
        return member

    def register(self, member: _SpooledMember) -> None:
        if member.path in self.members:
            raise BackupArchiveError("The export produced a duplicate archive member path.")
        if len(self.members) + 2 > self.limits.members:
            raise BackupArchiveError("The world exceeds the configured backup member limit.")
        self.uncompressed_bytes += member.size
        if self.uncompressed_bytes > self.limits.uncompressed_bytes:
            raise BackupArchiveError("The world archive exceeds the expanded size limit.")
        self.members[member.path] = member

    def rename(self, old_path: str, new_path: str) -> _SpooledMember:
        if new_path in self.members:
            raise BackupArchiveError("The export produced a duplicate archive member path.")
        member = self.members.pop(old_path)
        renamed = _SpooledMember(new_path, member.offset, member.size, member.digest, member.use_deflate)
        self.members[new_path] = renamed
        return renamed


def _measure_spooled_member(
    spool: BinaryIO,
    path: str,
    offset: int,
    size: int,
    limits: BackupLimits,
) -> _SpooledMember:
    """Hash and estimate ZIP compression without loading a member into RAM."""
    if size < 0 or size > limits.member_bytes:
        raise BackupArchiveError("An archive member exceeds the configured size limit.")
    original_position = spool.tell()
    digest = sha256()
    compressor = zlib.compressobj(level=6)
    compressed_size = 0
    remaining = size
    try:
        spool.seek(offset)
        while remaining:
            chunk = spool.read(min(64 * 1024, remaining))
            if not chunk:
                raise BackupArchiveError("The temporary archive spool ended before a member was complete.")
            digest.update(chunk)
            compressed_size += len(compressor.compress(chunk))
            remaining -= len(chunk)
        if size:
            compressed_size += len(compressor.flush())
    finally:
        spool.seek(original_position)
    use_deflate = not size or size / max(compressed_size, 1) <= limits.compression_ratio * 0.95
    return _SpooledMember(path, offset, size, digest.hexdigest(), use_deflate)


class _ArchiveMemberReader:
    """Lazily load declared members and verify their digest at first use."""

    def __init__(
        self,
        archive: zipfile.ZipFile,
        infos: dict[str, zipfile.ZipInfo],
        records: dict[str, ArchiveMember],
        limits: BackupLimits,
    ) -> None:
        self.archive = archive
        self.infos = infos
        self.records = records
        self.limits = limits
        self.consumed: set[str] = set()

    def read_bytes(self, path: str) -> bytes:
        if path in self.consumed:
            raise BackupArchiveError("An archive data member was referenced more than once.")
        info = self.infos.get(path)
        record = self.records.get(path)
        if info is None or record is None:
            raise BackupArchiveError("A declared archive data member is missing.")
        data = _read_zip_member(self.archive, info, self.limits.member_bytes)
        if (
            len(data) != info.file_size
            or len(data) != record.size
            or sha256(data).hexdigest() != record.sha256
        ):
            raise BackupArchiveError("A backup member failed its size or SHA-256 check.")
        self.consumed.add(path)
        return data

    def read_json(self, path: str) -> Any:
        return _parse_json(self.read_bytes(path))

    def iter_json_array(self, path: str, spool: _VerifiedBackupSpool) -> Iterator[Any]:
        """Verify and stream an event array while copying its bytes to the owned spool."""
        if path in self.consumed:
            raise BackupArchiveError("An archive data member was referenced more than once.")
        info = self.infos.get(path)
        record = self.records.get(path)
        if info is None or record is None:
            raise BackupArchiveError("A declared archive data member is missing.")
        spool.begin_event_member(path)
        try:
            with self.archive.open(info, mode="r") as member:
                yield from _iter_json_array_values(
                    member,
                    max_bytes=self.limits.member_bytes,
                    max_event_bytes=min(self.limits.event_bytes, self.limits.member_bytes),
                    expected_size=record.size,
                    expected_sha256=record.sha256,
                    on_chunk=spool.write_event_chunk,
                )
            self.consumed.add(path)
        except BaseException:
            spool.abort_event_member()
            raise

    def verify_all_used(self) -> None:
        if self.consumed != set(self.records):
            raise BackupArchiveError("The archive contains an unused or unverified data member.")


class BackupApplication:
    """Export authorized world history and validate untrusted backup archives.

    Restoring a verified dataset is intentionally a separate storage operation.
    The importer must use an isolated staged database namespace, preserve source
    world/branch/event IDs, set the archived active branch atomically, and verify
    the exact projections before making that namespace selectable.
    """

    def __init__(
        self,
        repository: Any,
        *,
        limits: BackupLimits = DEFAULT_LIMITS,
        scratch_directory: Path | None = None,
    ) -> None:
        self.repository = repository
        self.limits = limits
        self.scratch_directory = _ensure_scratch_directory(scratch_directory)

    def export_world(
        self,
        scope: AccessScope,
        world_id: UUID,
        *,
        format_version: Literal["1.0", "2.0"] | None = None,
    ) -> bytes:
        """Create a deterministic world-only archive for an owner or editor."""
        with io.BytesIO() as output:
            self.export_world_to(scope, world_id, output, format_version=format_version)
            return output.getvalue()

    def export_world_to(
        self,
        scope: AccessScope,
        world_id: UUID,
        output: BinaryIO,
        *,
        format_version: Literal["1.0", "2.0"] | None = None,
    ) -> int:
        """Write a deterministic world-only archive to an empty seekable stream.

        Small exports retain the established 1.0 layout. When the legacy
        per-branch event member would exceed the member limit, automatic mode
        switches to 2.0 segmented storage. ``format_version`` can force either
        supported layout for compatibility checks and tests. Member data is
        disk-spooled and branch events are consumed in pinned, bounded pages.
        """
        if format_version not in {None, "1.0", "2.0"}:
            raise ValueError("format_version must be '1.0', '2.0', or None.")
        self.repository.authorize_role(scope, world_id, {MembershipRole.OWNER, MembershipRole.EDITOR})
        active_view: WorldView = self.repository.read_world(scope, world_id)
        world = BackupWorld(
            world_id=active_view.world.world_id,
            title=active_view.world.title,
            active_branch_id=active_view.world.active_branch_id,
            ruleset_version=active_view.world.ruleset_version,
            pack_version=active_view.world.pack_version,
            created_at=active_view.world.created_at,
        )
        del active_view
        source_branches: list[BranchRecord] = self.repository.list_branches(scope, world_id)
        if not source_branches or len(source_branches) > min(MAX_BRANCHES, (self.limits.members - 3) // 2):
            raise BackupArchiveError("The world has an unsupported number of branches for this archive policy.")

        records: dict[UUID, dict[str, Any]] = {
            branch.branch_id: {"record": branch} for branch in source_branches
        }
        ordered_records = _topological_source_records(records)
        if world.active_branch_id not in records:
            raise Conflict("The active branch is not present in the authorized world.")

        version_sources: dict[UUID, tuple[UUID, int] | None] = {}
        version_queries: dict[UUID, set[int]] = {}
        for branch_id in ordered_records:
            record: BranchRecord = records[branch_id]["record"]
            if record.parent_branch_id is None:
                version_sources[branch_id] = None
                continue
            source = _version_lookup_source(records, record.parent_branch_id, record.fork_seq)
            version_sources[branch_id] = source
            if source is not None:
                version_queries.setdefault(source[0], set()).add(source[1])

        with _ExportSpool(self.limits, scratch_directory=self.scratch_directory) as spool:
            branch_material: dict[UUID, dict[str, Any]] = {}
            captured_versions: dict[tuple[UUID, int], int] = {}
            total_event_count = 0

            for branch_id in ordered_records:
                record: BranchRecord = records[branch_id]["record"]
                checkpoint_view = self.repository.replay(
                    scope, world_id, branch_id, through_seq=record.fork_seq,
                )
                if checkpoint_view.last_seq != record.fork_seq:
                    raise Conflict("Branch fork checkpoint could not be verified.")
                if record.parent_branch_id is None:
                    read_source = getattr(self.repository, "read_world_pack_archive", None)
                    world_pack_archive = read_source(scope, world_id) if read_source is not None else None
                    _check_world_pack_source_digest(checkpoint_view.state, world_pack_archive)
                    if world_pack_archive is not None:
                        _validate_world_pack_source(world_pack_archive)
                        spool.add_bytes(WORLD_PACK_ARCHIVE_MEMBER, world_pack_archive)
                    del world_pack_archive
                checkpoint_path = f"checkpoints/{branch_id}.json"
                checkpoint_payload = state_json(checkpoint_view.state)
                _reject_provider_configuration(checkpoint_payload)
                checkpoint_member = spool.add_bytes(checkpoint_path, _canonical_json(checkpoint_payload))
                checkpoint_hash = checkpoint_view.state_hash
                del checkpoint_view, checkpoint_payload

                segments, branch_event_count = self._spool_local_event_segments(
                    scope,
                    world_id,
                    record,
                    spool,
                    requested_versions=version_queries.get(branch_id, set()),
                    captured_versions=captured_versions,
                    remaining_event_limit=min(MAX_ARCHIVE_EVENTS, self.limits.events) - total_event_count,
                )
                total_event_count += branch_event_count

                head_view = self.repository.replay(
                    scope, world_id, branch_id, through_seq=record.head_seq,
                )
                if (
                    head_view.last_seq != record.head_seq
                    or head_view.state_hash != record.state_hash
                    or head_view.state.current_tick != record.current_tick
                ):
                    raise Conflict("Branch head failed its projection integrity check.")
                del head_view

                source = version_sources[branch_id]
                if source is None:
                    initial_state_version = 0
                else:
                    try:
                        initial_state_version = captured_versions[source]
                    except KeyError:
                        raise Conflict("A branch fork sequence does not resolve to committed history.") from None
                branch_material[branch_id] = {
                    "record": record,
                    "checkpoint_path": checkpoint_member.path,
                    "checkpoint_hash": checkpoint_hash,
                    "initial_state_version": initial_state_version,
                    "segments": segments,
                }

            selected_version = format_version
            if selected_version is None:
                selected_version = (
                    SEGMENTED_FORMAT_VERSION
                    if any(len(item["segments"]) > 1 for item in branch_material.values())
                    else FORMAT_VERSION
                )

            branch_rows: list[BackupBranch | BackupBranchV2] = []
            for branch_id in ordered_records:
                item = branch_material[branch_id]
                record: BranchRecord = item["record"]
                segments: list[_SpooledEventSegment] = item["segments"]
                checkpoint_path = item["checkpoint_path"]
                common = {
                    "branch_id": record.branch_id,
                    "name": record.name,
                    "parent_branch_id": record.parent_branch_id,
                    "fork_seq": record.fork_seq,
                    "head_seq": record.head_seq,
                    "initial_state_version": item["initial_state_version"],
                    "state_version": record.state_version,
                    "current_tick": record.current_tick,
                    "initial_state_hash": item["checkpoint_hash"],
                    "state_hash": record.state_hash,
                    "created_at": record.created_at,
                    "checkpoint_path": checkpoint_path,
                }
                if selected_version == FORMAT_VERSION:
                    if len(segments) > 1:
                        raise BackupArchiveError(
                            "The 1.0 event member exceeds the configured size limit; use segmented 2.0."
                        )
                    events_path = f"events/{branch_id}.json"
                    if segments:
                        spool.rename(segments[0].member.path, events_path)
                    else:
                        spool.add_bytes(events_path, b"[]")
                    branch_rows.append(BackupBranch(**common, events_path=events_path))
                else:
                    descriptors = tuple(
                        ArchiveEventSegment(
                            path=segment.member.path,
                            first_seq=segment.first_seq,
                            last_seq=segment.last_seq,
                            event_count=segment.event_count,
                        )
                        for segment in segments
                    )
                    branch_rows.append(BackupBranchV2(**common, event_segments=descriptors))

            world_bytes = _canonical_json(world.model_dump(mode="json"))
            branches_bytes = _canonical_json([row.model_dump(mode="json") for row in branch_rows])
            spool.add_bytes("world.json", world_bytes)
            spool.add_bytes("branches.json", branches_bytes)
            del world_bytes, branches_bytes

            if len(spool.members) + 1 > self.limits.members:
                raise BackupArchiveError("The world exceeds the configured backup member limit.")
            manifest_size = len(_canonical_json({
                "format_id": FORMAT_ID,
                "format_version": selected_version,
                "world_id": str(world.world_id),
                "members": [
                    {"path": path, "sha256": member.digest, "size": member.size}
                    for path, member in sorted(spool.members.items())
                ],
            }))
            if spool.uncompressed_bytes + manifest_size > self.limits.uncompressed_bytes:
                raise BackupArchiveError("The world archive exceeds the expanded size limit.")
            manifest = BackupManifest(
                format_id=FORMAT_ID,
                format_version=selected_version,
                world_id=world.world_id,
                members=tuple(
                    ArchiveMember(path=path, sha256=member.digest, size=member.size)
                    for path, member in sorted(spool.members.items())
                ),
            )
            return _write_spooled_zip(spool.file, spool.members, manifest, output=output, limits=self.limits)

    def _spool_local_event_segments(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch: BranchRecord,
        spool: _ExportSpool,
        *,
        requested_versions: set[int],
        captured_versions: dict[tuple[UUID, int], int],
        remaining_event_limit: int,
    ) -> tuple[list[_SpooledEventSegment], int]:
        segments: list[_SpooledEventSegment] = []
        total = 0
        cursor = branch.fork_seq
        expected_sequence = cursor + 1
        pending_command_id: UUID | None = None
        previous_command_id: UUID | None = None
        batch_file: BinaryIO | None = None
        batch_count = 0
        batch_size = 0
        batch_first_seq: int | None = None
        batch_last_seq: int | None = None
        segment_offset: int | None = None
        segment_size = 0
        segment_count = 0
        segment_first_seq: int | None = None
        segment_last_seq: int | None = None

        def finish_segment() -> None:
            nonlocal segment_offset, segment_size, segment_count, segment_first_seq, segment_last_seq
            if segment_count == 0 or segment_offset is None or segment_first_seq is None or segment_last_seq is None:
                return
            spool.file.write(b"]")
            segment_size += 1
            if segment_size > self.limits.member_bytes:
                raise BackupArchiveError("A segmented event member exceeds the configured size limit.")
            path = f"events/{branch.branch_id}/{segment_first_seq}-{segment_last_seq}.json"
            member = spool.add_range(path, segment_offset, segment_size)
            segments.append(_SpooledEventSegment(
                first_seq=segment_first_seq,
                last_seq=segment_last_seq,
                event_count=segment_count,
                member=member,
            ))
            segment_offset = None
            segment_size = 0
            segment_count = 0
            segment_first_seq = None
            segment_last_seq = None

        try:
            for event in self._iter_local_events(scope, world_id, branch):
                if event.sequence != expected_sequence:
                    raise Conflict("Branch-local event history contains a sequence gap.")
                expected_sequence += 1
                total += 1
                if total > remaining_event_limit:
                    raise BackupArchiveError("The world history exceeds the configured backup event limit.")
                if event.sequence in requested_versions:
                    captured_versions[(branch.branch_id, event.sequence)] = event.state_version_after
                if pending_command_id is None:
                    if event.command_id == previous_command_id:
                        raise Conflict("A stored command was split across branch event pages.")
                    pending_command_id = event.command_id
                    batch_file = tempfile.SpooledTemporaryFile(
                        max_size=min(1024 * 1024, self.limits.member_bytes), mode="w+b",
                        dir=self.scratch_directory,
                    )
                    batch_count = 0
                    batch_size = 0
                    batch_first_seq = event.sequence
                elif event.command_id != pending_command_id:
                    raise Conflict("A branch event batch is incomplete before its commit boundary.")

                payload = event.model_dump(mode="json")
                _reject_provider_configuration(payload)
                encoded = _canonical_json(payload)
                delimiter_size = 1 if batch_count else 0
                if batch_size + delimiter_size + len(encoded) + 2 > self.limits.member_bytes:
                    raise BackupArchiveError(
                        "A single committed event batch exceeds the segmented archive member limit."
                    )
                assert batch_file is not None
                if delimiter_size:
                    batch_file.write(b",")
                batch_file.write(encoded)
                batch_size += delimiter_size + len(encoded)
                batch_count += 1
                batch_last_seq = event.sequence

                if event.commit_boundary:
                    if batch_first_seq is None or batch_last_seq is None or batch_count <= 0:
                        raise Conflict("A committed event batch is empty.")
                    candidate_size = (segment_size if segment_count else 1) + (1 if segment_count else 0) + batch_size + 1
                    if segment_count and candidate_size > self.limits.member_bytes:
                        finish_segment()
                        candidate_size = 1 + batch_size + 1
                    if candidate_size > self.limits.member_bytes:
                        raise BackupArchiveError(
                            "A single committed event batch exceeds the segmented archive member limit."
                        )
                    if segment_count == 0:
                        spool.file.seek(0, io.SEEK_END)
                        segment_offset = spool.file.tell()
                        spool.file.write(b"[")
                        segment_size = 1
                        segment_first_seq = batch_first_seq
                    else:
                        spool.file.write(b",")
                        segment_size += 1
                    batch_file.seek(0)
                    while True:
                        chunk = batch_file.read(64 * 1024)
                        if not chunk:
                            break
                        spool.file.write(chunk)
                    segment_size += batch_size
                    segment_count += batch_count
                    segment_last_seq = batch_last_seq
                    previous_command_id = pending_command_id
                    batch_file.close()
                    batch_file = None
                    pending_command_id = None
                    batch_count = 0
                    batch_size = 0
                    batch_first_seq = None
                    batch_last_seq = None

            if pending_command_id is not None or batch_count or batch_file is not None:
                raise Conflict("Branch event history ends before a command commit boundary.")
            finish_segment()
        finally:
            if batch_file is not None:
                batch_file.close()

        if expected_sequence != branch.head_seq + 1:
            raise Conflict("Branch history ended before its pinned committed head.")
        if not segments and branch.head_seq != branch.fork_seq:
            raise Conflict("Branch history is missing events after its fork checkpoint.")
        return segments, total

    def _iter_local_events(self, scope: AccessScope, world_id: UUID, branch: BranchRecord) -> Iterator[EventRecord]:
        """Yield pinned branch-local events a bounded page at a time."""
        cursor = branch.fork_seq
        while cursor < branch.head_seq:
            previous_cursor = cursor
            limit = min(32, branch.head_seq - cursor)
            page = self.repository.list_events(
                scope,
                world_id,
                branch.branch_id,
                after_seq=cursor,
                limit=limit,
                include_ancestors=False,
            )
            if not page.events or len(page.events) > limit:
                raise Conflict("Branch history ended before its pinned committed head.")
            expected = cursor + 1
            for event in page.events:
                if event.sequence != expected or event.sequence > branch.head_seq:
                    raise Conflict("Branch-local event history contains an invalid pinned range.")
                expected += 1
                yield event
            next_cursor = page.next_cursor
            if next_cursor <= previous_cursor:
                raise Conflict("Branch history paging did not advance.")
            if next_cursor != page.events[-1].sequence:
                raise Conflict("Branch history cursor does not match its final event.")
            if not page.has_more and next_cursor < branch.head_seq:
                raise Conflict("Branch history ended before its pinned committed head.")
            cursor = next_cursor
            del page

    def validate_archive(self, archive_bytes: bytes) -> VerifiedWorldBackup:
        """Validate member safety, hashes, schemas, branch lineage, and exact replay."""
        if not isinstance(archive_bytes, bytes):
            raise BackupArchiveError("A backup archive must be supplied as bytes.")
        return self._validate_archive_source(_BytesViewStream(archive_bytes))

    def validate_archive_file(self, source: BinaryIO) -> VerifiedWorldBackup:
        """Validate a seekable archive file without loading the whole upload.

        The input is read in bounded chunks for its compressed-size check and
        SHA-256 digest, then rewound for ZIP validation. Its original position
        is restored before return or error.
        """
        return self._validate_archive_source(source)

    def validate_archive_file_streaming(self, source: BinaryIO) -> VerifiedWorldBackup:
        """Validate and replay a seekable archive using an owned disk spool.

        The returned ``VerifiedWorldBackup`` owns temporary files and must be
        closed explicitly (prefer ``with``). Branch headers stay in memory;
        events and initial/head snapshots are available through its lazy
        accessors. The established eager file/bytes APIs are unchanged.
        """
        return self._validate_archive_source(source, streaming=True)

    def _validate_archive_source(self, source: BinaryIO, *, streaming: bool = False) -> VerifiedWorldBackup:
        original_position: int | None = None
        try:
            if not callable(getattr(source, "read", None)) or not callable(getattr(source, "seek", None)):
                raise BackupArchiveError("A backup archive must be a seekable binary stream.")
            if callable(getattr(source, "seekable", None)) and not source.seekable():
                raise BackupArchiveError("A backup archive must be a seekable binary stream.")
            original_position = source.tell()
            source.seek(0, io.SEEK_END)
            source_size = source.tell()
            if source_size <= 0 or source_size > self.limits.compressed_bytes:
                raise BackupArchiveError("The archive is empty or exceeds the compressed size limit.")
            source.seek(0, io.SEEK_SET)
            digest = sha256()
            bytes_read = 0
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise BackupArchiveError("A backup archive stream must return bytes.")
                bytes_read += len(chunk)
                if bytes_read > self.limits.compressed_bytes:
                    raise BackupArchiveError("The archive exceeds the compressed size limit.")
                digest.update(chunk)
            if bytes_read != source_size:
                raise BackupArchiveError("The backup archive changed while it was being read.")
            source.seek(0, io.SEEK_SET)
            return self._validate_archive(source, digest.hexdigest(), streaming=streaming)
        except BackupArchiveError:
            raise
        except Exception:
            # Do not expose parser internals or attacker-controlled payloads in errors/logs.
            raise BackupArchiveError("The backup archive is malformed or incompatible.") from None
        finally:
            if original_position is not None:
                try:
                    source.seek(original_position, io.SEEK_SET)
                except Exception:
                    pass

    def _validate_archive(
        self, source: BinaryIO, archive_sha256: str, *, streaming: bool = False
    ) -> VerifiedWorldBackup:
        if not zipfile.is_zipfile(source):
            raise BackupArchiveError("The file is not a supported ZIP backup archive.")
        source.seek(0)
        try:
            with zipfile.ZipFile(source, mode="r") as archive:
                return self._validate_open_archive(archive, archive_sha256, streaming=streaming)
        except BackupArchiveError:
            raise
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error):
            raise BackupArchiveError("The ZIP archive is corrupt, encrypted, or unsupported.") from None

    def _validate_open_archive(
        self,
        archive: zipfile.ZipFile,
        archive_sha256: str,
        *,
        streaming: bool = False,
    ) -> VerifiedWorldBackup:
        if archive.comment:
            raise BackupArchiveError("ZIP archive comments are not supported.")
        infos = archive.infolist()
        if not infos or len(infos) > self.limits.members:
            raise BackupArchiveError("The archive member count is outside the configured limit.")
        names: set[str] = set()
        declared_total = 0
        info_by_name: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            _validate_zip_info(info, names, self.limits)
            declared_total += info.file_size
            if declared_total > self.limits.uncompressed_bytes:
                raise BackupArchiveError("The archive exceeds the expanded size limit.")
            info_by_name[info.filename] = info

        if "manifest.json" not in info_by_name:
            raise BackupArchiveError("The backup manifest is missing.")
        manifest_info = info_by_name["manifest.json"]
        manifest_bytes = _read_zip_member(archive, manifest_info, self.limits.member_bytes)
        if len(manifest_bytes) != manifest_info.file_size:
            raise BackupArchiveError("A ZIP member length does not match its directory record.")
        manifest = _validate_model(BackupManifest, _parse_json(manifest_bytes))
        if len(manifest.members) + 1 != len(info_by_name):
            raise BackupArchiveError("The manifest does not describe every archive member.")
        if manifest.world_id is None:
            raise BackupArchiveError("The manifest world identifier is missing.")

        member_records: dict[str, ArchiveMember] = {}
        for member in manifest.members:
            if member.path in member_records or member.path == "manifest.json":
                raise BackupArchiveError("The manifest contains a duplicate or reserved member path.")
            _validate_member_path(member.path)
            info = info_by_name.get(member.path)
            if info is None or info.file_size != member.size:
                raise BackupArchiveError("The manifest member list does not match the ZIP contents.")
            member_records[member.path] = member
        if set(member_records) != (set(info_by_name) - {"manifest.json"}):
            raise BackupArchiveError("The manifest member list does not match the ZIP contents.")
        member_reader = _ArchiveMemberReader(archive, info_by_name, member_records, self.limits)

        world = _validate_model(BackupWorld, member_reader.read_json("world.json"))
        if world.world_id != manifest.world_id:
            raise BackupArchiveError("The manifest and world identifiers do not match.")
        raw_branches = member_reader.read_json("branches.json")
        if not isinstance(raw_branches, list) or not raw_branches:
            raise BackupArchiveError("A backup must contain at least one branch.")
        if len(raw_branches) > min(MAX_BRANCHES, (self.limits.members - 3) // 2):
            raise BackupArchiveError("The branch count exceeds the configured archive limit.")
        branch_type = BackupBranch if manifest.format_version == "1.0" else BackupBranchV2
        branch_rows = tuple(_validate_model(branch_type, row) for row in raw_branches)

        expected_files = {"world.json", "branches.json"}
        has_world_pack_archive = WORLD_PACK_ARCHIVE_MEMBER in member_records
        if has_world_pack_archive:
            expected_files.add(WORLD_PACK_ARCHIVE_MEMBER)
        for row in branch_rows:
            expected_files.add(row.checkpoint_path)
            if manifest.format_version == "1.0":
                expected_files.add(row.events_path)
            else:
                expected_files.update(segment.path for segment in row.event_segments)
        if len(expected_files) != len(member_records):
            raise BackupArchiveError("Each branch data member must have one canonical archive path.")
        if set(member_records) != expected_files:
            raise BackupArchiveError("The archive contains unsupported or missing world data files.")

        world_pack_archive = None
        if has_world_pack_archive:
            if not 0 < member_records[WORLD_PACK_ARCHIVE_MEMBER].size <= MAX_WORLD_PACK_BYTES:
                raise BackupArchiveError("The nested world package exceeds the supported size limits.")
            world_pack_archive = member_reader.read_bytes(WORLD_PACK_ARCHIVE_MEMBER)
            _validate_world_pack_source(world_pack_archive)

        if streaming:
            return self._validate_open_archive_streaming(
                archive_sha256,
                manifest,
                world,
                branch_rows,
                member_reader,
                world_pack_archive,
            )

        branches: list[VerifiedBranch] = []
        total_events = 0
        for row in branch_rows:
            _validate_member_path(row.checkpoint_path)
            checkpoint_raw = member_reader.read_json(row.checkpoint_path)
            _reject_provider_configuration(checkpoint_raw)
            checkpoint = _validate_model(WorldState, checkpoint_raw)
            if row.parent_branch_id is None:
                _check_world_pack_source_digest(checkpoint, world_pack_archive)
            if manifest.format_version == "1.0":
                assert isinstance(row, BackupBranch)
                _validate_member_path(row.events_path)
                raw_events = member_reader.read_json(row.events_path)
                if not isinstance(raw_events, list):
                    raise BackupArchiveError("A branch event member must contain an array.")
                _reject_provider_configuration(raw_events)
                events = tuple(_validate_model(EventRecord, event) for event in raw_events)
                del raw_events
            else:
                assert isinstance(row, BackupBranchV2)
                events = _read_segmented_events(row, member_reader)
            total_events += len(events)
            if total_events > min(MAX_ARCHIVE_EVENTS, self.limits.events):
                raise BackupArchiveError("The archive event count exceeds the configured limit.")
            branches.append(
                VerifiedBranch(
                    branch_id=row.branch_id,
                    name=row.name,
                    parent_branch_id=row.parent_branch_id,
                    fork_seq=row.fork_seq,
                    head_seq=row.head_seq,
                    initial_state_version=row.initial_state_version,
                    state_version=row.state_version,
                    current_tick=row.current_tick,
                    initial_state_hash=row.initial_state_hash,
                    state_hash=row.state_hash,
                    created_at=row.created_at,
                    initial_state=checkpoint,
                    events=events,
                )
            )

        member_reader.verify_all_used()
        ordered = _validate_and_replay(world, tuple(branches))
        return VerifiedWorldBackup(
            archive_sha256=archive_sha256,
            format_version=manifest.format_version,
            world=world,
            branches=ordered,
            world_pack_archive=world_pack_archive,
        )

    def _validate_open_archive_streaming(
        self,
        archive_sha256: str,
        manifest: BackupManifest,
        world: BackupWorld,
        branch_rows: tuple[BackupBranch | BackupBranchV2, ...],
        member_reader: _ArchiveMemberReader,
        world_pack_archive: bytes | None,
    ) -> VerifiedWorldBackup:
        """Replay from the archive stream while retaining event data on disk."""
        spool = _VerifiedBackupSpool(
            snapshot_limit=self.limits.uncompressed_bytes,
            member_limit=self.limits.member_bytes,
            event_limit=self.limits.event_bytes,
            scratch_directory=self.scratch_directory,
        )
        connection: sqlite3.Connection | None = None
        succeeded = False
        try:
            connection = sqlite3.connect(spool.index_path)
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("PRAGMA cache_size=-2048")
            connection.execute(
                "CREATE TABLE event_origins (event_id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, sequence INTEGER NOT NULL)"
            )
            connection.execute("CREATE TABLE command_origins (command_id TEXT PRIMARY KEY)")

            row_by_id = {row.branch_id: row for row in branch_rows}
            if len(row_by_id) != len(branch_rows):
                raise BackupArchiveError("The backup contains a duplicate branch identifier.")
            if world.active_branch_id not in row_by_id:
                raise BackupArchiveError("The active branch is missing from the archive.")
            if len({row.name for row in branch_rows}) != len(branch_rows):
                raise BackupArchiveError("Branch names must be unique within a world.")
            roots = [row for row in branch_rows if row.parent_branch_id is None]
            if len(roots) != 1 or roots[0].fork_seq != 0:
                raise BackupArchiveError("The archive must contain exactly one genesis branch at sequence zero.")
            for row in branch_rows:
                if row.head_seq < row.fork_seq or row.state_version < row.initial_state_version:
                    raise BackupArchiveError("A branch head precedes its fork checkpoint.")
                if row.parent_branch_id is not None:
                    parent = row_by_id.get(row.parent_branch_id)
                    if parent is None or parent.branch_id == row.branch_id:
                        raise BackupArchiveError("A branch references a missing or invalid parent.")
                    if row.fork_seq > parent.head_seq:
                        raise BackupArchiveError("A branch fork exceeds its parent history.")

            ordered_rows = _topological_archive_rows(row_by_id)
            fork_targets: dict[UUID, set[int]] = {row.branch_id: set() for row in ordered_rows}
            for row in ordered_rows:
                if row.parent_branch_id is None:
                    continue
                source_id, source_seq = _effective_archive_branch_point(
                    row_by_id, row.parent_branch_id, row.fork_seq
                )
                source_row = row_by_id[source_id]
                if source_seq > source_row.fork_seq:
                    fork_targets[source_id].add(source_seq)

            point_hashes: dict[tuple[UUID, int], tuple[str, int, int]] = {}
            verified_rows: list[VerifiedBranch] = []
            total_events = 0
            connection.execute("BEGIN")

            for row in ordered_rows:
                _validate_member_path(row.checkpoint_path)
                checkpoint_raw = member_reader.read_bytes(row.checkpoint_path)
                checkpoint_json = _parse_json(checkpoint_raw)
                _reject_provider_configuration(checkpoint_json)
                checkpoint = _validate_model(WorldState, checkpoint_json)
                checkpoint_hash = state_hash(checkpoint)
                if checkpoint_hash != row.initial_state_hash:
                    raise BackupArchiveError("A branch fork checkpoint does not match its state hash.")
                spool.store_state(row.branch_id, "initial", checkpoint_raw)
                point_hashes[(row.branch_id, row.fork_seq)] = (
                    checkpoint_hash,
                    row.initial_state_version,
                    checkpoint.current_tick,
                )
                if row.parent_branch_id is None:
                    _check_world_pack_source_digest(checkpoint, world_pack_archive)
                    if row.initial_state_version != 0:
                        raise BackupArchiveError("The genesis branch must start at state version zero.")
                    if checkpoint.institutional_credit_contracts:
                        raise BackupArchiveError("Institutional credit needs committed events after genesis.")
                else:
                    source_id, source_seq = _effective_archive_branch_point(
                        row_by_id, row.parent_branch_id, row.fork_seq
                    )
                    source_point = point_hashes.get((source_id, source_seq))
                    if (
                        source_point is None
                        or source_point[0] != row.initial_state_hash
                        or source_point[1] != row.initial_state_version
                        or source_point[2] != checkpoint.current_tick
                    ):
                        raise BackupArchiveError("A branch fork checkpoint does not match its parent history.")

                state = checkpoint
                current_version = row.initial_state_version
                expected_sequence = row.fork_seq + 1
                branch_event_count = 0
                pending: list[EventRecord] = []
                pending_command: UUID | None = None
                previous_segment_command: UUID | None = None
                last_completed_command: UUID | None = None

                if manifest.format_version == "1.0":
                    assert isinstance(row, BackupBranch)
                    segment_specs: tuple[tuple[str, int, int, int | None], ...] = (
                        (row.events_path, expected_sequence, row.head_seq, None),
                    )
                else:
                    assert isinstance(row, BackupBranchV2)
                    specs: list[tuple[str, int, int, int | None]] = []
                    segment_expected_sequence = expected_sequence
                    for segment in row.event_segments:
                        _validate_member_path(segment.path)
                        if (
                            segment.first_seq != segment_expected_sequence
                            or segment.last_seq < segment.first_seq
                            or segment.event_count != segment.last_seq - segment.first_seq + 1
                            or segment.path != f"events/{row.branch_id}/{segment.first_seq}-{segment.last_seq}.json"
                        ):
                            raise BackupArchiveError("An event segment has invalid sequence bounds or path.")
                        specs.append((segment.path, segment.first_seq, segment.last_seq, segment.event_count))
                        segment_expected_sequence = segment.last_seq + 1
                    if segment_expected_sequence != row.head_seq + 1:
                        raise BackupArchiveError("Segmented branch events do not cover the declared branch head.")
                    segment_specs = tuple(specs)

                for segment_path, declared_first, declared_last, declared_count in segment_specs:
                    segment_first: int | None = None
                    segment_last: int | None = None
                    segment_count = 0
                    for raw in member_reader.iter_json_array(segment_path, spool):
                        _reject_provider_configuration(raw)
                        event = _validate_model(EventRecord, raw)
                        if event.world_id != world.world_id or event.branch_id != row.branch_id:
                            raise BackupArchiveError("An event is associated with a different world or branch.")
                        if event.sequence != expected_sequence:
                            raise BackupArchiveError("A branch event sequence is not contiguous.")
                        if event.sequence > row.head_seq:
                            raise BackupArchiveError("A branch event extends beyond its committed head.")
                        if segment_first is None:
                            segment_first = event.sequence
                            if previous_segment_command is not None and event.command_id == previous_segment_command:
                                raise BackupArchiveError("A command batch was split across event segments.")
                        segment_last = event.sequence
                        segment_count += 1
                        branch_event_count += 1
                        total_events += 1
                        if total_events > min(MAX_ARCHIVE_EVENTS, self.limits.events):
                            raise BackupArchiveError("The archive event count exceeds the configured limit.")

                        if pending_command is None:
                            pending_command = event.command_id
                            try:
                                connection.execute(
                                    "INSERT INTO command_origins(command_id) VALUES (?)",
                                    (str(event.command_id),),
                                )
                            except sqlite3.IntegrityError:
                                raise BackupArchiveError("Command identifiers must be unique throughout the archive.") from None
                        elif event.command_id != pending_command:
                            raise BackupArchiveError("An event batch ended before its commit boundary.")

                        if pending:
                            first = pending[0]
                            if (
                                event.actor_id != first.actor_id
                                or event.causation_ids != first.causation_ids
                                or event.correlation_id != first.correlation_id
                                or event.ruleset_version != first.ruleset_version
                                or event.state_version_after != first.state_version_after
                            ):
                                raise BackupArchiveError("An event batch contains inconsistent commit metadata.")

                        for causation_id in event.causation_ids:
                            source = connection.execute(
                                "SELECT branch_id, sequence FROM event_origins WHERE event_id = ?",
                                (str(causation_id),),
                            ).fetchone()
                            if source is None or not _archive_source_precedes(
                                row_by_id,
                                row.branch_id,
                                event.sequence,
                                UUID(source[0]) if source else None,
                                int(source[1]) if source else None,
                            ):
                                raise BackupArchiveError("An event causation reference is outside its committed branch history.")

                        try:
                            connection.execute(
                                "INSERT INTO event_origins(event_id, branch_id, sequence) VALUES (?, ?, ?)",
                                (str(event.event_id), str(row.branch_id), event.sequence),
                            )
                        except sqlite3.IntegrityError:
                            raise BackupArchiveError("Event identifiers must be unique throughout the archive.") from None

                        pending.append(event)
                        expected_sequence += 1
                        if event.commit_boundary:
                            if any(item.commit_boundary for item in pending[:-1]):
                                raise BackupArchiveError("An event batch has an invalid commit boundary.")
                            first = pending[0]
                            expected_version = current_version + 1
                            if event.state_version_after != expected_version:
                                raise BackupArchiveError("A committed event batch has an inconsistent state version.")
                            typed_batch = [
                                parse_event({
                                    "type": item.type,
                                    "event_id": str(item.event_id),
                                    "visibility": item.visibility,
                                    **item.payload,
                                })
                                for item in pending
                            ]
                            try:
                                validated_batch = validate_persisted_death_vacancies(
                                    state, typed_batch, world_id=first.world_id, branch_id=row.branch_id,
                                    command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
                                )
                                validate_institutional_finance_batch(
                                    validated_batch, world_id=first.world_id, branch_id=row.branch_id,
                                    command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
                                )
                                meeting_marker = next((
                                    item for item in validated_batch
                                    if isinstance(item, NpcMeetingAttendanceDecisionRecordedEvent)
                                ), None)
                                if meeting_marker is not None:
                                    validate_npc_meeting_attendance_decision_batch(
                                        validated_batch, pre_state=state, world_id=first.world_id,
                                        branch_id=row.branch_id, command_id=first.command_id,
                                        actor_id=meeting_marker.actor_id, ruleset_version=first.ruleset_version,
                                    )
                                else:
                                    validate_relationship_invitation_batch(
                                        validated_batch, world_id=first.world_id, branch_id=row.branch_id,
                                        command_id=first.command_id, actor_id=first.actor_id,
                                        ruleset_version=first.ruleset_version,
                                    )
                                validate_private_gift_batch(
                                    validated_batch, world_id=first.world_id, branch_id=row.branch_id,
                                    command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
                                    day_seconds=state.calendar.day_seconds,
                                )
                                validate_leadership_ballot_batch(
                                    validated_batch, world_id=first.world_id, branch_id=row.branch_id,
                                    command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
                                )
                                validate_safe_conduct_batch(
                                    validated_batch, world_id=first.world_id, branch_id=row.branch_id,
                                    command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
                                )
                                validate_npc_attendance_scope(
                                    validated_batch, world_id=first.world_id, branch_id=row.branch_id,
                                    command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
                                )
                                validate_background_clock_batch(state.current_tick, first.actor_id, validated_batch)
                                validate_safe_conduct_travel_batch(state, validated_batch)
                                validate_population_membership_batch(state, validated_batch)
                                validate_adjudication_batch(
                                    state.current_tick,
                                    first.actor_id,
                                    validated_batch,
                                    world_id=first.world_id,
                                    branch_id=row.branch_id,
                                    command_id=first.command_id,
                                    ruleset_version=first.ruleset_version,
                                    state=state,
                                )
                                validate_investigation_case_batch(
                                    state, first.actor_id, validated_batch, world_id=first.world_id,
                                    branch_id=row.branch_id, command_id=first.command_id,
                                    ruleset_version=first.ruleset_version, command_type=None, command_payload=None,
                                )
                                validate_historical_evidence_destruction_batch(
                                    state, first.actor_id, validated_batch, world_id=first.world_id,
                                    branch_id=row.branch_id, command_id=first.command_id,
                                    ruleset_version=first.ruleset_version, command_type=None, command_payload=None,
                                )
                                validate_archived_promise_batch(state, pending, validated_batch)
                                validate_kinship_batch(validated_batch, world_id=first.world_id, branch_id=row.branch_id,
                                                      command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version)
                                validate_occupation_batch(validated_batch, world_id=first.world_id, branch_id=row.branch_id,
                                                          command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version)
                                validate_completed_work_shift(state, validated_batch, world_id=first.world_id, branch_id=row.branch_id,
                                                          command_id=first.command_id, actor_id=first.actor_id,
                                                          ruleset_version=first.ruleset_version)
                                validate_market_price_review_batch(
                                    validated_batch, world_id=first.world_id, branch_id=row.branch_id,
                                    command_id=first.command_id, ruleset_version=first.ruleset_version,
                                    command_type=None, command_payload=None,
                                )
                                validate_shipment_spoilage_batch(
                                    state, first.actor_id, validated_batch, world_id=first.world_id,
                                    branch_id=row.branch_id, command_id=first.command_id,
                                    ruleset_version=first.ruleset_version, command_type=None, command_payload=None,
                                )
                                validate_combat_batch(
                                    validated_batch,
                                    world_id=first.world_id,
                                    branch_id=row.branch_id,
                                    command_id=first.command_id,
                                    ruleset_version=first.ruleset_version,
                                    actor_id=first.actor_id,
                                    state=state, causation_ids=first.causation_ids,
                                )
                                validate_treatment_batch(
                                    validated_batch,
                                    world_id=first.world_id,
                                    branch_id=row.branch_id,
                                    command_id=first.command_id,
                                    ruleset_version=first.ruleset_version,
                                    actor_id=first.actor_id,
                                    state=state,
                                )
                            except (InvalidEvent, ValueError):
                                raise BackupArchiveError("A backup batch violates the installed event contract.") from None

                            for item, typed in zip(pending, typed_batch, strict=True):
                                try:
                                    state = apply_event(state, typed)
                                except Exception:
                                    raise BackupArchiveError(
                                        "A backup event cannot be replayed under the installed domain contract."
                                    ) from None
                                if state.current_tick != item.tick:
                                    raise BackupArchiveError("A replayed state tick does not match the committed event record.")
                                if item.state_version_after != expected_version:
                                    raise BackupArchiveError("A committed event batch has an inconsistent state version.")
                                if item.sequence in fork_targets[row.branch_id]:
                                    if not item.commit_boundary:
                                        raise BackupArchiveError("A branch fork does not land on a committed command boundary.")
                                    point_hashes[(row.branch_id, item.sequence)] = (
                                        state_hash(state),
                                        item.state_version_after,
                                        state.current_tick,
                                    )
                            current_version = expected_version
                            last_completed_command = pending_command
                            pending = []
                            pending_command = None
                        expected_sequence += 0

                    if segment_count == 0 and declared_count is not None:
                        raise BackupArchiveError("An event segment does not match its declared event count.")
                    if declared_count is not None and segment_count != declared_count:
                        raise BackupArchiveError("An event segment does not match its declared event count.")
                    if declared_count is not None and (
                        segment_first != declared_first
                        or segment_last != declared_last
                        or pending
                        or last_completed_command is None
                    ):
                        raise BackupArchiveError("An event segment does not end on a complete command boundary.")
                    spool.finish_event_member(
                        row.branch_id,
                        first_seq=segment_first,
                        last_seq=segment_last,
                        event_count=segment_count,
                    )
                    if segment_count:
                        previous_segment_command = last_completed_command

                if pending or pending_command is not None:
                    raise BackupArchiveError("A branch ends with an incomplete event batch.")
                if expected_sequence != row.head_seq + 1 or branch_event_count != row.head_seq - row.fork_seq:
                    raise BackupArchiveError("A branch event history does not cover its committed sequence range.")
                if current_version != row.state_version:
                    raise BackupArchiveError("A branch head state version does not match its event history.")
                if state.current_tick != row.current_tick or state_hash(state) != row.state_hash:
                    raise BackupArchiveError("A branch replay does not match its committed head hash and tick.")
                head_json = state_json(state)
                head_bytes = _canonical_json(head_json)
                spool.store_state(row.branch_id, "head", head_bytes)
                del head_json, head_bytes, checkpoint_raw, checkpoint_json, checkpoint

                for target_seq in fork_targets[row.branch_id]:
                    if target_seq > row.fork_seq and (row.branch_id, target_seq) not in point_hashes:
                        raise BackupArchiveError("A branch fork point is absent from parent replay history.")

                verified_rows.append(
                    VerifiedBranch(
                        branch_id=row.branch_id,
                        name=row.name,
                        parent_branch_id=row.parent_branch_id,
                        fork_seq=row.fork_seq,
                        head_seq=row.head_seq,
                        initial_state_version=row.initial_state_version,
                        state_version=row.state_version,
                        current_tick=row.current_tick,
                        initial_state_hash=row.initial_state_hash,
                        state_hash=row.state_hash,
                        created_at=row.created_at,
                        initial_state=None,
                        events=None,
                    )
                )

            connection.commit()
            member_reader.verify_all_used()
            spool.finish_writing()
            result = VerifiedWorldBackup(
                archive_sha256=archive_sha256,
                format_version=manifest.format_version,
                world=world,
                branches=tuple(verified_rows),
                world_pack_archive=world_pack_archive,
            )
            result._spool = spool
            succeeded = True
            return result
        except BackupArchiveError:
            raise
        except sqlite3.Error:
            raise BackupArchiveError("The backup event index could not be validated within the temporary store.") from None
        finally:
            if connection is not None:
                connection.close()
            spool.remove_index()
            if not succeeded:
                spool.close()


def _topological_archive_rows(
    row_by_id: dict[UUID, BackupBranch | BackupBranchV2],
) -> list[BackupBranch | BackupBranchV2]:
    remaining = set(row_by_id)
    emitted: set[UUID] = set()
    ordered: list[BackupBranch | BackupBranchV2] = []
    while remaining:
        ready = sorted(
            (
                branch_id
                for branch_id in remaining
                if row_by_id[branch_id].parent_branch_id is None
                or row_by_id[branch_id].parent_branch_id in emitted
            ),
            key=str,
        )
        if not ready:
            raise BackupArchiveError("The branch graph is cyclic or disconnected.")
        for branch_id in ready:
            ordered.append(row_by_id[branch_id])
            emitted.add(branch_id)
            remaining.remove(branch_id)
    if sum(row.parent_branch_id is None for row in ordered) != 1:
        raise BackupArchiveError("The archive must contain exactly one genesis branch.")
    return ordered


def _effective_archive_branch_point(
    row_by_id: dict[UUID, BackupBranch | BackupBranchV2],
    branch_id: UUID,
    sequence: int,
) -> tuple[UUID, int]:
    row = row_by_id[branch_id]
    while sequence < row.fork_seq:
        if row.parent_branch_id is None:
            raise BackupArchiveError("A fork sequence precedes the genesis state.")
        row = row_by_id[row.parent_branch_id]
    if sequence > row.head_seq:
        raise BackupArchiveError("A fork sequence exceeds its parent branch head.")
    return row.branch_id, sequence


def _archive_source_precedes(
    row_by_id: dict[UUID, BackupBranch | BackupBranchV2],
    current_branch_id: UUID,
    current_sequence: int,
    source_branch_id: UUID | None,
    source_sequence: int | None,
) -> bool:
    if source_branch_id is None or source_sequence is None:
        return False
    row = row_by_id[current_branch_id]
    limit = current_sequence - 1
    while True:
        if row.branch_id == source_branch_id:
            return source_sequence <= limit
        if row.parent_branch_id is None:
            return False
        limit = min(limit, row.fork_seq)
        row = row_by_id[row.parent_branch_id]

def _iter_json_array_values(
    source: BinaryIO,
    *,
    max_bytes: int,
    max_event_bytes: int,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    on_chunk: Any = None,
) -> Iterator[dict[str, Any]]:
    """Incrementally parse one strict top-level JSON array of objects.

    Only one event object and one input chunk are retained at a time. A member
    byte cap and a separate per-event byte cap are enforced before a value is
    yielded. Duplicate keys and non-finite constants use the same rejection
    hooks as the eager archive parser.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    json_decoder = json.JSONDecoder(
        object_pairs_hook=_unique_object,
        parse_constant=_reject_json_constant,
    )
    text = ""
    position = 0
    state = "start"
    total = 0
    digest = sha256()

    def parse_available(*, final: bool = False) -> Iterator[dict[str, Any]]:
        nonlocal text, position, state
        while True:
            if state == "start":
                while position < len(text) and text[position] in " \t\r\n":
                    position += 1
                if position >= len(text):
                    break
                if text[position] != "[":
                    raise BackupArchiveError("An event member must contain one JSON array.")
                position += 1
                state = "value_or_end"
            elif state in {"value_or_end", "value_required"}:
                while position < len(text) and text[position] in " \t\r\n":
                    position += 1
                if position >= len(text):
                    break
                if state == "value_or_end" and text[position] == "]":
                    position += 1
                    state = "end"
                    continue
                if text[position] != "{":
                    raise BackupArchiveError("An event array must contain JSON objects without trailing commas.")
                event_start = position
                try:
                    value, end = json_decoder.raw_decode(text, position)
                except BackupArchiveError:
                    raise
                except (json.JSONDecodeError, RecursionError, ValueError, TypeError):
                    if len(text) - event_start > max_event_bytes:
                        raise BackupArchiveError("An event exceeds the configured per-event byte limit.") from None
                    if final:
                        raise BackupArchiveError("An archive event member is not valid canonical JSON.") from None
                    break
                if not isinstance(value, dict):
                    raise BackupArchiveError("An event array must contain JSON objects.")
                event_size = len(text[event_start:end].encode("utf-8"))
                if event_size > max_event_bytes:
                    raise BackupArchiveError("An event exceeds the configured per-event byte limit.")
                position = end
                state = "delimiter"
                yield value
            elif state == "delimiter":
                while position < len(text) and text[position] in " \t\r\n":
                    position += 1
                if position >= len(text):
                    break
                if text[position] == ",":
                    position += 1
                    state = "value_required"
                elif text[position] == "]":
                    position += 1
                    state = "end"
                else:
                    raise BackupArchiveError("An event array has an invalid delimiter.")
            elif state == "end":
                while position < len(text) and text[position] in " \t\r\n":
                    position += 1
                if position < len(text):
                    raise BackupArchiveError("An event member contains data after its JSON array.")
                break
            else:  # pragma: no cover - the parser state is closed above.
                raise BackupArchiveError("An event member has an invalid JSON array state.")

        if position:
            text = text[position:]
            position = 0

    try:
        while True:
            chunk = source.read(64 * 1024)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise BackupArchiveError("A ZIP member stream must return bytes.")
            total += len(chunk)
            if total > max_bytes:
                raise BackupArchiveError("An archive member exceeds the configured size limit.")
            digest.update(chunk)
            if on_chunk is not None:
                on_chunk(chunk)
            try:
                text += decoder.decode(chunk, final=False)
            except UnicodeDecodeError:
                raise BackupArchiveError("An archive member is not valid UTF-8 JSON.") from None
            yield from parse_available()
        try:
            text += decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            raise BackupArchiveError("An archive member is not valid UTF-8 JSON.") from None
        yield from parse_available(final=True)
        if state != "end":
            raise BackupArchiveError("An event member must contain one complete JSON array.")
        if expected_size is not None and total != expected_size:
            raise BackupArchiveError("A ZIP member length does not match its directory record.")
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            raise BackupArchiveError("A backup member failed its size or SHA-256 check.")
    except BackupArchiveError:
        raise
    except (OSError, RuntimeError, zlib.error):
        raise BackupArchiveError("An event member is corrupt or could not be read.") from None


def _validate_world_pack_source(archive: bytes) -> None:
    if not isinstance(archive, bytes) or not 0 < len(archive) <= MAX_WORLD_PACK_BYTES:
        raise BackupArchiveError("The nested world package exceeds the supported size limits.")
    try:
        load_world_pack(archive, engine_version=None)
    except WorldPackArchiveError:
        raise BackupArchiveError("The nested world package failed archive validation.") from None


def _check_world_pack_source_digest(root_state: WorldState, archive: bytes | None) -> None:
    if "world_pack_archive_sha256" not in root_state.metadata:
        return
    if archive is None:
        raise BackupArchiveError("The world package source is missing from the archive.")
    if root_state.metadata["world_pack_archive_sha256"] != sha256(archive).hexdigest():
        raise BackupArchiveError("The world package source does not match the genesis checkpoint.")


def _read_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    with archive.open(info, mode="r") as member:
        data = member.read(limit + 1)
    if len(data) > limit:
        raise BackupArchiveError("An archive member exceeds the configured size limit.")
    return data


def _validate_zip_info(info: zipfile.ZipInfo, names: set[str], limits: BackupLimits) -> None:
    name = info.filename
    _validate_member_path(name)
    if name in names:
        raise BackupArchiveError("The archive contains duplicate member names.")
    names.add(name)
    if info.is_dir() or name.endswith("/"):
        raise BackupArchiveError("Directory entries are not supported in a world backup.")
    if info.flag_bits & 0x41:
        raise BackupArchiveError("Encrypted ZIP members are not supported.")
    if info.comment or info.volume != 0:
        raise BackupArchiveError("ZIP member comments and split archives are not supported.")
    if info.extra:
        # Only admit the central-directory ZIP64 size/offset extension. The
        # standard library decodes it before the same caps below are applied.
        size = int.from_bytes(info.extra[2:4], "little")
        if (limits.compressed_bytes < zipfile.ZIP64_LIMIT or info.extra[:2] != b"\x01\x00"
                or size not in {8, 16, 24} or len(info.extra) != 4 + size):
            raise BackupArchiveError("The ZIP member uses an unsupported extra field.")
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise BackupArchiveError("The archive uses an unsupported compression method.")
    if info.file_size > limits.member_bytes:
        raise BackupArchiveError("An archive member exceeds the configured size limit.")
    if info.compress_size < 0 or info.file_size < 0:
        raise BackupArchiveError("A ZIP member has an invalid size.")
    if info.file_size and not info.compress_size:
        raise BackupArchiveError("A ZIP member has an invalid compression ratio.")
    if info.compress_size and info.file_size / info.compress_size > limits.compression_ratio:
        raise BackupArchiveError("A ZIP member exceeds the configured compression-ratio limit.")
    if info.create_system == 3:
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFREG}:
            raise BackupArchiveError("Links and special files are not supported in a world backup.")


def _validate_member_path(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or "\x00" in path
        or path.startswith("/")
        or re.match(r"^[A-Za-z]:", path) is not None
    ):
        raise BackupArchiveError("The archive contains an unsafe member path.")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BackupArchiveError("The archive contains an unsafe member path.")


def _parse_json(data: bytes) -> Any:
    try:
        text = data.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except BackupArchiveError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError, TypeError):
        raise BackupArchiveError("An archive member is not valid canonical JSON.") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BackupArchiveError("An archive JSON object contains duplicate keys.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise BackupArchiveError("Non-finite numbers are not allowed in backup JSON.")


def _validate_model(model_type: type[ContractModel], value: Any) -> Any:
    try:
        return model_type.model_validate(value)
    except ValidationError:
        raise BackupArchiveError("A backup member does not match the supported data contract.") from None


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise BackupArchiveError("World data cannot be represented in the supported JSON format.") from None


def _version_lookup_source(
    records: dict[UUID, dict[str, Any]],
    branch_id: UUID | None,
    sequence: int,
) -> tuple[UUID, int] | None:
    """Resolve a fork sequence to the local event carrying its version.

    A fork exactly at a branch's own fork point inherits its version from the
    parent chain, so lookup walks ancestors until it finds the local event that
    committed the requested sequence. The result is one sparse capture point,
    not an event index.
    """
    if branch_id is None:
        if sequence != 0:
            raise Conflict("The root branch has an invalid fork sequence.")
        return None
    seen: set[UUID] = set()
    current_id: UUID | None = branch_id
    while current_id is not None:
        if current_id in seen or current_id not in records:
            raise Conflict("The world branch lineage is incomplete or cyclic.")
        seen.add(current_id)
        record: BranchRecord = records[current_id]["record"]
        if sequence < 0 or sequence > record.head_seq:
            raise Conflict("A branch fork sequence does not resolve to committed history.")
        if sequence > record.fork_seq:
            return current_id, sequence
        if record.parent_branch_id is None:
            if sequence != 0:
                raise Conflict("The root branch has an invalid fork sequence.")
            return None
        current_id = record.parent_branch_id
    if sequence != 0:
        raise Conflict("A branch fork sequence does not resolve to committed history.")
    return None


def _write_spooled_zip(
    spool: BinaryIO,
    members: dict[str, _SpooledMember],
    manifest: BackupManifest,
    *,
    output: BinaryIO,
    limits: BackupLimits,
) -> int:
    """Stream sorted spool ranges into a ZIP without materializing members."""
    try:
        if not output.seekable() or output.tell() != 0 or output.seek(0, io.SEEK_END) != 0:
            raise BackupArchiveError("Archive output must be an empty seekable stream.")
        manifest_bytes = _canonical_json(manifest.model_dump(mode="json"))
        if len(manifest_bytes) > limits.member_bytes:
            raise BackupArchiveError("The archive manifest exceeds the configured member size limit.")
        if len(members) + 1 > limits.members:
            raise BackupArchiveError("The world exceeds the configured backup member limit.")
        if len(manifest_bytes) + sum(member.size for member in members.values()) > limits.uncompressed_bytes:
            raise BackupArchiveError("The world archive exceeds the expanded size limit.")

        manifest_compressed_size = 0
        if manifest_bytes:
            compressor = zlib.compressobj(level=6)
            manifest_compressed_size = len(compressor.compress(manifest_bytes)) + len(compressor.flush())
        manifest_use_deflate = (
            not manifest_bytes
            or len(manifest_bytes) / max(manifest_compressed_size, 1) <= limits.compression_ratio * 0.95
        )

        with zipfile.ZipFile(output, mode="w", allowZip64=limits.compressed_bytes >= zipfile.ZIP64_LIMIT,
                             compresslevel=6) as archive:
            archive.comment = b""
            for name in sorted(("manifest.json", *members.keys())):
                if name == "manifest.json":
                    size = len(manifest_bytes)
                    use_deflate = manifest_use_deflate
                    member = None
                else:
                    member = members[name]
                    size = member.size
                    use_deflate = member.use_deflate
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                info.compress_type = zipfile.ZIP_DEFLATED if use_deflate else zipfile.ZIP_STORED
                info.file_size = size
                with archive.open(info, mode="w", force_zip64=False) as destination:
                    if member is None:
                        destination.write(manifest_bytes)
                    else:
                        spool.seek(member.offset)
                        remaining = member.size
                        while remaining:
                            chunk = spool.read(min(64 * 1024, remaining))
                            if not chunk:
                                raise BackupArchiveError("The temporary archive spool ended before a member was written.")
                            destination.write(chunk)
                            remaining -= len(chunk)
                if output.tell() > limits.compressed_bytes:
                    raise BackupArchiveError("The world archive exceeds the compressed size limit.")

        archive_size = output.tell()
        if archive_size > limits.compressed_bytes:
            raise BackupArchiveError("The world archive exceeds the compressed size limit.")
        return archive_size
    except BackupArchiveError:
        _truncate_failed_archive_output(output)
        raise
    except (OSError, RuntimeError, ValueError, zipfile.LargeZipFile, zlib.error):
        _truncate_failed_archive_output(output)
        raise BackupArchiveError("The world archive could not be encoded within the configured limits.") from None


def _truncate_failed_archive_output(output: BinaryIO) -> None:
    try:
        output.seek(0)
        output.truncate(0)
    except (OSError, ValueError, AttributeError):
        pass


def _write_zip(
    member_data: dict[str, bytes],
    manifest: BackupManifest,
    *,
    output: BinaryIO,
    limits: BackupLimits,
) -> int:
    try:
        if not output.seekable() or output.tell() != 0 or output.seek(0, io.SEEK_END) != 0:
            raise BackupArchiveError("Archive output must be an empty seekable stream.")
        manifest_bytes = _canonical_json(manifest.model_dump(mode="json"))
        if len(manifest_bytes) > limits.member_bytes:
            raise BackupArchiveError("The archive manifest exceeds the configured member size limit.")
        if len(manifest_bytes) + sum(map(len, member_data.values())) > limits.uncompressed_bytes:
            raise BackupArchiveError("The world archive exceeds the expanded size limit.")
        with zipfile.ZipFile(output, mode="w", allowZip64=limits.compressed_bytes >= zipfile.ZIP64_LIMIT) as archive:
            archive.comment = b""
            entries = {"manifest.json": manifest_bytes, **member_data}
            for name, data in sorted(entries.items()):
                if len(data) > limits.member_bytes:
                    raise BackupArchiveError("An archive member exceeds the configured size limit.")
                compressed_size = len(zlib.compress(data, level=6)) if data else 0
                use_deflate = not data or len(data) / max(compressed_size, 1) <= limits.compression_ratio * 0.95
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                info.compress_type = zipfile.ZIP_DEFLATED if use_deflate else zipfile.ZIP_STORED
                archive.writestr(info, data, compress_type=info.compress_type, compresslevel=6 if use_deflate else None)
                if output.tell() > limits.compressed_bytes:
                    raise BackupArchiveError("The world archive exceeds the compressed size limit.")
        archive_size = output.tell()
        if archive_size > limits.compressed_bytes:
            raise BackupArchiveError("The world archive exceeds the compressed size limit.")
        return archive_size
    except BackupArchiveError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.LargeZipFile):
        raise BackupArchiveError("The world archive could not be encoded within the configured limits.") from None


def _segment_event_rows(
    branch_id: UUID,
    events: tuple[EventRecord, ...],
    encoded_rows: list[bytes],
    *,
    member_limit: int,
) -> list[tuple[ArchiveEventSegment, bytes]]:
    """Pack branch events into bounded members without splitting a command."""
    if len(events) != len(encoded_rows):
        raise BackupArchiveError("A branch event serialization lost an event.")
    if not events:
        return []

    batches: list[list[tuple[EventRecord, bytes]]] = []
    pending: list[tuple[EventRecord, bytes]] = []
    pending_command: UUID | None = None
    for event, encoded in zip(events, encoded_rows, strict=True):
        if pending_command is None:
            pending_command = event.command_id
        elif event.command_id != pending_command:
            raise BackupArchiveError("A branch event batch is incomplete before a command boundary.")
        pending.append((event, encoded))
        if event.commit_boundary:
            batches.append(pending)
            pending = []
            pending_command = None
    if pending:
        raise BackupArchiveError("A branch ends in a partial event batch.")

    packed: list[tuple[ArchiveEventSegment, bytes]] = []
    current: list[tuple[EventRecord, bytes]] = []

    def segment_size(items: list[tuple[EventRecord, bytes]]) -> int:
        return 2 + sum(len(payload) for _, payload in items) + max(0, len(items) - 1)

    def flush() -> None:
        if not current:
            return
        first, last = current[0][0].sequence, current[-1][0].sequence
        path = f"events/{branch_id}/{first}-{last}.json"
        payload = b"[" + b",".join(encoded for _, encoded in current) + b"]"
        packed.append(
            (
                ArchiveEventSegment(
                    path=path,
                    first_seq=first,
                    last_seq=last,
                    event_count=len(current),
                ),
                payload,
            )
        )
        current.clear()

    current_size = 2
    for batch in batches:
        batch_size = segment_size(batch)
        if batch_size > member_limit:
            raise BackupArchiveError(
                "A single committed event batch exceeds the segmented archive member limit."
            )
        if current and current_size + batch_size - 1 > member_limit:
            flush()
            current_size = 2
        current_size += batch_size - (1 if current else 2)
        current.extend(batch)
    flush()
    return packed


def _read_segmented_events(
    branch: BackupBranchV2,
    member_reader: _ArchiveMemberReader,
) -> tuple[EventRecord, ...]:
    """Verify each declared range and flatten it for the common replay path."""
    expected_sequence = branch.fork_seq + 1
    events: list[EventRecord] = []
    previous_command_id: UUID | None = None
    for segment in branch.event_segments:
        _validate_member_path(segment.path)
        if (
            segment.first_seq != expected_sequence
            or segment.last_seq < segment.first_seq
            or segment.event_count != segment.last_seq - segment.first_seq + 1
            or segment.path != f"events/{branch.branch_id}/{segment.first_seq}-{segment.last_seq}.json"
        ):
            raise BackupArchiveError("An event segment has invalid sequence bounds or path.")
        raw_events = member_reader.read_json(segment.path)
        if not isinstance(raw_events, list) or len(raw_events) != segment.event_count:
            raise BackupArchiveError("An event segment does not match its declared event count.")
        _reject_provider_configuration(raw_events)
        segment_count = 0
        segment_first: EventRecord | None = None
        segment_last: EventRecord | None = None
        for raw in raw_events:
            event = _validate_model(EventRecord, raw)
            if event.sequence != expected_sequence:
                raise BackupArchiveError("Event segment sequence ranges are not contiguous.")
            if previous_command_id is not None and segment_count == 0 and event.command_id == previous_command_id:
                raise BackupArchiveError("A command batch was split across event segments.")
            if segment_first is None:
                segment_first = event
            segment_last = event
            segment_count += 1
            events.append(event)
            expected_sequence += 1
        if (
            segment_first is None
            or segment_last is None
            or segment_first.sequence != segment.first_seq
            or segment_last.sequence != segment.last_seq
            or not segment_last.commit_boundary
        ):
            raise BackupArchiveError("An event segment does not end on a complete command boundary.")
        previous_command_id = segment_last.command_id
        del raw_events

    if expected_sequence != branch.head_seq + 1:
        raise BackupArchiveError("Segmented branch events do not cover the declared branch head.")
    return tuple(events)


def _topological_source_records(records: dict[UUID, dict[str, Any]]) -> list[UUID]:
    remaining = set(records)
    ordered: list[UUID] = []
    while remaining:
        ready = sorted(
            branch_id
            for branch_id in remaining
            if records[branch_id]["record"].parent_branch_id is None
            or records[branch_id]["record"].parent_branch_id in ordered
        )
        if not ready:
            raise Conflict("The world branch lineage is incomplete or cyclic.")
        for branch_id in ready:
            parent_id = records[branch_id]["record"].parent_branch_id
            if parent_id is not None and parent_id not in records:
                raise Conflict("The world branch lineage is incomplete.")
            ordered.append(branch_id)
            remaining.remove(branch_id)
    if sum(records[branch_id]["record"].parent_branch_id is None for branch_id in ordered) != 1:
        raise Conflict("The world does not have exactly one root branch.")
    return ordered


def _version_at(records: dict[UUID, dict[str, Any]], branch_id: UUID | None, sequence: int) -> int:
    """Find the state version at a source sequence using the exported local rows."""
    if branch_id is None:
        if sequence != 0:
            raise Conflict("The root branch has an invalid fork sequence.")
        return 0
    record: BranchRecord = records[branch_id]["record"]
    if sequence < record.fork_seq:
        return _version_at(records, record.parent_branch_id, sequence)
    if sequence == record.fork_seq:
        return _version_at(records, record.parent_branch_id, sequence) if record.parent_branch_id else 0
    event_by_sequence = {event.sequence: event for event in records[branch_id]["events"]}
    event = event_by_sequence.get(sequence)
    if event is None:
        raise Conflict("A branch fork sequence does not resolve to committed history.")
    return event.state_version_after


def _validate_and_replay(
    world: BackupWorld, branches: tuple[VerifiedBranch, ...]
) -> tuple[VerifiedBranch, ...]:
    by_id: dict[UUID, VerifiedBranch] = {}
    for branch in branches:
        if branch.branch_id in by_id:
            raise BackupArchiveError("The backup contains a duplicate branch identifier.")
        by_id[branch.branch_id] = branch
    if world.active_branch_id not in by_id:
        raise BackupArchiveError("The active branch is missing from the archive.")
    if len({branch.name for branch in branches}) != len(branches):
        raise BackupArchiveError("Branch names must be unique within a world.")
    roots = [branch for branch in branches if branch.parent_branch_id is None]
    if len(roots) != 1 or roots[0].fork_seq != 0:
        raise BackupArchiveError("The archive must contain exactly one genesis branch at sequence zero.")
    for branch in branches:
        if branch.head_seq < branch.fork_seq or branch.state_version < branch.initial_state_version:
            raise BackupArchiveError("A branch head precedes its fork checkpoint.")
        if branch.parent_branch_id is not None:
            parent = by_id.get(branch.parent_branch_id)
            if parent is None or parent.branch_id == branch.branch_id:
                raise BackupArchiveError("A branch references a missing or invalid parent.")
            if branch.fork_seq > parent.head_seq:
                raise BackupArchiveError("A branch fork exceeds its parent history.")

    ordered = _topological_branches(by_id)
    all_event_ids: dict[UUID, tuple[UUID, int]] = {}
    command_batches: dict[UUID, tuple[UUID, int]] = {}
    for branch in ordered:
        expected_count = branch.head_seq - branch.fork_seq
        if expected_count != len(branch.events):
            raise BackupArchiveError("A branch event history does not cover its committed sequence range.")
        expected_sequence = branch.fork_seq + 1
        for event in branch.events:
            if event.world_id != world.world_id or event.branch_id != branch.branch_id:
                raise BackupArchiveError("An event is associated with a different world or branch.")
            if event.sequence != expected_sequence:
                raise BackupArchiveError("A branch event sequence is not contiguous.")
            if event.event_id in all_event_ids:
                raise BackupArchiveError("Event identifiers must be unique throughout the world archive.")
            all_event_ids[event.event_id] = (branch.branch_id, event.sequence)
            expected_sequence += 1

        _validate_batches(branch)
        for start, end in _batch_ranges(branch.events):
            command_id = branch.events[start].command_id
            if command_id in command_batches:
                raise BackupArchiveError("A command identifier is reused by more than one event batch.")
            command_batches[command_id] = (branch.branch_id, branch.events[start].sequence)

    for branch in ordered:
        for event in branch.events:
            for causation_id in event.causation_ids:
                source = all_event_ids.get(causation_id)
                if source is None or not _is_lineage_event_before(by_id, branch, event.sequence, source):
                    raise BackupArchiveError("An event causation reference is outside its committed branch history.")

    fork_targets: dict[UUID, set[int]] = {branch.branch_id: set() for branch in ordered}
    for branch in ordered:
        if branch.parent_branch_id is None:
            continue
        source_id, source_seq = _effective_branch_point(by_id, branch.parent_branch_id, branch.fork_seq)
        source = by_id[source_id]
        if source_seq > source.fork_seq:
            fork_targets[source_id].add(source_seq)

    point_hashes: dict[tuple[UUID, int], tuple[str, int, int]] = {}
    verified: list[VerifiedBranch] = []
    for branch in ordered:
        if state_hash(branch.initial_state) != branch.initial_state_hash:
            raise BackupArchiveError("A branch fork checkpoint does not match its state hash.")
        point_hashes[(branch.branch_id, branch.fork_seq)] = (
            branch.initial_state_hash,
            branch.initial_state_version,
            branch.initial_state.current_tick,
        )
        if branch.parent_branch_id is None:
            if branch.initial_state_version != 0:
                raise BackupArchiveError("The genesis branch must start at state version zero.")
            if branch.initial_state.institutional_credit_contracts:
                raise BackupArchiveError("Institutional credit needs committed events after genesis.")
        else:
            source_id, source_seq = _effective_branch_point(by_id, branch.parent_branch_id, branch.fork_seq)
            source_point = point_hashes.get((source_id, source_seq))
            if source_point is None or source_point[0] != branch.initial_state_hash:
                raise BackupArchiveError("A branch fork checkpoint does not match its parent history.")
            if source_point[1] != branch.initial_state_version:
                raise BackupArchiveError("A branch fork state version does not match its parent history.")
            if source_point[2] != branch.initial_state.current_tick:
                raise BackupArchiveError("A branch fork tick does not match its parent history.")

        state = branch.initial_state
        current_version = branch.initial_state_version
        for start, end in _batch_ranges(branch.events):
            expected_version = current_version + 1
            batch = branch.events[start : end + 1]
            try:
                typed_batch = [parse_event({
                    "type": event.type, "event_id": str(event.event_id), "visibility": event.visibility,
                    **event.payload,
                }) for event in batch]
                validated_batch = validate_persisted_death_vacancies(
                    state, typed_batch, world_id=batch[0].world_id, branch_id=branch.branch_id,
                    command_id=batch[0].command_id, actor_id=batch[0].actor_id, ruleset_version=batch[0].ruleset_version,
                )
                validate_institutional_finance_batch(
                    validated_batch, world_id=batch[0].world_id, branch_id=branch.branch_id,
                    command_id=batch[0].command_id, actor_id=batch[0].actor_id, ruleset_version=batch[0].ruleset_version,
                )
                meeting_marker = next((
                    event for event in validated_batch
                    if isinstance(event, NpcMeetingAttendanceDecisionRecordedEvent)
                ), None)
                if meeting_marker is not None:
                    # Portable archives omit command rows and their payloads.
                    # Bind the typed marker to the archived event envelope and
                    # exact replay prestate; live commits additionally verify
                    # the source command and worker lease.
                    validate_npc_meeting_attendance_decision_batch(
                        validated_batch,
                        pre_state=state,
                        world_id=batch[0].world_id,
                        branch_id=branch.branch_id,
                        command_id=batch[0].command_id,
                        actor_id=meeting_marker.actor_id,
                        ruleset_version=batch[0].ruleset_version,
                    )
                else:
                    validate_relationship_invitation_batch(
                        validated_batch, world_id=batch[0].world_id, branch_id=branch.branch_id,
                        command_id=batch[0].command_id, actor_id=batch[0].actor_id,
                        ruleset_version=batch[0].ruleset_version,
                    )
                validate_private_gift_batch(
                    validated_batch, world_id=batch[0].world_id, branch_id=branch.branch_id,
                    command_id=batch[0].command_id, actor_id=batch[0].actor_id, ruleset_version=batch[0].ruleset_version,
                    day_seconds=state.calendar.day_seconds,
                )
                validate_leadership_ballot_batch(
                    validated_batch, world_id=batch[0].world_id, branch_id=branch.branch_id,
                    command_id=batch[0].command_id, actor_id=batch[0].actor_id, ruleset_version=batch[0].ruleset_version,
                )
                validate_safe_conduct_batch(
                    validated_batch, world_id=batch[0].world_id, branch_id=branch.branch_id,
                    command_id=batch[0].command_id, actor_id=batch[0].actor_id, ruleset_version=batch[0].ruleset_version,
                )
                validate_npc_attendance_scope(
                    validated_batch, world_id=batch[0].world_id, branch_id=branch.branch_id,
                    command_id=batch[0].command_id, actor_id=batch[0].actor_id, ruleset_version=batch[0].ruleset_version,
                )
                validate_background_clock_batch(state.current_tick, batch[0].actor_id, validated_batch)
                validate_safe_conduct_travel_batch(state, validated_batch)
                validate_population_membership_batch(state, validated_batch)
                validate_adjudication_batch(state.current_tick, batch[0].actor_id, validated_batch,
                                            world_id=batch[0].world_id, branch_id=branch.branch_id,
                                            command_id=batch[0].command_id, ruleset_version=batch[0].ruleset_version, state=state)
                first = batch[0]
                validate_investigation_case_batch(
                    state, first.actor_id, validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                    command_id=first.command_id, ruleset_version=first.ruleset_version,
                    command_type=None, command_payload=None,
                )
                validate_historical_evidence_destruction_batch(
                    state, first.actor_id, validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                    command_id=first.command_id, ruleset_version=first.ruleset_version,
                    command_type=None, command_payload=None,
                )
                validate_archived_promise_batch(state, batch, validated_batch)
                validate_kinship_batch(validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                                      command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version)
                validate_occupation_batch(validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                                          command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version)
                validate_completed_work_shift(state, validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                                          command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version)
                validate_market_price_review_batch(
                    validated_batch, world_id=first.world_id, branch_id=branch.branch_id, command_id=first.command_id,
                    ruleset_version=first.ruleset_version, command_type=None, command_payload=None,
                )
                validate_shipment_spoilage_batch(
                    state, first.actor_id, validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                    command_id=first.command_id, ruleset_version=first.ruleset_version,
                    command_type=None, command_payload=None,
                )
                validate_combat_batch(
                    validated_batch, world_id=batch[0].world_id, branch_id=branch.branch_id,
                    command_id=batch[0].command_id, ruleset_version=batch[0].ruleset_version,
                    actor_id=batch[0].actor_id, state=state, causation_ids=batch[0].causation_ids,
                )
                validate_treatment_batch(
                    validated_batch, world_id=batch[0].world_id, branch_id=branch.branch_id,
                    command_id=batch[0].command_id, ruleset_version=batch[0].ruleset_version,
                    actor_id=batch[0].actor_id, state=state,
                )
            except (InvalidEvent, ValueError):
                raise BackupArchiveError("A backup batch violates the installed event contract.") from None
            for event, typed in zip(batch, typed_batch, strict=True):
                try:
                    state = apply_event(state, typed)
                except Exception:
                    raise BackupArchiveError("A backup event cannot be replayed under the installed domain contract.") from None
                if state.current_tick != event.tick:
                    raise BackupArchiveError("A replayed state tick does not match the committed event record.")
                if event.sequence in fork_targets[branch.branch_id]:
                    point_hashes[(branch.branch_id, event.sequence)] = (
                        state_hash(state),
                        event.state_version_after,
                        state.current_tick,
                    )
                if event.state_version_after != expected_version:
                    raise BackupArchiveError("A committed event batch has an inconsistent state version.")
            current_version = expected_version

        if current_version != branch.state_version:
            raise BackupArchiveError("A branch head state version does not match its event history.")
        if state.current_tick != branch.current_tick or state_hash(state) != branch.state_hash:
            raise BackupArchiveError("A branch replay does not match its committed head hash and tick.")
        for target_seq in fork_targets[branch.branch_id]:
            if target_seq > branch.fork_seq and target_seq <= branch.head_seq:
                if target_seq not in {event.sequence for event in branch.events if event.commit_boundary}:
                    raise BackupArchiveError("A branch fork does not land on a committed command boundary.")
                if (branch.branch_id, target_seq) not in point_hashes:
                    raise BackupArchiveError("A branch fork checkpoint is absent from parent replay history.")
        verified.append(branch)

    return tuple(verified)


def _topological_branches(by_id: dict[UUID, VerifiedBranch]) -> list[VerifiedBranch]:
    remaining = set(by_id)
    ordered: list[VerifiedBranch] = []
    emitted: set[UUID] = set()
    while remaining:
        ready = sorted(
            (branch_id for branch_id in remaining if by_id[branch_id].parent_branch_id in emitted
             or by_id[branch_id].parent_branch_id is None),
            key=str,
        )
        if not ready:
            raise BackupArchiveError("The branch graph is cyclic or disconnected.")
        for branch_id in ready:
            ordered.append(by_id[branch_id])
            emitted.add(branch_id)
            remaining.remove(branch_id)
    return ordered


def _batch_ranges(events: tuple[EventRecord, ...]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    for index, event in enumerate(events):
        if event.commit_boundary:
            ranges.append((start, index))
            start = index + 1
    if start != len(events):
        raise BackupArchiveError("A branch ends in a partial event batch.")
    return ranges


def _validate_batches(branch: VerifiedBranch) -> None:
    events = branch.events
    if events and not events[-1].commit_boundary:
        raise BackupArchiveError("A branch ends in an uncommitted event batch.")
    for start, end in _batch_ranges(events):
        batch = events[start : end + 1]
        first = batch[0]
        if any(
            event.command_id != first.command_id
            or event.actor_id != first.actor_id
            or event.causation_ids != first.causation_ids
            or event.correlation_id != first.correlation_id
            or event.ruleset_version != first.ruleset_version
            or event.state_version_after != first.state_version_after
            for event in batch
        ):
            raise BackupArchiveError("An event batch contains inconsistent commit metadata.")
        if any(event.commit_boundary for event in batch[:-1]) or not batch[-1].commit_boundary:
            raise BackupArchiveError("An event batch has an invalid commit boundary.")


def _effective_branch_point(
    by_id: dict[UUID, VerifiedBranch], branch_id: UUID, sequence: int
) -> tuple[UUID, int]:
    branch = by_id[branch_id]
    while sequence < branch.fork_seq:
        if branch.parent_branch_id is None:
            raise BackupArchiveError("A fork sequence precedes the genesis state.")
        branch = by_id[branch.parent_branch_id]
    if sequence > branch.head_seq:
        raise BackupArchiveError("A fork sequence exceeds its parent branch head.")
    return branch.branch_id, sequence


def _is_lineage_event_before(
    by_id: dict[UUID, VerifiedBranch], current_branch: VerifiedBranch, current_sequence: int,
    source: tuple[UUID, int],
) -> bool:
    source_branch_id, source_sequence = source
    branch = current_branch
    limit = current_sequence - 1
    while True:
        if branch.branch_id == source_branch_id:
            return source_sequence <= limit
        if branch.parent_branch_id is None:
            return False
        limit = min(limit, branch.fork_seq)
        branch = by_id[branch.parent_branch_id]


def _reject_provider_configuration(value: Any) -> None:
    """Fail closed if credential or provider configuration fields entered world data."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key).strip())
                normalized = normalized.lower().replace("-", "_")
                if normalized in _SENSITIVE_CONFIG_KEYS:
                    raise BackupArchiveError("The world contains provider credential/configuration data that cannot be archived.")
                stack.append(child)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)

