"""Read-only, commit-by-commit capacity audit for a saved world-advance probe.

The audit consumes an existing ``.probe-world-advance.json`` and
``local-identity.json`` from a probe data directory. It never creates or
updates those files, opens SQLite in URI ``mode=ro``, and replays the selected
branch from its root genesis state before reporting committed-state minima.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from tempfile import NamedTemporaryFile
from typing import Any
from uuid import UUID


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from world_engine.domain.models import AccessScope, JobStatus, WorldState  # noqa: E402
from world_engine.domain.reducers import state_hash  # noqa: E402
from world_engine.storage.repository import WorldRepository  # noqa: E402


MANIFEST_NAME = ".probe-world-advance.json"
IDENTITY_NAME = "local-identity.json"
DATABASE_NAME = "world-engine.db"
MANIFEST_SCHEMA = 1
RESIDENT_THRESHOLD = 240
KEY_PROFILE_THRESHOLD = 32
STRUCTURAL_THRESHOLDS = {
    "active_countries": 24,
    "active_other_organizations": 24,
    "active_places": 120,
}
STRUCTURAL_REPORT_ONLY_METRICS = {
    "with_active_named_members": {
        "active_countries": "active_countries_with_active_named_members",
        "active_other_organizations": "active_other_organizations_with_active_named_members",
    },
    "with_active_member_leader": {
        "active_countries": "active_countries_with_active_member_leader",
        "active_other_organizations": "active_other_organizations_with_active_member_leader",
    },
}


class AuditError(RuntimeError):
    """A saved probe could not be safely and completely audited."""


class _CommitProgress:
    """Write compact, non-sensitive progress records to stderr at fixed intervals."""

    def __init__(self, every_commits: int, *, stream=None, clock: Callable[[], float] = time.monotonic):
        if type(every_commits) is not int or every_commits <= 0:
            raise ValueError("progress interval must be a positive integer")
        self.every_commits = every_commits
        self.stream = stream if stream is not None else sys.stderr
        self.clock = clock
        self.started_at = clock()
        self.last_reported_count = 0

    def report(self, audited_commits: int, sequence: int, world_tick: int) -> None:
        if audited_commits <= 0 or audited_commits % self.every_commits or audited_commits == self.last_reported_count:
            return
        payload = {
            "audited_commits": audited_commits,
            "sequence": sequence,
            "world_tick": str(world_tick),
            "elapsed_seconds": round(max(0.0, self.clock() - self.started_at), 3),
        }
        self.stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.stream.flush()
        self.last_reported_count = audited_commits


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"Could not read the saved {label}.") from exc
    if not isinstance(value, dict):
        raise AuditError(f"The saved {label} is not a JSON object.")
    return value


def _load_inputs(directory: Path) -> tuple[dict[str, Any], AccessScope, UUID, UUID, UUID, int]:
    manifest = _read_json_object(directory / MANIFEST_NAME, "probe manifest")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise AuditError("The probe manifest schema is unsupported.")
    if manifest.get("database_file", DATABASE_NAME) != DATABASE_NAME:
        raise AuditError("The probe manifest does not identify the expected database file.")
    if not manifest.get("world_created"):
        raise AuditError("The probe manifest does not record a created world.")

    raw_config = manifest.get("config")
    if not isinstance(raw_config, dict):
        raise AuditError("The probe manifest has no valid run configuration.")
    try:
        world_id = UUID(str(manifest["world_id"]))
        branch_id = UUID(str(manifest["branch_id"]))
        job_id = UUID(str(manifest["job_id"]))
        start_tick = int(manifest["start_tick"])
        target_tick = int(manifest["target_tick"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditError("The probe manifest is missing valid world, branch, job, or tick identifiers.") from exc
    if start_tick < 0 or target_tick <= start_tick:
        raise AuditError("The probe manifest has an invalid exact time interval.")
    total_seconds = raw_config.get("total_seconds")
    if type(total_seconds) is not int or total_seconds <= 0 or start_tick + total_seconds != target_tick:
        raise AuditError("The exact target tick does not match the saved duration configuration.")

    identity = _read_json_object(directory / IDENTITY_NAME, "probe owner identity")
    try:
        scope = AccessScope.model_validate(identity)
    except Exception as exc:
        raise AuditError("The saved probe owner identity is invalid.") from exc
    return manifest, scope, world_id, branch_id, job_id, target_tick


def _readonly_engine(database_path: Path) -> Engine:
    """Create an engine whose DBAPI connections are strictly read-only."""
    try:
        resolved = database_path.resolve(strict=True)
    except OSError as exc:
        raise AuditError("The saved probe database is missing or unavailable.") from exc
    if not resolved.is_file():
        raise AuditError("The probe database path is not a regular file.")
    database_uri = f"{resolved.as_uri()}?mode=ro"

    def connect_read_only() -> sqlite3.Connection:
        connection = sqlite3.connect(database_uri, uri=True, timeout=30.0, check_same_thread=False)
        # query_only is connection-local and prevents accidental DML even if
        # an audit code path is changed later. Do not set journal_mode here.
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    return create_engine("sqlite+pysqlite://", creator=connect_read_only, pool_pre_ping=True)


def _root_genesis(engine: Engine, world_id: UUID, branch_id: UUID) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve the root branch and independently verify its stored genesis."""
    with engine.connect() as connection:
        current_id = branch_id.hex
        visited: set[str] = set()
        row: dict[str, Any] | None = None
        while True:
            if current_id in visited:
                raise AuditError("The branch ancestry contains a cycle.")
            visited.add(current_id)
            result = connection.execute(
                text(
                    "SELECT branch_id, parent_branch_id, fork_seq, initial_state_version, "
                    "initial_state_hash, initial_state_json FROM world_branches "
                    "WHERE world_id = :world_id AND branch_id = :branch_id"
                ),
                {"world_id": world_id.hex, "branch_id": current_id},
            ).mappings().first()
            if result is None:
                raise AuditError("The saved branch or one of its ancestors is missing.")
            row = dict(result)
            parent_id = row["parent_branch_id"]
            if parent_id is None:
                break
            current_id = UUID(str(parent_id)).hex

    if row is None:
        raise AuditError("The root branch could not be resolved.")
    if int(row["fork_seq"]) != 0 or int(row["initial_state_version"]) != 0:
        raise AuditError("The root branch does not begin at the expected genesis boundary.")
    try:
        initial_payload = row["initial_state_json"]
        if isinstance(initial_payload, str):
            initial_payload = json.loads(initial_payload)
        initial_state = WorldState.model_validate(initial_payload)
    except Exception as exc:
        raise AuditError("The stored root genesis state is invalid.") from exc
    actual_hash = state_hash(initial_state)
    if actual_hash != row["initial_state_hash"]:
        raise AuditError("The stored root genesis hash does not match its state.")
    if initial_state.current_tick != 0:
        raise AuditError("The saved probe genesis tick is not zero.")
    return (
        {
            "branch_id": str(UUID(str(row["branch_id"]))),
            "state_hash": actual_hash,
            "current_tick": str(initial_state.current_tick),
            "state_version": int(row["initial_state_version"]),
            "calendar": {
                "day_seconds": initial_state.calendar.day_seconds,
                "year_days": initial_state.calendar.year_days,
            },
        },
        initial_state.metadata,
    )


