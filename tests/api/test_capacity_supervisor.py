from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "supervise-capacity-runs.py"
SPEC = importlib.util.spec_from_file_location("capacity_supervisor", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
capacity_supervisor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capacity_supervisor
SPEC.loader.exec_module(capacity_supervisor)


def test_recovery_matches_durable_decision_by_trace_parent(tmp_path):
    decision_id = str(uuid4())
    snapshot = _snapshot(tmp_path, status="running", error_code=None, result_available=True)
    call = replace(snapshot.latest_model_call, decision_request_id=decision_id)
    snapshot = replace(snapshot, latest_model_call=call, job_payload={"last_decision": {"request_id": decision_id}})
    now = datetime.now(timezone.utc)
    policy = capacity_supervisor.RecoveryPolicy()

    assert capacity_supervisor.assess_recovery(snapshot, [], now, policy).action == "spawn"
    unmatched = replace(snapshot, latest_model_call=replace(call, decision_request_id=str(uuid4())))
    assert capacity_supervisor.assess_recovery(unmatched, [], now, policy).reason_code == "successful_model_result_not_durable"


def test_legacy_job_trace_is_cleared_only_by_later_distinct_job_time_commits():
    world, branch, job = uuid4(), uuid4(), uuid4()
    call_end = datetime(2026, 11, 19, 0, 1, tzinfo=timezone.utc)
    db = sqlite3.connect(":memory:")
    try:
        db.executescript("""
            CREATE TABLE world_events (world_id, branch_id, state_version_after, event_type, tick, committed_at, command_id);
            CREATE TABLE world_commands (world_id, branch_id, command_type, payload_json, command_id);
        """)
        def add(key, tick, version, at, command_job=job):
            db.execute("INSERT INTO world_commands VALUES (?, ?, 'simulation.window', ?, ?)",
                       (world.hex, branch.hex, json.dumps({"job_id": str(command_job)}), key))
            db.execute("INSERT INTO world_events VALUES (?, ?, ?, 'core.time_advanced.v1', ?, ?, ?)",
                       (world.hex, branch.hex, version, tick, at, key))

        add("earlier", 100, 1, "2026-11-19 00:00:30")
        add("first", 200, 2, "2026-11-19 00:01:30")
        add("same-tick", 200, 3, "2026-11-19 00:01:40")
        add("other-job", 300, 4, "2026-11-19 00:02:00", uuid4())
        add("second", 400, 5, "2026-11-19 00:02:30")
        assert not capacity_supervisor._has_progress_after_legacy_call(db, world, branch, job, call_end, 4)
        assert capacity_supervisor._has_progress_after_legacy_call(db, world, branch, job, call_end, 5)
    finally:
        db.close()


def _snapshot(
    directory: Path,
    *,
    status: str = "paused",
    pause_code: str | None = "model_decision_failed",
    error_code: str | None = "network",
    billing: str = "unbilled",
    result_available: bool = False,
    retry_after: float | None = None,
    failure_at: datetime | None = None,
    decision_mode: str = "configured_local_model",
    current_tick: int = 100,
    target_tick: int = 1000,
):
    directory.mkdir(parents=True, exist_ok=True)
    world_id, branch_id, job_id = uuid4(), uuid4(), uuid4()
    manifest_path = directory / capacity_supervisor.MANIFEST_NAME
    manifest_bytes = json.dumps({"schema": 1, "world_id": str(world_id)}).encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)
    config = {
        "pack_id": "modern_city",
        "pack_version": "2.6.0",
        "seed": 42,
        "declared_years": 600,
        "declared_days": 0,
        "total_seconds": 18_662_400_000,
        "decision_mode": decision_mode,
        "population_mode": "sustained",
        "catalog_seeded": True,
    }
    call = None
    if error_code is not None or result_available:
        call = capacity_supervisor.ModelCallSnapshot(
            request_id="decision-request-001",
            error_code=error_code,
            retry_after_seconds=retry_after,
            billing_state=billing,
            result_available=result_available,
            failure_at=failure_at or datetime.now(timezone.utc) - timedelta(seconds=7200),
        )
    return capacity_supervisor.RunSnapshot(
        directory=directory.resolve(),
        manifest_path=manifest_path,
        manifest_bytes=manifest_bytes,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        world_id=world_id,
        branch_id=branch_id,
        job_id=job_id,
        job_status=status,
        job_payload={"plan": {"started_tick": 0, "target_tick": target_tick}},
        job_pause_code=pause_code,
        current_tick=current_tick,
        target_tick=target_tick,
        state_version=21,
        ruleset_version="2.6.0",
        pack_version="2.6.0",
        config=config,
        decision_mode=decision_mode,
        alembic_revision="6fbc6e5d1a70",
        latest_model_call=call,
    )


