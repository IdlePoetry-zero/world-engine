from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "audit-world-capacity.py"
_SPEC = importlib.util.spec_from_file_location("audit_world_capacity", SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


def _manifest_checkpoint(year: int = 20) -> dict[str, object]:
    return {
        "label": "decade",
        "scheduled_year": year,
        "tick": str(year * 360 * 86_400),
        "sequence": "800",
        "state_version": "300",
        "state_hash": "a" * 64,
    }


def test_manifest_checkpoint_is_exact_year_sequence_version_and_hash_pin():
    record = _manifest_checkpoint()
    manifest = {"checkpoints": [record]}

    pin = audit._manifest_checkpoint(
        manifest,
        20,
        start_tick=0,
        target_tick=25 * 360 * 86_400,
        day_seconds=86_400,
        year_days=360,
    )

    assert pin == {
        "year": 20,
        "tick": 20 * 360 * 86_400,
        "sequence": 800,
        "state_version": 300,
        "state_hash": "a" * 64,
    }
    record["tick"] = str(20 * 360 * 86_400 + 1)
    with pytest.raises(audit.AuditError, match="exact year boundary"):
        audit._manifest_checkpoint(
            manifest,
            20,
            start_tick=0,
            target_tick=25 * 360 * 86_400,
            day_seconds=86_400,
            year_days=360,
        )


def test_stored_checkpoint_witness_accepts_parent_boundary_while_child_head_advances():
    engine = create_engine("sqlite://")
    world_id, parent_id, child_id = uuid4(), uuid4(), uuid4()
    checkpoint = {
        "year": 20,
        "tick": 7_200,
        "sequence": 2,
        "state_version": 1,
        "state_hash": "b" * 64,
    }
    try:
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE world_branches (world_id TEXT, branch_id TEXT, parent_branch_id TEXT, "
                "fork_seq INTEGER, head_seq INTEGER)"
            ))
            connection.execute(text(
                "CREATE TABLE world_events (world_id TEXT, branch_id TEXT, sequence INTEGER, tick INTEGER, "
                "state_version_after INTEGER, commit_boundary BOOLEAN)"
            ))
            connection.execute(text(
                "CREATE TABLE world_snapshots (world_id TEXT, branch_id TEXT, last_seq INTEGER, current_tick INTEGER, "
                "state_version INTEGER, state_hash TEXT)"
            ))
            connection.execute(text(
                "INSERT INTO world_branches VALUES (:world, :parent, NULL, 0, 2)"
            ), {"world": world_id.hex, "parent": parent_id.hex})
            connection.execute(text(
                "INSERT INTO world_branches VALUES (:world, :child, :parent, 2, 5)"
            ), {"world": world_id.hex, "child": child_id.hex, "parent": parent_id.hex})
            connection.execute(text(
                "INSERT INTO world_events VALUES (:world, :parent, 2, :tick, :version, 1)"
            ), {"world": world_id.hex, "parent": parent_id.hex,
                "tick": checkpoint["tick"], "version": checkpoint["state_version"]})
            connection.execute(text(
                "INSERT INTO world_snapshots VALUES (:world, :parent, 2, :tick, :version, :digest)"
            ), {"world": world_id.hex, "parent": parent_id.hex,
                "tick": checkpoint["tick"], "version": checkpoint["state_version"],
                "digest": checkpoint["state_hash"]})

        assert audit._verify_stored_checkpoint_boundary(engine, world_id, child_id, checkpoint)

        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE world_snapshots SET state_hash = :digest WHERE world_id = :world AND last_seq = 2"
            ), {"world": world_id.hex, "digest": "c" * 64})
        assert not audit._verify_stored_checkpoint_boundary(engine, world_id, child_id, checkpoint)
    finally:
        engine.dispose()


