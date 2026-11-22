"""Read-only population audit between two exact saved decade checkpoints.

This verifier starts from the persisted start checkpoint and replays only the
selected interval. It checks the saved start/end pins and visits every commit
boundary in the interval. It does not replay the genesis prefix. Institutional
continuity metrics are opt-in and remain limited to the selected interval.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any
from uuid import UUID


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from world_engine.domain.models import AccessScope, WorldState  # noqa: E402
from world_engine.storage.repository import WorldRepository  # noqa: E402


_BASE_SPEC = importlib.util.spec_from_file_location(
    "audit_world_capacity_interval_base", PROJECT_ROOT / "scripts" / "audit-world-capacity.py",
)
if _BASE_SPEC is None or _BASE_SPEC.loader is None:
    raise RuntimeError("Could not load the shared capacity audit helpers.")
_BASE = importlib.util.module_from_spec(_BASE_SPEC)
sys.modules[_BASE_SPEC.name] = _BASE
_BASE_SPEC.loader.exec_module(_BASE)


MANIFEST_NAME = ".probe-world-advance.json"
IDENTITY_NAME = "local-identity.json"
DATABASE_NAME = "world-engine.db"
MANIFEST_SCHEMA = 1
ORIGINAL_KEY_NPC_COUNT = 32


AuditError = _BASE.AuditError


def _load_inputs(
    directory: Path, start_year: int, end_year: int,
) -> tuple[dict[str, Any], AccessScope, UUID, UUID, dict[str, Any], dict[str, Any], int, int]:
    if start_year >= end_year:
        raise AuditError("The end year must be later than the start year.")
    manifest = _BASE._read_json_object(directory / MANIFEST_NAME, "probe manifest")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise AuditError("The probe manifest schema is unsupported.")
    if manifest.get("database_file", DATABASE_NAME) != DATABASE_NAME:
        raise AuditError("The probe manifest does not identify the expected database file.")
    if not manifest.get("world_created"):
        raise AuditError("The probe manifest does not record a created world.")
    config = manifest.get("config")
    if not isinstance(config, dict):
        raise AuditError("The probe manifest has no valid run configuration.")
    try:
        world_id = UUID(str(manifest["world_id"]))
        branch_id = UUID(str(manifest["branch_id"]))
        start_tick = int(manifest["start_tick"])
        target_tick = int(manifest["target_tick"])
        day_seconds = config["day_seconds"]
        year_days = config["year_days"]
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditError("The probe manifest is missing valid world, branch, tick, or calendar data.") from exc
    if (
        start_tick < 0 or target_tick <= start_tick
        or type(day_seconds) is not int or day_seconds <= 0
        or type(year_days) is not int or year_days <= 0
    ):
        raise AuditError("The probe manifest has an invalid exact interval or calendar configuration.")
    total_seconds = config.get("total_seconds")
    if total_seconds is not None and (
        type(total_seconds) is not int or total_seconds <= 0 or start_tick + total_seconds != target_tick
    ):
        raise AuditError("The probe target tick differs from its saved duration configuration.")
    if start_year % 10 or end_year % 10:
        raise AuditError("Start and end years must be exact decade checkpoint years (multiples of 10).")

    start = _manifest_checkpoint(
        manifest, start_year, start_tick=start_tick, target_tick=target_tick,
        day_seconds=day_seconds, year_days=year_days,
    )
    end = _manifest_checkpoint(
        manifest, end_year, start_tick=start_tick, target_tick=target_tick,
        day_seconds=day_seconds, year_days=year_days,
    )
    if end["tick"] <= start["tick"] or end["sequence"] <= start["sequence"]:
        raise AuditError("The selected exact decade checkpoints are not in increasing tick and sequence order.")

    identity = _BASE._read_json_object(directory / IDENTITY_NAME, "probe owner identity")
    try:
        scope = AccessScope.model_validate(identity)
    except Exception as exc:
        raise AuditError("The saved probe owner identity is invalid.") from exc
    return manifest, scope, world_id, branch_id, start, end, day_seconds, year_days


def _expected_commit_boundaries(
    engine: Engine, world_id: UUID, branch_id: UUID, start_sequence: int, end_sequence: int,
) -> list[tuple[int, int]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT sequence, tick FROM world_events "
                "WHERE world_id = :world_id AND branch_id = :branch_id AND commit_boundary = 1 "
                "AND sequence > :start_sequence AND sequence <= :end_sequence ORDER BY sequence"
            ),
            {
                "world_id": world_id.hex,
                "branch_id": branch_id.hex,
                "start_sequence": start_sequence,
                "end_sequence": end_sequence,
            },
        ).all()
    result = [(int(row[0]), int(row[1])) for row in rows]
    sequences = [start_sequence, *(sequence for sequence, _tick in result)]
    if any(left >= right for left, right in zip(sequences, sequences[1:])):
        raise AuditError("The persisted commit-boundary sequence is not strictly increasing.")
    if not result or result[-1][0] != end_sequence:
        raise AuditError("The end checkpoint is missing from the persisted branch commit-boundary set.")
    return [(start_sequence, -1), *result]


def _manifest_checkpoint(manifest: dict[str, Any], year: int, **kwargs: Any) -> dict[str, Any]:
    if year % 10:
        raise AuditError("Start and end years must be exact decade checkpoint years (multiples of 10).")
    return _BASE._manifest_checkpoint(manifest, year, **kwargs)


def _decade_year(value: str) -> int:
    year = _BASE._positive_int(value)
    if year % 10:
        raise argparse.ArgumentTypeError("must be a saved decade checkpoint year (multiple of 10)")
    return year


_readonly_engine = _BASE._readonly_engine


def _population_counts(audited: dict[str, int]) -> dict[str, int]:
    return {
        "active_named_including_player": audited["active_named_people_including_player"],
        "active_player_named": (
            audited["active_named_people_including_player"]
            - audited["active_named_people_excluding_player"]
        ),
        "active_original_key_npcs": audited["active_authored_key_npcs"],
        "ordinary_active_named_excluding_player_and_original_key_npcs": (
            audited["active_named_residents_excluding_player_and_authored_keys"]
        ),
    }


def _count_population(state: WorldState, key_ids: set[UUID]) -> dict[str, int]:
    return _population_counts(_BASE._sample_counts(state, key_ids))


def _boundary_description(
    sequence: int,
    tick: int,
    start: dict[str, Any],
    end: dict[str, Any],
    *,
    start_tick: int,
    day_seconds: int,
    year_days: int,
) -> dict[str, Any]:
    if sequence == start["sequence"]:
        kind = "start_checkpoint"
        year = start["year"]
    elif sequence == end["sequence"]:
        kind = "end_checkpoint"
        year = end["year"]
    else:
        kind = "interior_committed_state"
        year = None
    elapsed = tick - start_tick
    year_seconds = year_days * day_seconds
    whole_years, year_remainder = divmod(elapsed, year_seconds)
    whole_days, day_remainder = divmod(year_remainder, day_seconds)
    return {
        "kind": kind,
        "scheduled_checkpoint_year": year,
        "tick": str(tick),
        "sequence": str(sequence),
        "calendar_offset_from_probe_start": {
            "whole_years": whole_years,
            "remaining_days": whole_days,
            "remainder_seconds": str(day_remainder),
            "exact_calendar_year_boundary": year_remainder == 0,
        },
    }


def _ordinary_population_shortfalls(
    samples: list[tuple[int, int, int]],
) -> dict[str, Any]:
    """Measure contiguous shortfalls between committed observations, including same-tick recovery."""
    threshold = _BASE.RESIDENT_THRESHOLD
    episodes = []
    opened = None
    for index, (sequence, tick, count) in enumerate(samples):
        if count < threshold:
            if opened is None:
                opened = {
                    "start_sequence": str(sequence), "start_tick": str(tick),
                    "left_censored": index == 0, "minimum_count": count,
                    "deficient_commit_count": 0,
                }
            opened["minimum_count"] = min(opened["minimum_count"], count)
            opened["deficient_commit_count"] += 1
        elif opened is not None:
            episodes.append({**opened, "end_sequence": str(sequence), "end_tick": str(tick),
                             "right_censored": False, "duration_seconds_observed": tick - int(opened["start_tick"])})
            opened = None
    if opened is not None:
        sequence, tick, _ = samples[-1]
        episodes.append({**opened, "end_sequence": str(sequence), "end_tick": str(tick),
                         "right_censored": True, "duration_seconds_observed": tick - int(opened["start_tick"])})
    return {
        "threshold": threshold,
        "definition": "Ends at the first observed recovery or at the interval end if still deficient.",
        "episodes": episodes,
        "deficient_commit_count": sum(item["deficient_commit_count"] for item in episodes),
    }


class _IntervalInstitutionWindows:
    """Keep every observed zero-member window and mark interval censoring."""

    def __init__(self) -> None:
        self._names: dict[UUID, str] = {}
        self._windows: dict[UUID, list[dict[str, Any]]] = {}
        self._open: dict[UUID, dict[str, Any]] = {}
        self._left_censored: set[UUID] = set()
        self._first_sample = True
        self._sample_count = 0

    def observe(
        self,
        sequence: int,
        tick: int,
        active_organizations: dict[UUID, tuple[str, bool]],
    ) -> None:
        gaps = {organization_id for organization_id, (_name, has_member) in active_organizations.items() if not has_member}
        for organization_id, (name, _has_member) in active_organizations.items():
            if name:
                self._names[organization_id] = name
        for organization_id, window in list(self._open.items()):
            if organization_id in gaps:
                window["observed_zero_member_committed_state_count"] += 1
                window["last_zero_member_sequence"] = str(sequence)
                window["last_zero_member_tick"] = str(tick)
            else:
                end_reason = (
                    "organization_inactive_or_missing"
                    if organization_id not in active_organizations else "active_named_membership_recovered"
                )
                self._close_window(organization_id, window, sequence, tick, end_reason)
                del self._open[organization_id]
        for organization_id in gaps - self._open.keys():
            name = active_organizations[organization_id][0]
            left_censored = self._first_sample
            if left_censored:
                self._left_censored.add(organization_id)
            self._open[organization_id] = self._new_window(
                organization_id, name, sequence, tick, left_censored=left_censored,
            )
        self._first_sample = False
        self._sample_count += 1

    @staticmethod
    def _new_window(
        organization_id: UUID,
        name: str,
        sequence: int,
        tick: int,
        *,
        left_censored: bool,
    ) -> dict[str, Any]:
        return {
            "organization_id": str(organization_id),
            "organization_name": name,
            "start_sequence": str(sequence),
            "start_tick": str(tick),
            "start_observation": (
                "already_without_active_named_members_at_interval_start"
                if left_censored else "first_committed_state_observed_without_active_named_members"
            ),
            "left_censored": left_censored,
            "right_censored": False,
            "observed_zero_member_committed_state_count": 1,
            "last_zero_member_sequence": str(sequence),
            "last_zero_member_tick": str(tick),
        }

    def _close_window(
        self,
        organization_id: UUID,
        window: dict[str, Any],
        sequence: int,
        tick: int,
        end_reason: str,
    ) -> None:
        start_tick = int(window["start_tick"])
        if tick < start_tick:
            raise AuditError("An institutional gap ended before it began.")
        self._windows.setdefault(organization_id, []).append({
            **window,
            "end_sequence": str(sequence),
            "end_tick": str(tick),
            "duration_seconds_observed": tick - start_tick,
            "end_reason": end_reason,
        })

    def finish(self, *, pin_tick: int, pin_sequence: int) -> dict[str, Any]:
        for organization_id, window in list(self._open.items()):
            self._close_window(
                organization_id, window, pin_sequence, pin_tick, "interval_end_still_without_active_named_members",
            )
            self._windows[organization_id][-1]["right_censored"] = True
        self._open.clear()
        observed_right = {
            UUID(row["organization_id"])
            for rows in self._windows.values()
            for row in rows
            if row["right_censored"]
        }
        return {
            "observed_commit_boundary_count": self._sample_count,
            "left_censored_organization_count": len(self._left_censored),
            "right_censored_organization_count": len(observed_right),
            "left_censored_organization_ids": sorted(map(str, self._left_censored)),
            "right_censored_organization_ids": sorted(map(str, observed_right)),
            "organizations": {
                str(organization_id): {
                    "organization_name": self._names.get(organization_id, ""),
                    "left_censored_at_interval_start": organization_id in self._left_censored,
                    "right_censored_at_interval_end": organization_id in observed_right,
                    "gap_windows": rows,
                }
                for organization_id, rows in sorted(self._windows.items(), key=lambda item: str(item[0]))
            },
        }


class _ActiveMemberLeaderAudit:
    """Summarize the existing capacity audit's active named member-leader count."""

    METRICS = (
        "active_countries_with_active_member_leader",
        "active_other_organizations_with_active_member_leader",
    )

    def __init__(self) -> None:
        self._samples: dict[str, dict[str, dict[str, int | str] | None]] = {
            metric: {"first": None, "last": None, "minimum": None, "maximum": None}
            for metric in self.METRICS
        }
        self._sample_count = 0

    def observe(self, sequence: int, tick: int, counts: dict[str, int]) -> None:
        for metric in self.METRICS:
            count = counts[metric]
            sample: dict[str, int | str] = {"count": count, "sequence": str(sequence), "tick": str(tick)}
            rows = self._samples[metric]
            if rows["first"] is None:
                rows["first"] = sample
            rows["last"] = sample
            if rows["minimum"] is None or count < int(rows["minimum"]["count"]):
                rows["minimum"] = sample
            if rows["maximum"] is None or count > int(rows["maximum"]["count"]):
                rows["maximum"] = sample
        self._sample_count += 1

    def report(self) -> dict[str, Any]:
        return {
            "definition": (
                "The existing audit counts a country or other organization leader only when governance leader_id is "
                "listed among members and resolves to an active named person."
            ),
            "observed_commit_boundary_count": self._sample_count,
            "tie_policy": "The earliest visited committed state is reported when minima or maxima tie.",
            "by_institution_kind": {
                metric: {
                    "at_interval_start": rows["first"],
                    "at_interval_end": rows["last"],
                    "minimum": rows["minimum"],
                    "maximum": rows["maximum"],
                }
                for metric, rows in self._samples.items()
            },
        }