def _manifest_checkpoint(
    manifest: dict[str, Any],
    checkpoint_year: int,
    *,
    start_tick: int,
    target_tick: int,
    day_seconds: int,
    year_days: int,
) -> dict[str, Any]:
    """Return the single exact decade witness recorded by the probe manifest."""
    if type(checkpoint_year) is not int or checkpoint_year <= 0:
        raise AuditError("The requested checkpoint year must be a positive integer.")
    checkpoints = manifest.get("checkpoints")
    if not isinstance(checkpoints, list):
        raise AuditError("The probe manifest has no checkpoint records.")
    matches = [
        item for item in checkpoints
        if isinstance(item, dict)
        and item.get("label") == "decade"
        and type(item.get("scheduled_year")) is int
        and item["scheduled_year"] == checkpoint_year
    ]
    if len(matches) != 1:
        raise AuditError("The requested year must identify exactly one saved decade checkpoint.")
    record = matches[0]

    def parse_checkpoint_integer(name: str) -> int:
        value = record.get(name)
        if not isinstance(value, str):
            raise AuditError("The selected manifest checkpoint is incomplete.")
        try:
            parsed = int(value)
        except ValueError as exc:
            raise AuditError("The selected manifest checkpoint is incomplete.") from exc
        if str(parsed) != value:
            raise AuditError("The selected manifest checkpoint contains a noncanonical integer.")
        return parsed

    try:
        tick = parse_checkpoint_integer("tick")
        sequence = parse_checkpoint_integer("sequence")
        state_version = parse_checkpoint_integer("state_version")
        state_digest = record["state_hash"]
    except (KeyError, TypeError) as exc:
        raise AuditError("The selected manifest checkpoint is incomplete.") from exc
    expected_tick = start_tick + checkpoint_year * year_days * day_seconds
    if (
        tick != expected_tick
        or tick > target_tick
        or sequence < 1
        or state_version < 1
        or not isinstance(state_digest, str)
        or re.fullmatch(r"[a-f0-9]{64}", state_digest) is None
    ):
        raise AuditError("The selected manifest checkpoint does not pin a valid exact year boundary.")
    return {
        "year": checkpoint_year,
        "tick": tick,
        "sequence": sequence,
        "state_version": state_version,
        "state_hash": state_digest,
    }


def _checkpoint_boundary_matches(
    event_row: dict[str, Any] | None,
    snapshot_rows: list[dict[str, Any]],
    checkpoint: dict[str, Any],
) -> bool:
    """Compare available persisted event/snapshot witnesses with a manifest pin."""
    if event_row is None or not (
        int(event_row.get("tick", -1)) == checkpoint["tick"]
        and int(event_row.get("state_version_after", -1)) == checkpoint["state_version"]
        and bool(event_row.get("commit_boundary"))
    ):
        return False
    return all(
        int(row.get("current_tick", -1)) == checkpoint["tick"]
        and int(row.get("state_version", -1)) == checkpoint["state_version"]
        and row.get("state_hash") == checkpoint["state_hash"]
        for row in snapshot_rows
    )