def test_report_output_cannot_replace_audit_inputs_but_can_be_written_inside_directory(tmp_path):
    data_directory = tmp_path / "probe"
    data_directory.mkdir()
    protected_names = (
        audit.MANIFEST_NAME,
        audit.IDENTITY_NAME,
        audit.DATABASE_NAME,
        f"{audit.DATABASE_NAME}-wal",
        f"{audit.DATABASE_NAME}-shm",
    )
    for name in protected_names:
        protected = data_directory / name
        protected.write_text("protected", encoding="utf-8")
        with pytest.raises(audit.AuditError, match="cannot replace"):
            audit._safe_report_output_path(protected, data_directory)

    report_path = audit._safe_report_output_path(data_directory / "audit-report.json", data_directory)
    assert report_path == (data_directory / "audit-report.json").resolve()

    source = data_directory / audit.DATABASE_NAME
    hard_link = data_directory / "database-alias.bin"
    try:
        hard_link.hardlink_to(source)
    except (AttributeError, OSError):
        return
    with pytest.raises(audit.AuditError, match="cannot replace"):
        audit._safe_report_output_path(hard_link, data_directory)


def test_cli_accepts_optional_checkpoint_year():
    args = audit._build_parser().parse_args([
        "--data-directory", "saved-probe", "--checkpoint-year", "20",
        "--progress-every-commits", "2500",
    ])
    assert args.checkpoint_year == 20
    assert args.progress_every_commits == 2500
    assert audit._build_parser().parse_args(["--data-directory", "saved-probe"]).progress_every_commits == 10_000
    with pytest.raises(SystemExit):
        audit._build_parser().parse_args([
            "--data-directory", "saved-probe", "--progress-every-commits", "0",
        ])


