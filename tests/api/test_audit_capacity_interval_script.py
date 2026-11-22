from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from world_engine.domain.models import (
    AccessScope,
    CalendarConfig,
    CommandEnvelope,
    EntityFieldChangedEvent,
    NewWorld,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "audit-capacity-interval.py"
_SPEC = importlib.util.spec_from_file_location("audit_capacity_interval", SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
audit = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = audit
_SPEC.loader.exec_module(audit)


def _commit(repository, scope, world_id, branch_id, actor_id, *events, key: str):
    branch = repository.read_world(scope, world_id, branch_id).branch
    return repository.commit_command(
        scope,
        world_id,
        branch_id,
        CommandEnvelope(
            actor_id=actor_id,
            idempotency_key=key,
            expected_state_version=branch.state_version,
            command_type="core.interval_audit_test",
        ),
        list(events),
    )


def test_audit_reads_every_commit_between_saved_decade_pins_without_writing_world_state(tmp_path):
    directory = tmp_path / "saved-probe"
    directory.mkdir()
    database_path = directory / audit.DATABASE_NAME
    database = Database(f"sqlite:///{database_path.as_posix()}")
    database.create_schema()
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    player_id = uuid4()
    key_ids = [uuid4() for _ in range(32)]
    ordinary_ids = [uuid4(), uuid4()]
    empty_org_id, residents_guild_id = uuid4(), uuid4()
    people = {
        player_id: WorldEntity(id=player_id, kind="character", name="Player"),
        **{
            identity: WorldEntity(id=identity, kind="person", name=f"Key profile {index}")
            for index, identity in enumerate(key_ids)
        },
        **{
            identity: WorldEntity(id=identity, kind="character", name=f"Resident {index}")
            for index, identity in enumerate(ordinary_ids)
        },
        empty_org_id: WorldEntity(
            id=empty_org_id,
            kind="organization",
            name="Empty Society",
            components={"governance": {"members": []}},
        ),
        residents_guild_id: WorldEntity(
            id=residents_guild_id,
            kind="organization",
            name="Residents Guild",
            components={"governance": {
                "members": [str(identity) for identity in ordinary_ids],
                "leader_id": str(ordinary_ids[0]),
            }},
        ),
    }
    state = WorldState(
        player_id=player_id,
        entities=people,
        calendar=CalendarConfig(day_seconds=1, year_days=3),
        metadata={"content_index": {"key_npc_ids": [str(identity) for identity in key_ids]}},
    )
    try:
        repository = WorldRepository(database)
        created = repository.create_world(scope, NewWorld(title="Capacity interval audit", state=state))
        world_id, branch_id = created.world.world_id, created.branch.branch_id

        _commit(repository, scope, world_id, branch_id, player_id, TimeAdvancedEvent(to_tick=30), key="advance-30")
        start_snapshot = repository.snapshot(scope, world_id, branch_id)
        _commit(
            repository, scope, world_id, branch_id, player_id,
            EntityFieldChangedEvent(entity_id=ordinary_ids[0], field_name="status", value="dead"),
            key="resident-one-dies",
        )
        _commit(repository, scope, world_id, branch_id, player_id, TimeAdvancedEvent(to_tick=45), key="advance-45")
        _commit(
            repository, scope, world_id, branch_id, player_id,
            EntityFieldChangedEvent(entity_id=ordinary_ids[1], field_name="status", value="dead"),
            key="resident-two-dies",
        )
        _commit(repository, scope, world_id, branch_id, player_id, TimeAdvancedEvent(to_tick=60), key="advance-60")
        end_snapshot = repository.snapshot(scope, world_id, branch_id)
        _commit(repository, scope, world_id, branch_id, player_id, TimeAdvancedEvent(to_tick=90), key="advance-90")
        last_snapshot = repository.snapshot(scope, world_id, branch_id)

        checkpoints = [
            {
                "label": "decade",
                "scheduled_year": year,
                "tick": str(snapshot.current_tick),
                "sequence": str(snapshot.last_seq),
                "state_version": str(snapshot.state_version),
                "state_hash": snapshot.state_hash,
            }
            for year, snapshot in ((10, start_snapshot), (20, end_snapshot), (30, last_snapshot))
        ]
        manifest = {
            "schema": 1,
            "database_file": audit.DATABASE_NAME,
            "world_created": True,
            "world_id": str(world_id),
            "branch_id": str(branch_id),
            "start_tick": "0",
            "target_tick": "90",
            "config": {"total_seconds": 90, "day_seconds": 1, "year_days": 3},
            "checkpoints": checkpoints,
        }
        manifest_path = directory / audit.MANIFEST_NAME
        identity_path = directory / audit.IDENTITY_NAME
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        identity_path.write_text(scope.model_dump_json(), encoding="utf-8")
        input_hashes = {path: path.read_bytes() for path in (manifest_path, identity_path)}
        with database.engine.connect() as connection:
            rows_before = {
                table: connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one()
                for table in ("world_branches", "world_events", "world_snapshots")
            }
    finally:
        database.dispose()

    report = audit._run_audit(directory, 10, 20, progress_every_commits=100)

    assert report["success"] is True
    assert report["scope"]["population_only"] is True
    assert report["scope"]["institutional_metrics_audited"] is False
    assert report["scope"]["genesis_prefix_replayed"] is False
    assert report["interval"]["expected_commit_boundary_count"] == 5
    assert report["interval"]["observed_commit_boundary_count"] == 5
    assert report["interval"]["commit_boundary_sequence_verified"] is True
    assert report["population"]["minimum_counts"] == {
        "active_named_including_player": {
            "count": 33,
            "sequence": str(end_snapshot.last_seq - 1),
            "tick": "45",
        },
        "active_player_named": {"count": 1, "sequence": str(start_snapshot.last_seq), "tick": "30"},
        "active_original_key_npcs": {"count": 32, "sequence": str(start_snapshot.last_seq), "tick": "30"},
        "ordinary_active_named_excluding_player_and_original_key_npcs": {
            "count": 0,
            "sequence": str(end_snapshot.last_seq - 1),
            "tick": "45",
        },
    }
    trough = report["population"]["ordinary_population_trough"]
    assert trough["exact_boundary"] == {
        "kind": "interior_committed_state",
        "scheduled_checkpoint_year": None,
        "tick": "45",
        "sequence": str(end_snapshot.last_seq - 1),
        "calendar_offset_from_probe_start": {
            "whole_years": 15,
            "remaining_days": 0,
            "remainder_seconds": "0",
            "exact_calendar_year_boundary": True,
        },
    }
    assert input_hashes == {path: path.read_bytes() for path in input_hashes}
    assert "institutions" not in report

    institution_report = audit._run_audit(
        directory, 10, 20, progress_every_commits=100, institutions=True,
    )
    assert institution_report["success"] is True
    continuity = institution_report["institutions"]
    assert institution_report["scope"]["population_only"] is False
    assert institution_report["scope"]["institutional_metrics_audited"] is True
    assert continuity["audited_from_sequence"] == str(start_snapshot.last_seq)
    assert continuity["audited_through_sequence"] == str(end_snapshot.last_seq)
    assert continuity["continuous_from_genesis"] is False
    assert continuity["censoring"]["left_censored_organization_ids"] == [str(empty_org_id)]
    assert set(continuity["censoring"]["right_censored_organization_ids"]) == {
        str(empty_org_id), str(residents_guild_id),
    }
    organizations = {row["organization_id"]: row for row in continuity["organizations"]}
    empty_society = organizations[str(empty_org_id)]
    assert empty_society["left_censored_at_interval_start"] is True
    assert empty_society["right_censored_at_interval_end"] is True
    assert empty_society["gap_windows"] == [{
        "organization_id": str(empty_org_id),
        "organization_name": "Empty Society",
        "start_sequence": str(start_snapshot.last_seq),
        "start_tick": "30",
        "start_observation": "already_without_active_named_members_at_interval_start",
        "left_censored": True,
        "right_censored": True,
        "observed_zero_member_committed_state_count": 5,
        "last_zero_member_sequence": str(end_snapshot.last_seq),
        "last_zero_member_tick": "60",
        "end_sequence": str(end_snapshot.last_seq),
        "end_tick": "60",
        "duration_seconds_observed": 30,
        "end_reason": "interval_end_still_without_active_named_members",
    }]
    guild_gap, = organizations[str(residents_guild_id)]["gap_windows"]
    assert guild_gap["start_sequence"] == str(end_snapshot.last_seq - 1)
    assert guild_gap["start_tick"] == "45"
    assert guild_gap["end_sequence"] == str(end_snapshot.last_seq)
    assert guild_gap["end_tick"] == "60"
    assert guild_gap["left_censored"] is False
    assert guild_gap["right_censored"] is True
    named_leaders = continuity["active_named_member_leaders"]["by_institution_kind"]
    assert named_leaders["active_other_organizations_with_active_member_leader"]["at_interval_start"] == {
        "count": 1, "sequence": str(start_snapshot.last_seq), "tick": "30",
    }
    assert named_leaders["active_other_organizations_with_active_member_leader"]["minimum"] == {
        "count": 0, "sequence": str(start_snapshot.last_seq + 1), "tick": "30",
    }
    assert named_leaders["active_countries_with_active_member_leader"]["minimum"]["count"] == 0
    structural = continuity["structural_capacity"]
    assert structural["thresholds"] == {
        "active_countries": 24,
        "active_other_organizations": 24,
        "active_places": 120,
    }
    assert structural["minimum_counts"] == {
        "active_countries": {"count": 0, "sequence": str(start_snapshot.last_seq), "tick": "30"},
        "active_other_organizations": {
            "count": 2, "sequence": str(start_snapshot.last_seq), "tick": "30",
        },
        "active_places": {"count": 0, "sequence": str(start_snapshot.last_seq), "tick": "30"},
    }
    assert structural["continuous_checks_over_replayed_interval"] == {
        "active_countries": {"minimum_count": 0, "threshold": 24, "passed_continuously": False},
        "active_other_organizations": {"minimum_count": 2, "threshold": 24, "passed_continuously": False},
        "active_places": {"minimum_count": 0, "threshold": 120, "passed_continuously": False},
    }
    assert structural["gate_passed"] is False
    assert structural["first_deficient_sequences"] == {
        "active_countries": {"count": 0, "sequence": str(start_snapshot.last_seq), "tick": "30"},
        "active_other_organizations": {
            "count": 2, "sequence": str(start_snapshot.last_seq), "tick": "30",
        },
        "active_places": {"count": 0, "sequence": str(start_snapshot.last_seq), "tick": "30"},
    }
    assert structural["observed_commit_boundary_count"] == 5

    single = audit._run_audit(directory, 10, 30, institutions=True)
    split = audit._run_audit(directory, 10, 30, institutions=True, split_decades=True)
    assert split["interval"]["replay_transaction_count"] == 2
    assert split["interval"]["observed_commit_boundary_count"] == 6
    for field in ("scope", "population", "institutions"):
        assert split[field] == single[field]
    assert split["interval"] == single["interval"] | {"replay_transaction_count": 2}

    readonly_engine = audit._readonly_engine(directory / audit.DATABASE_NAME)
    try:
        with readonly_engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
            with pytest.raises(OperationalError):
                connection.execute(
                    text("UPDATE world_branches SET head_seq = head_seq WHERE branch_id = :branch_id"),
                    {"branch_id": branch_id.hex},
                )
    finally:
        readonly_engine.dispose()

    reopened = Database(f"sqlite:///{(directory / audit.DATABASE_NAME).as_posix()}")
    try:
        with reopened.engine.connect() as connection:
            rows_after = {
                table: connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one()
                for table in ("world_branches", "world_events", "world_snapshots")
            }
        assert rows_after == rows_before
    finally:
        reopened.dispose()

    manifest["checkpoints"][1]["state_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(audit.AuditError, match="persisted boundary pin"):
        audit._run_audit(directory, 10, 20, progress_every_commits=100)
    with pytest.raises(audit.AuditError, match="year 20 checkpoint"):
        audit._run_audit(directory, 10, 30, split_decades=True)


def test_interval_cli_requires_decade_years_and_ordered_pins():
    args = audit._build_parser().parse_args([
        "--data-directory", "saved-probe", "--start-year", "10", "--end-year", "20",
        "--progress-every-commits", "250",
    ])
    assert (args.start_year, args.end_year, args.progress_every_commits, args.institutions) == (10, 20, 250, False)
    assert audit._build_parser().parse_args([
        "--data-directory", "saved-probe", "--start-year", "10", "--end-year", "20", "--institutions",
    ]).institutions is True
    with pytest.raises(SystemExit):
        audit._build_parser().parse_args([
            "--data-directory", "saved-probe", "--start-year", "11", "--end-year", "20",
        ])

    record = {
        "label": "decade",
        "scheduled_year": 10,
        "tick": "300",
        "sequence": "4",
        "state_version": "4",
        "state_hash": "a" * 64,
    }
    with pytest.raises(audit.AuditError, match="multiples of 10"):
        audit._manifest_checkpoint(
            {"checkpoints": [record]},
            11,
            start_tick=0,
            target_tick=600,
            day_seconds=1,
            year_days=30,
        )


def test_population_shortfalls_keep_same_tick_deficits_and_interval_censoring():
    report = audit._ordinary_population_shortfalls([
        (1, 10, 239), (2, 11, 240), (3, 11, 238), (4, 11, 241),
        (5, 14, 237), (6, 20, 238),
    ])
    assert report["threshold"] == 240 and report["deficient_commit_count"] == 4
    first, instant, last = report["episodes"]
    assert first["left_censored"] and first["duration_seconds_observed"] == 1
    assert instant["duration_seconds_observed"] == 0 and instant["minimum_count"] == 238
    assert instant["start_sequence"] == "3" and instant["end_sequence"] == "4"
    assert last["right_censored"] and last["duration_seconds_observed"] == 6
    assert last["deficient_commit_count"] == 2