def _verify_stored_checkpoint_boundary(
    engine: Engine,
    world_id: UUID,
    branch_id: UUID,
    checkpoint: dict[str, Any],
) -> bool:
    """Verify the selected sequence against the event log and any saved snapshot."""
    with engine.connect() as connection:
        branch_rows = connection.execute(
            text(
                "SELECT branch_id, parent_branch_id, fork_seq, head_seq FROM world_branches "
                "WHERE world_id = :world_id"
            ),
            {"world_id": world_id.hex},
        ).mappings().all()
        branch_by_id = {UUID(str(row["branch_id"])): dict(row) for row in branch_rows}
        current = branch_by_id.get(branch_id)
        if current is None:
            return False
        allowed_sequences: dict[UUID, int] = {}
        limit = int(current["head_seq"])
        visited: set[UUID] = set()
        while current is not None:
            current_id = UUID(str(current["branch_id"]))
            if current_id in visited:
                return False
            visited.add(current_id)
            allowed_sequences[current_id] = min(limit, int(current["head_seq"]))
            parent_raw = current["parent_branch_id"]
            if parent_raw is None:
                break
            limit = min(allowed_sequences[current_id], int(current["fork_seq"]))
            current = branch_by_id.get(UUID(str(parent_raw)))

        sequence = checkpoint["sequence"]
        event_rows = connection.execute(
            text(
                "SELECT branch_id, tick, state_version_after, commit_boundary FROM world_events "
                "WHERE world_id = :world_id AND sequence = :sequence"
            ),
            {"world_id": world_id.hex, "sequence": sequence},
        ).mappings().all()
        matching_events = [
            dict(row) for row in event_rows
            if UUID(str(row["branch_id"])) in allowed_sequences
            and sequence <= allowed_sequences[UUID(str(row["branch_id"]))]
        ]
        event_row = matching_events[0] if len(matching_events) == 1 else None

        snapshot_rows = connection.execute(
            text(
                "SELECT branch_id, current_tick, state_version, state_hash FROM world_snapshots "
                "WHERE world_id = :world_id AND last_seq = :sequence"
            ),
            {"world_id": world_id.hex, "sequence": sequence},
        ).mappings().all()
        matching_snapshots = [
            dict(row) for row in snapshot_rows
            if UUID(str(row["branch_id"])) in allowed_sequences
            and sequence <= allowed_sequences[UUID(str(row["branch_id"]))]
        ]
    return _checkpoint_boundary_matches(event_row, matching_snapshots, checkpoint)


def _safe_report_output_path(output_path: Path, data_directory: Path) -> Path:
    """Resolve a report target and forbid replacing any read-only audit input."""
    resolved_output = output_path.expanduser().resolve()
    protected_paths = [
        data_directory / MANIFEST_NAME,
        data_directory / IDENTITY_NAME,
        data_directory / DATABASE_NAME,
        data_directory / f"{DATABASE_NAME}-wal",
        data_directory / f"{DATABASE_NAME}-shm",
    ]
    for protected in protected_paths:
        resolved_protected = protected.resolve()
        if resolved_output == resolved_protected:
            raise AuditError("The report output cannot replace a protected audit input or database file.")
        try:
            if resolved_output.exists() and resolved_protected.exists() and os.path.samefile(
                resolved_output, resolved_protected,
            ):
                raise AuditError("The report output cannot replace a protected audit input or database file.")
        except OSError:
            # Resolution and direct equality checks remain in force when the
            # platform cannot compare file identities.
            pass
    return resolved_output


def _uuid_set(value: object) -> set[UUID]:
    if not isinstance(value, list):
        return set()
    result: set[UUID] = set()
    for item in value:
        try:
            result.add(UUID(str(item)))
        except (TypeError, ValueError, AttributeError):
            continue
    return result


def _is_uuid_index(value: object) -> bool:
    if not isinstance(value, list):
        return False
    parsed: list[UUID] = []
    for item in value:
        try:
            parsed.append(UUID(str(item)))
        except (TypeError, ValueError, AttributeError):
            return False
    return len(parsed) == len(set(parsed))


def _sample_counts(
    state: WorldState,
    authored_key_ids: set[UUID],
    *,
    active_organization_member_status: dict[UUID, tuple[str, bool]] | None = None,
) -> dict[str, int]:
    named_ids: set[UUID] = set()
    governed_structures: list[tuple[str, dict[str, Any], UUID | None]] = []
    if active_organization_member_status is not None:
        active_organization_member_status.clear()
    counts = {
        "active_countries": 0,
        "active_other_organizations": 0,
        "active_places": 0,
        "active_countries_with_active_named_members": 0,
        "active_other_organizations_with_active_named_members": 0,
        "active_countries_with_active_member_leader": 0,
        "active_other_organizations_with_active_member_leader": 0,
    }
    # Summarize each entity once. Governance counts are resolved afterward
    # from only the active country/organization entries, against this set of
    # active named people.
    for entity in state.entities.values():
        if entity.status != "active":
            continue
        if entity.kind in {"character", "person"} and bool(entity.name and entity.name.strip()):
            named_ids.add(entity.id)
        elif entity.kind == "polity":
            counts["active_countries"] += 1
            governance = entity.components.get("governance")
            if isinstance(governance, dict):
                governed_structures.append(("active_countries", governance, None))
        elif entity.kind == "organization":
            counts["active_other_organizations"] += 1
            governance = entity.components.get("governance")
            if active_organization_member_status is not None:
                active_organization_member_status[entity.id] = (str(entity.name or ""), False)
            if isinstance(governance, dict):
                governed_structures.append(("active_other_organizations", governance, entity.id))
        elif entity.kind in {"place", "location"}:
            counts["active_places"] += 1

    for structure_kind, governance, structure_id in governed_structures:
        active_named_members = _uuid_set(governance.get("members")).intersection(named_ids)
        if (
            structure_kind == "active_other_organizations"
            and structure_id is not None
            and active_organization_member_status is not None
        ):
            organization_name, _ = active_organization_member_status[structure_id]
            active_organization_member_status[structure_id] = (organization_name, bool(active_named_members))
        if active_named_members:
            counts[f"{structure_kind}_with_active_named_members"] += 1
        leader_id: UUID | None = None
        raw_leader_id = governance.get("leader_id")
        if raw_leader_id is not None:
            try:
                leader_id = UUID(str(raw_leader_id))
            except (TypeError, ValueError, AttributeError):
                pass
        # Count a current leader only when leader_id intersects the recorded
        # member IDs and the active named people in this state.
        if leader_id is not None and leader_id in active_named_members:
            counts[f"{structure_kind}_with_active_member_leader"] += 1

    player_is_active_named = state.player_id in named_ids if state.player_id is not None else False
    active_keys = named_ids.intersection(authored_key_ids)
    return {
        "active_named_people_including_player": len(named_ids),
        "active_named_people_excluding_player": len(named_ids) - int(player_is_active_named),
        "active_named_residents_excluding_player_and_authored_keys": len(
            named_ids - active_keys - ({state.player_id} if state.player_id is not None else set())
        ),
        "active_authored_key_npcs": len(active_keys),
        **counts,
    }