def test_unrequested_target_before_paused_head_is_reported_as_verified_committed_prefix(tmp_path):
    from world_engine.application.simulation_jobs import SimulationJobs
    from world_engine.domain.models import (
        AccessScope, CommandEnvelope, JobStatus, NewWorld, TimeAdvancedEvent, WorldEntity, WorldState,
    )
    from world_engine.simulation.contracts import WorldAdvancePlan
    from world_engine.storage.database import Database
    from world_engine.storage.repository import WorldRepository

    data_directory = tmp_path / "paused-prefix"
    data_directory.mkdir()
    database_path = data_directory / audit.DATABASE_NAME
    database = Database(f"sqlite:///{database_path.as_posix()}")
    database.create_schema()
    repository = WorldRepository(database)
    scope = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    target_tick = 600 * 360 * 86_400
    prefix_tick = 20 * 360 * 86_400
    people = [uuid4() for _ in range(241)]
    state = WorldState(
        entities={
            ident: WorldEntity(id=ident, kind="character", name=f"Resident {index}")
            for index, ident in enumerate(people)
        },
        player_id=people[0],
        metadata={
            "content_index": {"key_npc_ids": [], "other_resident_ids": []},
            "seed_counts": {"key_npcs": 0, "other_named_living_characters": 0},
        },
    )
    created = repository.create_world(scope, NewWorld(title="Paused audit prefix", state=state))
    world_id, branch_id = created.world.world_id, created.branch.branch_id
    queued = SimulationJobs(repository).enqueue_world_advance(
        scope,
        world_id,
        branch_id,
        WorldAdvancePlan(started_tick=0, target_tick=target_tick),
        idempotency_key="paused-prefix-600-year-target",
        speed="balanced",
        depth="deep",
        expected_state_version=0,
    )
    lease = repository.claim_job("paused-prefix-worker", job_type="simulation.world_advance")
    assert lease is not None and lease.job_id == queued.job_id
    repository.commit_command(
        scope,
        world_id,
        branch_id,
        CommandEnvelope(
            actor_id=scope.user_id,
            idempotency_key="paused-prefix-window-20y",
            expected_state_version=0,
            command_type="simulation.window",
            payload={"job_id": str(queued.job_id), "next_tick": str(prefix_tick)},
        ),
        [TimeAdvancedEvent(to_tick=prefix_tick)],
        lease=lease,
    )
    repository.finish_job(
        lease,
        JobStatus.PAUSED,
        result={"reason_code": "test_pause"},
        committed_through_tick=prefix_tick,
    )
    paused_head = repository.read_world(scope, world_id, branch_id)
    paused_job = repository.read_job(scope, queued.job_id)
    partial_pin = {
        "label": "decade",
        "scheduled_year": 20,
        "tick": str(paused_head.state.current_tick),
        "sequence": str(paused_head.last_seq),
        "state_version": str(paused_head.branch.state_version),
        "state_hash": paused_head.state_hash,
        "partial": True,
    }

    def save_manifest(*, last_view, last_job, last_status):
        manifest = {
            "schema": audit.MANIFEST_SCHEMA,
            "database_file": audit.DATABASE_NAME,
            "world_created": True,
            "world_id": str(world_id),
            "branch_id": str(branch_id),
            "job_id": str(queued.job_id),
            "start_tick": "0",
            "target_tick": str(target_tick),
            "config": {"total_seconds": target_tick},
            "status": last_status,
            "last_job_status": last_job.status.value,
            "last_tick": str(last_view.state.current_tick),
            "last_state_version": str(last_view.branch.state_version),
            "last_state_hash": last_view.state_hash,
            "checkpoints": [partial_pin],
        }
        (data_directory / audit.MANIFEST_NAME).write_text(
            json.dumps(manifest), encoding="utf-8",
        )
        (data_directory / audit.IDENTITY_NAME).write_text(
            json.dumps({"tenant_id": str(scope.tenant_id), "user_id": str(scope.user_id)}),
            encoding="utf-8",
        )

    try:
        save_manifest(last_view=paused_head, last_job=paused_job, last_status="paused")
        explicit_checkpoint = audit._run_audit(data_directory, checkpoint_year=20)
        assert explicit_checkpoint["success"] is True
        assert explicit_checkpoint["scope"]["audit_mode"] == "checkpoint_prefix"
        assert explicit_checkpoint["scope"]["checkpoint_year"] == 20

        prefix = audit._run_audit(data_directory)
        assert prefix["success"] is False
        assert prefix["capacity_passed"] is False
        assert prefix["status"] == "committed_prefix_verified"
        assert prefix["replay_verified"] is True
        assert prefix["scope"]["audit_mode"] == "committed_prefix"
        assert prefix["scope"]["checkpoint_year"] is None
        assert prefix["scope"]["audited_through_sequence"] == str(paused_head.last_seq)
        assert prefix["scope"]["audited_through_tick"] == str(prefix_tick)
        assert prefix["scope"]["committed_prefix_boundary"] == {
            "sequence": str(paused_head.last_seq),
            "tick": str(prefix_tick),
            "state_version": str(paused_head.branch.state_version),
            "state_hash": paused_head.state_hash,
        }
        assert prefix["scope"]["requested_target_is_ahead_of_audited_boundary"] is True
        assert prefix["time"]["expected_target_tick"] == str(target_tick)
        assert prefix["time"]["audited_boundary_tick"] == str(prefix_tick)
        assert prefix["time"]["audited_boundary_kind"] == "committed_branch_head_prefix"
        assert prefix["time"]["audited_boundary_replayed_and_stored"] is True
        assert prefix["time"]["requested_target_reached_at_audited_boundary"] is False
        assert prefix["capacity"]["audit_interval_scope"] == "committed_prefix"
        assert prefix["capacity"]["audited_through_year"] == 20
        assert prefix["population_600_year_passed"] is False
        assert prefix["coverage"]["requested_boundary_replayed_from_manifest_pin"] is None
        assert prefix["coverage"]["audited_boundary_replayed_from_genesis"] is True
        assert prefix["coverage"]["audited_boundary_matches_stored_authority"] is True
        assert prefix["coverage"]["committed_states_audited"] == 2

        repository.control_job(scope, queued.job_id, "resume")
        terminal_lease = repository.claim_job("terminal-audit-worker", job_type="simulation.world_advance")
        assert terminal_lease is not None and terminal_lease.job_id == queued.job_id
        repository.commit_command(
            scope,
            world_id,
            branch_id,
            CommandEnvelope(
                actor_id=scope.user_id,
                idempotency_key="paused-prefix-window-600y",
                expected_state_version=paused_head.branch.state_version,
                command_type="simulation.window",
                payload={"job_id": str(queued.job_id), "next_tick": str(target_tick)},
            ),
            [TimeAdvancedEvent(to_tick=target_tick)],
            lease=terminal_lease,
        )
        repository.finish_job(
            terminal_lease,
            JobStatus.SUCCEEDED,
            committed_through_tick=target_tick,
        )
        terminal_head = repository.read_world(scope, world_id, branch_id)
        terminal_job = repository.read_job(scope, queued.job_id)
        save_manifest(last_view=terminal_head, last_job=terminal_job, last_status="succeeded")

        terminal = audit._run_audit(data_directory)
        assert terminal["success"] is True
        assert terminal["status"] == "replay_verified"
        assert terminal["scope"]["audit_mode"] == "full_target"
        assert terminal["time"]["expected_target_tick"] == str(target_tick)
        assert terminal["time"]["audited_boundary_tick"] == str(target_tick)
        assert terminal["time"]["requested_target_reached_at_audited_boundary"] is True
        assert terminal["population_600_year_passed"] is True

        saved_manifest_path = data_directory / audit.MANIFEST_NAME
        corrupt_manifest = json.loads(saved_manifest_path.read_text(encoding="utf-8"))
        corrupt_manifest["last_state_hash"] = "f" * 64
        saved_manifest_path.write_text(json.dumps(corrupt_manifest), encoding="utf-8")
        corrupt_terminal = audit._run_audit(data_directory)
        assert corrupt_terminal["success"] is False
        assert corrupt_terminal["status"] == "audit_mismatch"
        assert corrupt_terminal["replay_verified"] is True
        assert corrupt_terminal["coverage"]["durable_job_succeeded_at_exact_target"] is True
        assert corrupt_terminal["coverage"]["saved_manifest_final_receipt_matches_head"] is False
        assert corrupt_terminal["time"]["audited_boundary_tick"] == str(target_tick)
    finally:
        database.dispose()


