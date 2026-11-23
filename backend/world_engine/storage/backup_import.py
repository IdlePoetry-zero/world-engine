"""Atomic import of a verified world archive into an isolated storage namespace.

The imported world keeps its source IDs and event history. Historical outbox,
job, and lease work is deliberately not re-created, so a restored copy cannot
re-dispatch already committed effects.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime
from itertools import zip_longest
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from world_engine.domain.errors import Conflict, InvalidEvent
from world_engine.domain.combat_validation import validate_combat_batch
from world_engine.domain.treatment_validation import validate_treatment_batch
from world_engine.domain.events import (
    parse_event, validate_adjudication_batch, validate_background_clock_batch,
    validate_historical_evidence_destruction_batch, validate_investigation_case_batch,
)
from world_engine.domain.family_events import validate_kinship_batch
from world_engine.domain.occupation_events import validate_occupation_batch
from world_engine.domain.occupation_death_validation import validate_persisted_death_vacancies
from world_engine.domain.npc_plan_events import NpcPlanCreatedEvent, NpcPlanRevisedEvent
from world_engine.domain.npc_plan_review_events import NpcPlanReviewResolvedEvent, validate_authored_npc_review_scope
from world_engine.domain.relationship_invitation_state import validate_relationship_invitation_batch
from world_engine.domain.institutional_finance_batch import validate_institutional_finance_batch
from world_engine.domain.npc_meeting_attendance import (
    NpcMeetingAttendanceDecisionRecordedEvent, validate_npc_meeting_attendance_decision_batch,
)
from world_engine.domain.private_gift_validation import validate_private_gift_batch
from world_engine.domain.relationship_invitation_events import RelationInvitationChangedEvent
from world_engine.domain.relationship_reflection_events import validate_meeting_reflection_batch
from world_engine.domain.relationship_partnership_events import validate_relationship_partnership_batch
from world_engine.domain.shipment_relief_events import validate_shipment_relief_batch
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent, validate_travel_journey_scope
from world_engine.domain.waypoint_opportunity_events import validate_waypoint_opportunity_batch
from world_engine.domain.leadership_ballot_state import validate_leadership_ballot_batch
from world_engine.domain.leadership_ballot_events import LeadershipBallotChangedEvent
from world_engine.domain.safe_conduct_state import validate_safe_conduct_batch, validate_safe_conduct_travel_batch
from world_engine.rules.work_shift_validation import validate_completed_work_shift
from world_engine.domain.shipment_validation import validate_shipment_spoilage_batch
from world_engine.domain.market_pricing_validation import validate_market_price_review_batch
from world_engine.domain.events import validate_npc_attendance_scope, validate_population_membership_batch
from world_engine.simulation.npc_plan_validation import NpcPlanSimulationBatchValidation
from world_engine.domain.npc_plans import npc_journey_ids
from world_engine.domain.models import (
    AccessScope,
    CommitResult,
    EventRecord,
    MembershipRole,
    WorldState,
    normalize_datetime,
    utc_now,
)
from world_engine.domain.story_nodes import validate_story_arrival_world
from world_engine.domain.reducers import (
    apply_event,
    ledger_postings,
    state_hash,
    state_json,
    validate_ledger_postings,
)
from .event_rows import append_event_row, prepare_event_payload, read_event_payload
from .models import (
    ActorControlRow,
    BranchLeaseRow,
    BranchRow,
    CommandRow,
    EventRow,
    JobRow,
    LedgerPostingRow,
    MembershipRow,
    OutboxRow,
    SnapshotRow,
    WorldPackArchiveRow,
    WorldRow,
)
from .npc_plan_replay import replay_npc_plan_batch, validate_archived_promise_batch


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
        "provider_config",
        "provider_configuration",
        "provider_settings",
        "base_url",
        "default_model",
        "model_config",
        "model_settings",
    }
)


class BackupBranchData(Protocol):
    branch_id: UUID
    name: str
    parent_branch_id: UUID | None
    fork_seq: int
    head_seq: int
    initial_state_version: int
    state_version: int
    current_tick: int
    initial_state_hash: str
    state_hash: str
    created_at: datetime
    initial_state: WorldState | None
    events: tuple[EventRecord, ...] | None


class BackupWorldData(Protocol):
    world_id: UUID
    title: str
    active_branch_id: UUID
    ruleset_version: str
    pack_version: str
    created_at: datetime


class VerifiedBackupData(Protocol):
    archive_sha256: str
    format_version: str
    world: BackupWorldData
    branches: tuple[BackupBranchData, ...]

    def validate_world_pack_source(self, root_state: WorldState) -> bytes | None: ...


class BackupImportError(Conflict):
    """A verified restore cannot be committed to the requested destination."""


@dataclass(frozen=True, slots=True)
class BackupImportResult:
    """Summary returned after the isolated import transaction commits."""

    world_id: UUID
    active_branch_id: UUID
    branch_count: int
    event_count: int
    state_hashes: dict[UUID, str]
    archive_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "world_id": str(self.world_id),
            "active_branch_id": str(self.active_branch_id),
            "branch_count": self.branch_count,
            "event_count": self.event_count,
            "state_hashes": {str(key): value for key, value in self.state_hashes.items()},
            "archive_sha256": self.archive_sha256,
        }


@dataclass(frozen=True, slots=True)
class _Batch:
    branch_id: UUID
    command_id: UUID
    events: tuple[EventRecord, ...]
    expected_state_version: int
    state_version: int
    state_hash: str


class _StreamingImportPlan:
    """Disk-backed exact expectations produced by streaming preflight."""

    def __init__(self, scratch_directory: Path | None = None) -> None:
        self._directory = tempfile.TemporaryDirectory(
            prefix="world-engine-restore-plan-", dir=scratch_directory,
        )
        self.connection = sqlite3.connect(f"{self._directory.name}/plan.sqlite3")
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.execute("PRAGMA cache_size=-2048")
        self.connection.execute(
            "CREATE TABLE event_origins (event_id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, sequence INTEGER NOT NULL)"
        )
        self.connection.execute("CREATE TABLE command_origins (command_id TEXT PRIMARY KEY)")
        self.connection.execute(
            "CREATE TABLE commands (command_id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, actor_id TEXT NOT NULL, "
            "expected_state_version INTEGER NOT NULL, first_seq INTEGER NOT NULL, last_seq INTEGER NOT NULL, "
            "event_count INTEGER NOT NULL, state_version INTEGER NOT NULL, state_hash TEXT NOT NULL, "
            "payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE postings (event_id TEXT NOT NULL, posting_index INTEGER NOT NULL, account_id TEXT NOT NULL, "
            "resource_id TEXT NOT NULL, delta TEXT NOT NULL, PRIMARY KEY(event_id, posting_index))"
        )
        self.connection.execute(
            "CREATE TABLE branch_digests (branch_id TEXT PRIMARY KEY, event_count INTEGER NOT NULL, digest TEXT NOT NULL)"
        )
        self.connection.execute("BEGIN")
        self.event_count = 0
        self.command_count = 0

    def close(self) -> None:
        try:
            self.connection.close()
        finally:
            self._directory.cleanup()

    def __enter__(self) -> _StreamingImportPlan:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def event_origin(self, event_id: UUID) -> tuple[UUID, int] | None:
        row = self.connection.execute(
            "SELECT branch_id, sequence FROM event_origins WHERE event_id = ?", (str(event_id),)
        ).fetchone()
        return None if row is None else (UUID(row[0]), int(row[1]))

    def add_event_origin(self, record: EventRecord) -> None:
        self.connection.execute(
            "INSERT INTO event_origins(event_id, branch_id, sequence) VALUES (?, ?, ?)",
            (str(record.event_id), str(record.branch_id), record.sequence),
        )
        self.event_count += 1

    def claim_command(self, command_id: UUID) -> None:
        self.connection.execute("INSERT INTO command_origins(command_id) VALUES (?)", (str(command_id),))

    def add_command(self, values: tuple[Any, ...]) -> None:
        self.connection.execute(
            "INSERT INTO commands(command_id, branch_id, actor_id, expected_state_version, first_seq, last_seq, "
            "event_count, state_version, state_hash, payload_hash, payload_json, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        self.command_count += 1

    def add_postings(self, event_id: UUID, postings: tuple[Any, ...]) -> None:
        self.connection.executemany(
            "INSERT INTO postings(event_id, posting_index, account_id, resource_id, delta) VALUES (?, ?, ?, ?, ?)",
            [
                (str(event_id), index, str(posting.account_id), posting.resource_id, format(posting.delta, "f"))
                for index, posting in enumerate(postings)
            ],
        )

    def add_branch_digest(self, branch_id: UUID, event_count: int, digest: str) -> None:
        self.connection.execute(
            "INSERT INTO branch_digests(branch_id, event_count, digest) VALUES (?, ?, ?)",
            (str(branch_id), event_count, digest),
        )

    def branch_digest(self, branch_id: UUID) -> tuple[int, str] | None:
        row = self.connection.execute(
            "SELECT event_count, digest FROM branch_digests WHERE branch_id = ?", (str(branch_id),)
        ).fetchone()
        return None if row is None else (int(row[0]), row[1])

    def command(self, command_id: UUID) -> tuple[Any, ...] | None:
        return self.connection.execute(
            "SELECT branch_id, actor_id, expected_state_version, first_seq, last_seq, event_count, state_version, "
            "state_hash, payload_hash, payload_json, result_json, created_at FROM commands WHERE command_id = ?",
            (str(command_id),),
        ).fetchone()


class WorldBackupImporter:
    """Import one verified archive into an empty, isolated world database.

    This operation retains archived world/branch/event/command IDs but does
    not update any source database. A database must be provisioned as a staged
    destination before calling this service; the local profile owner controls
    when that destination becomes active.
    """

    def __init__(self, repository: Any, *, scratch_directory: Path | None = None) -> None:
        self.repository = repository
        self.scratch_directory = Path(scratch_directory).resolve() if scratch_directory is not None else None
        if self.scratch_directory is not None:
            self.scratch_directory.mkdir(parents=True, exist_ok=True)

    def import_verified_backup(
        self, scope: AccessScope, backup: VerifiedBackupData
    ) -> BackupImportResult:
        if bool(getattr(backup, "is_spooled", False)):
            return self._import_spooled(scope, backup)
        ordered, head_states, batches = _preflight(backup)
        root_branch = next(branch for branch in ordered if branch.parent_branch_id is None)
        pack_source = self._validate_world_pack_source(backup, root_branch.initial_state)
        imported_at = utc_now()
        try:
            with self.repository._transaction(write=True) as session:
                self._require_empty_world_namespace(session)
                if root_branch.initial_state.private_gifts:
                    raise BackupImportError("Root private gifts must have committed proposal events.")
                if root_branch.initial_state.leadership_vacancy_reviews:
                    raise BackupImportError("Root autonomous vacancy reviews must have committed review events.")
                self._insert_world_and_membership(
                    session, scope, backup.world, imported_at, root_branch.initial_state.player_id, pack_source
                )
                self._insert_branches(session, backup.world.world_id, ordered, head_states)
                self._insert_command_snapshots(session, scope, backup, batches)
                self._insert_events_and_ledger(session, ordered, batches)
                self._insert_snapshots(session, backup.world.world_id, ordered, head_states, imported_at)
                session.flush()
                self._verify_written_rows(session, scope, backup, ordered, head_states, batches)
        except BackupImportError:
            raise
        except IntegrityError:
            raise BackupImportError("The destination already contains conflicting world or event IDs.") from None
        except (InvalidEvent, ValidationError, ValueError):
            raise BackupImportError("The verified backup could not be written to the destination namespace.") from None

        return BackupImportResult(
            world_id=backup.world.world_id,
            active_branch_id=backup.world.active_branch_id,
            branch_count=len(ordered),
            event_count=sum(len(branch.events) for branch in ordered),
            state_hashes={branch.branch_id: branch.state_hash for branch in ordered},
            archive_sha256=backup.archive_sha256,
        )

    def _import_spooled(self, scope: AccessScope, backup: VerifiedBackupData) -> BackupImportResult:
        with _StreamingImportPlan(self.scratch_directory) as plan:
            try:
                ordered = self._preflight_spooled(backup, plan)
                root_branch = next(branch for branch in ordered if branch.parent_branch_id is None)
                root_state = backup.load_initial_state(root_branch.branch_id)
                pack_source = self._validate_world_pack_source(backup, root_state)
            except BackupImportError:
                raise
            except (sqlite3.Error, InvalidEvent, ValidationError, ValueError):
                raise BackupImportError("The verified archive failed streaming preflight.") from None
            imported_at = utc_now()
            try:
                with self.repository._transaction(write=True) as session:
                    self._require_empty_world_namespace(session)
                    if root_state.private_gifts:
                        raise BackupImportError("Root private gifts must have committed proposal events.")
                    if root_state.leadership_vacancy_reviews:
                        raise BackupImportError("Root autonomous vacancy reviews must have committed review events.")
                    self._insert_world_and_membership(
                        session, scope, backup.world, imported_at, root_state.player_id, pack_source
                    )
                    del root_state
                    self._insert_spooled_branches(session, backup, ordered)
                    self._insert_spooled_events_and_ledgers(session, scope, backup, ordered, plan)
                    self._insert_spooled_snapshots(session, backup.world.world_id, backup, ordered, imported_at)
                    session.flush()
                    self._verify_spooled_rows(session, scope, backup, ordered, plan)
            except BackupImportError:
                raise
            except IntegrityError:
                raise BackupImportError("The destination already contains conflicting world or event IDs.") from None
            except (InvalidEvent, ValidationError, ValueError):
                raise BackupImportError("The verified backup could not be written to the destination namespace.") from None

            return BackupImportResult(
                world_id=backup.world.world_id,
                active_branch_id=backup.world.active_branch_id,
                branch_count=len(ordered),
                event_count=plan.event_count,
                state_hashes={branch.branch_id: branch.state_hash for branch in ordered},
                archive_sha256=backup.archive_sha256,
            )

    @staticmethod
    def _validate_world_pack_source(backup: VerifiedBackupData, root_state: WorldState) -> bytes | None:
        try:
            return backup.validate_world_pack_source(root_state)
        except ValueError:
            raise BackupImportError("The verified backup has an invalid world package source.") from None

    def _preflight_spooled(self, backup: VerifiedBackupData, plan: _StreamingImportPlan) -> tuple[BackupBranchData, ...]:
        if backup.format_version not in {"1.0", "2.0"} or not re.fullmatch(r"[a-f0-9]{64}", backup.archive_sha256):
            raise BackupImportError("The verified archive marker is invalid or unsupported.")
        branches = tuple(backup.branches)
        by_id: dict[UUID, BackupBranchData] = {}
        for branch in branches:
            if branch.branch_id in by_id:
                raise BackupImportError("The verified archive contains duplicate branch IDs.")
            by_id[branch.branch_id] = branch
        if not branches or backup.world.active_branch_id not in by_id:
            raise BackupImportError("The verified archive does not contain its active branch.")
        if len(branches) > 24_998:
            raise BackupImportError("The verified archive contains too many branches to restore safely.")
        expected_total = sum(branch.head_seq - branch.fork_seq for branch in branches)
        if expected_total > 1_000_000:
            raise BackupImportError("The verified archive contains too many events to restore safely.")
        if len({branch.name for branch in branches}) != len(branches):
            raise BackupImportError("Branch names must be unique in the restored world.")
        roots = [branch for branch in branches if branch.parent_branch_id is None]
        if len(roots) != 1 or roots[0].fork_seq != 0:
            raise BackupImportError("The verified archive must contain one genesis branch.")
        for branch in branches:
            if branch.parent_branch_id is not None:
                parent = by_id.get(branch.parent_branch_id)
                if parent is None or branch.fork_seq > parent.head_seq:
                    raise BackupImportError("The verified branch lineage is incomplete.")
            if (
                branch.head_seq < branch.fork_seq
                or branch.state_version < branch.initial_state_version
                or branch.head_seq - branch.fork_seq < 0
            ):
                raise BackupImportError("A verified branch has an inconsistent local event range.")

        ordered = tuple(_topological_branches(by_id))
        target_points: dict[UUID, set[int]] = {branch.branch_id: set() for branch in ordered}
        for branch in ordered:
            if branch.parent_branch_id is not None:
                source_id, source_seq = _effective_point(by_id, branch.parent_branch_id, branch.fork_seq)
                source_branch = by_id[source_id]
                if source_seq > source_branch.fork_seq:
                    target_points[source_id].add(source_seq)

        point_hashes: dict[tuple[UUID, int], tuple[str, int, int]] = {}
        verified_journey_receipts: dict[UUID, list[tuple[int, TravelJourneyChangedEvent]]] = {}
        for branch in ordered:
            initial_state = backup.load_initial_state(branch.branch_id)
            _reject_sensitive_configuration(state_json(initial_state))
            initial_hash = state_hash(initial_state)
            if initial_hash != branch.initial_state_hash:
                raise BackupImportError("A branch checkpoint failed its exact state-hash check.")
            point_hashes[(branch.branch_id, branch.fork_seq)] = (
                initial_hash,
                branch.initial_state_version,
                initial_state.current_tick,
            )
            if branch.parent_branch_id is None:
                if branch.initial_state_version != 0:
                    raise BackupImportError("The genesis checkpoint must have version zero.")
                if any(promise.review_receipt is not None for promise in initial_state.relationship_promises):
                    raise BackupImportError("Promise review receipts need committed events after genesis.")
            else:
                source_id, source_seq = _effective_point(by_id, branch.parent_branch_id, branch.fork_seq)
                parent_point = point_hashes.get((source_id, source_seq))
                if (
                    parent_point is None
                    or parent_point[0] != branch.initial_state_hash
                    or parent_point[1] != branch.initial_state_version
                    or parent_point[2] != initial_state.current_tick
                ):
                    raise BackupImportError("A branch checkpoint does not match its parent history.")

            state = initial_state
            del initial_state
            current_version = branch.initial_state_version
            expected_sequence = branch.fork_seq + 1
            event_count = 0
            pending: list[EventRecord] = []
            pending_size = 0
            digest = hashlib.sha256()
            with _branch_event_iterator(backup, branch.branch_id) as source_events:
                for record in source_events:
                    if record.world_id != backup.world.world_id or record.branch_id != branch.branch_id:
                        raise BackupImportError("A verified event references a different world or branch.")
                    if record.sequence != expected_sequence:
                        raise BackupImportError("A verified branch event sequence contains a gap.")
                    _reject_sensitive_configuration(record.model_dump(mode="json"))
                    _update_event_digest(digest, record)
                    expected_sequence += 1
                    event_count += 1

                    for reference in record.causation_ids:
                        source = plan.event_origin(reference)
                        if source is None or not _is_prior_lineage_event(by_id, branch, record.sequence, source):
                            raise BackupImportError("A verified event causation reference is outside its branch history.")

                    if not pending:
                        try:
                            plan.claim_command(record.command_id)
                        except sqlite3.IntegrityError:
                            raise BackupImportError("A command ID is reused by multiple event batches.") from None
                    else:
                        first = pending[0]
                        if record.command_id != first.command_id:
                            raise BackupImportError("A command event batch is interrupted before its commit boundary.")
                        if (
                            record.actor_id != first.actor_id
                            or record.causation_ids != first.causation_ids
                            or record.correlation_id != first.correlation_id
                            or record.ruleset_version != first.ruleset_version
                            or record.state_version_after != first.state_version_after
                        ):
                            raise BackupImportError("An event batch contains inconsistent command metadata.")

                    record_size = len(_canonical_record_json(record))
                    pending_size += record_size
                    if pending_size > 32 * 1024 * 1024:
                        raise BackupImportError("A source event batch exceeds the configured member-size bound.")
                    pending.append(record)
                    try:
                        plan.add_event_origin(record)
                    except sqlite3.IntegrityError:
                        raise BackupImportError("The verified archive contains duplicate event IDs.") from None

                    if record.commit_boundary:
                        journey_receipts = _prior_journey_resolution_receipts(
                            by_id, verified_journey_receipts, branch, pending[0].sequence - 1,
                            state,
                        )
                        state, current_version = self._preflight_spooled_batch(
                            backup,
                            branch,
                            pending,
                            state,
                            current_version,
                            plan,
                            target_points[branch.branch_id],
                            point_hashes,
                            journey_receipts,
                        )
                        _record_verified_journey_receipts(
                            verified_journey_receipts, branch.branch_id, pending,
                        )
                        pending = []
                        pending_size = 0

            if pending:
                raise BackupImportError("The branch ends with an incomplete committed event batch.")
            if event_count != branch.head_seq - branch.fork_seq or expected_sequence != branch.head_seq + 1:
                raise BackupImportError("A verified branch has an inconsistent local event range.")
            if current_version != branch.state_version or state.current_tick != branch.current_tick:
                raise BackupImportError("A replayed branch head does not match its recorded version or tick.")
            if state_hash(state) != branch.state_hash:
                raise BackupImportError("A replayed branch head does not match its recorded state hash.")
            head_state = backup.load_head_state(branch.branch_id)
            if head_state.current_tick != branch.current_tick or state_hash(head_state) != branch.state_hash:
                raise BackupImportError("A stored verified head projection failed its state-hash check.")
            del head_state
            plan.add_branch_digest(branch.branch_id, event_count, digest.hexdigest())
            for target_seq in target_points[branch.branch_id]:
                if target_seq > branch.fork_seq and (branch.branch_id, target_seq) not in point_hashes:
                    raise BackupImportError("A branch fork point does not match a committed parent boundary.")
            del state

        if plan.event_count != expected_total:
            raise BackupImportError("The verified archive event count is inconsistent.")
        plan.connection.commit()
        return ordered

    def _preflight_spooled_batch(
        self,
        backup: VerifiedBackupData,
        branch: BackupBranchData,
        batch: list[EventRecord],
        state: WorldState,
        current_version: int,
        plan: _StreamingImportPlan,
        fork_targets: set[int],
        point_hashes: dict[tuple[UUID, int], tuple[str, int, int]],
        journey_resolution_receipts: dict[UUID, TravelJourneyChangedEvent],
    ) -> tuple[WorldState, int]:
        first, last = batch[0], batch[-1]
        if not last.commit_boundary or any(row.commit_boundary for row in batch[:-1]):
            raise BackupImportError("A source event batch has an invalid commit boundary.")
        expected_version = current_version + 1
        if last.state_version_after != expected_version:
            raise BackupImportError("A restored event batch does not advance exactly one state version.")
        try:
            typed_batch = [_typed_event(record) for record in batch]
            npc_validation = replay_npc_plan_batch(
                state,
                batch,
                journey_resolution_receipts=journey_resolution_receipts,
                source_command_type="backup.restored_event_batch",
                source_command_payload=_restored_command_payload(backup.archive_sha256, first, len(batch)),
            )
            validate_story_arrival_world(typed_batch, first.world_id)
            validated_batch = validate_persisted_death_vacancies(
                state, typed_batch, world_id=first.world_id, branch_id=branch.branch_id,
                command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
            )
            validate_institutional_finance_batch(
                validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
            )
            _validate_npc_aware_invitation_and_ballot_batches(
                validated_batch, npc_validation, pre_state=state, world_id=first.world_id, branch_id=branch.branch_id,
                command_id=first.command_id, source_actor_id=first.actor_id,
                ruleset_version=first.ruleset_version,
            )
            validate_private_gift_batch(
                validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
                day_seconds=state.calendar.day_seconds,
            )
            validate_meeting_reflection_batch(
                validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
            )
            validate_relationship_partnership_batch(
                validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
            )
            validate_shipment_relief_batch(
                validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
            )
            validate_travel_journey_scope(
                validated_batch, world_id=first.world_id, branch_id=branch.branch_id, command_id=first.command_id,
                ruleset_version=first.ruleset_version,
            )
            validate_waypoint_opportunity_batch(
                validated_batch, world_id=first.world_id, branch_id=branch.branch_id, command_id=first.command_id,
                actor_id=first.actor_id, ruleset_version=first.ruleset_version,
                state_version=int(first.state_version_after) - 1,
            )
            validate_authored_npc_review_scope(
                validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                command_id=first.command_id, command_actor_id=first.actor_id,
            )
            validate_safe_conduct_batch(
                validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
            )
            validate_npc_attendance_scope(
                validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
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
                branch_id=branch.branch_id,
                command_id=first.command_id,
                ruleset_version=first.ruleset_version,
                state=state,
            )
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
            validate_archived_promise_batch(state, batch, validated_batch, npc_validation=npc_validation)
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
                validated_batch,
                world_id=first.world_id,
                branch_id=branch.branch_id,
                command_id=first.command_id,
                ruleset_version=first.ruleset_version,
                actor_id=first.actor_id,
                state=state, causation_ids=first.causation_ids,
            )
            validate_treatment_batch(
                validated_batch,
                world_id=first.world_id,
                branch_id=branch.branch_id,
                command_id=first.command_id,
                ruleset_version=first.ruleset_version,
                actor_id=first.actor_id,
                state=state,
            )
        except InvalidEvent:
            raise BackupImportError("A verified batch violates the installed event contract.") from None

        per_event_postings = [ledger_postings(event) for event in typed_batch]
        all_postings = tuple(posting for items in per_event_postings for posting in items)
        try:
            validate_ledger_postings(all_postings)
        except InvalidEvent:
            raise BackupImportError("A verified ledger event batch is not balanced.") from None

        for record, postings in zip(batch, per_event_postings, strict=True):
            plan.add_postings(record.event_id, postings)
        for record, event in zip(batch, typed_batch, strict=True):
            try:
                state = apply_event(state, event)
            except Exception:
                raise BackupImportError("A verified event could not be replayed by this domain version.") from None
            if state.current_tick != record.tick or record.state_version_after != expected_version:
                raise BackupImportError("A replayed event tick or state version differs from its committed record.")
            if record.sequence in fork_targets:
                if not record.commit_boundary:
                    raise BackupImportError("A branch fork point does not match a committed parent boundary.")
                point_hashes[(branch.branch_id, record.sequence)] = (
                    state_hash(state),
                    record.state_version_after,
                    state.current_tick,
                )

        import_payload = _restored_command_payload(backup.archive_sha256, first, len(batch))
        result = CommitResult(
            command_id=first.command_id,
            branch_id=branch.branch_id,
            duplicate=False,
            state_version=expected_version,
            first_seq=first.sequence,
            last_seq=last.sequence,
            events=list(batch),
            state_hash=state_hash(state),
        )
        compact_result = self.repository._compact_commit_result(result)
        plan.add_command(
            (
                str(first.command_id),
                str(branch.branch_id),
                str(first.actor_id),
                current_version,
                first.sequence,
                last.sequence,
                len(batch),
                expected_version,
                result.state_hash,
                _hash_json(import_payload),
                _json_dump(import_payload),
                _json_dump(compact_result),
                last.committed_at.isoformat(),
            )
        )
        return state, expected_version

    def _insert_spooled_branches(
        self,
        session: Session,
        backup: VerifiedBackupData,
        branches: tuple[BackupBranchData, ...],
    ) -> None:
        for branch in branches:
            initial_state = backup.load_initial_state(branch.branch_id)
            initial_state_json = state_json(initial_state)
            del initial_state
            head_state = backup.load_head_state(branch.branch_id)
            head_state_json = state_json(head_state)
            del head_state
            row = BranchRow(
                branch_id=branch.branch_id,
                world_id=backup.world.world_id,
                name=branch.name,
                parent_branch_id=branch.parent_branch_id,
                fork_seq=branch.fork_seq,
                head_seq=branch.head_seq,
                state_version=branch.state_version,
                initial_state_version=branch.initial_state_version,
                current_tick=branch.current_tick,
                initial_state_hash=branch.initial_state_hash,
                state_hash=branch.state_hash,
                initial_state_json=initial_state_json,
                state_json=head_state_json,
                created_at=branch.created_at,
            )
            session.add(row)
            session.flush()
            session.expunge(row)
            del initial_state_json, head_state_json

    def _insert_spooled_events_and_ledgers(
        self,
        session: Session,
        scope: AccessScope,
        backup: VerifiedBackupData,
        branches: tuple[BackupBranchData, ...],
        plan: _StreamingImportPlan,
    ) -> None:
        for branch in branches:
            pending: list[EventRecord] = []
            digest = hashlib.sha256()
            branch_count = 0
            with _branch_event_iterator(backup, branch.branch_id) as source_events:
                for record in source_events:
                    _update_event_digest(digest, record)
                    branch_count += 1
                    pending.append(record)
                    if record.commit_boundary:
                        self._insert_spooled_batch(session, scope, backup, branch, pending, plan)
                        pending = []
            if pending:
                raise BackupImportError("The verified event stream ended inside a command batch.")
            expected_digest = plan.branch_digest(branch.branch_id)
            if expected_digest is None or expected_digest != (branch_count, digest.hexdigest()):
                raise BackupImportError("The verified event spool changed between preflight and restore.")

    def _insert_spooled_batch(
        self,
        session: Session,
        scope: AccessScope,
        backup: VerifiedBackupData,
        branch: BackupBranchData,
        batch: list[EventRecord],
        plan: _StreamingImportPlan,
    ) -> None:
        first, last = batch[0], batch[-1]
        expected = plan.command(first.command_id)
        if expected is None:
            raise BackupImportError("A verified event batch has no preflight command receipt.")
        import_payload = _restored_command_payload(backup.archive_sha256, first, len(batch))
        result = CommitResult(
            command_id=first.command_id,
            branch_id=branch.branch_id,
            duplicate=False,
            state_version=last.state_version_after,
            first_seq=first.sequence,
            last_seq=last.sequence,
            events=list(batch),
            state_hash=expected[7],
        )
        compact_result = self.repository._compact_commit_result(result)
        if (
            expected[0] != str(branch.branch_id)
            or expected[1] != str(first.actor_id)
            or expected[2] != last.state_version_after - 1
            or expected[3] != first.sequence
            or expected[4] != last.sequence
            or expected[5] != len(batch)
            or expected[6] != last.state_version_after
            or expected[8] != _hash_json(import_payload)
            or expected[9] != _json_dump(import_payload)
            or expected[10] != _json_dump(compact_result)
            or expected[11] != last.committed_at.isoformat()
        ):
            raise BackupImportError("A source command receipt changed between preflight and restore.")

        event_rows = []
        compressed_rows = []
        for record in batch:
            payload, compressed = prepare_event_payload(record.event_id, record.type, record.payload)
            if compressed is not None:
                compressed_rows.append(compressed)
            event_rows.append({
                "branch_id": record.branch_id,
                "sequence": record.sequence,
                "event_id": record.event_id,
                "world_id": record.world_id,
                "tick": record.tick,
                "event_type": record.type,
                "command_id": record.command_id,
                "actor_id": record.actor_id,
                "causation_json": [str(item) for item in record.causation_ids],
                "correlation_id": record.correlation_id,
                "ruleset_version": record.ruleset_version,
                "payload_json": payload,
                "visibility_json": record.visibility,
                "state_version_after": record.state_version_after,
                "commit_boundary": record.commit_boundary,
                "committed_at": record.committed_at,
            })
        session.execute(EventRow.__table__.insert(), event_rows)
        session.add_all(compressed_rows)

        posting_rows: list[dict[str, Any]] = []
        for record in batch:
            for posting_index, posting in enumerate(ledger_postings(_typed_event(record))):
                posting_rows.append(
                    {
                        "posting_id": uuid4(),
                        "event_id": record.event_id,
                        "posting_index": posting_index,
                        "account_id": posting.account_id,
                        "resource_id": posting.resource_id,
                        "delta": format(posting.delta, "f"),
                        "created_at": record.committed_at,
                    }
                )
        if posting_rows:
            session.execute(LedgerPostingRow.__table__.insert(), posting_rows)
        session.execute(
            CommandRow.__table__.insert(),
            [
                {
                    "command_id": first.command_id,
                    "tenant_id": scope.tenant_id,
                    "world_id": backup.world.world_id,
                    "branch_id": branch.branch_id,
                    "actor_id": first.actor_id,
                    "idempotency_key": f"backup:{backup.archive_sha256[:16]}:{first.command_id}",
                    "payload_hash": _hash_json(import_payload),
                    "expected_state_version": last.state_version_after - 1,
                    "command_type": "backup.restored_event_batch",
                    "payload_json": import_payload,
                    "result_json": compact_result,
                    "created_at": last.committed_at,
                }
            ],
        )

    @staticmethod
    def _insert_spooled_snapshots(
        session: Session,
        world_id: UUID,
        backup: VerifiedBackupData,
        branches: tuple[BackupBranchData, ...],
        imported_at: datetime,
    ) -> None:
        for branch in branches:
            initial_state = backup.load_initial_state(branch.branch_id)
            initial_json = state_json(initial_state)
            initial_tick = initial_state.current_tick
            del initial_state
            snapshots = [
                (
                    branch.fork_seq,
                    branch.initial_state_version,
                    initial_tick,
                    branch.initial_state_hash,
                    initial_json,
                )
            ]
            if branch.head_seq != branch.fork_seq:
                head_state = backup.load_head_state(branch.branch_id)
                head_json = state_json(head_state)
                snapshots.append(
                    (
                        branch.head_seq,
                        branch.state_version,
                        head_state.current_tick,
                        branch.state_hash,
                        head_json,
                    )
                )
                del head_state, head_json
            for sequence, version, tick, digest, state_payload in snapshots:
                row = SnapshotRow(
                    snapshot_id=uuid4(),
                    world_id=world_id,
                    branch_id=branch.branch_id,
                    last_seq=sequence,
                    state_version=version,
                    current_tick=tick,
                    state_hash=digest,
                    state_json=state_payload,
                    created_at=imported_at,
                )
                session.add(row)
                session.flush()
                session.expunge(row)
            del initial_json, snapshots

    def _verify_spooled_rows(
        self,
        session: Session,
        scope: AccessScope,
        backup: VerifiedBackupData,
        branches: tuple[BackupBranchData, ...],
        plan: _StreamingImportPlan,
    ) -> None:
        world = session.get(WorldRow, backup.world.world_id)
        if (
            world is None
            or world.tenant_id != scope.tenant_id
            or world.owner_id != scope.user_id
            or world.active_branch_id != backup.world.active_branch_id
            or world.title != backup.world.title
            or world.ruleset_version != backup.world.ruleset_version
            or world.pack_version != backup.world.pack_version
            or normalize_datetime(world.created_at) != normalize_datetime(backup.world.created_at)
        ):
            raise BackupImportError("The restored world header failed its transactional verification.")
        membership = session.get(MembershipRow, (backup.world.world_id, scope.user_id))
        if membership is None or membership.tenant_id != scope.tenant_id or membership.role != MembershipRole.OWNER.value:
            raise BackupImportError("The restored owner membership failed its transactional verification.")
        if session.scalar(
            select(func.count()).select_from(BranchRow).where(BranchRow.world_id == backup.world.world_id)
        ) != len(branches):
            raise BackupImportError("Restored branch headers failed their transactional verification.")
        if session.scalar(
            select(func.count()).select_from(EventRow).where(EventRow.world_id == backup.world.world_id)
        ) != plan.event_count:
            raise BackupImportError("Restored event counts failed their transactional verification.")

        for branch in branches:
            row = session.get(BranchRow, branch.branch_id)
            if row is None:
                raise BackupImportError("A restored branch is missing from the transactional write.")
            initial_state = backup.load_initial_state(branch.branch_id)
            expected_initial = state_json(initial_state)
            del initial_state
            head_state = backup.load_head_state(branch.branch_id)
            expected_head = state_json(head_state)
            del head_state
            stored_state = WorldState.model_validate(row.state_json)
            if (
                row.world_id != backup.world.world_id
                or row.name != branch.name
                or row.parent_branch_id != branch.parent_branch_id
                or row.fork_seq != branch.fork_seq
                or row.head_seq != branch.head_seq
                or row.state_version != branch.state_version
                or row.current_tick != branch.current_tick
                or row.initial_state_version != branch.initial_state_version
                or row.initial_state_hash != branch.initial_state_hash
                or row.state_hash != branch.state_hash
                or row.initial_state_json != expected_initial
                or row.state_json != expected_head
                or state_hash(stored_state) != branch.state_hash
            ):
                raise BackupImportError("A restored branch projection failed its transactional verification.")
            session.expunge(row)
            del expected_initial, expected_head, stored_state

            stored_events = session.scalars(
                select(EventRow)
                .where(EventRow.branch_id == branch.branch_id)
                .order_by(EventRow.sequence)
                .execution_options(yield_per=32)
            )
            try:
                with _branch_event_iterator(backup, branch.branch_id) as expected_events:
                    for stored, expected in zip_longest(stored_events, expected_events):
                        if stored is None or expected is None:
                            raise BackupImportError("A restored branch event count failed its transactional verification.")
                        if (
                            stored.event_id != expected.event_id
                            or stored.world_id != expected.world_id
                            or stored.sequence != expected.sequence
                            or stored.command_id != expected.command_id
                            or stored.event_type != expected.type
                            or stored.actor_id != expected.actor_id
                            or stored.causation_json != [str(item) for item in expected.causation_ids]
                            or stored.correlation_id != expected.correlation_id
                            or stored.ruleset_version != expected.ruleset_version
                            or stored.tick != expected.tick
                            or read_event_payload(stored) != expected.payload
                            or stored.visibility_json != expected.visibility
                            or stored.state_version_after != expected.state_version_after
                            or stored.commit_boundary != expected.commit_boundary
                            or normalize_datetime(stored.committed_at) != normalize_datetime(expected.committed_at)
                        ):
                            raise BackupImportError("A restored event row failed its transactional verification.")
                        session.expunge(stored)
            finally:
                stored_events.close()

        command_count = session.scalar(
            select(func.count()).select_from(CommandRow).where(CommandRow.world_id == backup.world.world_id)
        )
        if command_count != plan.command_count:
            raise BackupImportError("Restored command snapshots failed their transactional verification.")
        stored_commands = session.scalars(
            select(CommandRow)
            .where(CommandRow.world_id == backup.world.world_id)
            .order_by(CommandRow.command_id)
            .execution_options(yield_per=32)
        )
        expected_commands = plan.connection.execute(
            "SELECT command_id, branch_id, actor_id, expected_state_version, payload_hash, payload_json, "
            "result_json, created_at FROM commands ORDER BY command_id"
        )
        try:
            for stored, expected in zip_longest(stored_commands, expected_commands):
                if stored is None or expected is None:
                    raise BackupImportError("Restored command snapshots failed their transactional verification.")
                (
                    command_id,
                    branch_id,
                    actor_id,
                    expected_state_version,
                    payload_hash,
                    payload_json,
                    result_json,
                    created_at,
                ) = expected
                if (
                    stored.command_id != UUID(command_id)
                    or stored.branch_id != UUID(branch_id)
                    or stored.actor_id != UUID(actor_id)
                    or stored.tenant_id != scope.tenant_id
                    or stored.world_id != backup.world.world_id
                    or stored.expected_state_version != expected_state_version
                    or stored.command_type != "backup.restored_event_batch"
                    or stored.payload_hash != payload_hash
                    or stored.payload_json != json.loads(payload_json)
                    or stored.result_json != json.loads(result_json)
                    or stored.idempotency_key != f"backup:{backup.archive_sha256[:16]}:{command_id}"
                    or normalize_datetime(stored.created_at) != normalize_datetime(datetime.fromisoformat(created_at))
                ):
                    raise BackupImportError("A restored command snapshot does not match its validated batch.")
                session.expunge(stored)
        finally:
            stored_commands.close()
            expected_commands.close()

        expected_snapshot_count = sum(1 if branch.head_seq == branch.fork_seq else 2 for branch in branches)
        snapshot_count = session.scalar(
            select(func.count()).select_from(SnapshotRow).where(SnapshotRow.world_id == backup.world.world_id)
        )
        if snapshot_count != expected_snapshot_count:
            raise BackupImportError("Restored replay checkpoints failed their transactional verification.")
        for branch in branches:
            expected_points = [(branch.fork_seq, branch.initial_state_version, "initial", branch.initial_state_hash)]
            if branch.head_seq != branch.fork_seq:
                expected_points.append((branch.head_seq, branch.state_version, "head", branch.state_hash))
            for sequence, version, state_kind, digest in expected_points:
                stored = session.scalars(
                    select(SnapshotRow).where(
                        SnapshotRow.world_id == backup.world.world_id,
                        SnapshotRow.branch_id == branch.branch_id,
                        SnapshotRow.last_seq == sequence,
                    )
                ).one_or_none()
                source_state = (
                    backup.load_initial_state(branch.branch_id)
                    if state_kind == "initial"
                    else backup.load_head_state(branch.branch_id)
                )
                expected_tick = source_state.current_tick
                expected_state = state_json(source_state)
                del source_state
                if (
                    stored is None
                    or stored.state_version != version
                    or stored.state_hash != digest
                    or stored.current_tick != expected_tick
                    or stored.state_json != expected_state
                ):
                    raise BackupImportError("A restored replay checkpoint failed its transactional verification.")
                session.expunge(stored)
                del expected_state

        expected_posting_count = plan.connection.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
        actual_posting_count = session.scalar(
            select(func.count())
            .select_from(LedgerPostingRow)
            .join(EventRow, EventRow.event_id == LedgerPostingRow.event_id)
            .where(EventRow.world_id == backup.world.world_id)
        )
        if actual_posting_count != expected_posting_count:
            raise BackupImportError("Restored ledger posting counts failed their transactional verification.")
        stored_postings = session.execute(
            select(
                LedgerPostingRow.event_id,
                LedgerPostingRow.posting_index,
                LedgerPostingRow.account_id,
                LedgerPostingRow.resource_id,
                LedgerPostingRow.delta,
            )
            .join(EventRow, EventRow.event_id == LedgerPostingRow.event_id)
            .where(EventRow.world_id == backup.world.world_id)
            .order_by(LedgerPostingRow.event_id, LedgerPostingRow.posting_index)
            .execution_options(yield_per=32)
        )
        expected_postings = plan.connection.execute(
            "SELECT event_id, posting_index, account_id, resource_id, delta FROM postings "
            "ORDER BY event_id, posting_index"
        )
        try:
            for stored, expected in zip_longest(stored_postings, expected_postings):
                if stored is None or expected is None:
                    raise BackupImportError("Restored ledger postings failed their transactional verification.")
                normalized_expected = (
                    UUID(expected[0]),
                    int(expected[1]),
                    UUID(expected[2]),
                    expected[3],
                    expected[4],
                )
                if tuple(stored) != normalized_expected:
                    raise BackupImportError("Restored ledger postings failed their transactional verification.")
        finally:
            stored_postings.close()
            expected_postings.close()

        pending_rows = [
            session.scalar(select(func.count()).select_from(OutboxRow).where(OutboxRow.world_id == backup.world.world_id)),
            session.scalar(select(func.count()).select_from(JobRow).where(JobRow.world_id == backup.world.world_id)),
            session.scalar(
                select(func.count())
                .select_from(BranchLeaseRow)
                .join(BranchRow, BranchRow.branch_id == BranchLeaseRow.branch_id)
                .where(BranchRow.world_id == backup.world.world_id)
            ),
        ]
        if any(pending_rows):
            raise BackupImportError("A restored world unexpectedly contains active or pending work.")

    @staticmethod
    def _require_empty_world_namespace(session: Session) -> None:
        existing = session.scalar(select(WorldRow.world_id).limit(1))
        if existing is not None:
            raise BackupImportError("Restore requires an empty, isolated destination world database.")

    @staticmethod
    def _insert_world_and_membership(
        session: Session,
        scope: AccessScope,
        world: BackupWorldData,
        imported_at: datetime,
        player_id: UUID | None,
        pack_source: bytes | None,
    ) -> None:
        session.add(
            WorldRow(
                world_id=world.world_id,
                tenant_id=scope.tenant_id,
                owner_id=scope.user_id,
                title=world.title,
                active_branch_id=world.active_branch_id,
                ruleset_version=world.ruleset_version,
                pack_version=world.pack_version,
                created_at=world.created_at,
            )
        )
        session.flush()
        if pack_source is not None:
            session.add(WorldPackArchiveRow(world_id=world.world_id, archive_bytes=pack_source))
        session.add(
            MembershipRow(
                world_id=world.world_id,
                user_id=scope.user_id,
                tenant_id=scope.tenant_id,
                role=MembershipRole.OWNER.value,
                created_at=imported_at,
            )
        )
        if player_id is not None:
            session.add(
                ActorControlRow(
                    world_id=world.world_id,
                    user_id=scope.user_id,
                    actor_id=player_id,
                    granted_by=scope.user_id,
                    created_at=imported_at,
                )
            )

    @staticmethod
    def _insert_branches(
        session: Session,
        world_id: UUID,
        branches: tuple[BackupBranchData, ...],
        head_states: dict[UUID, WorldState],
    ) -> None:
        for branch in branches:
            session.add(
                BranchRow(
                    branch_id=branch.branch_id,
                    world_id=world_id,
                    name=branch.name,
                    parent_branch_id=branch.parent_branch_id,
                    fork_seq=branch.fork_seq,
                    head_seq=branch.head_seq,
                    state_version=branch.state_version,
                    initial_state_version=branch.initial_state_version,
                    current_tick=branch.current_tick,
                    initial_state_hash=branch.initial_state_hash,
                    state_hash=branch.state_hash,
                    initial_state_json=state_json(branch.initial_state),
                    state_json=state_json(head_states[branch.branch_id]),
                    created_at=branch.created_at,
                )
            )
            # BranchRow has a parent FK. Flush in topological order so the
            # parent exists before the child row is inserted on SQLite.
            session.flush()

    def _insert_command_snapshots(
        self,
        session: Session,
        scope: AccessScope,
        backup: VerifiedBackupData,
        batches: dict[UUID, tuple[_Batch, ...]],
    ) -> None:
        for branch_id, branch_batches in batches.items():
            for batch in branch_batches:
                first = batch.events[0]
                last = batch.events[-1]
                # Command request bodies are intentionally absent from the
                # backup. This typed snapshot records the committed event batch
                # and says so explicitly instead of fabricating source input.
                import_payload = _restored_command_payload(
                    backup.archive_sha256, first, len(batch.events),
                )
                payload_hash = _hash_json(import_payload)
                result = CommitResult(
                    command_id=batch.command_id,
                    branch_id=branch_id,
                    duplicate=False,
                    state_version=batch.state_version,
                    first_seq=first.sequence,
                    last_seq=last.sequence,
                    events=list(batch.events),
                    state_hash=batch.state_hash,
                )
                session.add(
                    CommandRow(
                        command_id=batch.command_id,
                        tenant_id=scope.tenant_id,
                        world_id=backup.world.world_id,
                        branch_id=branch_id,
                        actor_id=first.actor_id,
                        idempotency_key=f"backup:{backup.archive_sha256[:16]}:{batch.command_id}",
                        payload_hash=payload_hash,
                        expected_state_version=batch.expected_state_version,
                        command_type="backup.restored_event_batch",
                        payload_json=import_payload,
                        result_json=self.repository._compact_commit_result(result),
                        created_at=last.committed_at,
                    )
                )
        session.flush()

    @staticmethod
    def _insert_events_and_ledger(
        session: Session,
        branches: tuple[BackupBranchData, ...],
        batches: dict[UUID, tuple[_Batch, ...]],
    ) -> None:
        for branch in branches:
            for batch in batches[branch.branch_id]:
                batch_postings = []
                for record in batch.events:
                    append_event_row(session,
                        EventRow(
                            branch_id=record.branch_id,
                            sequence=record.sequence,
                            event_id=record.event_id,
                            world_id=record.world_id,
                            tick=record.tick,
                            event_type=record.type,
                            command_id=record.command_id,
                            actor_id=record.actor_id,
                            causation_json=[str(item) for item in record.causation_ids],
                            correlation_id=record.correlation_id,
                            ruleset_version=record.ruleset_version,
                            payload_json=record.payload,
                            visibility_json=record.visibility,
                            state_version_after=record.state_version_after,
                            commit_boundary=record.commit_boundary,
                            committed_at=record.committed_at,
                        )
                    )
                    event = _typed_event(record)
                    batch_postings.extend(ledger_postings(event))
                validate_ledger_postings(tuple(batch_postings))
        session.flush()

        for branch in branches:
            for batch in batches[branch.branch_id]:
                for record in batch.events:
                    event = _typed_event(record)
                    for posting_index, posting in enumerate(ledger_postings(event)):
                        session.add(
                            LedgerPostingRow(
                                posting_id=uuid4(),
                                event_id=record.event_id,
                                posting_index=posting_index,
                                account_id=posting.account_id,
                                resource_id=posting.resource_id,
                                delta=format(posting.delta, "f"),
                                created_at=record.committed_at,
                            )
                        )

    @staticmethod
    def _insert_snapshots(
        session: Session,
        world_id: UUID,
        branches: tuple[BackupBranchData, ...],
        head_states: dict[UUID, WorldState],
        imported_at: datetime,
    ) -> None:
        for branch in branches:
            checkpoints: list[tuple[int, int, WorldState, str]] = [
                (
                    branch.fork_seq,
                    branch.initial_state_version,
                    branch.initial_state,
                    branch.initial_state_hash,
                )
            ]
            if branch.head_seq != branch.fork_seq:
                checkpoints.append(
                    (branch.head_seq, branch.state_version, head_states[branch.branch_id], branch.state_hash)
                )
            for sequence, version, state, digest in checkpoints:
                session.add(
                    SnapshotRow(
                        snapshot_id=uuid4(),
                        world_id=world_id,
                        branch_id=branch.branch_id,
                        last_seq=sequence,
                        state_version=version,
                        current_tick=state.current_tick,
                        state_hash=digest,
                        state_json=state_json(state),
                        created_at=imported_at,
                    )
                )

    @staticmethod
    def _verify_written_rows(
        session: Session,
        scope: AccessScope,
        backup: VerifiedBackupData,
        branches: tuple[BackupBranchData, ...],
        head_states: dict[UUID, WorldState],
        batches: dict[UUID, tuple[_Batch, ...]],
    ) -> None:
        world = session.get(WorldRow, backup.world.world_id)
        if (
            world is None
            or world.tenant_id != scope.tenant_id
            or world.owner_id != scope.user_id
            or world.active_branch_id != backup.world.active_branch_id
            or world.title != backup.world.title
            or world.ruleset_version != backup.world.ruleset_version
            or world.pack_version != backup.world.pack_version
            or normalize_datetime(world.created_at) != normalize_datetime(backup.world.created_at)
        ):
            raise BackupImportError("The restored world header failed its transactional verification.")
        membership = session.get(MembershipRow, (backup.world.world_id, scope.user_id))
        if membership is None or membership.tenant_id != scope.tenant_id or membership.role != MembershipRole.OWNER.value:
            raise BackupImportError("The restored owner membership failed its transactional verification.")
        for branch in branches:
            row = session.get(BranchRow, branch.branch_id)
            if row is None:
                raise BackupImportError("A restored branch is missing from the transactional write.")
            state = WorldState.model_validate(row.state_json)
            if (
                row.world_id != backup.world.world_id
                or row.name != branch.name
                or row.parent_branch_id != branch.parent_branch_id
                or row.fork_seq != branch.fork_seq
                or row.head_seq != branch.head_seq
                or row.state_version != branch.state_version
                or row.current_tick != branch.current_tick
                or row.initial_state_version != branch.initial_state_version
                or row.initial_state_hash != branch.initial_state_hash
                or row.state_hash != branch.state_hash
                or row.initial_state_json != state_json(branch.initial_state)
                or state_hash(state) != branch.state_hash
                or state_json(state) != state_json(head_states[branch.branch_id])
            ):
                raise BackupImportError("A restored branch projection failed its transactional verification.")

            stored_events = session.scalars(
                select(EventRow)
                .where(EventRow.branch_id == branch.branch_id)
                .order_by(EventRow.sequence)
                .execution_options(yield_per=32)
            )
            for stored, expected in zip_longest(stored_events, branch.events):
                if stored is None or expected is None:
                    raise BackupImportError("A restored branch event count failed its transactional verification.")
                if (
                    stored.event_id != expected.event_id
                    or stored.world_id != expected.world_id
                    or stored.sequence != expected.sequence
                    or stored.command_id != expected.command_id
                    or stored.event_type != expected.type
                    or stored.actor_id != expected.actor_id
                    or stored.causation_json != [str(item) for item in expected.causation_ids]
                    or stored.correlation_id != expected.correlation_id
                    or stored.ruleset_version != expected.ruleset_version
                    or stored.tick != expected.tick
                    or read_event_payload(stored) != expected.payload
                    or stored.visibility_json != expected.visibility
                    or stored.state_version_after != expected.state_version_after
                    or stored.commit_boundary != expected.commit_boundary
                    or normalize_datetime(stored.committed_at) != normalize_datetime(expected.committed_at)
                ):
                    raise BackupImportError("A restored event row failed its transactional verification.")

        expected_batches = [batch for items in batches.values() for batch in items]
        stored_commands = session.scalars(
            select(CommandRow).where(CommandRow.world_id == backup.world.world_id)
        ).all()
        if len(stored_commands) != len(expected_batches):
            raise BackupImportError("Restored command snapshots failed their transactional verification.")
        command_rows = {row.command_id: row for row in stored_commands}
        for batch in expected_batches:
            row = command_rows.get(batch.command_id)
            expected_payload = _restored_command_payload(
                backup.archive_sha256, batch.events[0], len(batch.events),
            )
            if (
                row is None
                or row.branch_id != batch.branch_id
                or row.tenant_id != scope.tenant_id
                or row.expected_state_version != batch.expected_state_version
                or row.command_type != "backup.restored_event_batch"
                or row.payload_json != expected_payload
                or row.payload_hash != _hash_json(expected_payload)
                or row.idempotency_key != f"backup:{backup.archive_sha256[:16]}:{batch.command_id}"
            ):
                raise BackupImportError("A restored command snapshot does not identify its missing source payload.")

        expected_snapshot_count = sum(1 if row.head_seq == row.fork_seq else 2 for row in branches)
        snapshots = session.scalars(
            select(SnapshotRow).where(SnapshotRow.world_id == backup.world.world_id)
        ).all()
        if len(snapshots) != expected_snapshot_count:
            raise BackupImportError("Restored replay checkpoints failed their transactional verification.")
        snapshots_by_branch_seq = {(row.branch_id, row.last_seq): row for row in snapshots}
        for branch in branches:
            expected_points = [
                (
                    branch.fork_seq,
                    branch.initial_state_version,
                    branch.initial_state,
                    branch.initial_state_hash,
                )
            ]
            if branch.head_seq != branch.fork_seq:
                expected_points.append(
                    (branch.head_seq, branch.state_version, head_states[branch.branch_id], branch.state_hash)
                )
            for sequence, version, state, digest in expected_points:
                row = snapshots_by_branch_seq.get((branch.branch_id, sequence))
                if (
                    row is None
                    or row.state_version != version
                    or row.state_hash != digest
                    or row.current_tick != state.current_tick
                    or row.state_json != state_json(state)
                ):
                    raise BackupImportError("A restored replay checkpoint failed its transactional verification.")

        expected_postings: list[tuple[UUID, int, UUID, str, str]] = []
        for branch in branches:
            for event_record in branch.events:
                for index, posting in enumerate(ledger_postings(_typed_event(event_record))):
                    expected_postings.append(
                        (
                            event_record.event_id,
                            index,
                            posting.account_id,
                            posting.resource_id,
                            format(posting.delta, "f"),
                        )
                    )
        stored_postings = session.scalars(
            select(LedgerPostingRow)
            .join(EventRow, EventRow.event_id == LedgerPostingRow.event_id)
            .where(EventRow.world_id == backup.world.world_id)
            .order_by(LedgerPostingRow.event_id, LedgerPostingRow.posting_index)
        ).all()
        actual_postings = [
            (row.event_id, row.posting_index, row.account_id, row.resource_id, row.delta)
            for row in stored_postings
        ]
        if actual_postings != sorted(expected_postings, key=lambda row: (str(row[0]), row[1])):
            raise BackupImportError("Restored ledger postings failed their transactional verification.")
        # Imported history is not work to publish or resume. The isolated store
        # starts without jobs, leases or pending outbox rows.
        pending_rows = [
            session.scalar(select(func.count()).select_from(OutboxRow).where(OutboxRow.world_id == backup.world.world_id)),
            session.scalar(select(func.count()).select_from(JobRow).where(JobRow.world_id == backup.world.world_id)),
            session.scalar(
                select(func.count())
                .select_from(BranchLeaseRow)
                .join(BranchRow, BranchRow.branch_id == BranchLeaseRow.branch_id)
                .where(BranchRow.world_id == backup.world.world_id)
            ),
        ]
        if any(pending_rows):
            raise BackupImportError("A restored world unexpectedly contains active or pending work.")


def _preflight(
    backup: VerifiedBackupData,
) -> tuple[tuple[BackupBranchData, ...], dict[UUID, WorldState], dict[UUID, tuple[_Batch, ...]]]:
    if backup.format_version not in {"1.0", "2.0"} or not re.fullmatch(r"[a-f0-9]{64}", backup.archive_sha256):
        raise BackupImportError("The verified archive marker is invalid or unsupported.")
    branches = tuple(backup.branches)
    by_id: dict[UUID, BackupBranchData] = {}
    for branch in branches:
        if branch.branch_id in by_id:
            raise BackupImportError("The verified archive contains duplicate branch IDs.")
        by_id[branch.branch_id] = branch
    if not branches or backup.world.active_branch_id not in by_id:
        raise BackupImportError("The verified archive does not contain its active branch.")
    if len(branches) > 24_998:
        raise BackupImportError("The verified archive contains too many branches to restore safely.")
    if sum(len(branch.events) for branch in branches) > 1_000_000:
        raise BackupImportError("The verified archive contains too many events to restore safely.")
    if len({branch.name for branch in branches}) != len(branches):
        raise BackupImportError("Branch names must be unique in the restored world.")
    roots = [branch for branch in branches if branch.parent_branch_id is None]
    if len(roots) != 1 or roots[0].fork_seq != 0:
        raise BackupImportError("The verified archive must contain one genesis branch.")
    for branch in branches:
        if branch.parent_branch_id is not None:
            parent = by_id.get(branch.parent_branch_id)
            if parent is None or branch.fork_seq > parent.head_seq:
                raise BackupImportError("The verified branch lineage is incomplete.")
        if (
            branch.head_seq < branch.fork_seq
            or branch.state_version < branch.initial_state_version
            or branch.head_seq - branch.fork_seq != len(branch.events)
        ):
            raise BackupImportError("A verified branch has an inconsistent local event range.")

    ordered = _topological_branches(by_id)
    event_origins: dict[UUID, tuple[UUID, int]] = {}
    target_points: dict[UUID, set[int]] = {branch.branch_id: set() for branch in ordered}
    batches_by_branch: dict[UUID, tuple[_Batch, ...]] = {}
    seen_commands: set[UUID] = set()
    for branch in ordered:
        _reject_sensitive_configuration(state_json(branch.initial_state))
        _reject_sensitive_configuration([event.model_dump(mode="json") for event in branch.events])
        expected_seq = branch.fork_seq + 1
        for event in branch.events:
            if event.world_id != backup.world.world_id or event.branch_id != branch.branch_id:
                raise BackupImportError("A verified event references a different world or branch.")
            if event.sequence != expected_seq:
                raise BackupImportError("A verified branch event sequence contains a gap.")
            if event.event_id in event_origins:
                raise BackupImportError("The verified archive contains duplicate event IDs.")
            event_origins[event.event_id] = (branch.branch_id, event.sequence)
            expected_seq += 1
        batches = _parse_batches(branch, seen_commands)
        batches_by_branch[branch.branch_id] = batches

    for branch in ordered:
        for event in branch.events:
            for reference in event.causation_ids:
                source = event_origins.get(reference)
                if source is None or not _is_prior_lineage_event(by_id, branch, event.sequence, source):
                    raise BackupImportError("A verified event causation reference is outside its branch history.")
        if branch.parent_branch_id is not None:
            source_id, source_seq = _effective_point(by_id, branch.parent_branch_id, branch.fork_seq)
            source_branch = by_id[source_id]
            if source_seq > source_branch.fork_seq:
                target_points[source_id].add(source_seq)

    hashes_at: dict[tuple[UUID, int], tuple[str, int, int]] = {}
    head_states: dict[UUID, WorldState] = {}
    verified_journey_receipts: dict[UUID, list[tuple[int, TravelJourneyChangedEvent]]] = {}
    for branch in ordered:
        try:
            # model_dump() materializes newly added optional defaults. Keep the
            # compatibility-aware representation so old checkpoints retain the
            # exact canonical payload they were hashed with.
            initial_state = WorldState.model_validate(state_json(branch.initial_state))
        except ValidationError:
            raise BackupImportError("A branch checkpoint no longer matches the world-state schema.") from None
        if state_hash(initial_state) != branch.initial_state_hash:
            raise BackupImportError("A branch checkpoint failed its exact state-hash check.")
        hashes_at[(branch.branch_id, branch.fork_seq)] = (
            branch.initial_state_hash,
            branch.initial_state_version,
            initial_state.current_tick,
        )
        if branch.parent_branch_id is None:
            if branch.initial_state_version != 0:
                raise BackupImportError("The genesis checkpoint must have version zero.")
            if any(promise.review_receipt is not None for promise in initial_state.relationship_promises):
                raise BackupImportError("Promise review receipts need committed events after genesis.")
        else:
            source_id, source_seq = _effective_point(by_id, branch.parent_branch_id, branch.fork_seq)
            parent_point = hashes_at.get((source_id, source_seq))
            if (
                parent_point is None
                or parent_point[0] != branch.initial_state_hash
                or parent_point[1] != branch.initial_state_version
                or parent_point[2] != initial_state.current_tick
            ):
                raise BackupImportError("A branch checkpoint does not match its parent history.")

        state = initial_state
        version = branch.initial_state_version
        computed_batches: list[_Batch] = []
        for event_batch in batches_by_branch[branch.branch_id]:
            postings = []
            first = event_batch.events[0]
            journey_receipts = _prior_journey_resolution_receipts(
                by_id, verified_journey_receipts, branch, first.sequence - 1, state,
            )
            try:
                typed_batch = [_typed_event(record) for record in event_batch.events]
                npc_validation = replay_npc_plan_batch(
                    state,
                    event_batch.events,
                    journey_resolution_receipts=journey_receipts,
                    source_command_type="backup.restored_event_batch",
                    source_command_payload=_restored_command_payload(
                        backup.archive_sha256, first, len(event_batch.events),
                    ),
                )
                validate_story_arrival_world(typed_batch, first.world_id)
                validated_batch = validate_persisted_death_vacancies(
                    state, typed_batch, world_id=event_batch.events[0].world_id, branch_id=branch.branch_id,
                    command_id=event_batch.events[0].command_id, actor_id=event_batch.events[0].actor_id, ruleset_version=event_batch.events[0].ruleset_version,
                )
                validate_institutional_finance_batch(
                    validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                    command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
                )
                _validate_npc_aware_invitation_and_ballot_batches(
                    validated_batch, npc_validation, pre_state=state,
                    world_id=first.world_id, branch_id=branch.branch_id,
                    command_id=first.command_id, source_actor_id=first.actor_id,
                    ruleset_version=first.ruleset_version,
                )
                validate_private_gift_batch(
                    validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                    command_id=first.command_id, actor_id=first.actor_id, ruleset_version=first.ruleset_version,
                    day_seconds=state.calendar.day_seconds,
                )
                validate_meeting_reflection_batch(
                    validated_batch, world_id=event_batch.events[0].world_id, branch_id=branch.branch_id,
                    command_id=event_batch.events[0].command_id, actor_id=event_batch.events[0].actor_id,
                    ruleset_version=event_batch.events[0].ruleset_version,
                )
                validate_relationship_partnership_batch(
                    validated_batch, world_id=event_batch.events[0].world_id, branch_id=branch.branch_id,
                    command_id=event_batch.events[0].command_id, actor_id=event_batch.events[0].actor_id, ruleset_version=event_batch.events[0].ruleset_version,
                )
                validate_shipment_relief_batch(
                    validated_batch, world_id=event_batch.events[0].world_id, branch_id=branch.branch_id,
                    command_id=event_batch.events[0].command_id, actor_id=event_batch.events[0].actor_id, ruleset_version=event_batch.events[0].ruleset_version,
                )
                validate_travel_journey_scope(
                    validated_batch, world_id=event_batch.events[0].world_id, branch_id=branch.branch_id,
                    command_id=event_batch.events[0].command_id, ruleset_version=event_batch.events[0].ruleset_version,
                )
                waypoint_batch_head = event_batch.events[0]
                validate_waypoint_opportunity_batch(
                    validated_batch, world_id=waypoint_batch_head.world_id, branch_id=branch.branch_id,
                    command_id=waypoint_batch_head.command_id, actor_id=waypoint_batch_head.actor_id,
                    ruleset_version=waypoint_batch_head.ruleset_version,
                    state_version=int(waypoint_batch_head.state_version_after) - 1,
                )
                validate_authored_npc_review_scope(
                    validated_batch, world_id=event_batch.events[0].world_id, branch_id=branch.branch_id,
                    command_id=event_batch.events[0].command_id, command_actor_id=event_batch.events[0].actor_id,
                )
                validate_safe_conduct_batch(
                    validated_batch, world_id=event_batch.events[0].world_id, branch_id=branch.branch_id,
                    command_id=event_batch.events[0].command_id, actor_id=event_batch.events[0].actor_id, ruleset_version=event_batch.events[0].ruleset_version,
                )
                validate_npc_attendance_scope(
                    validated_batch, world_id=event_batch.events[0].world_id, branch_id=branch.branch_id,
                    command_id=event_batch.events[0].command_id, actor_id=event_batch.events[0].actor_id, ruleset_version=event_batch.events[0].ruleset_version,
                )
                validate_background_clock_batch(state.current_tick, event_batch.events[0].actor_id, validated_batch)
                validate_safe_conduct_travel_batch(state, validated_batch)
                validate_population_membership_batch(state, validated_batch)
                first = event_batch.events[0]
                validate_adjudication_batch(state.current_tick, first.actor_id, validated_batch,
                                            world_id=first.world_id, branch_id=branch.branch_id,
                                            command_id=first.command_id, ruleset_version=first.ruleset_version, state=state)
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
                validate_archived_promise_batch(state, event_batch.events, validated_batch, npc_validation=npc_validation)
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
                    validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                    command_id=first.command_id, ruleset_version=first.ruleset_version,
                    actor_id=first.actor_id, state=state, causation_ids=first.causation_ids,
                )
                validate_treatment_batch(
                    validated_batch, world_id=first.world_id, branch_id=branch.branch_id,
                    command_id=first.command_id, ruleset_version=first.ruleset_version,
                    actor_id=first.actor_id, state=state,
                )
            except InvalidEvent:
                raise BackupImportError("A verified batch violates the installed event contract.") from None
            for record, event in zip(event_batch.events, typed_batch, strict=True):
                try:
                    state = apply_event(state, event)
                except Exception:
                    raise BackupImportError("A verified event could not be replayed by this domain version.") from None
                if state.current_tick != record.tick:
                    raise BackupImportError("A replayed event tick differs from its committed record.")
                postings.extend(ledger_postings(event))
                if record.sequence in target_points[branch.branch_id]:
                    hashes_at[(branch.branch_id, record.sequence)] = (
                        state_hash(state), record.state_version_after, state.current_tick
                    )
            try:
                validate_ledger_postings(tuple(postings))
            except InvalidEvent:
                raise BackupImportError("A verified ledger event batch is not balanced.") from None
            if event_batch.state_version != version + 1:
                raise BackupImportError("A restored event batch does not advance exactly one state version.")
            version = event_batch.state_version
            _record_verified_journey_receipts(
                verified_journey_receipts, branch.branch_id, event_batch.events,
            )
            computed_batches.append(
                _Batch(
                    branch_id=event_batch.branch_id,
                    command_id=event_batch.command_id,
                    events=event_batch.events,
                    expected_state_version=version - 1,
                    state_version=version,
                    state_hash=state_hash(state),
                )
            )
        if version != branch.state_version or state.current_tick != branch.current_tick:
            raise BackupImportError("A replayed branch head does not match its recorded version or tick.")
        if state_hash(state) != branch.state_hash:
            raise BackupImportError("A replayed branch head does not match its recorded state hash.")
        head_states[branch.branch_id] = state
        batches_by_branch[branch.branch_id] = tuple(computed_batches)

        for target_seq in target_points[branch.branch_id]:
            if target_seq <= branch.fork_seq or target_seq > branch.head_seq:
                raise BackupImportError("A branch fork point is outside the parent history.")
            point = hashes_at.get((branch.branch_id, target_seq))
            target_event = next((item for item in branch.events if item.sequence == target_seq), None)
            if point is None or target_event is None or not target_event.commit_boundary:
                raise BackupImportError("A branch fork point does not match a committed parent boundary.")

    return tuple(ordered), head_states, batches_by_branch


def _reject_sensitive_configuration(value: Any) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key).strip())
                if normalized.lower().replace("-", "_") in _SENSITIVE_CONFIG_KEYS:
                    raise BackupImportError(
                        "Provider credentials/configuration cannot be imported as world state."
                    )
                stack.append(child)
        elif isinstance(item, (list, tuple)):
            stack.extend(item)


def _parse_batches(branch: BackupBranchData, seen_commands: set[UUID]) -> tuple[_Batch, ...]:
    batches: list[_Batch] = []
    pending: list[EventRecord] = []
    for event in branch.events:
        if pending and event.command_id != pending[0].command_id:
            raise BackupImportError("A command event batch is interrupted before its commit boundary.")
        pending.append(event)
        if event.commit_boundary:
            command_id = event.command_id
            if command_id in seen_commands:
                raise BackupImportError("A command ID is reused by multiple event batches.")
            seen_commands.add(command_id)
            first = pending[0]
            if any(
                row.actor_id != first.actor_id
                or row.causation_ids != first.causation_ids
                or row.correlation_id != first.correlation_id
                or row.ruleset_version != first.ruleset_version
                or row.state_version_after != event.state_version_after
                for row in pending
            ):
                raise BackupImportError("A source event batch has inconsistent command metadata.")
            batches.append(
                _Batch(
                    branch_id=branch.branch_id,
                    command_id=command_id,
                    events=tuple(pending),
                    expected_state_version=event.state_version_after - 1,
                    state_version=event.state_version_after,
                    state_hash="",
                )
            )
            pending = []
    if pending:
        raise BackupImportError("The branch ends with an incomplete committed event batch.")
    return tuple(batches)


@contextmanager
def _branch_event_iterator(backup: VerifiedBackupData, branch_id: UUID):
    iterator = backup.iter_branch_events(branch_id)
    try:
        yield iterator
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()


def _canonical_record_json(record: EventRecord) -> bytes:
    return json.dumps(
        record.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _update_event_digest(digest: Any, record: EventRecord) -> None:
    encoded = _canonical_record_json(record)
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _topological_branches(by_id: dict[UUID, BackupBranchData]) -> list[BackupBranchData]:
    emitted: set[UUID] = set()
    remaining = set(by_id)
    result: list[BackupBranchData] = []
    while remaining:
        ready = sorted(
            (
                branch_id
                for branch_id in remaining
                if by_id[branch_id].parent_branch_id is None
                or by_id[branch_id].parent_branch_id in emitted
            ),
            key=str,
        )
        if not ready:
            raise BackupImportError("The branch graph is cyclic or disconnected.")
        for branch_id in ready:
            result.append(by_id[branch_id])
            emitted.add(branch_id)
            remaining.remove(branch_id)
    return result


def _effective_point(
    by_id: dict[UUID, BackupBranchData], branch_id: UUID, sequence: int
) -> tuple[UUID, int]:
    branch = by_id[branch_id]
    while sequence < branch.fork_seq:
        if branch.parent_branch_id is None:
            raise BackupImportError("The branch fork sequence precedes genesis.")
        branch = by_id[branch.parent_branch_id]
    if sequence > branch.head_seq:
        raise BackupImportError("The branch fork sequence exceeds its parent history.")
    return branch.branch_id, sequence


def _prior_journey_resolution_receipts(
    by_id: dict[UUID, BackupBranchData],
    verified_by_branch: dict[UUID, list[tuple[int, TravelJourneyChangedEvent]]],
    branch: BackupBranchData,
    before_sequence: int,
    state: WorldState,
) -> dict[UUID, TravelJourneyChangedEvent]:
    """Return only already-validated terminal receipts in this branch's lineage."""
    needed_journey_ids = npc_journey_ids(state)
    if not needed_journey_ids:
        return {}

    lineage: list[tuple[BackupBranchData, int]] = []
    current = branch
    cutoff = before_sequence
    while True:
        lineage.append((current, cutoff))
        if current.parent_branch_id is None:
            break
        cutoff = min(cutoff, current.fork_seq)
        current = by_id[current.parent_branch_id]

    receipts: dict[UUID, TravelJourneyChangedEvent] = {}
    for ancestor, ancestor_cutoff in reversed(lineage):
        for sequence, event in sorted(verified_by_branch.get(ancestor.branch_id, ()), key=lambda item: item[0]):
            journey_id = event.journey.journey_id
            if ancestor.fork_seq < sequence <= ancestor_cutoff and journey_id in needed_journey_ids:
                receipts[journey_id] = event
    return receipts