class FakeClock:
    def __init__(self, now: datetime):
        self.value = now
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class FakeInventory:
    def __init__(self, processes=(), error: Exception | None = None):
        self.processes = tuple(processes)
        self.error = error
        self.calls = 0

    def list_probe_processes(self, probe_script: Path):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.processes


class FakeInspector:
    def __init__(self, snapshot=None, error: Exception | None = None):
        self.snapshot = snapshot
        self.error = error
        self.calls = 0

    def inspect(self, directory: Path, model_data_directory: Path):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.snapshot


class FakeSpawner:
    def __init__(self, pid: int = 43210):
        self.pid = pid
        self.requests = []

    def spawn(self, request):
        self.requests.append(request)
        return self.pid


def _dependencies(tmp_path: Path, snapshot, *, processes=(), inventory_error=None):
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    probe_script = project_root / "scripts" / "probe-world-advance.py"
    probe_script.parent.mkdir(parents=True, exist_ok=True)
    probe_script.write_text("# fake script for argv matching\n", encoding="utf-8")
    return SimpleNamespace(
        clock=FakeClock(datetime.now(timezone.utc)),
        inventory=FakeInventory(processes, inventory_error),
        inspector=FakeInspector(snapshot),
        spawner=FakeSpawner(),
        project_root=project_root,
        probe_script=probe_script,
    )


def _config(tmp_path: Path, directory: Path, **kwargs):
    frozen = tmp_path / "frozen-backend"
    (frozen / "world_engine" / "simulation").mkdir(parents=True, exist_ok=True)
    (frozen / "world_engine" / "storage").mkdir(parents=True, exist_ok=True)
    for relative in (
        "world_engine/__init__.py",
        "world_engine/simulation/engine.py",
        "world_engine/storage/repository.py",
    ):
        (frozen / relative).write_text("# frozen test module\n", encoding="utf-8")
    return capacity_supervisor.SupervisorConfig(
        data_directories=(directory,),
        backend_path=frozen,
        model_data_directory=tmp_path / "model-data",
        project_root=tmp_path / "project",
        **kwargs,
    )


def _process_for(directory: Path, script_path: Path, *, pid: int = 1234):
    return capacity_supervisor.ObservedProcess(
        pid,
        ("python.exe", str(script_path), "--data-directory", str(directory.resolve())),
    )


def test_active_probe_process_prevents_duplicate_launch_even_if_job_pause_looks_recoverable(tmp_path: Path):
    directory = tmp_path / "run"
    snapshot = _snapshot(directory)
    dependencies = _dependencies(tmp_path, snapshot)
    dependencies.inventory.processes = (_process_for(directory, dependencies.probe_script),)
    config = _config(tmp_path, directory)

    result = capacity_supervisor.supervise_once(directory, config, dependencies)

    assert result.recovery_action == "observe"
    assert result.recovery_reason == "probe_process_active"
    assert result.pids == (1234,)
    assert dependencies.spawner.requests == []


def test_process_inventory_failure_fails_closed_without_starting_probe(tmp_path: Path):
    directory = tmp_path / "run"
    snapshot = _snapshot(directory)
    dependencies = _dependencies(
        tmp_path, snapshot,
        inventory_error=capacity_supervisor.SupervisorSafetyError("process_inventory_failed"),
    )
    config = _config(tmp_path, directory)

    result = capacity_supervisor.supervise_once(directory, config, dependencies)

    assert result.recovery_action == "refuse"
    assert result.recovery_reason == "process_inventory_failed"
    assert dependencies.spawner.requests == []


def test_unknown_billing_needs_explicit_retry_opt_in(tmp_path: Path):
    now = datetime.now(timezone.utc)
    snapshot = _snapshot(tmp_path / "run", billing="unknown", failure_at=now - timedelta(hours=2))

    withheld = capacity_supervisor.assess_recovery(
        snapshot, (), now, capacity_supervisor.RecoveryPolicy(),
    )
    authorized = capacity_supervisor.assess_recovery(
        snapshot, (), now,
        capacity_supervisor.RecoveryPolicy(allow_possibly_billed_retry=True),
    )

    assert withheld.action == "wait"
    assert withheld.reason_code == "possibly_billed_retry_requires_opt_in"
    assert authorized.action == "spawn"
    assert authorized.billing_state == "unknown"
    assert capacity_supervisor.RecoveryPolicy().allow_possibly_billed_retry is False