def test_commit_progress_is_compact_stderr_json_and_never_repeats_a_count():
    stream = io.StringIO()
    clock_values = iter([12.0, 13.25, 20.0])
    progress = audit._CommitProgress(2, stream=stream, clock=lambda: next(clock_values))

    progress.report(1, 0, 0)
    progress.report(2, 1, 86_400)
    progress.report(2, 1, 86_400)
    progress.report(4, 3, 259_200)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert rows == [
        {
            "audited_commits": 2,
            "sequence": 1,
            "world_tick": "86400",
            "elapsed_seconds": 1.25,
        },
        {
            "audited_commits": 4,
            "sequence": 3,
            "world_tick": "259200",
            "elapsed_seconds": 8.0,
        },
    ]
    assert all(set(row) == {"audited_commits", "sequence", "world_tick", "elapsed_seconds"} for row in rows)


def test_sample_counts_separate_active_structure_from_member_and_leader_counts():
    player_id, key_id, member_id, inactive_id = (uuid4() for _ in range(4))
    country_id, empty_country_id, member_org_id, inactive_org_id, dead_org_id = (
        uuid4() for _ in range(5)
    )
    place_id, location_id, dead_place_id = (uuid4() for _ in range(3))

    def entity(
        entity_id, kind, name=None, *, status="active", members=None, leader_id=None,
    ):
        governance = {}
        if members is not None or leader_id is not None:
            governance = {"members": members or [], "leader_id": leader_id}
        components = {"governance": governance} if governance else {}
        return SimpleNamespace(
            id=entity_id, kind=kind, name=name, status=status, components=components,
        )

    entities = {
        item.id: item for item in (
            entity(player_id, "character", "Player"),
            entity(key_id, "character", "Key profile"),
            entity(member_id, "person", "Member"),
            entity(inactive_id, "person", "Former member", status="dead"),
            entity(country_id, "polity", "Country", members=[member_id, inactive_id], leader_id=member_id),
            entity(empty_country_id, "polity", "Unled country"),
            # The named leader is active but not a member, so it must not count
            # as an active member leader for this organization.
            entity(member_org_id, "organization", "Guild", members=[key_id], leader_id=member_id),
            entity(inactive_org_id, "organization", "Dormant society", members=[inactive_id],
                   leader_id=inactive_id),
            entity(dead_org_id, "organization", "Dissolved society", status="dead",
                   members=[member_id], leader_id=member_id),
            entity(place_id, "place", "Village"),
            entity(location_id, "location", "Region"),
            entity(dead_place_id, "place", "Ruins", status="destroyed"),
        )
    }
    state = SimpleNamespace(entities=entities, player_id=player_id)

    counts = audit._sample_counts(state, {key_id})

    assert counts == {
        "active_named_people_including_player": 3,
        "active_named_people_excluding_player": 2,
        "active_named_residents_excluding_player_and_authored_keys": 1,
        "active_authored_key_npcs": 1,
        "active_countries": 2,
        "active_other_organizations": 2,
        "active_places": 2,
        "active_countries_with_active_named_members": 1,
        "active_other_organizations_with_active_named_members": 1,
        "active_countries_with_active_member_leader": 1,
        "active_other_organizations_with_active_member_leader": 0,
    }