class _StructuralCapacityAudit:
    """Track structural count minima and first threshold failures from shared samples."""

    def __init__(self) -> None:
        self._minimum_counts: dict[str, dict[str, int | str]] = {}
        self._first_deficient: dict[str, dict[str, int | str]] = {}
        self._sample_count = 0

    def observe(self, sequence: int, tick: int, counts: dict[str, int]) -> None:
        for metric, threshold in _BASE.STRUCTURAL_THRESHOLDS.items():
            count = counts[metric]
            sample: dict[str, int | str] = {"count": count, "sequence": str(sequence), "tick": str(tick)}
            minimum = self._minimum_counts.get(metric)
            if minimum is None or count < int(minimum["count"]):
                self._minimum_counts[metric] = sample
            if count < threshold and metric not in self._first_deficient:
                self._first_deficient[metric] = sample
        self._sample_count += 1

    def report(self) -> dict[str, Any]:
        thresholds = dict(_BASE.STRUCTURAL_THRESHOLDS)
        checks = _BASE._continuous_threshold_checks(self._minimum_counts, thresholds)
        return {
            "thresholds": thresholds,
            "minimum_counts": {
                metric: self._minimum_counts.get(metric) for metric in thresholds
            },
            "continuous_checks_over_replayed_interval": checks,
            "gate_passed": all(check["passed_continuously"] for check in checks.values()),
            "first_deficient_sequences": {
                metric: self._first_deficient.get(metric) for metric in thresholds
            },
            "observed_commit_boundary_count": self._sample_count,
            "minimum_tie_policy": "The earliest visited committed state is reported when minima tie.",
        }


