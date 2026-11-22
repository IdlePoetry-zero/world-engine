"""Safely supervise only existing resumable capacity-probe runs.

The supervisor is intentionally separate from probe-world-advance.py.  Its
decision path is pure and injectable; its production inspectors use SQLite in
read-only mode, and it never makes a world, job, model decision, or provider
configuration itself.
"""

from __future__ import annotations

import argparse
from contextlib import closing, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any, Literal, Mapping, Protocol, Sequence
from uuid import UUID


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE_SCRIPT = PROJECT_ROOT / "scripts" / "probe-world-advance.py"
MANIFEST_NAME = ".probe-world-advance.json"
DATABASE_NAME = "world-engine.db"
IDENTITY_NAME = "local-identity.json"
OPERATIONS_NAME = "operations.db"
JOB_TYPE = "simulation.world_advance"
TRANSIENT_ERROR_CODES = frozenset({"network", "timeout", "server", "rate_limited"})
# probe-world-advance.py uses the checkout's alembic.ini. Permit only the
# initial schema and its one known side-table migration; never upgrade an
# unknown database schema through a potentially unrelated checkout.
SUPPORTED_CHECKOUT_MIGRATIONS = frozenset({"bdc44c16f77a", "6fbc6e5d1a70"})
BOOTSTRAP_CODE = """import hashlib,json,os,runpy,sys
from datetime import datetime,timezone
from pathlib import Path
backend=Path(os.environ['WORLD_ENGINE_FROZEN_BACKEND']).resolve()
sys.path.insert(0,str(backend))
def contained(path):
    try:
        Path(path).resolve().relative_to(backend)
        return True
    except (ValueError,OSError,TypeError):
        return False
def evidence(status,reason,modules):
    target=Path(os.environ['WORLD_ENGINE_SUPERVISOR_BOOTSTRAP_EVIDENCE'])
    doc={'schema':1,'status':status,'reason_code':reason,'at_utc':datetime.now(timezone.utc).isoformat(),
         'pid':os.getpid(),'world_id':os.environ['WORLD_ENGINE_SUPERVISOR_WORLD_ID'],
         'branch_id':os.environ['WORLD_ENGINE_SUPERVISOR_BRANCH_ID'],
         'job_id':os.environ['WORLD_ENGINE_SUPERVISOR_JOB_ID'],
         'data_directory':os.environ['WORLD_ENGINE_SUPERVISOR_DATA_DIRECTORY'],
         'backend_path':str(backend),'modules':modules}
    try:
        with target.open('x',encoding='utf-8',newline='\\n') as handle:
            json.dump(doc,handle,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            handle.write('\\n');handle.flush();os.fsync(handle.fileno())
    except Exception:
        raise SystemExit(79)
try:
    import world_engine
    import world_engine.simulation.engine as simulation_engine
    import world_engine.storage.repository as storage_repository
except Exception:
    evidence('refused','frozen_module_import_failed',{})
    raise SystemExit(78)
module_files={
    'world_engine':Path(world_engine.__file__).resolve(),
    'simulation_engine':Path(simulation_engine.__file__).resolve(),
    'storage_repository':Path(storage_repository.__file__).resolve(),
}
package_paths=[Path(item).resolve() for item in getattr(world_engine,'__path__',())]
verified=bool(package_paths) and all(contained(path) for path in [*package_paths,*module_files.values()])
modules={name:{'path':str(path.relative_to(backend)) if contained(path) else '[outside-frozen-backend]',
               'sha256':hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
         for name,path in module_files.items()}
if not verified:
    evidence('refused','frozen_module_path_mismatch',modules)
    raise SystemExit(78)
evidence('verified','frozen_backend_verified',modules)
script=sys.argv[1]
sys.argv=[script,*sys.argv[2:]]
runpy.run_path(script,run_name='__main__')
"""

BillingState = Literal["unbilled", "possibly_billed", "unknown"]
RecoveryAction = Literal["spawn", "wait", "observe", "report", "refuse"]