class _InstitutionalContinuityAudit:
    """Accumulate active organization membership gaps from committed-state samples."""

    def __init__(self) -> None:
        self._organizations: dict[UUID, dict[str, Any]] = {}
        self._open_gaps: dict[UUID, tuple[int, int]] = {}
        self._last_sequence: int | None = None
        self._last_tick: int | None = None

    def observe(
        self,
        sequence: int,
        tick: int,
        active_organizations: dict[UUID, tuple[str, bool]],
    ) -> None:
        if self._last_sequence is not None and sequence <= self._last_sequence:
            raise AuditError("Institutional continuity samples must have increasing sequences.")
        if self._last_tick is not None and tick < self._last_tick:
            raise AuditError("Institutional continuity samples must have nondecreasing ticks.")
        for organization_id, (name, has_active_named_member) in active_organizations.items():
            row = self._organizations.setdefault(
                organization_id,
                {
                    "organization_id": str(organization_id),
                    "organization_name": name,
                    "zero_member_gap_episode_count": 0,
                    "zero_duration_same_tick_episode_count": 0,
                    "total_positive_gap_seconds": 0,
                    "longest_zero_member_gap": None,
                },
            )
            if name:
                row["organization_name"] = name

        observed_ids = set(active_organizations)
        for organization_id in set(self._open_gaps) | observed_ids:
            current = active_organizations.get(organization_id)
            has_gap = current is not None and not current[1]
            if organization_id in self._open_gaps and not has_gap:
                start_tick, start_sequence = self._open_gaps.pop(organization_id)
                self._close_gap(
                    organization_id,
                    start_tick=start_tick,
                    start_sequence=start_sequence,
                    end_tick=tick,
                    end_sequence=sequence,
                )
            elif has_gap and organization_id not in self._open_gaps:
                self._open_gaps[organization_id] = (tick, sequence)

        self._last_sequence = sequence
        self._last_tick = tick

    def _close_gap(
        self,
        organization_id: UUID,
        *,
        start_tick: int,
        start_sequence: int,
        end_tick: int,
        end_sequence: int,
    ) -> None:
        duration = end_tick - start_tick
        if duration < 0:
            raise AuditError("An institutional continuity gap ended before it began.")
        row = self._organizations[organization_id]
        self._record_gap(
            row,
            start_tick=start_tick,
            start_sequence=start_sequence,
            end_tick=end_tick,
            end_sequence=end_sequence,
            open_at_pin=False,
        )

    @staticmethod
    def _record_gap(
        row: dict[str, Any],
        *,
        start_tick: int,
        start_sequence: int,
        end_tick: int,
        end_sequence: int,
        open_at_pin: bool,
    ) -> None:
        duration = end_tick - start_tick
        if duration < 0:
            raise AuditError("An institutional continuity gap ended before it began.")
        row["zero_member_gap_episode_count"] += 1
        if duration > 0:
            row["total_positive_gap_seconds"] += duration
        elif not open_at_pin and end_tick == start_tick:
            row["zero_duration_same_tick_episode_count"] += 1
        current_longest = row["longest_zero_member_gap"]
        if current_longest is None or duration > int(current_longest["duration_seconds"]):
            row["longest_zero_member_gap"] = {
                "duration_seconds": duration,
                "start_tick": str(start_tick),
                "start_sequence": str(start_sequence),
                "end_tick": str(end_tick),
                "end_sequence": str(end_sequence),
                "open_at_pin": open_at_pin,
            }

    def report(self, *, pin_tick: int, pin_sequence: int) -> dict[str, Any]:
        boundary_matches = (self._last_tick, self._last_sequence) == (pin_tick, pin_sequence)
        organizations: list[dict[str, Any]] = []
        for organization_id, stored in sorted(self._organizations.items(), key=lambda item: str(item[0])):
            row = dict(stored)
            open_gap = self._open_gaps.get(organization_id)
            row["open_at_pin"] = (open_gap is not None) if boundary_matches else None
            if open_gap is not None and boundary_matches:
                start_tick, start_sequence = open_gap
                self._record_gap(
                    row,
                    start_tick=start_tick,
                    start_sequence=start_sequence,
                    end_tick=pin_tick,
                    end_sequence=pin_sequence,
                    open_at_pin=True,
                )
            organizations.append(row)
        return {
            "definition": (
                "An episode is an observed interval in which an active organization has no active named member; "
                "it ends when membership recovers or the organization becomes inactive or missing."
            ),
            "audited_from_tick": "0",
            "audited_from_sequence": "0",
            "audited_through_tick": str(pin_tick),
            "audited_through_sequence": str(pin_sequence),
            "pin_matches_last_committed_sample": boundary_matches,
            "observed_active_other_organization_count": len(self._organizations),
            "organizations": organizations,
        }


def _continuous_threshold_checks(
    minima: dict[str, dict[str, int | str]],
    thresholds: dict[str, int],
) -> dict[str, dict[str, int | bool | None]]:
    """Evaluate independent thresholds from the minimum observed on replay."""
    checks: dict[str, dict[str, int | bool | None]] = {}
    for metric, threshold in thresholds.items():
        record = minima.get(metric)
        minimum_count = int(record["count"]) if record is not None else None
        checks[metric] = {
            "minimum_count": minimum_count,
            "threshold": threshold,
            "passed_continuously": minimum_count is not None and minimum_count >= threshold,
        }
    return checks