def _run_audit(
    directory: Path,
    start_year: int,
    end_year: int,
    *,
    progress_every_commits: int = 10_000,
    institutions: bool = False,
    split_decades: bool = False,
) -> dict[str, Any]:
    manifest, scope, world_id, branch_id, start, end, day_seconds, year_days = _load_inputs(
        directory, start_year, end_year,
    )
    engine = _BASE._readonly_engine(directory / DATABASE_NAME)
    try:
        root_genesis, genesis_metadata = _BASE._root_genesis(engine, world_id, branch_id)
        root_calendar = root_genesis["calendar"]
        if int(root_calendar["day_seconds"]) != day_seconds or int(root_calendar["year_days"]) != year_days:
            raise AuditError("The saved probe calendar differs from the verified root genesis calendar.")
        raw_content_index = genesis_metadata.get("content_index")
        content_index = raw_content_index if isinstance(raw_content_index, dict) else {}
        raw_key_ids = content_index.get("key_npc_ids")
        key_ids = _BASE._uuid_set(raw_key_ids)
        if not _BASE._is_uuid_index(raw_key_ids) or len(key_ids) != ORIGINAL_KEY_NPC_COUNT:
            raise AuditError(
                f"The verified root genesis must identify exactly {ORIGINAL_KEY_NPC_COUNT} original key NPCs."
            )
        genesis_hash = root_genesis["state_hash"]
        pins = [start]
        if split_decades:
            pins.extend(
                _manifest_checkpoint(
                    manifest, year, start_tick=int(manifest["start_tick"]), target_tick=int(manifest["target_tick"]),
                    day_seconds=day_seconds, year_days=year_days,
                )
                for year in range(start_year + 10, end_year, 10)
            )
        pins.append(end)
        for pin in pins:
            if not _BASE._verify_stored_checkpoint_boundary(engine, world_id, branch_id, pin):
                raise AuditError(f"The saved year {pin['year']} checkpoint does not match a persisted boundary pin.")
        expected = _expected_commit_boundaries(engine, world_id, branch_id, start["sequence"], end["sequence"])
        repository = WorldRepository(engine)
        view = repository.read_world(scope, world_id, branch_id)
        if view.world.owner_id != scope.user_id or view.world.tenant_id != scope.tenant_id:
            raise AuditError("The saved identity is not the owner of this probe world.")
        if end["sequence"] > view.last_seq:
            raise AuditError("The selected end checkpoint is ahead of the authoritative branch head.")

        observed = 0
        previous_sequence: int | None = None
        minima: dict[str, dict[str, int | str]] = {}
        trough_counts: dict[str, int] | None = None
        ordinary_samples: list[tuple[int, int, int]] = []
        progress = _BASE._CommitProgress(progress_every_commits)
        institution_tracker = _BASE._InstitutionalContinuityAudit() if institutions else None
        institution_windows = _IntervalInstitutionWindows() if institutions else None
        active_member_leaders = _ActiveMemberLeaderAudit() if institutions else None
        structural_capacity = _StructuralCapacityAudit() if institutions else None

        def observe_commit(sequence: int, state: WorldState) -> None:
            nonlocal observed, previous_sequence, trough_counts
            if observed >= len(expected):
                raise AuditError("Replay visited a commit boundary outside the persisted interval set.")
            expected_sequence, expected_tick = expected[observed]
            if sequence != expected_sequence:
                raise AuditError("Replay sequence does not exactly match the persisted branch commit-boundary set.")
            if previous_sequence is not None and sequence <= previous_sequence:
                raise AuditError("Replay commit sequences are not strictly increasing.")
            if observed == 0:
                if sequence != start["sequence"] or state.current_tick != start["tick"]:
                    raise AuditError("Replay did not begin at the exact saved start checkpoint boundary.")
                if _BASE.state_hash(state) != start["state_hash"]:
                    raise AuditError("Replayed start state does not match the saved checkpoint hash.")
                if state.player_id in key_ids:
                    raise AuditError("The player identity overlaps the original key NPC identity list.")
            elif state.current_tick != expected_tick:
                raise AuditError("A replayed state tick differs from its persisted commit-boundary tick.")
            if institution_tracker is not None:
                active_organizations: dict[UUID, tuple[str, bool]] = {}
                sampled = _BASE._sample_counts(
                    state,
                    key_ids,
                    active_organization_member_status=active_organizations,
                )
                counts = _population_counts(sampled)
                institution_tracker.observe(sequence, state.current_tick, active_organizations)
                assert institution_windows is not None and active_member_leaders is not None
                institution_windows.observe(sequence, state.current_tick, active_organizations)
                active_member_leaders.observe(sequence, state.current_tick, sampled)
                assert structural_capacity is not None
                structural_capacity.observe(sequence, state.current_tick, sampled)
            else:
                counts = _count_population(state, key_ids)
            ordinary_samples.append((
                sequence, state.current_tick, counts["ordinary_active_named_excluding_player_and_original_key_npcs"],
            ))
            for metric, count in counts.items():
                previous = minima.get(metric)
                if previous is None or count < int(previous["count"]):
                    minima[metric] = {
                        "count": count,
                        "sequence": str(sequence),
                        "tick": str(state.current_tick),
                    }
                    if metric == "ordinary_active_named_excluding_player_and_original_key_npcs":
                        trough_counts = counts.copy()
            previous_sequence = sequence
            observed += 1
            progress.report(observed, sequence, state.current_tick)

        for left, right in zip(pins, pins[1:]):
            if right["sequence"] <= left["sequence"]:
                raise AuditError("Replay checkpoint sequences are not strictly increasing.")
            first_visit = True

            def observe_chunk(sequence: int, state: WorldState) -> None:
                nonlocal first_visit
                if first_visit:
                    first_visit = False
                    if sequence != left["sequence"] or state.current_tick != left["tick"]:
                        raise AuditError("A replay transaction did not start at its exact saved pin.")
                    if left is not start:
                        if previous_sequence != sequence or _BASE.state_hash(state) != left["state_hash"]:
                            raise AuditError("Adjacent replay transactions do not share the same verified boundary.")
                        return
                observe_commit(sequence, state)

            replay = repository.replay_from_checkpoint(
                scope, world_id, branch_id, checkpoint_seq=left["sequence"], through_seq=right["sequence"],
                checkpoint_hash=left["state_hash"], visit_commit=observe_chunk,
            )
            if (
                first_visit or previous_sequence != right["sequence"] or replay.last_seq != right["sequence"]
                or replay.state.current_tick != right["tick"] or replay.state_hash != right["state_hash"]
            ):
                raise AuditError(f"Replay result does not match the exact saved year {right['year']} checkpoint pin.")
        if observed != len(expected):
            raise AuditError("Replay did not visit every persisted commit boundary in the selected interval.")
        if not minima or trough_counts is None:
            raise AuditError("The selected interval contained no replayed committed states.")

        trough = minima["ordinary_active_named_excluding_player_and_original_key_npcs"]
        trough_sequence, trough_tick = int(trough["sequence"]), int(trough["tick"])
        report = {
            "audit": "capacity_interval_population",
            "schema": 1,
            "status": "verified",
            "success": True,
            "scope": {
                "world_id": str(world_id),
                "branch_id": str(branch_id),
                "interval_type": "exact_saved_decade_checkpoint_interval",
                "population_only": not institutions,
                "institutional_metrics_audited": institutions,
                "genesis_prefix_replayed": False,
                "original_key_identity_source": "key_npc_ids from the hash-verified stored root genesis state",
                "verified_root_genesis_hash": genesis_hash,
            },
            "interval": {
                "start_checkpoint": {
                    "year": start["year"],
                    "sequence": str(start["sequence"]),
                    "tick": str(start["tick"]),
                    "state_version": str(start["state_version"]),
                    "state_hash": start["state_hash"],
                },
                "end_checkpoint": {
                    "year": end["year"],
                    "sequence": str(end["sequence"]),
                    "tick": str(end["tick"]),
                    "state_version": str(end["state_version"]),
                    "state_hash": end["state_hash"],
                },
                "expected_commit_boundary_count": len(expected),
                "observed_commit_boundary_count": observed,
                "replay_transaction_count": len(pins) - 1,
                "commit_boundary_sequence_verified": True,
                "first_sequence": str(expected[0][0]),
                "last_sequence": str(expected[-1][0]),
                "calendar_basis": {"day_seconds": day_seconds, "year_days": year_days},
            },
            "population": {
                "definitions": {
                    "active_named_including_player": "Active character/person entities with a non-empty name.",
                    "active_player_named": "Whether the saved player entity is an active named character/person (0 or 1).",
                    "active_original_key_npcs": "Active named identities from the 32-ID root genesis key_npc_ids list.",
                    "ordinary_active_named_excluding_player_and_original_key_npcs": (
                        "Active named character/person entities, excluding the player and the original 32 key NPC identities."
                    ),
                    "minimum_tie_policy": "The earliest visited committed state is reported when minima tie.",
                },
                "minimum_counts": minima,
                "ordinary_population_shortfalls": _ordinary_population_shortfalls(ordinary_samples),
                "ordinary_population_trough": {
                    "count": int(trough["count"]),
                    "sequence": str(trough_sequence),
                    "tick": str(trough_tick),
                    "exact_boundary": _boundary_description(
                        trough_sequence,
                        trough_tick,
                        start,
                        end,
                        start_tick=int(manifest["start_tick"]),
                        day_seconds=day_seconds,
                        year_days=year_days,
                    ),
                    "other_population_counts_at_trough": trough_counts,
                },
            },
            "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        if institution_tracker is not None:
            assert institution_windows is not None and active_member_leaders is not None
            continuity = institution_tracker.report(
                pin_tick=end["tick"],
                pin_sequence=end["sequence"],
            )
            continuity["audited_from_tick"] = str(start["tick"])
            continuity["audited_from_sequence"] = str(start["sequence"])
            continuity["audited_through_tick"] = str(end["tick"])
            continuity["audited_through_sequence"] = str(end["sequence"])
            continuity["interval_scope"] = "exact_saved_checkpoint_interval_only"
            continuity["continuous_from_genesis"] = False
            gap_report = institution_windows.finish(
                pin_tick=end["tick"],
                pin_sequence=end["sequence"],
            )
            gap_rows = gap_report["organizations"]
            for organization in continuity["organizations"]:
                observed = gap_rows.get(organization["organization_id"], {})
                organization["left_censored_at_interval_start"] = observed.get(
                    "left_censored_at_interval_start", False,
                )
                organization["right_censored_at_interval_end"] = observed.get(
                    "right_censored_at_interval_end", False,
                )
                organization["gap_windows"] = observed.get("gap_windows", [])
            continuity["censoring"] = {
                "definition": (
                    "Left-censored organizations were already active with no active named member at the start checkpoint. "
                    "Right-censored organizations remain in that condition at the end checkpoint. Durations are observed "
                    "within this interval and make no claim about time before its start."
                ),
                "left_censored_organization_count": gap_report["left_censored_organization_count"],
                "right_censored_organization_count": gap_report["right_censored_organization_count"],
                "left_censored_organization_ids": gap_report["left_censored_organization_ids"],
                "right_censored_organization_ids": gap_report["right_censored_organization_ids"],
                "observed_commit_boundary_count": gap_report["observed_commit_boundary_count"],
            }
            continuity["active_named_member_leaders"] = active_member_leaders.report()
            assert structural_capacity is not None
            continuity["structural_capacity"] = structural_capacity.report()
            report["institutions"] = continuity
        return report
    finally:
        engine.dispose()


_safe_report_output_path = _BASE._safe_report_output_path
_atomic_write_json = _BASE._atomic_write_json


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only population audit for every committed state between two exact saved decade checkpoints. "
            "The interval starts from the pinned snapshot; genesis-prefix replay and institutional metrics are not included."
        )
    )
    parser.add_argument("--data-directory", required=True, type=Path, help="existing saved probe directory")
    parser.add_argument("--start-year", required=True, type=_decade_year, help="saved decade checkpoint year to start from")
    parser.add_argument("--end-year", required=True, type=_decade_year, help="saved decade checkpoint year to audit through")
    parser.add_argument("--output", type=Path, help="optional atomically written JSON report")
    parser.add_argument("--split-decades", action="store_true",
                        help="release the read transaction at each verified saved decade pin")
    parser.add_argument(
        "--institutions",
        action="store_true",
        help="also report active-organization gap windows and named member-leader metrics across commit boundaries",
    )
    parser.add_argument(
        "--progress-every-commits",
        type=_BASE._positive_int,
        default=10_000,
        metavar="N",
        help="write compact replay progress to stderr every N audited boundaries (default: 10000)",
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
            args.start_year,
            args.end_year,
            progress_every_commits=args.progress_every_commits,
            institutions=args.institutions,
            split_decades=args.split_decades,
        )
        if output_path is not None:
            report["report_file"] = str(output_path)
            _atomic_write_json(output_path, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except AuditError as exc:
        print(f"audit error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"audit failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