@pytest.mark.parametrize("error_code", ["invalid_output", "truncated", "client", "authentication", None])
def test_semantic_or_nontransient_call_results_are_never_retried(tmp_path: Path, error_code: str | None):
    now = datetime.now(timezone.utc)
    snapshot = _snapshot(
        tmp_path / "run", error_code=error_code,
        result_available=error_code is None,
        failure_at=now - timedelta(hours=2),
    )

    decision = capacity_supervisor.assess_recovery(
        snapshot, (), now,
        capacity_supervisor.RecoveryPolicy(allow_possibly_billed_retry=True),
    )

    assert decision.action == "report"
    assert decision.reason_code in {"model_failure_not_transient", "successful_model_result_not_durable"}


@pytest.mark.parametrize(
    ("status", "pause_code", "expected"),
    [
        ("paused", "waiting_input", "paused_for_nonmodel_reason"),
        ("waiting_input", None, "job_waiting_input"),
        ("succeeded", None, "job_completed"),
        ("failed", None, "job_failed"),
    ],
)
def test_nonretryable_job_lifecycle_statuses_are_report_only(
    tmp_path: Path, status: str, pause_code: str | None, expected: str,
):
    snapshot = _snapshot(tmp_path / "run", status=status, pause_code=pause_code, current_tick=1000)

    decision = capacity_supervisor.assess_recovery(
        snapshot, (), datetime.now(timezone.utc), capacity_supervisor.RecoveryPolicy(),
    )

    assert decision.action == "report"
    assert decision.reason_code == expected


def test_backoff_and_retry_after_are_both_enforced_for_the_same_failure_trace(tmp_path: Path):
    now = datetime.now(timezone.utc)
    failed_at = now - timedelta(seconds=20)
    snapshot = _snapshot(
        tmp_path / "run", retry_after=200, failure_at=failed_at,
    )
    history = [{
        "event": "attempt_begin",
        "job_id": str(snapshot.job_id),
        "failure_key": snapshot.latest_model_call.request_id,
        "at_utc": (now - timedelta(seconds=100)).isoformat(),
    }]

    decision = capacity_supervisor.assess_recovery(
        snapshot, history, now, capacity_supervisor.RecoveryPolicy(),
    )

    assert decision.action == "wait"
    assert decision.reason_code == "retry_backoff"
    assert decision.attempt_index == 2
    assert decision.retry_after_seconds == 200
    assert decision.wait_seconds == pytest.approx(180, abs=1)


def test_first_retry_waits_from_the_persisted_failure_time(tmp_path: Path):
    now = datetime.now(timezone.utc)
    snapshot = _snapshot(tmp_path / "run", failure_at=now - timedelta(seconds=15))

    decision = capacity_supervisor.assess_recovery(
        snapshot, (), now, capacity_supervisor.RecoveryPolicy(),
    )

    assert decision.action == "wait"
    assert decision.attempt_index == 1
    assert decision.wait_seconds == pytest.approx(45, abs=1)


def test_running_job_retries_are_also_blocked_when_model_billing_is_ambiguous(tmp_path: Path):
    now = datetime.now(timezone.utc)
    snapshot = _snapshot(
        tmp_path / "run", status="running", pause_code=None, billing="unknown",
        failure_at=now - timedelta(hours=2),
    )
    snapshot = replace(snapshot, job_payload={"plan": {}, "pending_decision": None, "last_decision": None})

    decision = capacity_supervisor.assess_recovery(
        snapshot, (), now, capacity_supervisor.RecoveryPolicy(),
    )

    assert decision.action == "wait"
    assert decision.reason_code == "possibly_billed_retry_requires_opt_in"


def test_paused_job_requires_a_matching_transient_operations_error(tmp_path: Path):
    snapshot = replace(_snapshot(tmp_path / "run", error_code="network"), latest_model_call=None)

    decision = capacity_supervisor.assess_recovery(
        snapshot, (), datetime.now(timezone.utc), capacity_supervisor.RecoveryPolicy(),
    )

    assert decision.action == "report"
    assert decision.reason_code == "matching_model_failure_missing"


def test_cancelled_trace_is_retryable_only_with_the_matching_local_deadline_receipt(tmp_path: Path):
    snapshot = _snapshot(tmp_path / "run", error_code="cancelled", billing="unknown")
    request_id = str(uuid4())
    receipt = {"request_id": request_id, "tick": snapshot.current_tick, "state_version": snapshot.state_version}
    call = replace(snapshot.latest_model_call, decision_request_id=request_id)
    confirmed = replace(snapshot, latest_model_call=call, model_host_deadline=receipt)
    now = datetime.now(timezone.utc)
    policy = capacity_supervisor.RecoveryPolicy()
    opted_in = replace(policy, allow_possibly_billed_retry=True)

    waiting = capacity_supervisor.assess_recovery(confirmed, (), now, policy)
    assert waiting.action == "wait" and waiting.reason_code == "possibly_billed_retry_requires_opt_in"
    retry = capacity_supervisor.assess_recovery(confirmed, (), now, opted_in)
    assert retry.action == "spawn" and retry.error_code == "host_deadline" and retry.failure_key == request_id

    for mismatch in (
        replace(confirmed, model_host_deadline=None),
        replace(confirmed, model_host_deadline=receipt | {"request_id": str(uuid4())}),
        replace(confirmed, model_host_deadline=receipt | {"tick": snapshot.current_tick - 1}),
        replace(confirmed, model_host_deadline=receipt | {"state_version": snapshot.state_version - 1}),
        replace(confirmed, job_payload={"pending_decision": {"request_id": request_id}}),
        replace(confirmed, job_payload={"last_decision": {"request_id": request_id}}),
        replace(confirmed, latest_call_boundary_cleared=True),
        replace(confirmed, latest_model_call=replace(call, result_available=True)),
    ):
        decision = capacity_supervisor.assess_recovery(mismatch, (), now, opted_in)
        assert decision.action == "report" and decision.error_code == "cancelled"