def test_institutional_continuity_tracks_recovery_same_tick_gaps_and_inactive_splits():
    first_org_id, second_org_id, first_member_id, second_member_id = (uuid4() for _ in range(4))
    tracker = audit._InstitutionalContinuityAudit()

    def observe(sequence, tick, *, first_member=False, second_status="active", second_member=False):
        entities = {}
        for org_id, name, status, has_member, member_id in (
            (first_org_id, "First society", "active", first_member, first_member_id),
            (second_org_id, "Second society", second_status, second_member, second_member_id),
        ):
            if status is None:
                continue
            member_ids = [member_id] if has_member else []
            entities[org_id] = SimpleNamespace(
                id=org_id,
                kind="organization",
                name=name,
                status=status,
                components={"governance": {"members": [str(value) for value in member_ids]}},
            )
            if has_member:
                entities[member_id] = SimpleNamespace(
                    id=member_id,
                    kind="character",
                    name=f"Member of {name}",
                    status="active",
                    components={},
                )
        state = SimpleNamespace(entities=entities, player_id=None, current_tick=tick)
        active_organizations = {}
        audit._sample_counts(
            state,
            set(),
            active_organization_member_status=active_organizations,
        )
        tracker.observe(sequence, tick, active_organizations)

    observe(0, 0)
    observe(1, 5, first_member=True, second_status="dead")
    observe(2, 5)
    observe(3, 5, first_member=True, second_member=True)
    observe(4, 12, second_status=None)
    observe(5, 20, second_status=None)

    report = tracker.report(pin_tick=20, pin_sequence=5)
    assert report["pin_matches_last_committed_sample"] is True
    rows = {row["organization_id"]: row for row in report["organizations"]}

    first = rows[str(first_org_id)]
    assert first["zero_member_gap_episode_count"] == 3
    assert first["zero_duration_same_tick_episode_count"] == 1
    assert first["total_positive_gap_seconds"] == 13
    assert first["open_at_pin"] is True
    assert first["longest_zero_member_gap"] == {
        "duration_seconds": 8,
        "start_tick": "12",
        "start_sequence": "4",
        "end_tick": "20",
        "end_sequence": "5",
        "open_at_pin": True,
    }

    second = rows[str(second_org_id)]
    assert second["zero_member_gap_episode_count"] == 2
    assert second["zero_duration_same_tick_episode_count"] == 1
    assert second["total_positive_gap_seconds"] == 5
    assert second["open_at_pin"] is False
    assert second["longest_zero_member_gap"] == {
        "duration_seconds": 5,
        "start_tick": "0",
        "start_sequence": "0",
        "end_tick": "5",
        "end_sequence": "1",
        "open_at_pin": False,
    }


def test_structural_threshold_checks_use_interval_minima_and_fail_when_missing():
    checks = audit._continuous_threshold_checks(
        {
            "active_countries": {"count": 24, "sequence": "0", "tick": "0"},
            "active_other_organizations": {"count": 23, "sequence": "2", "tick": "5"},
        },
        audit.STRUCTURAL_THRESHOLDS,
    )

    assert checks["active_countries"] == {
        "minimum_count": 24, "threshold": 24, "passed_continuously": True,
    }
    assert checks["active_other_organizations"] == {
        "minimum_count": 23, "threshold": 24, "passed_continuously": False,
    }
    assert checks["active_places"] == {
        "minimum_count": None, "threshold": 120, "passed_continuously": False,
    }
    assert not all(check["passed_continuously"] for check in checks.values())


def test_initial_key_profile_threshold_counts_unique_named_genesis_profiles_only():
    authored_key_ids = {uuid4() for _ in range(32)}

    passed = audit._initial_key_profile_threshold(
        authored_key_ids, 32, key_index_valid=True,
    )
    insufficient_profiles = audit._initial_key_profile_threshold(
        authored_key_ids, 31, key_index_valid=True,
    )

    assert passed == {
        "threshold": 32,
        "unique_authored_identity_count": 32,
        "active_named_profiles_at_genesis": 32,
        "passed_at_genesis": True,
        "requires_survival_after_genesis": False,
    }
    assert insufficient_profiles["passed_at_genesis"] is False