def _record_verified_journey_receipts(
    verified_by_branch: dict[UUID, list[tuple[int, TravelJourneyChangedEvent]]],
    branch_id: UUID,
    records: list[EventRecord] | tuple[EventRecord, ...],
) -> None:
    """Remember actual terminal events only after their whole source batch validates."""
    for record in records:
        event = _typed_event(record)
        if isinstance(event, TravelJourneyChangedEvent) and event.operation in {"arrive", "interrupt"}:
            verified_by_branch.setdefault(branch_id, []).append((record.sequence, event))


def _restored_command_payload(
    archive_sha256: str,
    first: EventRecord,
    event_count: int,
) -> dict[str, object]:
    return {
        "kind": "verified_backup_event_batch",
        "source_request_payload_available": False,
        "source_archive_sha256": archive_sha256,
        "source_command_id": str(first.command_id),
        "source_event_count": event_count,
    }


def _validate_npc_aware_invitation_and_ballot_batches(
    events: list[object],
    npc_validation: NpcPlanSimulationBatchValidation | None,
    *,
    pre_state: WorldState,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    source_actor_id: UUID,
    ruleset_version: str,
) -> None:
    marker = next((event for event in events if isinstance(event, NpcMeetingAttendanceDecisionRecordedEvent)), None)
    if marker is not None:
        validate_npc_meeting_attendance_decision_batch(
            events, pre_state=pre_state, world_id=world_id, branch_id=branch_id,
            command_id=command_id, actor_id=marker.actor_id, ruleset_version=ruleset_version,
        )
        return
    if npc_validation is None:
        validate_relationship_invitation_batch(
            events,
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            actor_id=source_actor_id,
            ruleset_version=ruleset_version,
        )
        validate_leadership_ballot_batch(
            events,
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            actor_id=source_actor_id,
            ruleset_version=ruleset_version,
        )
        return

    lifecycle_types = (NpcPlanCreatedEvent, NpcPlanRevisedEvent, NpcPlanReviewResolvedEvent)
    lifecycle_ids = {
        getattr(event, "event_id") for event in events if isinstance(event, lifecycle_types)
    }
    core_ids = {getattr(event, "event_id") for event in npc_validation.core_rule_events}
    event_ids = {getattr(event, "event_id", None) for event in events}
    if not lifecycle_ids or not lifecycle_ids <= event_ids or not core_ids <= event_ids:
        raise InvalidEvent("Validated NPC plan helper output does not match its source event batch.")
    source_events = [
        event for event in events if getattr(event, "event_id", None) not in lifecycle_ids | core_ids
    ]
    for validator in (validate_relationship_invitation_batch, validate_leadership_ballot_batch):
        validator(
            source_events,
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            actor_id=source_actor_id,
            ruleset_version=ruleset_version,
            command_type=None,
            command_payload=None,
        )

    if not npc_validation.core_rule_events:
        return
    has_strict_action = any(
        isinstance(event, (RelationInvitationChangedEvent, LeadershipBallotChangedEvent))
        for event in npc_validation.core_rule_events
    )
    if has_strict_action and (
        npc_validation.action_command_type is None or npc_validation.action_command_payload is None
    ):
        raise InvalidEvent("NPC rule events lack their independently reconstructed action command.")
    for validator in (validate_relationship_invitation_batch, validate_leadership_ballot_batch):
        validator(
            npc_validation.core_rule_events,
            world_id=world_id,
            branch_id=branch_id,
            command_id=command_id,
            actor_id=npc_validation.actual_npc_actor_id,
            ruleset_version=ruleset_version,
            command_type=npc_validation.action_command_type,
            command_payload=npc_validation.action_command_payload,
        )


def _is_prior_lineage_event(
    by_id: dict[UUID, BackupBranchData],
    current_branch: BackupBranchData,
    current_sequence: int,
    source: tuple[UUID, int],
) -> bool:
    source_branch_id, source_sequence = source
    branch = current_branch
    cutoff = current_sequence - 1
    while True:
        if branch.branch_id == source_branch_id:
            return source_sequence <= cutoff
        if branch.parent_branch_id is None:
            return False
        cutoff = min(cutoff, branch.fork_seq)
        branch = by_id[branch.parent_branch_id]


def _typed_event(record: EventRecord):
    return parse_event(
        {
            "type": record.type,
            "event_id": str(record.event_id),
            "visibility": record.visibility,
            **record.payload,
        }
    )


def _hash_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