def test_process_matching_uses_the_resolved_probe_and_data_directory_argv(tmp_path: Path):
    directory = tmp_path / "a path with spaces" / "run"
    directory.mkdir(parents=True)
    script = tmp_path / "scripts" / "probe-world-advance.py"
    script.parent.mkdir()
    script.write_text("", encoding="utf-8")
    matching = _process_for(directory, script)
    different = _process_for(tmp_path / "other", script, pid=1235)

    assert capacity_supervisor._process_matches_data_directory(matching, directory, script)
    assert not capacity_supervisor._process_matches_data_directory(different, directory, script)


def test_supervisor_resumes_same_paused_job_with_exact_manifest_configuration_and_records_bootstrap(tmp_path: Path):
    directory = tmp_path / "run"
    snapshot = _snapshot(directory, failure_at=datetime.now(timezone.utc) - timedelta(hours=2))
    dependencies = _dependencies(tmp_path, snapshot)
    config = _config(tmp_path, directory, backup_compressed_mib=2048, backup_expanded_mib=8192)

    result = capacity_supervisor.supervise_once(directory, config, dependencies)

    assert result.recovery_action == "spawn"
    assert result.job_id == str(snapshot.job_id)
    assert result.current_tick == snapshot.current_tick
    assert len(dependencies.spawner.requests) == 1
    request = dependencies.spawner.requests[0]
    args = list(request.argv)
    assert args[0] == sys.executable
    assert args[1] == "-c"
    assert args[3] == str(dependencies.probe_script.resolve())
    assert args[args.index("--pack") + 1] == snapshot.config["pack_id"]
    assert args[args.index("--pack-version") + 1] == snapshot.pack_version
    assert args[args.index("--seed") + 1] == str(snapshot.config["seed"])
    assert args[args.index("--years") + 1] == str(snapshot.config["declared_years"])
    assert args[args.index("--days") + 1] == str(snapshot.config["declared_days"])
    assert args[args.index("--data-directory") + 1] == str(directory.resolve())
    assert args[args.index("--model-data-directory") + 1] == str(config.model_data_directory.resolve())
    assert args[args.index("--backup-compressed-mib") + 1] == "2048"
    assert args[args.index("--backup-expanded-mib") + 1] == "8192"
    assert "--resume-model-failure" in args
    assert request.resume_model_failure is True
    assert request.env["PYTHONPATH"] == str(config.backend_path.resolve())
    assert request.env["WORLD_ENGINE_FROZEN_BACKEND"] == str(config.backend_path.resolve())
    assert request.bootstrap_evidence_path.name.endswith("-bootstrap.json")
    assert request.bootstrap_evidence_path.parent.exists()
    assert not request.bootstrap_evidence_path.exists()  # only the real verified bootstrap writes this file

    evidence_dir = dependencies.project_root / ".runtime" / "capacity-supervisor" / "evidence"
    manifest_copies = list(evidence_dir.rglob("*-manifest.json"))
    assert len(manifest_copies) == 1
    assert manifest_copies[0].read_bytes() == snapshot.manifest_bytes
    audit_file = next((dependencies.project_root / ".runtime" / "capacity-supervisor" / "logs").glob("*.jsonl"))
    entries = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
    begin = next(item for item in entries if item["event"] == "attempt_begin")
    assert begin["job_id"] == str(snapshot.job_id)
    assert begin["possibly_billed_retry_authorized"] is False
    assert "prompt" not in json.dumps(begin).casefold()


def test_missing_manifest_is_read_only_and_does_not_initialize_a_run(tmp_path: Path):
    directory = tmp_path / "empty-existing-directory"
    directory.mkdir()
    before = set(directory.iterdir())
    inspector = capacity_supervisor.SQLiteReadOnlyInspector()

    with pytest.raises(capacity_supervisor.SupervisorSafetyError) as exc:
        inspector.inspect(directory, tmp_path / "model-data")

    assert exc.value.code == "manifest_missing"
    assert set(directory.iterdir()) == before
    assert not (directory / capacity_supervisor.DATABASE_NAME).exists()