def _initial_key_profile_threshold(
    authored_key_ids: set[UUID],
    active_named_profile_count: int,
    *,
    key_index_valid: bool,
) -> dict[str, Any]:
    """Report the unique, named key profiles present at genesis, independent of later mortality."""
    return {
        "threshold": KEY_PROFILE_THRESHOLD,
        "unique_authored_identity_count": len(authored_key_ids),
        "active_named_profiles_at_genesis": active_named_profile_count,
        "passed_at_genesis": (
            key_index_valid
            and len(authored_key_ids) >= KEY_PROFILE_THRESHOLD
            and active_named_profile_count >= KEY_PROFILE_THRESHOLD
        ),
        "requires_survival_after_genesis": False,
    }


def _run_audit(
    directory: Path,
    *,
    checkpoint_year: int | None = None,
    progress_every_commits: int = 10_000,
) -> dict[str, Any]:
    manifest, scope, world_id, branch_id, job_id, target_tick = _load_inputs(directory)
    database_path = directory / DATABASE_NAME
    engine = _readonly_engine(database_path)
    try:
        repository = WorldRepository(engine)
        view = repository.read_world(scope, world_id, branch_id)
        if view.world.owner_id != scope.user_id or view.world.tenant_id != scope.tenant_id:
            raise AuditError("The saved identity is not the owner of this probe world.")

        job = repository.read_job(scope, job_id)
        if (job.world_id, job.branch_id) != (world_id, branch_id):
            raise AuditError("The saved job does not belong to the selected world branch.")
        if job.job_type != "simulation.world_advance":
            raise AuditError("The saved job is not a world-advance job.")
        job_plan = job.payload.get("plan", {}) if isinstance(job.payload, dict) else {}
        if not isinstance(job_plan, dict) or int(job_plan.get("target_tick", -1)) != target_tick:
            raise AuditError("The durable job target differs from the saved exact target.")

        root_genesis, genesis_metadata = _root_genesis(engine, world_id, branch_id)
        calendar = root_genesis["calendar"]
        day_seconds = int(calendar["day_seconds"])
        year_days = int(calendar["year_days"])
        checkpoint = None
        if checkpoint_year is not None:
            checkpoint = _manifest_checkpoint(
                manifest,
                checkpoint_year,
                start_tick=int(manifest["start_tick"]),
                target_tick=target_tick,
                day_seconds=day_seconds,
                year_days=year_days,
            )
            if checkpoint["sequence"] > view.last_seq or checkpoint["state_version"] > view.branch.state_version:
                raise AuditError("The selected checkpoint is beyond the current branch head.")

        raw_genesis_index = genesis_metadata.get("content_index")
        genesis_content_index = raw_genesis_index if isinstance(raw_genesis_index, dict) else {}
        raw_head_index = view.state.metadata.get("content_index")
        content_index_matches_genesis = raw_head_index == raw_genesis_index
        content_index = genesis_content_index
        raw_key_ids = content_index.get("key_npc_ids")
        raw_resident_ids = content_index.get("other_resident_ids")
        authored_key_ids = _uuid_set(raw_key_ids)
        resident_ids = _uuid_set(raw_resident_ids)
        key_index_valid = _is_uuid_index(raw_key_ids)
        resident_index_valid = _is_uuid_index(raw_resident_ids)
        seed_counts = genesis_metadata.get("seed_counts")
        head_seed_counts = view.state.metadata.get("seed_counts")
        seed_counts_match_genesis = head_seed_counts == seed_counts
        seed_counts_match = None
        if isinstance(seed_counts, dict):
            expected_keys = seed_counts.get("key_npcs")
            expected_residents = seed_counts.get("other_named_living_characters")
            if type(expected_keys) is int and type(expected_residents) is int:
                seed_counts_match = (
                    expected_keys == len(authored_key_ids)
                    and expected_residents == len(resident_ids)
                )

        minima: dict[str, dict[str, int | str]] = {}
        first_below: dict[str, dict[str, int | str] | None] = {
            "active_named_people_including_player": None,
            "active_named_people_excluding_player": None,
            "active_named_residents_excluding_player_and_authored_keys": None,
        }
        audited_by_sequence: set[int] = set()
        last_observed_sequence: int | None = None
        last_observed_state: WorldState | None = None
        committed_states_audited = 0
        initial_replay_hash_matches = False
        monotonic_sequences = True
        initial_active_named_key_profile_count = 0
        last_observed_sample: dict[str, int] = {}
        institutional_continuity = _InstitutionalContinuityAudit()
        progress = _CommitProgress(progress_every_commits)

        def observe_commit(sequence: int, state: WorldState) -> None:
            nonlocal committed_states_audited, initial_replay_hash_matches
            nonlocal last_observed_sequence, last_observed_state, monotonic_sequences
            nonlocal initial_active_named_key_profile_count, last_observed_sample
            if last_observed_sequence is None:
                if sequence != 0:
                    raise AuditError("Genesis replay did not begin at sequence zero.")
            elif sequence < last_observed_sequence:
                monotonic_sequences = False
                raise AuditError("Genesis replay commit sequences are not monotonic.")
            elif sequence == last_observed_sequence:
                # A fork replay can expose its inherited boundary twice. Hash
                # only that duplicate; normal commits need no extra full-state
                # serialization beyond repository replay itself.
                if last_observed_state is None or state_hash(last_observed_state) != state_hash(state):
                    raise AuditError("Replay visited the same sequence with different states.")
                return
            last_observed_sequence = sequence
            last_observed_state = state
            audited_by_sequence.add(sequence)
            committed_states_audited += 1
            if sequence == 0:
                initial_replay_hash_matches = state_hash(state) == root_genesis["state_hash"]
            active_organization_member_status: dict[UUID, tuple[str, bool]] = {}
            sample = _sample_counts(
                state,
                authored_key_ids,
                active_organization_member_status=active_organization_member_status,
            )
            institutional_continuity.observe(
                sequence,
                state.current_tick,
                active_organization_member_status,
            )
            last_observed_sample = sample
            if sequence == 0:
                initial_active_named_key_profile_count = sample["active_authored_key_npcs"]
            for metric, count in sample.items():
                previous = minima.get(metric)
                if previous is None or count < int(previous["count"]):
                    minima[metric] = {
                        "count": count,
                        "sequence": str(sequence),
                        "tick": str(state.current_tick),
                    }
                if metric in first_below and count < RESIDENT_THRESHOLD and first_below[metric] is None:
                    first_below[metric] = {
                        "count": count,
                        "sequence": str(sequence),
                        "tick": str(state.current_tick),
                        "below_by": RESIDENT_THRESHOLD - count,
                    }
            progress.report(committed_states_audited, sequence, state.current_tick)

        audit_sequence = checkpoint["sequence"] if checkpoint is not None else view.last_seq
        audit_state_version = checkpoint["state_version"] if checkpoint is not None else view.branch.state_version
        audit_tick = checkpoint["tick"] if checkpoint is not None else view.state.current_tick
        audit_scope = (
            "checkpoint_prefix" if checkpoint is not None
            else "committed_prefix" if audit_tick < target_tick
            else "full_target" if audit_tick == target_tick
            else "head_after_target"
        )
        stored_checkpoint_matches = (
            _verify_stored_checkpoint_boundary(engine, world_id, branch_id, checkpoint)
            if checkpoint is not None else None
        )

        replay = repository.replay(
            scope,
            world_id,
            branch_id,
            through_seq=audit_sequence if checkpoint is not None else None,
            from_genesis=True,
            visit_commit=observe_commit,
        )
        replay_matches_head = (
            replay.state_hash == view.state_hash
            and replay.branch.state_version == view.branch.state_version
            and replay.state.current_tick == view.state.current_tick
            and replay.last_seq == view.last_seq
        )
        replay_matches_checkpoint = (
            checkpoint is not None
            and replay.state_hash == checkpoint["state_hash"]
            and replay.state.current_tick == checkpoint["tick"]
            and replay.last_seq == checkpoint["sequence"]
        )
        expected_states = audit_state_version - int(root_genesis["state_version"]) + 1
        replay_boundary_count_matches = (
            committed_states_audited == expected_states
            and len(audited_by_sequence) == expected_states
            and last_observed_sequence == audit_sequence
        )
        institutional_continuity_report = institutional_continuity.report(
            pin_tick=audit_tick,
            pin_sequence=audit_sequence,
        )
        institutional_continuity_report["interval_scope"] = audit_scope

        expected_final_tick = checkpoint is None and (
            job.status == JobStatus.SUCCEEDED
            and job.committed_through_tick == target_tick
            and view.state.current_tick == target_tick
            and int(job_plan.get("target_tick", -1)) == target_tick
        )
        persisted_manifest_matches = checkpoint is None and (
            manifest.get("last_tick") == str(view.state.current_tick)
            and manifest.get("last_state_version") == str(view.branch.state_version)
            and manifest.get("last_state_hash") == view.state_hash
            and manifest.get("last_job_status") == JobStatus.SUCCEEDED.value
        )
        stored_head_matches = None
        if checkpoint is None:
            with engine.connect() as connection:
                raw_head = connection.execute(
                    text(
                        "SELECT head_seq, state_version, current_tick, state_hash FROM world_branches "
                        "WHERE world_id = :world_id AND branch_id = :branch_id"
                    ),
                    {"world_id": world_id.hex, "branch_id": branch_id.hex},
                ).mappings().one()
            stored_head_matches = (
                int(raw_head["head_seq"]) == view.last_seq
                and int(raw_head["state_version"]) == view.branch.state_version
                and int(raw_head["current_tick"]) == view.state.current_tick
                and raw_head["state_hash"] == view.state_hash
            )
        audit_state = replay.state

        key_status_counts: dict[str, int] = {}
        for key_id in authored_key_ids:
            entity = audit_state.entities.get(key_id)
            status = entity.status if entity is not None else "missing"
            key_status_counts[status] = key_status_counts.get(status, 0) + 1

        stored_boundary_matches = stored_checkpoint_matches if checkpoint is not None else stored_head_matches
        requested_boundary_replayed = replay_matches_checkpoint if checkpoint is not None else replay_matches_head
        requested_boundary_verified = requested_boundary_replayed and stored_boundary_matches

        coverage = {
            "sqlite_open_mode": "uri_mode_ro_with_query_only",
            "manifest_and_owner_identity_read_only": True,
            "database_or_manifest_mutated": False,
            "model_calls": 0,
            "replayed_from_root_genesis": True,
            "root_initial_state_hash_verified": True,
            "replay_initial_callback_matches_root_genesis": initial_replay_hash_matches,
            "replay_sequences_monotonic": monotonic_sequences,
            "replay_boundary_count_matches_versions_plus_initial": replay_boundary_count_matches,
            "requested_boundary_replayed_from_manifest_pin": (
                requested_boundary_replayed if checkpoint is not None else None
            ),
            "audited_boundary_replayed_from_genesis": requested_boundary_replayed,
            "audited_boundary_matches_stored_authority": stored_boundary_matches,
            "stored_checkpoint_event_and_snapshot_match": stored_checkpoint_matches,
            "expected_committed_states_including_initial": expected_states,
            "committed_states_audited": committed_states_audited,
            "distinct_sequences_audited": len(audited_by_sequence),
            "final_replay_matches_authoritative_head": replay_matches_head if checkpoint is None else None,
            "raw_head_metadata_matches_repository_view": stored_head_matches,
            "durable_job_succeeded_at_exact_target": expected_final_tick if checkpoint is None else None,
            "saved_manifest_final_receipt_matches_head": persisted_manifest_matches if checkpoint is None else None,
            "content_index_key_names": ["key_npc_ids", "other_resident_ids"],
            "authored_key_id_index_valid": key_index_valid,
            "authored_resident_id_index_valid": resident_index_valid,
            "content_index_matches_root_genesis": content_index_matches_genesis,
            "seed_counts_unchanged_from_root_genesis": seed_counts_match_genesis,
            "seed_counts_match_content_index": seed_counts_match,
            "authored_resident_id_index_count": len(resident_ids),
        }
        replay_verified = all(
            coverage[key] is True
            for key in (
                "root_initial_state_hash_verified",
                "replay_initial_callback_matches_root_genesis",
                "replay_sequences_monotonic",
                "replay_boundary_count_matches_versions_plus_initial",
            )
        ) and requested_boundary_verified is True
        if checkpoint is None:
            replay_verified = replay_verified and stored_head_matches is True and replay_matches_head
        audit_passed = (
            replay_verified
            and (checkpoint is not None or (
                coverage["durable_job_succeeded_at_exact_target"]
                and coverage["saved_manifest_final_receipt_matches_head"]
            ))
            and coverage["authored_key_id_index_valid"]
            and coverage["authored_resident_id_index_valid"]
            and coverage["content_index_matches_root_genesis"]
            and coverage["seed_counts_unchanged_from_root_genesis"]
            and seed_counts_match is not False
        )
        capacity_checks = {
            metric: {
                "minimum_count": int(record["count"]),
                "threshold": RESIDENT_THRESHOLD,
                "passed_continuously": int(record["count"]) >= RESIDENT_THRESHOLD,
            }
            for metric, record in minima.items()
            if metric in first_below
        }
        structural_checks = _continuous_threshold_checks(minima, STRUCTURAL_THRESHOLDS)
        structural_thresholds_passed = all(
            bool(check["passed_continuously"]) for check in structural_checks.values()
        )
        capacity_interval_passed = len(capacity_checks) == len(first_below) and all(
            check["passed_continuously"] for check in capacity_checks.values()
        )
        report_only_structural_counts = {
            category: {
                structure: {
                    "minimum_during_replay": minima.get(metric),
                    "at_audited_boundary": last_observed_sample.get(metric),
                }
                for structure, metric in metric_by_structure.items()
            }
            for category, metric_by_structure in STRUCTURAL_REPORT_ONLY_METRICS.items()
        }
        audit_boundary_settled = replay_verified and (
            checkpoint is not None or expected_final_tick and persisted_manifest_matches
        )
        capacity_passed = audit_boundary_settled and capacity_interval_passed
        target_span_ticks = audit_tick - int(manifest["start_tick"])
        target_span_days, target_day_remainder_ticks = divmod(target_span_ticks, day_seconds)
        target_span_years, target_year_remainder_days = divmod(target_span_days, year_days)
        target_span_is_exact_600_years = (
            target_span_years == 600
            and target_year_remainder_days == 0
            and target_day_remainder_ticks == 0
        )
        population_600_year_passed = (
            replay_verified
            and audit_boundary_settled
            and target_span_is_exact_600_years
            and capacity_interval_passed
        )
        return {
            "audit": "world_capacity_genesis_replay",
            "schema": 1,
            "status": (
                "checkpoint_prefix_verified" if audit_passed and checkpoint is not None
                else "replay_verified" if audit_passed
                else "committed_prefix_verified" if replay_verified and audit_scope == "committed_prefix"
                else "audit_mismatch"
            ),
            "success": audit_passed,
            "replay_verified": replay_verified,
            "capacity_passed": capacity_passed,
            "scope": {
                "world_id": str(world_id),
                "branch_id": str(branch_id),
                "job_id": str(job_id),
                "owner_matches": True,
                "audit_mode": audit_scope,
                "checkpoint_year": checkpoint["year"] if checkpoint is not None else None,
                "audited_through_sequence": str(audit_sequence),
                "audited_through_tick": str(audit_tick),
                "committed_prefix_boundary": (
                    {
                        "sequence": str(audit_sequence),
                        "tick": str(audit_tick),
                        "state_version": str(audit_state_version),
                        "state_hash": view.state_hash,
                    }
                    if audit_scope == "committed_prefix" else None
                ),
                "audited_boundary_is_authoritative_head": checkpoint is None,
                "requested_target_is_ahead_of_audited_boundary": audit_tick < target_tick,
                "pinned_checkpoint": (
                    {
                        "sequence": str(checkpoint["sequence"]),
                        "tick": str(checkpoint["tick"]),
                        "state_version": str(checkpoint["state_version"]),
                        "state_hash": checkpoint["state_hash"],
                    }
                    if checkpoint is not None else None
                ),
                "authoritative_head_is_ahead_of_audited_prefix": (
                    bool(view.last_seq > audit_sequence or view.state.current_tick > audit_tick)
                    if checkpoint is not None else False
                ),
            },
            "time": {
                "start_tick": str(manifest["start_tick"]),
                "expected_target_tick": str(target_tick),
                "audited_boundary_tick": str(audit_tick),
                "audited_boundary_kind": (
                    "manifest_checkpoint" if checkpoint is not None
                    else "committed_branch_head_prefix" if audit_scope == "committed_prefix"
                    else "authoritative_head_at_target" if audit_scope == "full_target"
                    else "authoritative_head_after_target"
                ),
                "audited_boundary_replayed_and_stored": requested_boundary_verified,
                "requested_target_reached_at_audited_boundary": audit_tick == target_tick,
                "authoritative_tick": str(view.state.current_tick),
                "committed_through_tick": (
                    str(job.committed_through_tick) if job.committed_through_tick is not None else None
                ),
                "job_status": job.status.value,
                "calendar_basis": {
                    "day_seconds": day_seconds,
                    "year_days": year_days,
                    "source": "verified root genesis calendar",
                },
                "settled_target_span_days": {
                    "whole_days": target_span_days,
                    "remainder_ticks": str(target_day_remainder_ticks),
                    "exact_whole_days": target_day_remainder_ticks == 0,
                    "settled_at_exact_target": expected_final_tick,
                    "settled_at_audited_boundary": requested_boundary_verified,
                },
                "settled_target_span_years": {
                    "whole_years": target_span_years,
                    "remaining_days": target_year_remainder_days,
                    "remainder_ticks": str(target_day_remainder_ticks),
                    "exact_whole_years": (
                        target_year_remainder_days == 0 and target_day_remainder_ticks == 0
                    ),
                    "settled_at_exact_target": expected_final_tick,
                    "settled_at_audited_boundary": requested_boundary_verified,
                },
            },
            "head": {
                "state_hash": view.state_hash,
                "state_version": str(view.branch.state_version),
                "last_sequence": str(view.last_seq),
                "tick": str(view.state.current_tick),
                "replayed_state_hash": replay.state_hash,
                "replayed_state_version": str(audit_state_version),
                "replayed_last_sequence": str(replay.last_seq),
                "replayed_tick": str(replay.state.current_tick),
            },
            "authored_key_npcs": {
                "authored_id_count": len(authored_key_ids),
                "initial_profile_threshold": _initial_key_profile_threshold(
                    authored_key_ids,
                    initial_active_named_key_profile_count,
                    key_index_valid=key_index_valid,
                ),
                "active_at_audited_boundary": key_status_counts.get("active", 0),
                "status_counts_at_audited_boundary": dict(sorted(key_status_counts.items())),
                "minimum_active_during_replay": minima.get("active_authored_key_npcs"),
                "note": "Authored key identities are reported separately from the 240 active named resident threshold.",
            },
            "content_index": {
                "pack_id": genesis_metadata.get("pack_id"),
                "pack_version": genesis_metadata.get("pack_version"),
                "recognized_id_fields": ["key_npc_ids", "other_resident_ids"],
                "key_npc_ids_valid": key_index_valid,
                "other_resident_ids_valid": resident_index_valid,
                "matches_root_genesis_metadata": content_index_matches_genesis,
                "seed_counts_unchanged_from_root_genesis": seed_counts_match_genesis,
                "authored_key_id_count": len(authored_key_ids),
                "authored_resident_id_count": len(resident_ids),
                "seed_counts_match": seed_counts_match,
            },
            "capacity": {
                "threshold_active_named_residents": RESIDENT_THRESHOLD,
                "audit_interval_scope": audit_scope,
                "audited_through_year": target_span_years,
                "minimum_counts": minima,
                "continuous_240_checks_over_replayed_history": capacity_checks,
                "thresholds_passed_over_replayed_history": capacity_interval_passed,
                "all_three_definitions_passed": capacity_passed,
                "first_below_240_by_definition": first_below,
                "authored_seed_counts_match_content_index": seed_counts_match,
                "first_below_240": first_below[
                    "active_named_residents_excluding_player_and_authored_keys"
                ],
            },
            "structural_capacity": {
                "audit_interval_scope": audit_scope,
                "thresholds": STRUCTURAL_THRESHOLDS,
                "minimum_counts": {
                    metric: minima.get(metric) for metric in STRUCTURAL_THRESHOLDS
                },
                "continuous_checks_over_replayed_history": structural_checks,
                "thresholds_passed_over_replayed_history": structural_thresholds_passed,
                "report_only_counts": report_only_structural_counts,
                "report_only_definition": (
                    "Active named members are the intersection of governance member IDs and active named people. "
                    "An active member leader additionally requires governance leader_id to be in that intersection."
                ),
            },
            "institutional_continuity": institutional_continuity_report,
            "population_600_year_passed": population_600_year_passed,
            "population_600_year_pass_scope": (
                "Requires a replay-verified audited interval of exactly 600 calendar years and all three "
                "continuous 240-person count definitions; it is not full product acceptance certification."
            ),
            "root_genesis": root_genesis,
            "coverage": coverage,
            "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        engine.dispose()


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    parent = path.parent
    if not parent.is_dir():
        raise AuditError("The output directory does not exist.")
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=f".{path.name}.", suffix=".tmp",
            dir=parent, delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only genesis replay and commit-by-commit capacity audit for an existing "
            "probe-world-advance data directory. No world or model writes are performed."
        )
    )
    parser.add_argument("--data-directory", required=True, type=Path, help="existing saved probe directory")
    parser.add_argument("--output", type=Path, help="optional atomically written JSON report")
    parser.add_argument(
        "--checkpoint-year",
        type=int,
        help="audit only the exact saved decade checkpoint at year N (for example, 20)",
    )
    parser.add_argument(
        "--progress-every-commits",
        type=_positive_int,
        default=10_000,
        metavar="N",
        help="write compact replay progress to stderr every N audited commits (default: 10000)",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    directory = args.data_directory.expanduser().resolve()
    if not directory.is_dir():
        print("audit error: the saved probe directory does not exist", file=sys.stderr)
        return 2
    try:
        output_path = _safe_report_output_path(args.output, directory) if args.output is not None else None
        report = _run_audit(
            directory,
            checkpoint_year=args.checkpoint_year,
            progress_every_commits=args.progress_every_commits,
        )
        if output_path is not None:
            report["report_file"] = str(output_path)
            _atomic_write_json(output_path, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0 if report["success"] else 2
    except AuditError as exc:
        print(f"audit error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Avoid printing arbitrary state or SQL values; keep diagnostics safe.
        print(f"audit failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