class SupervisorSafetyError(ValueError):
    """Safe validation error; messages are fixed codes, not source exceptions."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ModelCallSnapshot:
    request_id: str
    error_code: str | None
    retry_after_seconds: float | None
    billing_state: BillingState
    result_available: bool
    failure_at: datetime | None
    decision_request_id: str | None = None
    ended_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    directory: Path
    manifest_path: Path
    manifest_bytes: bytes
    manifest_sha256: str
    world_id: UUID
    branch_id: UUID
    job_id: UUID
    job_status: str
    job_payload: Mapping[str, Any]
    job_pause_code: str | None
    current_tick: int
    target_tick: int
    state_version: int
    ruleset_version: str
    pack_version: str
    config: Mapping[str, Any]
    decision_mode: str
    alembic_revision: str
    latest_model_call: ModelCallSnapshot | None
    latest_call_boundary_cleared: bool = False
    model_host_deadline: Mapping[str, Any] | None = None
    writer_lease: WriterLeaseSnapshot | None = None


@dataclass(frozen=True, slots=True)
class WriterLeaseSnapshot:
    """Validated branch-writer ownership without exposing worker credentials."""

    owner: Literal["target_job", "other_job"]
    expires_at: datetime
    job_status: str


@dataclass(frozen=True, slots=True)
class ObservedProcess:
    pid: int
    argv: tuple[str, ...]
    executable_path: str | None = None
    uncertain: bool = False


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    backoff_initial_seconds: int = 60
    backoff_max_seconds: int = 1800
    allow_possibly_billed_retry: bool = False

    def __post_init__(self) -> None:
        if type(self.backoff_initial_seconds) is not int or self.backoff_initial_seconds < 1:
            raise ValueError("backoff_initial_seconds must be a positive integer")
        if type(self.backoff_max_seconds) is not int or self.backoff_max_seconds < self.backoff_initial_seconds:
            raise ValueError("backoff_max_seconds must be at least backoff_initial_seconds")
        if type(self.allow_possibly_billed_retry) is not bool:
            raise ValueError("allow_possibly_billed_retry must be a bool")


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    action: RecoveryAction
    reason_code: str
    failure_key: str | None = None
    failure_phase: str | None = None
    error_code: str | None = None
    billing_state: BillingState = "unknown"
    retry_after_seconds: float | None = None
    attempt_index: int = 0
    wait_seconds: float = 0


@dataclass(frozen=True, slots=True)
class SpawnRequest:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    data_directory: Path
    job_id: UUID
    failure_key: str
    resume_model_failure: bool
    attempt_index: int
    bootstrap_evidence_path: Path


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    data_directories: tuple[Path, ...]
    backend_path: Path
    model_data_directory: Path
    project_root: Path = PROJECT_ROOT
    poll_interval_seconds: int = 30
    backoff_initial_seconds: int = 60
    backoff_max_seconds: int = 1800
    backup_compressed_mib: int = 512
    backup_expanded_mib: int = 4096
    allow_possibly_billed_retry: bool = False
    max_windows_per_invocation: int | None = None
    lease_wait_seconds: int | None = None
    inspect_only: bool = False
    once: bool = False

    def __post_init__(self) -> None:
        if not self.data_directories:
            raise ValueError("at least one existing data directory is required")
        if type(self.poll_interval_seconds) is not int or self.poll_interval_seconds < 1:
            raise ValueError("poll_interval_seconds must be positive")
        if type(self.backup_compressed_mib) is not int or not 1 <= self.backup_compressed_mib <= 65_536:
            raise ValueError("backup_compressed_mib must be between 1 and 65536")
        if type(self.backup_expanded_mib) is not int or not 1 <= self.backup_expanded_mib <= 262_144:
            raise ValueError("backup_expanded_mib must be between 1 and 262144")
        RecoveryPolicy(
            self.backoff_initial_seconds,
            self.backoff_max_seconds,
            self.allow_possibly_billed_retry,
        )
        if self.max_windows_per_invocation is not None and (
            type(self.max_windows_per_invocation) is not int or self.max_windows_per_invocation < 1
        ):
            raise ValueError("max_windows_per_invocation must be positive when supplied")
        if self.lease_wait_seconds is not None and (
            type(self.lease_wait_seconds) is not int or self.lease_wait_seconds < 0
        ):
            raise ValueError("lease_wait_seconds must be nonnegative when supplied")
        resolved = [Path(item).expanduser().resolve() for item in self.data_directories]
        if len(set(resolved)) != len(resolved):
            raise ValueError("data directories must be unique after path resolution")


class Clock(Protocol):
    def now(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


class ProcessInventory(Protocol):
    def list_probe_processes(self, probe_script: Path) -> Sequence[ObservedProcess]: ...


class ReadOnlyRunInspector(Protocol):
    def inspect(self, directory: Path, model_data_directory: Path) -> RunSnapshot: ...


class ProbeSpawner(Protocol):
    def spawn(self, request: SpawnRequest) -> int: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class WindowsProcessInventory:
    """Read process argv through CIM JSON, never by inserting user text in PowerShell."""

    _COMMAND = (
        "$ErrorActionPreference='Stop';"
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();"
        "Get-CimInstance -ClassName Win32_Process | "
        "Select-Object ProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    )

    def __init__(self, powershell: str = "powershell.exe") -> None:
        self.powershell = powershell

    def list_probe_processes(self, probe_script: Path) -> Sequence[ObservedProcess]:
        if os.name != "nt":
            raise SupervisorSafetyError("process_inventory_unsupported")
        try:
            result = subprocess.run(
                [self.powershell, "-NoProfile", "-NonInteractive", "-Command", self._COMMAND],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                shell=False,
            )
            decoded = json.loads(result.stdout)
        except Exception:
            raise SupervisorSafetyError("process_inventory_failed") from None
        if decoded is None:
            rows: list[dict[str, Any]] = []
        elif isinstance(decoded, dict):
            rows = [decoded]
        elif isinstance(decoded, list) and all(isinstance(item, dict) for item in decoded):
            rows = decoded
        else:
            raise SupervisorSafetyError("process_inventory_invalid_json")

        matches: list[ObservedProcess] = []
        for row in rows:
            name = row.get("Name")
            raw_command = row.get("CommandLine")
            executable = row.get("ExecutablePath")
            pid = row.get("ProcessId")
            if not isinstance(name, str) or "python" not in name.casefold():
                continue
            if raw_command is None:
                # An inaccessible Python command line makes absence uncertain.
                matches.append(ObservedProcess(pid=int(pid or 0), argv=(), executable_path=executable, uncertain=True))
                continue
            if not isinstance(raw_command, str) or "probe-world-advance.py" not in raw_command.casefold():
                continue
            try:
                argv = _windows_command_line_argv(raw_command)
            except Exception:
                raise SupervisorSafetyError("process_inventory_commandline_unreadable") from None
            if _argv_names_probe(argv, probe_script):
                if type(pid) is not int or pid <= 0:
                    raise SupervisorSafetyError("process_inventory_pid_invalid")
                matches.append(ObservedProcess(int(pid), tuple(argv), executable_path=executable))
        return tuple(matches)


def _windows_command_line_argv(command_line: str) -> tuple[str, ...]:
    if os.name != "nt":
        raise OSError("Windows command-line parsing is only available on Windows")
    import ctypes
    from ctypes import wintypes

    argc = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL
    pointer = command_line_to_argv(command_line, ctypes.byref(argc))
    if not pointer:
        raise OSError("CommandLineToArgvW failed")
    try:
        return tuple(pointer[index] for index in range(argc.value))
    finally:
        local_free(pointer)


def _argv_names_probe(argv: Sequence[str], probe_script: Path) -> bool:
    expected = probe_script.expanduser().resolve()
    for token in argv:
        if Path(token).name.casefold() != probe_script.name.casefold():
            continue
        candidate = Path(token)
        if not candidate.is_absolute():
            return True  # conservative: unresolved relative script is a candidate
        try:
            if candidate.resolve() == expected:
                return True
        except OSError:
            return True
    return False


def _argv_data_directory(argv: Sequence[str]) -> Path | None:
    for index, token in enumerate(argv):
        value: str | None = None
        if token == "--data-directory" and index + 1 < len(argv):
            value = argv[index + 1]
        elif token.startswith("--data-directory="):
            value = token.split("=", 1)[1]
        if value is None or not value:
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            return None
        try:
            return candidate.resolve()
        except OSError:
            return None
    return None


class SQLiteReadOnlyInspector:
    """Validate an existing manifest, branch and job without creating any file."""

    def inspect(self, directory: Path, model_data_directory: Path) -> RunSnapshot:
        run_dir = directory.expanduser().resolve()
        if not run_dir.is_dir():
            raise SupervisorSafetyError("data_directory_missing")
        manifest_path = run_dir / MANIFEST_NAME
        try:
            manifest_bytes = manifest_path.read_bytes()
        except FileNotFoundError:
            raise SupervisorSafetyError("manifest_missing") from None
        except OSError:
            raise SupervisorSafetyError("manifest_unreadable") from None
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SupervisorSafetyError("manifest_invalid_json") from None
        if not isinstance(manifest, dict) or manifest.get("schema") != 1:
            raise SupervisorSafetyError("manifest_schema_unsupported")
        config = manifest.get("config")
        if not isinstance(config, dict):
            raise SupervisorSafetyError("manifest_config_missing")
        try:
            encoded_config = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            raise SupervisorSafetyError("manifest_config_invalid") from None
        expected_hash = hashlib.sha256(encoded_config.encode("utf-8")).hexdigest()
        if manifest.get("config_hash") != expected_hash:
            raise SupervisorSafetyError("manifest_config_hash_mismatch")
        if manifest.get("idempotency_key") != f"capacity-probe:{expected_hash}":
            raise SupervisorSafetyError("manifest_idempotency_key_mismatch")
        if manifest.get("world_created") is not True:
            raise SupervisorSafetyError("manifest_world_not_created")

        try:
            world_id = UUID(str(manifest["world_id"]))
            branch_id = UUID(str(manifest["branch_id"]))
            job_id = UUID(str(manifest["job_id"]))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SupervisorSafetyError("manifest_ids_invalid") from None
        start_tick = _manifest_int(manifest, "start_tick")
        target_tick = _manifest_int(manifest, "target_tick")
        config_seconds = config.get("total_seconds")
        if type(config_seconds) is not int or config_seconds <= 0 or target_tick != start_tick + config_seconds:
            raise SupervisorSafetyError("manifest_target_config_mismatch")
        pack_version = config.get("pack_version")
        if pack_version is None and "population_mode" not in config and "catalog_seeded" not in config:
            pack_version = "1.7.0"
        pack_id = config.get("pack_id")
        decision_mode = config.get("decision_mode", "deterministic_no_model")
        if not isinstance(pack_version, str) or not pack_version or not isinstance(pack_id, str) or not pack_id:
            raise SupervisorSafetyError("manifest_content_config_invalid")
        if decision_mode not in {"deterministic_no_model", "configured_local_model"}:
            raise SupervisorSafetyError("manifest_decision_mode_invalid")

        database_name = manifest.get("database_file", DATABASE_NAME)
        if database_name != DATABASE_NAME:
            raise SupervisorSafetyError("manifest_database_path_invalid")
        database_path = run_dir / DATABASE_NAME
        if not database_path.is_file():
            raise SupervisorSafetyError("world_database_missing")
        db_uri = database_path.as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(db_uri, uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            # Keep world, branch, job and writer-lease facts in one SQLite read
            # snapshot. A worker may renew or hand off a lease concurrently.
            connection.execute("BEGIN")
            snapshot_fields = self._read_world_job(
                connection, manifest, world_id, branch_id, job_id, expected_hash,
                start_tick, target_tick, pack_version, run_dir,
            )
            connection.close()
        except SupervisorSafetyError:
            try:
                connection.close()
            except Exception:
                pass
            raise
        except Exception:
            try:
                connection.close()
            except Exception:
                pass
            raise SupervisorSafetyError("world_database_read_failed") from None

        latest_call = None
        if decision_mode == "configured_local_model":
            latest_call = self._read_latest_model_call(
                model_data_directory, world_id, branch_id, job_id,
            )
        committed_event = False
        last_decision = snapshot_fields["job_payload"].get("last_decision")
        last_request_id = last_decision.get("request_id") if isinstance(last_decision, dict) else None
        if (
            snapshot_fields["job_status"] == "running"
            and latest_call is not None and latest_call.result_available
            and latest_call.decision_request_id is not None
            and latest_call.decision_request_id != last_request_id
        ):
            try:
                with closing(sqlite3.connect(db_uri, uri=True, timeout=5)) as receipt_db:
                    if latest_call.decision_request_id == str(job_id):
                        committed_event = latest_call.ended_at is not None and _has_progress_after_legacy_call(
                            receipt_db, world_id, branch_id, job_id,
                            latest_call.ended_at, snapshot_fields["state_version"],
                        )
                    else:
                        committed_event = _has_succession_receipt(
                            receipt_db, world_id, branch_id, job_id,
                            latest_call.decision_request_id, snapshot_fields["state_version"],
                        )
            except sqlite3.Error:
                raise SupervisorSafetyError("decision_receipt_read_failed") from None
        digest = hashlib.sha256(manifest_bytes).hexdigest()
        return RunSnapshot(
            directory=run_dir,
            manifest_path=manifest_path,
            manifest_bytes=manifest_bytes,
            manifest_sha256=digest,
            world_id=world_id,
            branch_id=branch_id,
            job_id=job_id,
            job_status=snapshot_fields["job_status"],
            job_payload=snapshot_fields["job_payload"],
            job_pause_code=snapshot_fields["job_pause_code"],
            current_tick=snapshot_fields["current_tick"],
            target_tick=target_tick,
            state_version=snapshot_fields["state_version"],
            ruleset_version=snapshot_fields["ruleset_version"],
            pack_version=pack_version,
            config=config,
            decision_mode=decision_mode,
            alembic_revision=snapshot_fields["alembic_revision"],
            latest_model_call=latest_call,
            latest_call_boundary_cleared=committed_event,
            model_host_deadline=snapshot_fields["model_host_deadline"],
            writer_lease=snapshot_fields["writer_lease"],
        )

    @staticmethod
    def _read_world_job(connection, manifest, world_id, branch_id, job_id, config_hash,
                        start_tick, target_tick, pack_version, run_dir):
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        required_tables = {"worlds", "world_branches", "world_jobs", "branch_writer_leases", "alembic_version"}
        if not required_tables.issubset(tables):
            raise SupervisorSafetyError("world_database_schema_incomplete")
        revision_rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        if len(revision_rows) != 1 or revision_rows[0][0] not in SUPPORTED_CHECKOUT_MIGRATIONS:
            raise SupervisorSafetyError("database_migration_version_unsupported")
        world = connection.execute(
            "SELECT world_id, active_branch_id, pack_version, ruleset_version FROM worlds WHERE world_id=?",
            (world_id.hex,),
        ).fetchone()
        branch = connection.execute(
            "SELECT world_id, current_tick, state_version FROM world_branches WHERE branch_id=?",
            (branch_id.hex,),
        ).fetchone()
        job = connection.execute(
            "SELECT job_id, world_id, branch_id, job_type, idempotency_key, status, payload_json, result_json, committed_through_tick, "
            "worker_id, lease_expires_at, fencing_token "
            "FROM world_jobs WHERE job_id=?",
            (job_id.hex,),
        ).fetchone()
        if world is None or branch is None or job is None:
            raise SupervisorSafetyError("world_branch_or_job_missing")
        if (
            not _db_uuid_matches(world["world_id"], world_id)
            or not _db_uuid_matches(world["active_branch_id"], branch_id)
            or world["pack_version"] != pack_version
            or not _db_uuid_matches(branch["world_id"], world_id)
            or not _db_uuid_matches(job["job_id"], job_id)
            or not _db_uuid_matches(job["world_id"], world_id)
            or not _db_uuid_matches(job["branch_id"], branch_id)
            or job["job_type"] != JOB_TYPE
            or job["idempotency_key"] != manifest.get("idempotency_key")
        ):
            raise SupervisorSafetyError("world_branch_job_identity_mismatch")
        writer_lease = SQLiteReadOnlyInspector._read_branch_writer_lease(
            connection, world_id, branch_id, job_id,
        )
        try:
            payload = json.loads(job["payload_json"])
            result = json.loads(job["result_json"]) if job["result_json"] else None
        except (TypeError, json.JSONDecodeError):
            raise SupervisorSafetyError("job_payload_invalid") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("plan"), dict):
            raise SupervisorSafetyError("job_plan_missing")
        plan = payload["plan"]
        job_start_tick = _continuation_job_start_tick(
            connection, run_dir, manifest, world_id, branch_id, start_tick, target_tick,
        )
        if (
            plan.get("started_tick") != job_start_tick
            or plan.get("target_tick") != target_tick
            or payload.get("speed") != "balanced"
            or payload.get("depth") != "deep"
        ):
            raise SupervisorSafetyError("job_plan_manifest_mismatch")
        committed_version = payload.get("committed_state_version", payload.get("initial_state_version"))
        if (
            type(branch["current_tick"]) is not int
            or branch["current_tick"] != job["committed_through_tick"]
            or type(branch["state_version"]) is not int
            or committed_version != branch["state_version"]
            or branch["current_tick"] < job_start_tick
            or branch["current_tick"] > target_tick
        ):
            raise SupervisorSafetyError("job_branch_progress_mismatch")
        pause_code = result.get("probe_pause") if isinstance(result, dict) else None
        host_deadline = result.get("model_host_deadline") if isinstance(result, dict) else None
        return {
            "job_status": job["status"],
            "job_payload": payload,
            "job_pause_code": pause_code if isinstance(pause_code, str) else None,
            "model_host_deadline": host_deadline if isinstance(host_deadline, dict) else None,
            "current_tick": branch["current_tick"],
            "state_version": branch["state_version"],
            "ruleset_version": world["ruleset_version"],
            "alembic_revision": revision_rows[0][0],
            "writer_lease": writer_lease,
        }

    @staticmethod
    def _read_branch_writer_lease(connection, world_id: UUID, branch_id: UUID, target_job_id: UUID):
        lease = connection.execute(
            "SELECT branch_id, job_id, worker_id, fencing_token, lease_expires_at "
            "FROM branch_writer_leases WHERE branch_id=?",
            (branch_id.hex,),
        ).fetchone()
        if lease is None:
            return None
        try:
            if (
                not _db_uuid_matches(lease["branch_id"], branch_id)
                or not isinstance(lease["worker_id"], str)
                or not lease["worker_id"]
                or len(lease["worker_id"]) > 200
                or type(lease["fencing_token"]) is not int
                or lease["fencing_token"] < 1
            ):
                raise ValueError
            lease_job_id = UUID(str(lease["job_id"]))
            lease_expiry = _parse_database_time(lease["lease_expires_at"])
            lease_job = connection.execute(
                "SELECT job_id, world_id, branch_id, status, worker_id, lease_expires_at, fencing_token "
                "FROM world_jobs WHERE job_id=?",
                (lease_job_id.hex,),
            ).fetchone()
            if lease_job is None:
                raise ValueError
            job_expiry = _parse_database_time(lease_job["lease_expires_at"])
            if (
                not _db_uuid_matches(lease_job["job_id"], lease_job_id)
                or not _db_uuid_matches(lease_job["world_id"], world_id)
                or not _db_uuid_matches(lease_job["branch_id"], branch_id)
                or not isinstance(lease_job["status"], str)
                or lease_job["status"] not in {"queued", "running", "waiting_input", "paused", "succeeded", "failed", "cancelled"}
                or lease_job["worker_id"] != lease["worker_id"]
                or type(lease_job["fencing_token"]) is not int
                or lease_job["fencing_token"] != lease["fencing_token"]
                or job_expiry != lease_expiry
            ):
                raise ValueError
        except (ValueError, TypeError, OverflowError, AttributeError):
            raise SupervisorSafetyError("branch_writer_lease_invalid") from None
        return WriterLeaseSnapshot(
            owner="target_job" if lease_job_id == target_job_id else "other_job",
            expires_at=lease_expiry,
            job_status=lease_job["status"],
        )


    @staticmethod
    def _read_latest_model_call(
        model_data_directory: Path, world_id: UUID, branch_id: UUID, job_id: UUID,
    ) -> ModelCallSnapshot | None:
        model_dir = model_data_directory.expanduser().resolve()
        identity_path = model_dir / IDENTITY_NAME
        operations_path = model_dir / OPERATIONS_NAME
        if not model_dir.is_dir() or not identity_path.is_file() or not operations_path.is_file():
            raise SupervisorSafetyError("model_operations_records_missing")
        try:
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            tenant_id = str(UUID(str(identity["tenant_id"])))
            principal_id = str(UUID(str(identity["user_id"])))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError, TypeError):
            raise SupervisorSafetyError("model_identity_invalid") from None
        connection = None
        try:
            connection = sqlite3.connect(operations_path.as_uri() + "?mode=ro", uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "calls" not in tables:
                raise SupervisorSafetyError("model_operations_schema_incomplete")
            row = connection.execute(
                "SELECT id, document FROM calls WHERE tenant=? AND principal=? "
                "AND json_extract(document, '$.world_id')=? "
                "AND json_extract(document, '$.branch_id')=? "
                "AND json_extract(document, '$.job_id')=? "
                "AND json_extract(document, '$.role_task')='npc_decision' "
                "ORDER BY started_at DESC LIMIT 1",
                (tenant_id, principal_id, str(world_id), str(branch_id), str(job_id)),
            ).fetchone()
        except SupervisorSafetyError:
            raise
        except Exception:
            raise SupervisorSafetyError("model_operations_read_failed") from None
        finally:
            if connection is not None:
                connection.close()
        if row is None:
            return None
        try:
            trace = json.loads(row["document"])
        except (TypeError, json.JSONDecodeError):
            raise SupervisorSafetyError("model_call_record_invalid") from None
        if not isinstance(trace, dict):
            raise SupervisorSafetyError("model_call_record_invalid")
        request_id = trace.get("request_id", row["id"])
        if not isinstance(request_id, str) or not request_id or len(request_id) > 160:
            raise SupervisorSafetyError("model_call_identity_invalid")
        parent_trace = trace.get("parent_trace")
        try:
            decision_request_id = str(UUID(parent_trace)) if isinstance(parent_trace, str) else None
        except ValueError:
            decision_request_id = None
        error_code = trace.get("error_code")
        if error_code is not None and not isinstance(error_code, str):
            error_code = None
        attempts = trace.get("attempts", [])
        if not isinstance(attempts, list):
            attempts = []
        billing_flags = [item.get("possibly_billed") for item in attempts if isinstance(item, dict)]
        if any(item is True for item in billing_flags):
            billing: BillingState = "possibly_billed"
        elif error_code is not None and billing_flags and all(item is False for item in billing_flags):
            billing = "unbilled"
        elif error_code is None and _trace_has_usage(trace):
            billing = "possibly_billed"
        else:
            billing = "unknown"
        retry_after_values = [
            value for item in attempts if isinstance(item, dict)
            if _valid_seconds(value := item.get("retry_after_seconds"))
        ]
        retry_after = max(retry_after_values) if retry_after_values else None
        failure_at = _parse_trace_time(trace.get("ended_at")) if error_code is not None else None
        finish_reason = trace.get("finish_reason")
        result_available = (
            error_code is None
            and isinstance(finish_reason, str)
            and finish_reason.casefold() not in {"aborted", "cancelled"}
            and trace.get("end_reason") != "cancelled"
        )
        return ModelCallSnapshot(
            request_id=request_id,
            error_code=error_code,
            retry_after_seconds=retry_after,
            billing_state=billing,
            result_available=result_available,
            failure_at=failure_at,
            decision_request_id=decision_request_id,
            ended_at=_parse_trace_time(trace.get("ended_at")),
        )


def _continuation_job_start_tick(
    connection: sqlite3.Connection,
    run_dir: Path,
    manifest: Mapping[str, Any],
    world_id: UUID,
    branch_id: UUID,
    start_tick: int,
    target_tick: int,
) -> int:
    """Resolve a continuation's job boundary from its archived predecessor.

    Ordinary and legacy manifests start their job at the genesis tick. A
    continued manifest carries an immutable predecessor archive and terminal
    pin; both are checked against the stored predecessor job before the
    current durable plan is allowed to start at that later tick.
    """
    if "predecessor" not in manifest:
        try:
            job_start = _manifest_int(manifest, "job_start_tick") if "job_start_tick" in manifest else start_tick
        except SupervisorSafetyError:
            raise SupervisorSafetyError("manifest_job_start_tick_invalid") from None
        if job_start != start_tick:
            raise SupervisorSafetyError("manifest_job_start_tick_invalid")
        return start_tick

    predecessor = manifest.get("predecessor")
    if not isinstance(predecessor, dict):
        raise SupervisorSafetyError("manifest_predecessor_invalid")
    config_hash = predecessor.get("config_hash")
    archive_name = predecessor.get("manifest_archive")
    raw_job_id = predecessor.get("job_id")
    if (
        not isinstance(config_hash, str)
        or len(config_hash) != 64
        or any(character not in "0123456789abcdef" for character in config_hash)
        or not isinstance(archive_name, str)
        or archive_name != f".probe-world-advance.completed.{config_hash}.json"
        or Path(archive_name).name != archive_name
    ):
        raise SupervisorSafetyError("manifest_predecessor_invalid")
    try:
        predecessor_job_id = UUID(str(raw_job_id))
        predecessor_tick = _manifest_int(predecessor, "target_tick")
        job_start_tick = _manifest_int(manifest, "job_start_tick")
    except (ValueError, TypeError, AttributeError, SupervisorSafetyError):
        raise SupervisorSafetyError("manifest_predecessor_invalid") from None
    if (
        job_start_tick != predecessor_tick
        or predecessor_tick <= start_tick
        or predecessor_tick >= target_tick
    ):
        raise SupervisorSafetyError("manifest_predecessor_invalid")

    archive_path = run_dir / archive_name
    try:
        if archive_path.is_symlink() or archive_path.resolve().parent != run_dir.resolve():
            raise SupervisorSafetyError("manifest_predecessor_invalid")
        archived = json.loads(archive_path.read_text(encoding="utf-8"))
    except SupervisorSafetyError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SupervisorSafetyError("manifest_predecessor_invalid") from None
    if not isinstance(archived, dict) or not isinstance(archived.get("config"), dict):
        raise SupervisorSafetyError("manifest_predecessor_invalid")
    old_config = archived["config"]
    try:
        old_config_json = json.dumps(
            old_config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        archived_config_hash = hashlib.sha256(old_config_json.encode("utf-8")).hexdigest()
        archived_start_tick = _manifest_int(archived, "start_tick")
        archived_target_tick = _manifest_int(archived, "target_tick")
        old_duration = old_config.get("total_seconds")
        pin = _archived_terminal_pin(archived, predecessor_tick)
    except (TypeError, ValueError, UnicodeError, SupervisorSafetyError):
        raise SupervisorSafetyError("manifest_predecessor_invalid") from None
    if (
        archived_config_hash != config_hash
        or archived.get("config_hash") != config_hash
        or archived.get("idempotency_key") != f"capacity-probe:{config_hash}"
        or archived.get("job_id") != str(predecessor_job_id)
        or archived.get("world_id") != str(world_id)
        or archived.get("branch_id") != str(branch_id)
        or archived.get("world_created") is not True
        or archived.get("status") != "succeeded"
        or archived.get("last_job_status") != "succeeded"
        or archived_start_tick != start_tick
        or archived_target_tick != predecessor_tick
        or type(old_duration) is not int
        or old_duration <= 0
        or archived_start_tick + old_duration != archived_target_tick
        or predecessor.get("terminal_checkpoint") != pin
    ):
        raise SupervisorSafetyError("manifest_predecessor_invalid")

    old_job = connection.execute(
        "SELECT job_id, world_id, branch_id, job_type, idempotency_key, status, payload_json, committed_through_tick "
        "FROM world_jobs WHERE job_id=?",
        (predecessor_job_id.hex,),
    ).fetchone()
    if old_job is None:
        raise SupervisorSafetyError("manifest_predecessor_invalid")
    try:
        old_payload = json.loads(old_job["payload_json"])
    except (TypeError, json.JSONDecodeError):
        raise SupervisorSafetyError("manifest_predecessor_invalid") from None
    old_plan = old_payload.get("plan") if isinstance(old_payload, dict) else None
    try:
        predecessor_job_start_tick = (
            _manifest_int(archived, "job_start_tick")
            if "job_start_tick" in archived else archived_start_tick
        )
    except SupervisorSafetyError:
        raise SupervisorSafetyError("manifest_predecessor_invalid") from None
    if (
        not _db_uuid_matches(old_job["job_id"], predecessor_job_id)
        or not _db_uuid_matches(old_job["world_id"], world_id)
        or not _db_uuid_matches(old_job["branch_id"], branch_id)
        or old_job["job_type"] != JOB_TYPE
        or old_job["idempotency_key"] != f"capacity-probe:{config_hash}"
        or old_job["status"] != "succeeded"
        or old_job["committed_through_tick"] != predecessor_tick
        or not isinstance(old_plan, dict)
        or old_plan.get("kind") != "world_advance"
        or old_plan.get("started_tick") != predecessor_job_start_tick
        or old_plan.get("target_tick") != predecessor_tick
        or old_payload.get("speed") != "balanced"
        or old_payload.get("depth") != "deep"
    ):
        raise SupervisorSafetyError("manifest_predecessor_invalid")
    return predecessor_tick


def _archived_terminal_pin(manifest: Mapping[str, Any], target_tick: int) -> dict[str, Any]:
    checkpoints = manifest.get("checkpoints")
    if not isinstance(checkpoints, list):
        raise SupervisorSafetyError("manifest_predecessor_invalid")
    matches = [
        checkpoint for checkpoint in checkpoints
        if isinstance(checkpoint, dict)
        and checkpoint.get("label") == "target"
        and checkpoint.get("tick") == str(target_tick)
    ]
    if len(matches) != 1:
        raise SupervisorSafetyError("manifest_predecessor_invalid")
    checkpoint = matches[0]
    try:
        sequence = _manifest_int(checkpoint, "sequence")
        state_version = _manifest_int(checkpoint, "state_version")
    except SupervisorSafetyError:
        raise SupervisorSafetyError("manifest_predecessor_invalid") from None
    state_hash = checkpoint.get("state_hash")
    if (
        sequence < 0 or state_version < 0
        or not isinstance(state_hash, str) or len(state_hash) != 64
    ):
        raise SupervisorSafetyError("manifest_predecessor_invalid")
    return {
        "label": "target",
        "scheduled_year": None,
        "tick": str(target_tick),
        "sequence": str(sequence),
        "state_version": str(state_version),
        "state_hash": state_hash,
    }

def _has_succession_receipt(connection, world_id, branch_id, job_id, request_id, state_version):
    """Older workers stored the decision in the committed succession event."""
    return connection.execute(
        "SELECT 1 FROM world_events AS event "
        "JOIN world_commands AS command ON command.command_id=event.command_id "
        "WHERE event.world_id=? AND event.branch_id=? AND event.state_version_after<=? "
        "AND event.event_type='core.governance_succession.v1' "
        "AND command.world_id=event.world_id AND command.branch_id=event.branch_id "
        "AND command.command_type='simulation.window' "
        "AND json_extract(command.payload_json, '$.job_id')=? "
        "AND json_extract(event.payload_json, '$.decision_request_id')=? LIMIT 1",
        (world_id.hex, branch_id.hex, state_version, str(job_id), request_id),
    ).fetchone() is not None


def _has_progress_after_legacy_call(connection, world_id, branch_id, job_id, ended_at, state_version):
    """A job-level trace predates two later time commits made by that same job."""
    ended_utc = ended_at.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    ticks = connection.execute(
        "SELECT DISTINCT event.tick FROM world_events AS event "
        "JOIN world_commands AS command ON command.command_id=event.command_id "
        "WHERE event.world_id=? AND event.branch_id=? AND event.state_version_after<=? "
        "AND event.event_type='core.time_advanced.v1' "
        "AND event.committed_at>? "
        "AND command.world_id=event.world_id AND command.branch_id=event.branch_id "
        "AND command.command_type='simulation.window' "
        "AND json_extract(command.payload_json, '$.job_id')=? LIMIT 2",
        (world_id.hex, branch_id.hex, state_version, ended_utc, str(job_id)),
    ).fetchall()
    return len(ticks) == 2


def _db_uuid_matches(value: object, expected: UUID) -> bool:
    try:
        return UUID(str(value)) == expected
    except (ValueError, TypeError, AttributeError):
        return False


def _trace_has_usage(trace: Mapping[str, Any]) -> bool:
    for key in ("usage", "estimated_usage"):
        usage = trace.get(key)
        if isinstance(usage, dict):
            for amount in usage.values():
                if type(amount) is int and amount > 0:
                    return True
    cost = trace.get("cost")
    if isinstance(cost, dict):
        amount = cost.get("amount")
        if amount is not None and str(amount) not in {"0", "0.0", "0.00"}:
            return True
    return False


def _valid_seconds(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and float(value) >= 0)


def _parse_trace_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_database_time(value: object) -> datetime:
    """Parse a SQLite DateTime value and normalize naive SQLite times as UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value and value == value.strip():
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("invalid database timestamp") from None
    else:
        raise ValueError("invalid database timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _manifest_int(manifest: Mapping[str, Any], key: str) -> int:
    value = manifest.get(key)
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            value = None
    if type(value) is not int:
        raise SupervisorSafetyError("manifest_tick_invalid")
    return value


def _billing_retry_allowed(
    billing_state: BillingState,
    policy: RecoveryPolicy,
) -> bool:
    return billing_state == "unbilled" or policy.allow_possibly_billed_retry


def _history_for(history: Sequence[Mapping[str, Any]], job_id: UUID, failure_key: str) -> list[Mapping[str, Any]]:
    return [
        entry for entry in history
        if entry.get("event") == "attempt_begin"
        and entry.get("job_id") == str(job_id)
        and entry.get("failure_key") == failure_key
    ]


def _confirmed_model_host_deadline(snapshot: RunSnapshot, call: ModelCallSnapshot) -> bool:
    receipt = snapshot.model_host_deadline
    request_id = call.decision_request_id
    if (
        receipt is None or request_id is None or call.error_code != "cancelled" or call.result_available
        or receipt.get("request_id") != request_id
        or type(receipt.get("state_version")) is not int or receipt["state_version"] != snapshot.state_version
        or type(receipt.get("tick")) is not int or receipt["tick"] != snapshot.current_tick
        or snapshot.latest_call_boundary_cleared or snapshot.job_payload.get("pending_decision")
    ):
        return False
    last = snapshot.job_payload.get("last_decision")
    return not (isinstance(last, dict) and last.get("request_id") == request_id)


def assess_recovery(
    snapshot: RunSnapshot,
    attempt_history: Sequence[Mapping[str, Any]],
    now: datetime,
    policy: RecoveryPolicy,
    *,
    active_pids: Sequence[int] = (),
    inventory_uncertain: bool = False,
) -> RecoveryDecision:
    """Pure lifecycle, billing, transient-error, and retry-backoff decision."""
    if inventory_uncertain:
        return RecoveryDecision("refuse", "process_inventory_inconclusive")
    if active_pids:
        return RecoveryDecision("observe", "probe_process_active")

    writer_lease = snapshot.writer_lease
    if writer_lease is not None:
        if (
            writer_lease.owner not in {"target_job", "other_job"}
            or not isinstance(writer_lease.job_status, str)
        ):
            return RecoveryDecision("refuse", "branch_writer_lease_invalid")
        try:
            expires_at = _parse_database_time(writer_lease.expires_at)
            now_utc = _parse_database_time(now)
        except (TypeError, ValueError, OverflowError):
            return RecoveryDecision("refuse", "branch_writer_lease_invalid")
        if expires_at > now_utc:
            if writer_lease.job_status.casefold() != "running":
                return RecoveryDecision("refuse", "branch_writer_lease_inconsistent")
            if writer_lease.owner == "target_job":
                return RecoveryDecision("observe", "branch_writer_lease_active")
            if writer_lease.owner == "other_job":
                return RecoveryDecision("wait", "other_branch_writer_lease_active")
            return RecoveryDecision("refuse", "branch_writer_lease_invalid")

    status = snapshot.job_status.casefold()
    if status in {"succeeded", "failed", "cancelled"}:
        completed = status == "succeeded" and snapshot.current_tick >= snapshot.target_tick
        return RecoveryDecision("report", "job_completed" if completed else f"job_{status}")
    if status == "waiting_input":
        return RecoveryDecision("report", "job_waiting_input")
    if status == "paused":
        if snapshot.job_pause_code != "model_decision_failed":
            return RecoveryDecision("report", "paused_for_nonmodel_reason")
        if snapshot.decision_mode != "configured_local_model":
            return RecoveryDecision("report", "paused_model_mode_mismatch")
        call = snapshot.latest_model_call
        if call is None:
            return RecoveryDecision("report", "matching_model_failure_missing")
        confirmed_deadline = _confirmed_model_host_deadline(snapshot, call)
        error_code = "host_deadline" if confirmed_deadline else call.error_code
        if not confirmed_deadline and error_code not in TRANSIENT_ERROR_CODES:
            return RecoveryDecision("report", "model_failure_not_transient", error_code=error_code)
        if call.result_available:
            return RecoveryDecision("report", "successful_model_result_not_durable")
        if call.failure_at is None:
            return RecoveryDecision("report", "model_failure_timestamp_missing", error_code=error_code)
        failure_key = call.decision_request_id or call.request_id
        billing = call.billing_state
        if not _billing_retry_allowed(billing, policy):
            return RecoveryDecision(
                "wait", "possibly_billed_retry_requires_opt_in", failure_key=failure_key,
                failure_phase="model_decision", error_code=error_code, billing_state=billing,
                retry_after_seconds=call.retry_after_seconds,
            )
        failure_phase = "model_decision"
        retry_after = call.retry_after_seconds
        failure_at = call.failure_at
    elif status in {"queued", "running"}:
        if status == "running" and snapshot.decision_mode == "configured_local_model":
            payload = snapshot.job_payload
            pending = payload.get("pending_decision")
            pending_request_id = pending.get("request_id") if isinstance(pending, dict) else None
            last = payload.get("last_decision")
            last_request_id = last.get("request_id") if isinstance(last, dict) else None
            call = snapshot.latest_model_call
            if pending_request_id:
                failure_key = f"pending:{pending_request_id}"
                billing: BillingState = "unbilled"
                error_code = None
                retry_after = None
                failure_phase = "lease_recovery"
                failure_at = None
            elif (
                call is not None
                and call.decision_request_id is not None
                and (call.decision_request_id == last_request_id or snapshot.latest_call_boundary_cleared)
            ):
                # The exact prior semantic answer is durable and committed.
                failure_key = f"committed:{call.request_id}"
                billing = "unbilled"
                error_code = None
                retry_after = None
                failure_phase = "lease_recovery"
                failure_at = None
            elif call is not None and call.result_available:
                # The provider returned a result, but no durable job receipt
                # matches it. Re-drawing that semantic answer is forbidden.
                return RecoveryDecision("report", "successful_model_result_not_durable")
            elif call is not None and call.error_code not in TRANSIENT_ERROR_CODES:
                return RecoveryDecision(
                    "report", "model_failure_not_transient", error_code=call.error_code,
                )
            else:
                call = snapshot.latest_model_call
                failure_key = (call.decision_request_id or call.request_id) if call is not None else f"unknown:{snapshot.job_id}"
                billing = call.billing_state if call is not None else "unknown"
                if not _billing_retry_allowed(billing, policy):
                    return RecoveryDecision(
                        "wait", "possibly_billed_retry_requires_opt_in", failure_key=failure_key,
                        failure_phase="model_decision", error_code=call.error_code if call else None,
                        billing_state=billing,
                        retry_after_seconds=call.retry_after_seconds if call else None,
                    )
                error_code = call.error_code if call else None
                retry_after = call.retry_after_seconds if call else None
                failure_phase = "model_decision"
                failure_at = call.failure_at if call else None
        else:
            failure_key = f"runner-missing:{snapshot.job_id}"
            billing = "unbilled"
            error_code = None
            retry_after = None
            failure_phase = "lease_recovery"
            failure_at = None
    else:
        return RecoveryDecision("refuse", "job_status_unknown")

    prior_attempts = _history_for(attempt_history, snapshot.job_id, failure_key)
    attempt_index = len(prior_attempts) + 1
    exponent = min(attempt_index - 1, 62)
    backoff = min(policy.backoff_max_seconds, policy.backoff_initial_seconds * (2 ** exponent))
    delay = max(float(backoff), retry_after or 0.0)
    eligible_times: list[datetime] = []
    if failure_at is not None:
        eligible_times.append(failure_at + timedelta(seconds=delay))
    if prior_attempts:
        try:
            last_attempt_at = datetime.fromisoformat(str(prior_attempts[-1]["at_utc"]))
            if last_attempt_at.tzinfo is None:
                last_attempt_at = last_attempt_at.replace(tzinfo=timezone.utc)
            eligible_times.append(last_attempt_at.astimezone(timezone.utc) + timedelta(seconds=delay))
        except (KeyError, TypeError, ValueError, OverflowError):
            return RecoveryDecision("refuse", "attempt_history_invalid")
    if eligible_times:
        eligible_at = max(eligible_times)
        remaining = (eligible_at - now.astimezone(timezone.utc)).total_seconds()
        if remaining > 0:
            return RecoveryDecision(
                "wait", "retry_backoff", failure_key=failure_key, failure_phase=failure_phase,
                error_code=error_code, billing_state=billing,
                retry_after_seconds=retry_after, attempt_index=attempt_index,
                wait_seconds=remaining,
            )
    return RecoveryDecision(
        "spawn", "transient_model_failure_retry" if error_code else "missing_worker_recovery",
        failure_key=failure_key, failure_phase=failure_phase, error_code=error_code,
        billing_state=billing, retry_after_seconds=retry_after, attempt_index=attempt_index,
    )


def _normalized_directory(path: Path) -> Path:
    return path.expanduser().resolve()


def _process_matches_data_directory(
    process: ObservedProcess,
    data_directory: Path,
    probe_script: Path,
) -> bool:
    if process.uncertain:
        return True
    if not _argv_names_probe(process.argv, probe_script):
        return False
    process_directory = _argv_data_directory(process.argv)
    if process_directory is None:
        return True  # a probe candidate with unresolved argv must block restarts
    return process_directory == _normalized_directory(data_directory)


class PopenProbeSpawner:
    def __init__(self, probe_script: Path, backend_path: Path, project_root: Path) -> None:
        self.probe_script = probe_script.resolve()
        self.backend_path = backend_path.resolve()
        self.project_root = project_root.resolve()

    def spawn(self, request: SpawnRequest) -> int:
        env = dict(request.env)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            child = subprocess.Popen(
                list(request.argv),
                cwd=str(request.cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                shell=False,
                creationflags=creationflags,
            )
        except Exception:
            raise SupervisorSafetyError("probe_spawn_failed") from None
        return int(child.pid)


def build_spawn_request(
    snapshot: RunSnapshot,
    config: SupervisorConfig,
    *,
    probe_script: Path = PROBE_SCRIPT,
    resume_model_failure: bool = False,
    failure_key: str,
    attempt_index: int,
    manifest_evidence_path: Path,
) -> SpawnRequest:
    manifest_config = snapshot.config
    backend_path = config.backend_path.expanduser().resolve()
    required_frozen_modules = (
        backend_path / "world_engine" / "__init__.py",
        backend_path / "world_engine" / "simulation" / "engine.py",
        backend_path / "world_engine" / "storage" / "repository.py",
    )
    if not backend_path.is_dir() or any(not item.is_file() for item in required_frozen_modules):
        raise SupervisorSafetyError("frozen_backend_incomplete")
    try:
        pack = str(manifest_config["pack_id"])
        pack_version = snapshot.pack_version
        seed = int(manifest_config["seed"])
        years = int(manifest_config["declared_years"])
        days = int(manifest_config["declared_days"])
    except (KeyError, TypeError, ValueError):
        raise SupervisorSafetyError("manifest_config_incomplete") from None
    population_mode = manifest_config.get("population_mode", "natural")
    if population_mode not in {"natural", "sustained"}:
        raise SupervisorSafetyError("manifest_population_mode_invalid")
    decision_mode = snapshot.decision_mode
    evidence_index = int(manifest_evidence_path.name.split("-", 2)[1])
    bootstrap_evidence_path = manifest_evidence_path.with_name(
        f"attempt-{evidence_index:06d}-bootstrap.json"
    ).resolve()
    args = [
        str(sys.executable), "-c", BOOTSTRAP_CODE, str(probe_script.resolve()),
        "--pack", pack,
        "--pack-version", pack_version,
        "--seed", str(seed),
        "--population-mode", str(population_mode),
        "--decision-mode", str(decision_mode),
        "--model-data-directory", str(config.model_data_directory.expanduser().resolve()),
        "--backup-compressed-mib", str(config.backup_compressed_mib),
        "--backup-expanded-mib", str(config.backup_expanded_mib),
        "--years", str(years),
        "--days", str(days),
        "--data-directory", str(snapshot.directory.resolve()),
    ]
    if resume_model_failure:
        args.append("--resume-model-failure")
    if config.max_windows_per_invocation is not None:
        args.extend(("--max-windows", str(config.max_windows_per_invocation)))
    if config.lease_wait_seconds is not None:
        args.extend(("--lease-wait-seconds", str(config.lease_wait_seconds)))
    env = dict(os.environ)
    env["PYTHONPATH"] = str(backend_path)
    env["WORLD_ENGINE_FROZEN_BACKEND"] = str(backend_path)
    env["WORLD_ENGINE_SUPERVISOR_BOOTSTRAP_EVIDENCE"] = str(bootstrap_evidence_path)
    env["WORLD_ENGINE_SUPERVISOR_WORLD_ID"] = str(snapshot.world_id)
    env["WORLD_ENGINE_SUPERVISOR_BRANCH_ID"] = str(snapshot.branch_id)
    env["WORLD_ENGINE_SUPERVISOR_JOB_ID"] = str(snapshot.job_id)
    env["WORLD_ENGINE_SUPERVISOR_DATA_DIRECTORY"] = str(snapshot.directory.resolve())
    return SpawnRequest(
        argv=tuple(args),
        cwd=config.project_root.resolve(),
        env=env,
        data_directory=snapshot.directory.resolve(),
        job_id=snapshot.job_id,
        failure_key=failure_key,
        resume_model_failure=resume_model_failure,
        attempt_index=attempt_index,
        bootstrap_evidence_path=bootstrap_evidence_path,
    )


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    data_directory: str
    world_id: str | None
    branch_id: str | None
    job_id: str | None
    job_status: str | None
    current_tick: int | None
    target_tick: int | None
    state_version: int | None
    ruleset_version: str | None
    pack_version: str | None
    error_phase: str | None
    error_code: str | None
    billing_state: BillingState
    recovery_action: str
    recovery_reason: str
    attempt_index: int
    wait_seconds: float
    pids: tuple[int, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "data_directory": self.data_directory,
            "world_id": self.world_id,
            "branch_id": self.branch_id,
            "job_id": self.job_id,
            "job_status": self.job_status,
            "current_tick": self.current_tick,
            "target_tick": self.target_tick,
            "state_version": self.state_version,
            "ruleset_version": self.ruleset_version,
            "pack_version": self.pack_version,
            "error_phase": self.error_phase,
            "error_code": self.error_code,
            "billing_state": self.billing_state,
            "recovery_action": self.recovery_action,
            "recovery_reason": self.recovery_reason,
            "attempt_index": self.attempt_index,
            "wait_seconds": round(self.wait_seconds, 3),
            "pids": list(self.pids),
        }


class _SystemDependencies:
    def __init__(self, config: SupervisorConfig) -> None:
        self.clock: Clock = SystemClock()
        self.inventory: ProcessInventory = WindowsProcessInventory()
        self.inspector: ReadOnlyRunInspector = SQLiteReadOnlyInspector()
        self.spawner: ProbeSpawner = PopenProbeSpawner(
            config.project_root / "scripts" / "probe-world-advance.py",
            config.backend_path,
            config.project_root,
        )
        self.project_root = config.project_root.resolve()
        self.probe_script = (config.project_root / "scripts" / "probe-world-advance.py").resolve()


class SupervisorDependencies(Protocol):
    clock: Clock
    inventory: ProcessInventory
    inspector: ReadOnlyRunInspector
    spawner: ProbeSpawner
    project_root: Path
    probe_script: Path


def supervise_once(
    data_directory: Path,
    config: SupervisorConfig,
    dependencies: SupervisorDependencies,
) -> SupervisorResult:
    directory = _normalized_directory(data_directory)
    result_base = {
        "data_directory": str(directory), "world_id": None, "branch_id": None, "job_id": None,
        "job_status": None, "current_tick": None, "target_tick": None, "state_version": None,
        "ruleset_version": None, "pack_version": None, "error_phase": None,
        "error_code": None, "billing_state": "unknown", "attempt_index": 0,
        "wait_seconds": 0.0, "pids": (),
    }
    try:
        run_lock = nullcontext(True) if config.inspect_only else _directory_supervisor_lock(
            dependencies.project_root / ".runtime" / "capacity-supervisor" / "locks" /
            (_directory_key(directory) + ".lock")
        )
        with run_lock as acquired:
            if not acquired:
                return SupervisorResult(**result_base, recovery_action="observe", recovery_reason="supervisor_already_running")
            try:
                processes = tuple(dependencies.inventory.list_probe_processes(dependencies.probe_script))
            except Exception as exc:
                code = exc.code if isinstance(exc, SupervisorSafetyError) else "process_inventory_failed"
                return SupervisorResult(**(result_base | {"error_code": code}), recovery_action="refuse", recovery_reason=code)
            try:
                snapshot = dependencies.inspector.inspect(directory, config.model_data_directory)
            except Exception as exc:
                code = exc.code if isinstance(exc, SupervisorSafetyError) else "run_validation_failed"
                if not config.inspect_only:
                    _try_audit(dependencies.project_root, directory, {
                        "event": "observation", "at_utc": _utc(dependencies.clock.now()),
                        "recovery_action": "refuse", "reason_code": code,
                    })
                return SupervisorResult(**(result_base | {"error_code": code}), recovery_action="refuse", recovery_reason=code)

            matching_processes = [
                process for process in processes
                if _process_matches_data_directory(process, directory, dependencies.probe_script)
            ]
            uncertain = any(item.uncertain for item in matching_processes)
            pids = tuple(sorted(item.pid for item in matching_processes if item.pid > 0))
            history_path = _audit_path(dependencies.project_root, directory)
            try:
                history = _read_audit_history(history_path)
            except SupervisorSafetyError as exc:
                code = exc.code
                decision = RecoveryDecision("refuse", code)
            else:
                policy = RecoveryPolicy(
                    config.backoff_initial_seconds,
                    config.backoff_max_seconds,
                    config.allow_possibly_billed_retry,
                )
                decision = assess_recovery(
                    snapshot, history, dependencies.clock.now(), policy,
                    active_pids=pids, inventory_uncertain=uncertain,
                )
            result_fields = result_base | {
                "world_id": str(snapshot.world_id), "branch_id": str(snapshot.branch_id),
                "job_id": str(snapshot.job_id), "job_status": snapshot.job_status,
                "current_tick": snapshot.current_tick, "target_tick": snapshot.target_tick,
                "state_version": snapshot.state_version, "ruleset_version": snapshot.ruleset_version,
                "pack_version": snapshot.pack_version, "error_phase": decision.failure_phase,
                "error_code": decision.error_code, "billing_state": decision.billing_state,
                "attempt_index": decision.attempt_index, "wait_seconds": decision.wait_seconds,
                "pids": pids,
            }
            if decision.action != "spawn":
                if not config.inspect_only:
                    _try_audit(dependencies.project_root, directory, {
                        "event": "observation", "at_utc": _utc(dependencies.clock.now()),
                        "job_id": str(snapshot.job_id), "job_status": snapshot.job_status,
                        "current_tick": snapshot.current_tick, "target_tick": snapshot.target_tick,
                        "state_version": snapshot.state_version, "ruleset_version": snapshot.ruleset_version,
                        "pack_version": snapshot.pack_version, "error_phase": decision.failure_phase,
                        "error_code": decision.error_code, "billing_state": decision.billing_state,
                        "recovery_action": decision.action, "reason_code": decision.reason_code,
                        "attempt_index": decision.attempt_index, "wait_seconds": decision.wait_seconds,
                        "pids": list(pids),
                    })
                return SupervisorResult(**result_fields, recovery_action=decision.action, recovery_reason=decision.reason_code)

            if config.inspect_only:
                return SupervisorResult(**result_fields, recovery_action="report", recovery_reason="inspection_only_would_spawn")

            try:
                manifest_copy = _write_manifest_evidence(
                    dependencies.project_root, directory, snapshot, decision.attempt_index,
                )
                _append_audit(history_path, {
                    "event": "attempt_begin", "at_utc": _utc(dependencies.clock.now()),
                    "job_id": str(snapshot.job_id), "world_id": str(snapshot.world_id),
                    "branch_id": str(snapshot.branch_id), "failure_key": decision.failure_key,
                    "attempt_index": decision.attempt_index, "error_phase": decision.failure_phase,
                    "error_code": decision.error_code, "billing_state": decision.billing_state,
                    "possibly_billed_retry_authorized": (
                        decision.billing_state != "unbilled" and config.allow_possibly_billed_retry
                    ),
                    "manifest_evidence": str(manifest_copy.relative_to(dependencies.project_root)),
                    "manifest_sha256": snapshot.manifest_sha256,
                })
                request = build_spawn_request(
                    snapshot, config, probe_script=dependencies.probe_script,
                    resume_model_failure=(snapshot.job_status == "paused"),
                    failure_key=decision.failure_key or f"runner-missing:{snapshot.job_id}",
                    attempt_index=decision.attempt_index,
                    manifest_evidence_path=manifest_copy,
                )
                pid = dependencies.spawner.spawn(request)
                _append_audit(history_path, {
                    "event": "spawn_started", "at_utc": _utc(dependencies.clock.now()),
                    "job_id": str(snapshot.job_id), "failure_key": decision.failure_key,
                    "attempt_index": decision.attempt_index, "pid": pid,
                })
            except SupervisorSafetyError as exc:
                code = exc.code
                _try_audit(dependencies.project_root, directory, {
                    "event": "spawn_failed", "at_utc": _utc(dependencies.clock.now()),
                    "job_id": str(snapshot.job_id), "failure_key": decision.failure_key,
                    "attempt_index": decision.attempt_index, "reason_code": code,
                })
                return SupervisorResult(**(result_fields | {"error_code": code}), recovery_action="refuse", recovery_reason=code)
            except Exception:
                code = "supervisor_audit_or_spawn_failed"
                _try_audit(dependencies.project_root, directory, {
                    "event": "spawn_failed", "at_utc": _utc(dependencies.clock.now()),
                    "job_id": str(snapshot.job_id), "failure_key": decision.failure_key,
                    "attempt_index": decision.attempt_index, "reason_code": code,
                })
                return SupervisorResult(**(result_fields | {"error_code": code}), recovery_action="refuse", recovery_reason=code)
            return SupervisorResult(**(result_fields | {"pids": (pid,)}), recovery_action="spawn", recovery_reason=decision.reason_code)
    except Exception:
        code = "supervisor_internal_error"
        return SupervisorResult(**(result_base | {"error_code": code}), recovery_action="refuse", recovery_reason=code)


def _directory_supervisor_lock(path: Path):
    """Nonblocking cross-process lock in .runtime, never in a world directory."""
    return _FileLock(path)


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                if self.handle.read(1) == b"":
                    self.handle.seek(0)
                    self.handle.write(b"\0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except (OSError, BlockingIOError):
            self.handle.close()
            self.handle = None
        return self.acquired

    def __exit__(self, *_exc):
        if self.handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
        return False


def _directory_key(directory: Path) -> str:
    return hashlib.sha256(str(_normalized_directory(directory)).encode("utf-8")).hexdigest()[:24]


def _audit_path(project_root: Path, directory: Path) -> Path:
    return project_root / ".runtime" / "capacity-supervisor" / "logs" / f"{_directory_key(directory)}.jsonl"


def _read_audit_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError
            entries.append(item)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise SupervisorSafetyError("audit_history_invalid") from None
    return entries


def _append_audit(path: Path, item: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(item), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _try_audit(project_root: Path, directory: Path, item: Mapping[str, Any]) -> None:
    try:
        _append_audit(_audit_path(project_root, directory), item)
    except Exception:
        # Reporting remains safe when the append-only audit target is unavailable.
        pass


def _write_manifest_evidence(
    project_root: Path,
    directory: Path,
    snapshot: RunSnapshot,
    attempt_index: int,
) -> Path:
    evidence_dir = project_root / ".runtime" / "capacity-supervisor" / "evidence" / _directory_key(directory)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    index = max(1, attempt_index)
    while True:
        destination = evidence_dir / f"attempt-{index:06d}-manifest.json"
        try:
            with destination.open("xb") as handle:
                handle.write(snapshot.manifest_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            return destination
        except FileExistsError:
            index += 1


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _load_cli_config(argv: Sequence[str] | None = None) -> SupervisorConfig:
    parser = argparse.ArgumentParser(description="Supervise only existing leased capacity probe runs.")
    parser.add_argument("--data-directory", type=Path, action="append", required=True,
                        help="existing probe data directory; repeat for multiple runs")
    parser.add_argument("--backend-path", type=Path, required=True,
                        help="explicit frozen backend directory containing world_engine")
    parser.add_argument("--model-data-directory", type=Path, required=True,
                        help="explicit local model settings and operations directory")
    parser.add_argument("--poll-interval-seconds", type=int, default=30)
    parser.add_argument("--backoff-initial-seconds", type=int, default=60)
    parser.add_argument("--backoff-max-seconds", type=int, default=1800)
    parser.add_argument("--backup-compressed-mib", type=int, default=512,
                        help="maximum compressed archive size passed to the probe (1..65536 MiB)")
    parser.add_argument("--backup-expanded-mib", type=int, default=4096,
                        help="maximum expanded archive size passed to the probe (1..262144 MiB)")
    parser.add_argument("--allow-possibly-billed-retry", action="store_true", default=False,
                        help="explicitly authorize retrying ambiguous or possibly billed transient failures")
    parser.add_argument("--inspect", action="store_true",
                        help="read-only one-pass dry run; report actions without logging, copying, or spawning")
    parser.add_argument("--once", action="store_true",
                        help="run one supervisor pass and exit")
    parser.add_argument("--max-windows-per-invocation", type=int,
                        help="optional exact worker bound per recovery invocation")
    parser.add_argument("--lease-wait-seconds", type=int,
                        help="optional explicit probe lease wait limit")
    args = parser.parse_args(argv)
    return SupervisorConfig(
        data_directories=tuple(args.data_directory),
        backend_path=args.backend_path,
        model_data_directory=args.model_data_directory,
        poll_interval_seconds=args.poll_interval_seconds,
        backoff_initial_seconds=args.backoff_initial_seconds,
        backoff_max_seconds=args.backoff_max_seconds,
        backup_compressed_mib=args.backup_compressed_mib,
        backup_expanded_mib=args.backup_expanded_mib,
        allow_possibly_billed_retry=args.allow_possibly_billed_retry,
        max_windows_per_invocation=args.max_windows_per_invocation,
        lease_wait_seconds=args.lease_wait_seconds,
        inspect_only=args.inspect,
        once=args.once or args.inspect,
    )


def run_loop(
    config: SupervisorConfig,
    dependencies: SupervisorDependencies,
    *,
    max_iterations: int | None = None,
) -> int:
    if max_iterations is None and config.once:
        max_iterations = 1
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        for directory in config.data_directories:
            result = supervise_once(directory, config, dependencies)
            print(json.dumps(result.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)
        iteration += 1
        if max_iterations is None or iteration < max_iterations:
            dependencies.clock.sleep(config.poll_interval_seconds)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = _load_cli_config(argv)
    except (SystemExit, ValueError):
        raise
    dependencies = _SystemDependencies(config)
    try:
        return run_loop(config, dependencies)
    except KeyboardInterrupt:
        # Spawned probes use CREATE_NO_WINDOW and are intentionally left alive.
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