def test_unknown_checkout_migration_version_is_refused_without_database_modification(tmp_path: Path):
    directory = tmp_path / "run"
    directory.mkdir()
    world_id, branch_id, job_id = uuid4(), uuid4(), uuid4()
    config = {
        "pack_id": "modern_city", "pack_version": "2.6.0", "seed": 1,
        "declared_years": 600, "declared_days": 0, "total_seconds": 18_662_400_000,
        "decision_mode": "deterministic_no_model", "population_mode": "sustained", "catalog_seeded": True,
    }
    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    config_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    manifest = {
        "schema": 1, "config": config, "config_hash": config_hash,
        "idempotency_key": f"capacity-probe:{config_hash}", "world_id": str(world_id),
        "branch_id": str(branch_id), "job_id": str(job_id), "world_created": True,
        "start_tick": "0", "target_tick": str(config["total_seconds"]),
    }
    (directory / capacity_supervisor.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    database = directory / capacity_supervisor.DATABASE_NAME
    import sqlite3
    connection = sqlite3.connect(database)
    connection.executescript(
        "CREATE TABLE worlds(world_id TEXT,active_branch_id TEXT,pack_version TEXT,ruleset_version TEXT);"
        "CREATE TABLE world_branches(branch_id TEXT,world_id TEXT,current_tick INTEGER,state_version INTEGER);"
        "CREATE TABLE world_jobs(job_id TEXT,world_id TEXT,branch_id TEXT,job_type TEXT,idempotency_key TEXT,status TEXT,payload_json TEXT,result_json TEXT,committed_through_tick INTEGER,worker_id TEXT,lease_expires_at TEXT,fencing_token INTEGER);"
        "CREATE TABLE branch_writer_leases(branch_id TEXT,job_id TEXT,worker_id TEXT,fencing_token INTEGER,lease_expires_at TEXT);"
        "CREATE TABLE alembic_version(version_num TEXT);"
        "INSERT INTO alembic_version VALUES('unknown-migration');"
    )
    connection.commit()
    connection.close()
    before_bytes = database.read_bytes()

    with pytest.raises(capacity_supervisor.SupervisorSafetyError) as exc:
        capacity_supervisor.SQLiteReadOnlyInspector().inspect(directory, tmp_path / "model-data")

    assert exc.value.code == "database_migration_version_unsupported"
    assert database.read_bytes() == before_bytes


def test_inspect_once_is_read_only_and_never_calls_spawner(tmp_path: Path):
    directory = tmp_path / "run"
    snapshot = _snapshot(directory, failure_at=datetime.now(timezone.utc) - timedelta(hours=2))
    dependencies = _dependencies(tmp_path, snapshot)
    config = _config(tmp_path, directory, inspect_only=True, once=True)

    result = capacity_supervisor.supervise_once(directory, config, dependencies)

    assert result.recovery_action == "report"
    assert result.recovery_reason == "inspection_only_would_spawn"
    assert dependencies.spawner.requests == []
    assert not (dependencies.project_root / ".runtime").exists()


def test_backup_limits_have_safe_cli_defaults_and_reject_out_of_range_values(tmp_path: Path):
    config = capacity_supervisor._load_cli_config([
        "--data-directory", str(tmp_path / "existing-run"),
        "--backend-path", str(tmp_path / "frozen"),
        "--model-data-directory", str(tmp_path / "model-data"),
        "--inspect",
    ])
    assert config.backup_compressed_mib == 512
    assert config.backup_expanded_mib == 4096
    assert config.allow_possibly_billed_retry is False

    with pytest.raises(ValueError, match="backup_compressed_mib"):
        capacity_supervisor.SupervisorConfig(
            data_directories=(tmp_path / "run",), backend_path=tmp_path / "frozen",
            model_data_directory=tmp_path / "model-data", backup_compressed_mib=0,
        )
    with pytest.raises(ValueError, match="backup_expanded_mib"):
        capacity_supervisor.SupervisorConfig(
            data_directories=(tmp_path / "run",), backend_path=tmp_path / "frozen",
            model_data_directory=tmp_path / "model-data", backup_expanded_mib=262_145,
        )


def _write_inspector_fixture(
    directory: Path,
    *,
    continued: bool,
    plan_started_tick: int | None = None,
    manifest_job_start_tick: int | None = None,
    predecessor_target_tick: int = 1_100,
    tamper_predecessor_pin: bool = False,
) -> tuple[Path, int, int]:
    directory.mkdir(parents=True)
    world_id, branch_id, job_id = uuid4(), uuid4(), uuid4()
    genesis_tick = 100
    current_tick = 1_200
    state_version = 2
    target_tick = 2_100
    job_start_tick = predecessor_target_tick if continued else genesis_tick
    config = {
        "pack_id": "modern_city",
        "pack_version": "2.7.0",
        "seed": 42,
        "declared_years": 0,
        "declared_days": 20,
        "total_seconds": 2_000,
        "decision_mode": "deterministic_no_model",
        "population_mode": "sustained",
        "catalog_seeded": True,
    }
    config_hash = hashlib.sha256(
        json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema": 1,
        "config": config,
        "config_hash": config_hash,
        "idempotency_key": f"capacity-probe:{config_hash}",
        "world_id": str(world_id),
        "branch_id": str(branch_id),
        "job_id": str(job_id),
        "world_created": True,
        "database_file": capacity_supervisor.DATABASE_NAME,
        "start_tick": str(genesis_tick),
        "target_tick": str(target_tick),
    }
    if continued:
        old_job_id = uuid4()
        old_config = dict(config, total_seconds=predecessor_target_tick - genesis_tick)
        old_config_hash = hashlib.sha256(
            json.dumps(old_config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        archive_name = f".probe-world-advance.completed.{old_config_hash}.json"
        original_pin = {
            "label": "target",
            "scheduled_year": None,
            "tick": str(predecessor_target_tick),
            "sequence": "1",
            "state_version": "1",
            "state_hash": "a" * 64,
        }
        archived = {
            "schema": 1,
            "config": old_config,
            "config_hash": old_config_hash,
            "idempotency_key": f"capacity-probe:{old_config_hash}",
            "world_id": str(world_id),
            "branch_id": str(branch_id),
            "job_id": str(old_job_id),
            "world_created": True,
            "start_tick": str(genesis_tick),
            "job_start_tick": str(genesis_tick),
            "target_tick": str(predecessor_target_tick),
            "status": "succeeded",
            "last_job_status": "succeeded",
            "checkpoints": [original_pin],
        }
        (directory / archive_name).write_text(json.dumps(archived), encoding="utf-8")
        predecessor_pin = dict(original_pin)
        if tamper_predecessor_pin:
            predecessor_pin["state_hash"] = "b" * 64
        manifest.update({
            "job_start_tick": str(
                predecessor_target_tick if manifest_job_start_tick is None else manifest_job_start_tick
            ),
            "predecessor": {
                "manifest_archive": archive_name,
                "config_hash": old_config_hash,
                "job_id": str(old_job_id),
                "target_tick": str(predecessor_target_tick),
                "terminal_checkpoint": predecessor_pin,
            },
        })
    (directory / capacity_supervisor.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

    database = directory / capacity_supervisor.DATABASE_NAME
    connection = sqlite3.connect(database)
    connection.executescript(
        "CREATE TABLE worlds(world_id TEXT,active_branch_id TEXT,pack_version TEXT,ruleset_version TEXT);"
        "CREATE TABLE world_branches(branch_id TEXT,world_id TEXT,current_tick INTEGER,state_version INTEGER);"
        "CREATE TABLE world_jobs(job_id TEXT,world_id TEXT,branch_id TEXT,job_type TEXT,idempotency_key TEXT,status TEXT,payload_json TEXT,result_json TEXT,committed_through_tick INTEGER,worker_id TEXT,lease_expires_at TEXT,fencing_token INTEGER);"
        "CREATE TABLE branch_writer_leases(branch_id TEXT,job_id TEXT,worker_id TEXT,fencing_token INTEGER,lease_expires_at TEXT);"
        "CREATE TABLE alembic_version(version_num TEXT);"
        "INSERT INTO alembic_version VALUES('6fbc6e5d1a70');"
    )
    connection.execute(
        "INSERT INTO worlds VALUES(?,?,?,?)",
        (world_id.hex, branch_id.hex, config["pack_version"], config["pack_version"]),
    )
    connection.execute(
        "INSERT INTO world_branches VALUES(?,?,?,?)",
        (branch_id.hex, world_id.hex, current_tick, state_version),
    )
    stored_plan_start = job_start_tick if plan_started_tick is None else plan_started_tick
    current_payload = {
        "plan": {"kind": "world_advance", "started_tick": stored_plan_start, "target_tick": target_tick},
        "speed": "balanced",
        "depth": "deep",
        "committed_state_version": state_version,
        "initial_state_version": 1,
    }
    connection.execute(
        "INSERT INTO world_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            job_id.hex, world_id.hex, branch_id.hex, capacity_supervisor.JOB_TYPE,
            manifest["idempotency_key"], "paused", json.dumps(current_payload),
            json.dumps({"probe_pause": "model_decision_failed"}), current_tick, None, None, 0,
        ),
    )
    if continued:
        predecessor = manifest["predecessor"]
        predecessor_job_id = UUID(predecessor["job_id"])
        old_payload = {
            "plan": {
                "kind": "world_advance",
                "started_tick": genesis_tick,
                "target_tick": predecessor_target_tick,
            },
            "speed": "balanced",
            "depth": "deep",
            "committed_state_version": 1,
            "initial_state_version": 0,
        }
        connection.execute(
            "INSERT INTO world_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                predecessor_job_id.hex, world_id.hex, branch_id.hex, capacity_supervisor.JOB_TYPE,
                f"capacity-probe:{predecessor['config_hash']}", "succeeded", json.dumps(old_payload),
                json.dumps({"probe_pause": None}), predecessor_target_tick, None, None, 0,
            ),
        )
    connection.commit()
    connection.close()
    return directory, current_tick, target_tick


def _persist_writer_lease(
    directory: Path,
    *,
    owner: str = "target",
    expires_at: datetime | None = None,
    lease_worker: str = "lease-worker-private",
    job_worker: str | None = None,
    status: str = "running",
) -> None:
    manifest = json.loads((directory / capacity_supervisor.MANIFEST_NAME).read_text(encoding="utf-8"))
    world_id = UUID(manifest["world_id"])
    branch_id = UUID(manifest["branch_id"])
    target_job_id = UUID(manifest["job_id"])
    lease_job_id = target_job_id if owner == "target" else uuid4()
    worker = job_worker if job_worker is not None else lease_worker
    expiry = (expires_at or (datetime.now(timezone.utc) + timedelta(minutes=3))).isoformat()
    connection = sqlite3.connect(directory / capacity_supervisor.DATABASE_NAME)
    if owner == "target":
        connection.execute(
            "UPDATE world_jobs SET status=?,worker_id=?,lease_expires_at=?,fencing_token=? WHERE job_id=?",
            (status, worker, expiry, 7, target_job_id.hex),
        )
    else:
        connection.execute(
            "INSERT INTO world_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (lease_job_id.hex, world_id.hex, branch_id.hex, "simulation.long_action", "other-writer",
             status, "{}", None, 1_200, worker, expiry, 7),
        )
    connection.execute(
        "INSERT INTO branch_writer_leases VALUES(?,?,?,?,?)",
        (branch_id.hex, lease_job_id.hex, lease_worker, 7, expiry),
    )
    connection.commit()
    connection.close()


def _make_running_without_lease(directory: Path) -> None:
    manifest = json.loads((directory / capacity_supervisor.MANIFEST_NAME).read_text(encoding="utf-8"))
    connection = sqlite3.connect(directory / capacity_supervisor.DATABASE_NAME)
    connection.execute(
        "UPDATE world_jobs SET status='running',worker_id=NULL,lease_expires_at=NULL,fencing_token=0 WHERE job_id=?",
        (UUID(manifest["job_id"]).hex,),
    )
    connection.commit()
    connection.close()


def _real_inspector_dependencies(tmp_path: Path, snapshot, *, now: datetime):
    dependencies = _dependencies(tmp_path, snapshot)
    dependencies.inspector = capacity_supervisor.SQLiteReadOnlyInspector()
    dependencies.clock = FakeClock(now)
    return dependencies


def test_live_target_writer_lease_observes_without_pid_or_mutation(tmp_path: Path):
    directory, _, _ = _write_inspector_fixture(tmp_path / "live-writer", continued=False)
    now = datetime.now(timezone.utc)
    _persist_writer_lease(directory, expires_at=now + timedelta(minutes=3))
    database = directory / capacity_supervisor.DATABASE_NAME
    before = database.read_bytes()
    inspected = capacity_supervisor.SQLiteReadOnlyInspector().inspect(directory, tmp_path / "model-data")
    assert inspected.writer_lease is not None
    assert inspected.writer_lease.owner == "target_job"
    assert inspected.writer_lease.job_status == "running"

    dependencies = _real_inspector_dependencies(tmp_path, inspected, now=now)
    config = _config(tmp_path, directory, inspect_only=True, once=True)
    result = capacity_supervisor.supervise_once(directory, config, dependencies)

    assert result.recovery_action == "observe"
    assert result.recovery_reason == "branch_writer_lease_active"
    assert result.pids == ()
    assert dependencies.spawner.requests == []
    assert database.read_bytes() == before
    assert not (dependencies.project_root / ".runtime").exists()
    assert "lease-worker-private" not in json.dumps(result.to_json())


def test_live_other_job_branch_writer_lease_waits_without_launch(tmp_path: Path):
    directory, _, _ = _write_inspector_fixture(tmp_path / "other-writer", continued=False)
    now = datetime.now(timezone.utc)
    _persist_writer_lease(directory, owner="other", expires_at=now + timedelta(minutes=3))
    snapshot = capacity_supervisor.SQLiteReadOnlyInspector().inspect(directory, tmp_path / "model-data")
    dependencies = _real_inspector_dependencies(tmp_path, snapshot, now=now)
    config = _config(tmp_path, directory, inspect_only=True, once=True)

    result = capacity_supervisor.supervise_once(directory, config, dependencies)

    assert result.recovery_action == "wait"
    assert result.recovery_reason == "other_branch_writer_lease_active"
    assert result.pids == ()
    assert dependencies.spawner.requests == []


@pytest.mark.parametrize("lease_kind", ["missing", "expired"])
def test_missing_or_expired_writer_lease_preserves_normal_recovery(tmp_path: Path, lease_kind: str):
    directory, _, _ = _write_inspector_fixture(tmp_path / lease_kind, continued=False)
    now = datetime.now(timezone.utc)
    if lease_kind == "missing":
        _make_running_without_lease(directory)
    else:
        _persist_writer_lease(directory, expires_at=now - timedelta(seconds=5))
    snapshot = capacity_supervisor.SQLiteReadOnlyInspector().inspect(directory, tmp_path / "model-data")

    decision = capacity_supervisor.assess_recovery(
        snapshot, (), now, capacity_supervisor.RecoveryPolicy(),
    )

    assert decision.action == "spawn"
    if lease_kind == "missing":
        assert snapshot.writer_lease is None
    else:
        assert snapshot.writer_lease is not None
        assert snapshot.writer_lease.expires_at <= now


def test_mismatched_live_writer_lease_metadata_fails_closed_without_mutation(tmp_path: Path):
    directory, _, _ = _write_inspector_fixture(tmp_path / "bad-writer", continued=False)
    now = datetime.now(timezone.utc)
    _persist_writer_lease(
        directory, expires_at=now + timedelta(minutes=3),
        lease_worker="branch-lease-worker", job_worker="different-job-worker",
    )
    database = directory / capacity_supervisor.DATABASE_NAME
    before = database.read_bytes()
    dependencies = _dependencies(tmp_path, None)
    dependencies.inspector = capacity_supervisor.SQLiteReadOnlyInspector()
    config = _config(tmp_path, directory, inspect_only=True, once=True)

    result = capacity_supervisor.supervise_once(directory, config, dependencies)

    assert result.recovery_action == "refuse"
    assert result.recovery_reason == "branch_writer_lease_invalid"
    assert result.pids == ()
    assert dependencies.spawner.requests == []
    assert database.read_bytes() == before


def test_inspector_accepts_continuation_start_at_verified_predecessor_tick(tmp_path: Path):
    directory, current_tick, target_tick = _write_inspector_fixture(tmp_path / "continued", continued=True)

    snapshot = capacity_supervisor.SQLiteReadOnlyInspector().inspect(directory, tmp_path / "model-data")

    assert snapshot.current_tick == current_tick
    assert snapshot.target_tick == target_tick
    assert snapshot.job_payload["plan"]["started_tick"] == 1_100


@pytest.mark.parametrize(
    ("kwargs", "expected_code"),
    [
        ({"plan_started_tick": 1_101}, "job_plan_manifest_mismatch"),
        ({"manifest_job_start_tick": 1_101, "plan_started_tick": 1_101}, "manifest_predecessor_invalid"),
        ({"tamper_predecessor_pin": True}, "manifest_predecessor_invalid"),
    ],
)
def test_inspector_rejects_tampered_continuation_start_or_predecessor(
    tmp_path: Path, kwargs: dict, expected_code: str,
):
    directory, _, _ = _write_inspector_fixture(tmp_path / "tampered", continued=True, **kwargs)

    with pytest.raises(capacity_supervisor.SupervisorSafetyError) as exc:
        capacity_supervisor.SQLiteReadOnlyInspector().inspect(directory, tmp_path / "model-data")

    assert exc.value.code == expected_code


def test_inspector_keeps_genesis_start_validation_for_legacy_manifest(tmp_path: Path):
    directory, _, _ = _write_inspector_fixture(tmp_path / "legacy", continued=False)

    snapshot = capacity_supervisor.SQLiteReadOnlyInspector().inspect(directory, tmp_path / "model-data")

    assert snapshot.job_payload["plan"]["started_tick"] == 100

    mismatched, _, _ = _write_inspector_fixture(
        tmp_path / "legacy-mismatch", continued=False, plan_started_tick=101,
    )
    with pytest.raises(capacity_supervisor.SupervisorSafetyError) as exc:
        capacity_supervisor.SQLiteReadOnlyInspector().inspect(mismatched, tmp_path / "model-data")
    assert exc.value.code == "job_plan_manifest_mismatch"
