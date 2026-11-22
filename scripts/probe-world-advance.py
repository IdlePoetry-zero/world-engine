"""Run a resumable world-advance capacity probe.

This uses the real content catalog, SQLite repository, leased SimulationJobs,
and simulation engine. The default decision mode stops at a model-owned
decision. An explicit configured-local-model mode uses the project's local
operational profile for decisions and metering while keeping the probe's world
database in its dedicated data directory.

New probe worlds pass the requested seed and population mode into the content
catalog. Existing manifests created before native catalog seeding retain their
legacy unseeded draft and exact saved configuration when resumed.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from tempfile import SpooledTemporaryFile
import time
from typing import Any, Iterator
from uuid import UUID, uuid4
from zipfile import ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.engine import URL  # noqa: E402

from world_engine.adapters.credentials import WindowsCredentialStore  # noqa: E402
from world_engine.adapters.models.contracts import RoleTask  # noqa: E402
from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode  # noqa: E402
from world_engine.adapters.models.gateway import ModelGateway  # noqa: E402
from world_engine.adapters.models.secrets import CompositeSecretResolver, EnvironmentSecretResolver  # noqa: E402
from world_engine.application.backups import BackupApplication, BackupArchiveError, BackupLimits, DEFAULT_LIMITS  # noqa: E402
from world_engine.application.credentials import AccountCredentialResolver, CredentialService  # noqa: E402
from world_engine.application.model_decisions import (  # noqa: E402
    MODEL_DECISION_TIMEOUT_SECONDS,
    invoke_model_decision,
)
from world_engine.application.operations import ModelOperations, OperationsStore  # noqa: E402
from world_engine.application.pricing import bundled_price  # noqa: E402
from world_engine.application.simulation_jobs import SimulationJobs  # noqa: E402
from world_engine.content import ContentCatalog  # noqa: E402
from world_engine.domain.components import GovernanceComponent, LifeComponent, PopulationComponent  # noqa: E402
from world_engine.domain.errors import Conflict, InvalidLease  # noqa: E402
from world_engine.domain.models import AccessScope, JobRecord, JobStatus, NewWorld, WorldState  # noqa: E402
from world_engine.simulation import WorldAdvancePlan  # noqa: E402
from world_engine.storage.database import Database  # noqa: E402
from world_engine.storage.repository import WorldRepository  # noqa: E402


MANIFEST_NAME = ".probe-world-advance.json"
MAINTENANCE_REQUEST_NAME = ".probe-world-advance-maintenance.json"
MAINTENANCE_PAUSE = "storage_maintenance"
IDENTITY_NAME = "local-identity.json"
JOB_TYPE = "simulation.world_advance"
MANIFEST_SCHEMA = 1
INT64_MAX = (1 << 63) - 1
INT64_MIN = -(1 << 63)
LEASE_SECONDS = 120
MIB = 1024 * 1024
DECISION_MODES = ("deterministic_no_model", "configured_local_model")
MODEL_DECISION_RETRY_BACKOFF_SECONDS = (2, 4)
DURATION_CONFIG_KEYS = frozenset({
    "declared_years", "declared_days", "total_days", "normalized_years",
    "partial_year_days", "total_seconds",
})


class ProbeModelDeadline(TimeoutError):
    """The probe's own timer expired during a model decision."""


async def _invoke_probe_model_decision(operations, repository, scope, lease, job, decision):
    """Dispatch NPC planning, reflection and ordinary model-owned choices."""
    if decision.kind == "npc.plan_review":
        # Kept lazy for probes resuming under older frozen PYTHONPATH releases.
        from world_engine.application.model_decisions import invoke_npc_planning

        return await invoke_npc_planning(operations, repository, scope, lease, job, decision)
    if decision.kind == "npc.reflection":
        from world_engine.application.model_decisions import invoke_npc_reflection

        return await invoke_npc_reflection(operations, repository, scope, lease, job, decision)
    # Monitor our own timer so recovery can distinguish its expiry from an
    # external cancellation, including when the model adapter is frozen.
    deadline = asyncio.timeout(MODEL_DECISION_TIMEOUT_SECONDS)
    try:
        async with deadline:
            if decision.kind == "organization.vacancy_vote":
                from world_engine.application.autonomous_vacancy_decisions import invoke_autonomous_vacancy_vote

                return await invoke_autonomous_vacancy_vote(operations, repository, scope, lease, job, decision)
            return await invoke_model_decision(
                operations, scope, lease, job, decision,
                timeout_seconds=MODEL_DECISION_TIMEOUT_SECONDS,
            )
    except TimeoutError as error:
        if deadline.expired():
            raise ProbeModelDeadline("The world decision exceeded its local deadline.") from error
        raise


class ProbeError(RuntimeError):
    """A configuration or persistence error safe to show to the user."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _maintenance_requested(path: Path, job_id: UUID) -> bool:
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    if request != {"schema": 1, "job_id": str(job_id)}:
        raise ProbeError("The storage maintenance request does not identify this capacity job.")
    return True


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _directory_lock(path: Path) -> Iterator[None]:
    """Hold an OS lock so two probe processes cannot claim the same job."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ProbeError("Another probe process is using this data directory.") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ProbeError("Another probe process is using this data directory.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _load_or_create_scope(directory: Path) -> AccessScope:
    identity_path = directory / IDENTITY_NAME
    if not identity_path.exists():
        _atomic_json(identity_path, {"tenant_id": str(uuid4()), "user_id": str(uuid4())})
    try:
        return AccessScope.model_validate_json(identity_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ProbeError("The persistent local identity file is invalid; refusing to replace it.") from exc


def _configured_model_operations(directory: Path) -> tuple[ModelOperations, AccessScope]:
    """Use the selected local operational profile, separate from probe worlds."""
    model_directory = directory.expanduser().resolve()
    identity_path = model_directory / IDENTITY_NAME
    operations_path = model_directory / "operations.db"
    if not identity_path.is_file() or not operations_path.is_file():
        raise ProbeError(
            "configured_local_model requires an initialized local operational profile "
            "(local identity and operations database)."
        )
    try:
        model_scope = AccessScope.model_validate_json(identity_path.read_text(encoding="utf-8"))
        store = OperationsStore(operations_path)
        settings = store.settings(model_scope)
    except Exception:
        raise ProbeError("The configured local model profile could not be opened safely.") from None

    route = settings.get("routes", {}).get(RoleTask.NPC_DECISION.value, {})
    provider_id = route.get("provider_id") if isinstance(route, dict) else None
    if not provider_id:
        raise ProbeError(
            "configured_local_model requires an enabled NPC decision route in the local model settings."
        )
    try:
        provider = store.provider(model_scope, provider_id)
    except Exception:
        raise ProbeError("The configured NPC decision provider is unavailable.") from None
    if not provider.get("enabled"):
        raise ProbeError("The configured NPC decision provider is disabled.")

    credential_service = CredentialService(store, WindowsCredentialStore())
    gateway = ModelGateway(
        secret_resolver=CompositeSecretResolver(
            (
                EnvironmentSecretResolver({"WORLD_ENGINE_DEEPSEEK_API_KEY"}),
                AccountCredentialResolver(credential_service, model_scope),
            )
        ),
        price_resolver=bundled_price,
    )
    # ModelOperations persists call metering to operations.db. The world
    # repository below remains scoped exclusively to the probe data directory.
    return ModelOperations(store, gateway), model_scope


def _migrate(database_url: str) -> None:
    config = Config(str(PROJECT_ROOT / "backend" / "migrations" / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def _run_config(
    args: argparse.Namespace,
    pack_version: str,
    *,
    year_days: int,
    day_seconds: int,
    catalog_seeded: bool,
) -> dict[str, Any]:
    decision_mode = getattr(args, "decision_mode", "deterministic_no_model")
    if decision_mode not in DECISION_MODES:
        raise ProbeError("Unsupported model decision mode.")
    total_days = args.years * year_days + args.days
    total_seconds = total_days * day_seconds
    if total_days <= 0 or total_seconds <= 0:
        raise ProbeError("The combined years and days must be a positive duration.")
    if total_seconds > INT64_MAX:
        raise ProbeError("The requested exact target exceeds the supported signed 64-bit tick range.")
    normalized_years, partial_year_days = divmod(total_days, year_days)
    return {
        "pack_id": args.pack,
        "pack_version": pack_version,
        "seed": args.seed,
        "declared_years": args.years,
        "declared_days": args.days,
        "total_days": total_days,
        "normalized_years": normalized_years,
        "partial_year_days": partial_year_days,
        "day_seconds": day_seconds,
        "year_days": year_days,
        "total_seconds": total_seconds,
        "decision_mode": decision_mode,
        "population_mode": args.population_mode,
        "catalog_seeded": catalog_seeded,
        "seed_semantics": (
            "The explicit signed 64-bit seed is passed to the catalog and determines generated initial state."
            if catalog_seeded else
            "Legacy resume: the saved run predates native catalog seeding; the requested value only labels the draft."
        ),
    }


def _existing_manifest(directory: Path) -> dict[str, Any] | None:
    path = directory / MANIFEST_NAME
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeError("The existing probe manifest is unreadable; refusing to replace it.") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ProbeError("The existing probe manifest uses an unsupported schema.")
    config = manifest.get("config")
    if not isinstance(config, dict):
        raise ProbeError("The existing probe manifest has no valid configuration.")
    return manifest


def _saved_pack_version(manifest: dict[str, Any] | None) -> str | None:
    """Return the manifest's exact pack version, including the legacy 1.7 default."""
    if manifest is None:
        return None
    config = manifest["config"]
    is_legacy_unseeded = "population_mode" not in config and "catalog_seeded" not in config
    if "pack_version" not in config:
        if is_legacy_unseeded:
            return "1.7.0"
        raise ProbeError("The saved probe manifest is missing its content pack version.")
    version = config["pack_version"]
    if not isinstance(version, str) or not version.strip():
        raise ProbeError("The saved probe manifest has an invalid content pack version.")
    if is_legacy_unseeded and version != "1.7.0":
        raise ProbeError("A legacy unseeded probe must retain content pack version 1.7.0.")
    return version


def _legacy_config_for_resume(
    manifest: dict[str, Any] | None,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    if manifest is None:
        return None
    config = manifest["config"]
    # Before native seed/mode support, the runner always created the natural,
    # unseeded catalog world while putting the requested seed in its title.
    if "population_mode" in config or "catalog_seeded" in config:
        return None
    expected = {
        "pack_id": args.pack,
        "seed": args.seed,
        "declared_years": args.years,
        "declared_days": args.days,
    }
    if any(config.get(key) != value for key, value in expected.items()):
        raise ProbeError("The saved legacy probe parameters do not match this invocation.")
    if args.population_mode != "natural":
        raise ProbeError("A legacy probe cannot be resumed with sustained population mode.")
    requested_pack_version = getattr(args, "pack_version", None)
    if requested_pack_version not in (None, "1.7.0"):
        raise ProbeError("A legacy unseeded probe can only resume with content pack version 1.7.0.")
    requested_decision_mode = getattr(args, "decision_mode", "deterministic_no_model")
    if config.get("decision_mode", "deterministic_no_model") != requested_decision_mode:
        raise ProbeError("A legacy probe cannot change its decision mode when resumed.")
    return config


def _manifest_for(
    directory: Path,
    config: dict[str, Any],
    draft: NewWorld,
    *,
    allow_world_id_drift: bool = False,
) -> tuple[dict[str, Any], Path]:
    manifest_path = directory / MANIFEST_NAME
    config_hash = hashlib.sha256(_canonical_json(config).encode("utf-8")).hexdigest()
    idempotency_key = f"capacity-probe:{config_hash}"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProbeError("The existing probe manifest is unreadable; refusing to overwrite it.") from exc
        if manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("config_hash") != config_hash:
            raise ProbeError("This data directory belongs to a different probe configuration.")
        if not allow_world_id_drift and (
            manifest.get("world_id") != str(draft.world_id)
            or manifest.get("branch_id") != str(draft.branch_id)
        ):
            raise ProbeError("The stored probe world IDs do not match the requested catalog draft.")
        if manifest.get("idempotency_key") != idempotency_key:
            raise ProbeError("The stored job key does not match the requested probe.")
        if manifest.get("world_created") and not (directory / "world-engine.db").exists():
            raise ProbeError("The probe database is missing although the manifest records a created world.")
        return manifest, manifest_path

    database_path = directory / "world-engine.db"
    if database_path.exists():
        raise ProbeError(
            f"{database_path} exists without a probe manifest. Choose a fresh data directory; "
            "the runner will not take over an existing world database."
        )
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "config_hash": config_hash,
        "config": config,
        "world_id": str(draft.world_id),
        "branch_id": str(draft.branch_id),
        "job_id": None,
        "idempotency_key": idempotency_key,
        "status": "initializing",
        "checkpoints": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json(manifest_path, manifest)
    return manifest, manifest_path


def _config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(config).encode("utf-8")).hexdigest()


def _terminal_checkpoint(manifest: dict[str, Any], *, target_tick: int) -> dict[str, Any]:
    checkpoints = [
        item for item in manifest.get("checkpoints", [])
        if isinstance(item, dict) and item.get("label") == "target"
        and item.get("tick") == str(target_tick)
    ]
    if len(checkpoints) != 1:
        raise ProbeError("The completed predecessor has no unique exact target checkpoint.")
    pin = checkpoints[0]
    try:
        sequence = int(pin["sequence"])
        state_version = int(pin["state_version"])
        state_hash_value = pin["state_hash"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbeError("The predecessor target checkpoint is malformed.") from exc
    if sequence < 0 or state_version < 0 or not isinstance(state_hash_value, str) or len(state_hash_value) != 64:
        raise ProbeError("The predecessor target checkpoint is malformed.")
    return {
        "label": "target",
        "scheduled_year": None,
        "tick": str(target_tick),
        "sequence": str(sequence),
        "state_version": str(state_version),
        "state_hash": state_hash_value,
    }


def _completed_predecessor_replay_anchor(
    manifest: dict[str, Any],
    *,
    genesis_tick: int,
    terminal_pin: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the latest structurally valid decade pin before the terminal pin.

    Endpoint decade pins are valid historical records, but they cannot anchor
    the terminal audit because an anchor must precede it in both tick and
    sequence. The repository's checkpoint replay validates the selected pin
    against its persisted snapshot and event history.
    """
    checkpoints = manifest.get("checkpoints")
    config = manifest.get("config")
    if not isinstance(checkpoints, list) or not isinstance(config, dict):
        raise ProbeError("The predecessor checkpoint list or configuration is malformed.")
    for key in ("normalized_years", "year_days", "day_seconds"):
        value = config.get(key)
        if type(value) is not int or value < 0 or key != "normalized_years" and value <= 0:
            raise ProbeError("The predecessor checkpoint calendar configuration is malformed.")

    terminal_tick = int(terminal_pin["tick"])
    terminal_sequence = int(terminal_pin["sequence"])
    terminal_version = int(terminal_pin["state_version"])
    terminal_hash = terminal_pin["state_hash"]
    if not isinstance(terminal_hash, str) or re.fullmatch(r"[0-9a-f]{64}", terminal_hash) is None:
        raise ProbeError("The predecessor terminal checkpoint hash is malformed.")

    pins_by_year: dict[int, dict[str, Any]] = {}
    for candidate in checkpoints:
        if not isinstance(candidate, dict) or candidate.get("label") != "decade":
            continue
        year = candidate.get("scheduled_year")
        if type(year) is not int or year <= 0 or year > config["normalized_years"]:
            raise ProbeError("A predecessor decade checkpoint has an invalid scheduled year.")
        if year in pins_by_year:
            raise ProbeError("The predecessor has duplicate decade checkpoints for one scheduled year.")
        expected_tick = genesis_tick + year * config["year_days"] * config["day_seconds"]
        try:
            tick_value = candidate["tick"]
            sequence_value = candidate["sequence"]
            version_value = candidate["state_version"]
            pin_hash = candidate["state_hash"]
            tick = int(tick_value)
            sequence = int(sequence_value)
            version = int(version_value)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProbeError("A predecessor decade checkpoint is malformed.") from exc
        if (
            not isinstance(tick_value, str) or str(tick) != tick_value
            or tick != expected_tick
            or not isinstance(sequence_value, str) or str(sequence) != sequence_value or sequence <= 0
            or not isinstance(version_value, str) or str(version) != version_value or version <= 0
            or not isinstance(pin_hash, str) or re.fullmatch(r"[0-9a-f]{64}", pin_hash) is None
        ):
            raise ProbeError("A predecessor decade checkpoint is malformed or does not match its calendar tick.")

        if tick > terminal_tick:
            raise ProbeError("A predecessor decade checkpoint is not ordered before the terminal pin.")
        if tick == terminal_tick:
            if (
                sequence > terminal_sequence or version > terminal_version
                or sequence == terminal_sequence and version != terminal_version
                or version == terminal_version and sequence != terminal_sequence
                or sequence == terminal_sequence and pin_hash != terminal_hash
            ):
                raise ProbeError("The predecessor endpoint decade checkpoint differs from its target pin.")
        elif sequence >= terminal_sequence or version >= terminal_version:
            raise ProbeError("A predecessor decade checkpoint is not ordered before the terminal pin.")
        pins_by_year[year] = {
            "label": "decade",
            "scheduled_year": year,
            "tick": tick_value,
            "sequence": sequence_value,
            "state_version": version_value,
            "state_hash": pin_hash,
        }

    ordered = [pins_by_year[year] for year in sorted(pins_by_year)]
    for earlier, later in zip(ordered, ordered[1:]):
        if (
            int(earlier["tick"]) >= int(later["tick"])
            or int(earlier["sequence"]) >= int(later["sequence"])
            or int(earlier["state_version"]) >= int(later["state_version"])
        ):
            raise ProbeError("The predecessor decade checkpoints are not strictly ordered.")
    prior = [
        pin for pin in ordered
        if int(pin["tick"]) < terminal_tick and int(pin["sequence"]) < terminal_sequence
    ]
    return prior[-1] if prior else None


def _duration_only_extension(old: dict[str, Any], new: dict[str, Any]) -> bool:
    if set(old) != set(new):
        return False
    if any(old.get(key) != new.get(key) for key in set(old) - DURATION_CONFIG_KEYS):
        return False
    try:
        return (
            type(old.get("total_seconds")) is int
            and type(new.get("total_seconds")) is int
            and new["total_seconds"] > old["total_seconds"]
        )
    except (KeyError, TypeError):
        return False


def _preserves_checkpoints(current: Any, predecessor: Any) -> bool:
    """Require every predecessor pin to remain unchanged while allowing new pins."""
    if not isinstance(current, list) or not isinstance(predecessor, list):
        return False
    current_index = 0
    for pin in predecessor:
        while current_index < len(current) and current[current_index] != pin:
            current_index += 1
        if current_index == len(current):
            return False
        current_index += 1
    return True


def _archive_completed_manifest(directory: Path, manifest: dict[str, Any]) -> str:
    """Atomically preserve one predecessor manifest, without replacing an archive."""
    archive_name = f".probe-world-advance.completed.{manifest['config_hash']}.json"
    archive_path = directory / archive_name
    source = (directory / MANIFEST_NAME).read_bytes()
    if archive_path.exists():
        if archive_path.read_bytes() != source:
            raise ProbeError("The immutable predecessor manifest archive conflicts with the saved manifest.")
        return archive_name

    temporary = archive_path.with_name(f".{archive_path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(source)
            handle.flush()
            os.fsync(handle.fileno())
        # Linking publishes the complete archive atomically and refuses to
        # replace a file that appeared after the initial existence check.
        try:
            os.link(temporary, archive_path)
        except FileExistsError:
            if archive_path.read_bytes() != source:
                raise ProbeError("The immutable predecessor manifest archive conflicts with the saved manifest.")
        except OSError as exc:
            if not archive_path.exists() or archive_path.read_bytes() != source:
                raise ProbeError("The predecessor manifest archive could not be written safely.") from exc
        return archive_name
    finally:
        temporary.unlink(missing_ok=True)


def _verify_stored_world_advance_job(job: JobRecord, *, world_id: UUID, branch_id: UUID,
                                    job_id: str, started_tick: int,
                                    target_tick: int, require_succeeded: bool) -> None:
    expected_status = JobStatus.SUCCEEDED if require_succeeded else None
    if (
        str(job.job_id) != job_id
        or job.world_id != world_id or job.branch_id != branch_id
        or job.job_type != JOB_TYPE
        or expected_status is not None and job.status != expected_status
    ):
        raise ProbeError("The stored predecessor job does not match the completed probe manifest.")
    plan = job.payload.get("plan")
    if not isinstance(plan, dict):
        raise ProbeError("The stored capacity job has no valid world-advance plan.")
    if (plan.get("kind") != "world_advance" or plan.get("target_tick") != target_tick
            or plan.get("started_tick") != started_tick):
        raise ProbeError("The stored capacity job plan does not match its manifest ticks.")
    if require_succeeded and job.committed_through_tick != target_tick:
        raise ProbeError("The predecessor job did not commit through its exact target tick.")


def _validate_completed_predecessor(
    manifest: dict[str, Any],
    new_config: dict[str, Any],
    *,
    repository: WorldRepository,
    scope: AccessScope,
    world_id: UUID,
    branch_id: UUID,
    database: Database,
) -> tuple[int, dict[str, Any]]:
    old_config = manifest.get("config")
    if not isinstance(old_config, dict) or not _duration_only_extension(old_config, new_config):
        raise ProbeError("--continue-completed may only increase the saved probe duration.")
    old_hash = _config_hash(old_config)
    expected_key = f"capacity-probe:{old_hash}"
    if manifest.get("config_hash") != old_hash or manifest.get("idempotency_key") != expected_key:
        raise ProbeError("The predecessor manifest configuration or idempotency key is invalid.")
    if manifest.get("status") != JobStatus.SUCCEEDED.value or manifest.get("last_job_status") != JobStatus.SUCCEEDED.value:
        raise ProbeError("--continue-completed requires an audited succeeded probe.")
    if manifest.get("world_created") is not True:
        raise ProbeError("--continue-completed requires the original persisted world.")
    if manifest.get("world_id") != str(world_id) or manifest.get("branch_id") != str(branch_id):
        raise ProbeError("The predecessor manifest does not identify this world and branch.")
    try:
        genesis_tick = int(manifest["start_tick"])
        job_start_tick = int(manifest.get("job_start_tick", genesis_tick))
        target_tick = int(manifest["target_tick"])
        if target_tick != genesis_tick + old_config["total_seconds"]:
            raise ValueError
        job_id = str(UUID(manifest["job_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbeError("The predecessor manifest has invalid target or job metadata.") from exc

    predecessor = repository.read_job(scope, UUID(job_id))
    _verify_stored_world_advance_job(
        predecessor, world_id=world_id, branch_id=branch_id, job_id=job_id,
        started_tick=job_start_tick, target_tick=target_tick,
        require_succeeded=True,
    )
    if repository.find_job_by_key(scope, world_id, branch_id, expected_key) != predecessor:
        raise ProbeError("The predecessor idempotency key does not identify its stored completed job.")

    pin = _terminal_checkpoint(manifest, target_tick=target_tick)
    view = repository.read_world(scope, world_id, branch_id)
    if (
        view.state.current_tick != target_tick or view.branch.current_tick != target_tick
        or view.last_seq != int(pin["sequence"]) or view.branch.head_seq != int(pin["sequence"])
        or view.branch.state_version != int(pin["state_version"])
        or view.state_hash != pin["state_hash"] or view.branch.state_hash != pin["state_hash"]
        or manifest.get("last_tick") != str(target_tick)
        or manifest.get("last_state_version") != pin["state_version"]
        or manifest.get("last_state_hash") != pin["state_hash"]
    ):
        raise ProbeError("The current branch head no longer matches the predecessor terminal checkpoint.")

    anchor = _completed_predecessor_replay_anchor(
        manifest, genesis_tick=genesis_tick, terminal_pin=pin,
    )
    if anchor is None:
        replay = repository.replay(
            scope, world_id, branch_id, through_seq=int(pin["sequence"]), from_genesis=True,
        )
    else:
        anchor_sequence = int(anchor["sequence"])
        anchor_tick = int(anchor["tick"])
        anchor_seen = False

        def verify_anchor_start(sequence: int, state: WorldState) -> None:
            nonlocal anchor_seen
            if anchor_seen:
                return
            anchor_seen = True
            if sequence != anchor_sequence or state.current_tick != anchor_tick:
                raise ProbeError("The predecessor decade checkpoint does not match its exact replay anchor.")

        try:
            replay = repository.replay_from_checkpoint(
                scope,
                world_id,
                branch_id,
                checkpoint_seq=anchor_sequence,
                through_seq=int(pin["sequence"]),
                checkpoint_hash=anchor["state_hash"],
                visit_commit=verify_anchor_start,
            )
        except Conflict as exc:
            raise ProbeError("The predecessor decade checkpoint does not validate against persisted history.") from exc
        if not anchor_seen:
            raise ProbeError("The predecessor decade checkpoint was not visited by range replay.")
    if (
        replay.last_seq != int(pin["sequence"]) or replay.state.current_tick != target_tick
        or replay.branch.state_version != int(pin["state_version"])
        or replay.state_hash != pin["state_hash"]
    ):
        raise ProbeError("The predecessor terminal checkpoint does not replay exactly from its saved anchor.")
    integrity = _sqlite_integrity(database)
    if not integrity["integrity_ok"] or not integrity["foreign_keys_ok"]:
        raise ProbeError("The predecessor database does not pass SQLite integrity checks.")
    return target_tick, pin


def _prepare_completed_continuation(
    directory: Path,
    manifest: dict[str, Any],
    config: dict[str, Any],
    *,
    repository: WorldRepository,
    scope: AccessScope,
    world_id: UUID,
    branch_id: UUID,
    database: Database,
) -> tuple[dict[str, Any], Path]:
    predecessor_tick, pin = _validate_completed_predecessor(
        manifest, config, repository=repository, scope=scope,
        world_id=world_id, branch_id=branch_id, database=database,
    )
    config_hash = _config_hash(config)
    target_tick = int(manifest["start_tick"]) + config["total_seconds"]
    if target_tick > INT64_MAX or target_tick <= predecessor_tick:
        raise ProbeError("The extended exact target is outside the supported tick range.")
    archive_name = _archive_completed_manifest(directory, manifest)
    continuation = dict(manifest)
    continuation.update({
        "config_hash": config_hash,
        "config": config,
        "idempotency_key": f"capacity-probe:{config_hash}",
        "job_id": None,
        "status": "initializing",
        "job_start_tick": str(predecessor_tick),
        "target_tick": str(target_tick),
        "last_tick": str(predecessor_tick),
        "last_state_version": pin["state_version"],
        "last_state_hash": pin["state_hash"],
        "last_job_status": JobStatus.SUCCEEDED.value,
        "checkpoints": list(manifest.get("checkpoints", [])),
        "predecessor": {
            "manifest_archive": archive_name,
            "config_hash": manifest["config_hash"],
            "job_id": manifest["job_id"],
            "target_tick": str(predecessor_tick),
            "terminal_checkpoint": pin,
        },
        "continued_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    manifest_path = directory / MANIFEST_NAME
    _atomic_json(manifest_path, continuation)
    return continuation, manifest_path


def _verify_prepared_continuation(
    directory: Path,
    manifest: dict[str, Any],
    config: dict[str, Any],
    *,
    repository: WorldRepository,
    scope: AccessScope,
    world_id: UUID,
    branch_id: UUID,
) -> None:
    config_hash = _config_hash(config)
    predecessor_ref = manifest.get("predecessor")
    if (
        manifest.get("config_hash") != config_hash or manifest.get("config") != config
        or manifest.get("idempotency_key") != f"capacity-probe:{config_hash}"
        or not isinstance(predecessor_ref, dict)
    ):
        raise ProbeError("The saved continuation manifest does not match this duration-only request.")
    archive_name = predecessor_ref.get("manifest_archive")
    if (
        not isinstance(archive_name, str)
        or Path(archive_name).name != archive_name
        or archive_name != f".probe-world-advance.completed.{predecessor_ref.get('config_hash')}.json"
    ):
        raise ProbeError("The continuation predecessor archive reference is invalid.")
    archive_path = directory / archive_name
    try:
        archived = json.loads(archive_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeError("The continuation predecessor archive is missing or unreadable.") from exc
    if (
        not isinstance(archived, dict)
        or archived.get("config_hash") != predecessor_ref.get("config_hash")
        or archived.get("job_id") != predecessor_ref.get("job_id")
        or archived.get("idempotency_key") != f"capacity-probe:{predecessor_ref.get('config_hash')}"
        or _config_hash(archived.get("config", {})) != predecessor_ref.get("config_hash")
        or not _duration_only_extension(archived.get("config", {}), config)
        or archived.get("status") != JobStatus.SUCCEEDED.value
        or archived.get("last_job_status") != JobStatus.SUCCEEDED.value
        or archived.get("world_created") is not True
    ):
        raise ProbeError("The continuation predecessor archive no longer matches its immutable pin.")
    try:
        old_target = int(predecessor_ref["target_tick"])
        genesis_tick = int(manifest["start_tick"])
        archived_genesis_tick = int(archived["start_tick"])
        archived_target_tick = int(archived["target_tick"])
        target_tick = int(manifest["target_tick"])
        old_job_id = str(UUID(predecessor_ref["job_id"]))
        job_start_tick = int(manifest["job_start_tick"])
        predecessor_job_start_tick = int(archived.get("job_start_tick", genesis_tick))
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbeError("The saved continuation manifest has invalid tick or job metadata.") from exc
    old_job = repository.read_job(scope, UUID(old_job_id))
    if (
        archived_genesis_tick != genesis_tick
        or archived_target_tick != old_target
        or archived_target_tick != archived_genesis_tick + archived["config"]["total_seconds"]
        or archived.get("world_id") != str(world_id)
        or archived.get("branch_id") != str(branch_id)
    ):
        raise ProbeError("The archived predecessor scope or terminal tick is inconsistent.")
    _verify_stored_world_advance_job(
        old_job, world_id=world_id, branch_id=branch_id, job_id=old_job_id,
        started_tick=predecessor_job_start_tick, target_tick=old_target, require_succeeded=True,
    )
    if repository.find_job_by_key(
        scope, world_id, branch_id, f"capacity-probe:{predecessor_ref['config_hash']}"
    ) != old_job:
        raise ProbeError("The continuation predecessor key no longer identifies its completed job.")
    if job_start_tick != old_target or target_tick != genesis_tick + config["total_seconds"]:
        raise ProbeError("The continuation job does not start at the predecessor target.")
    if target_tick > INT64_MAX:
        raise ProbeError("The continuation target exceeds the supported signed 64-bit tick range.")
    if not _preserves_checkpoints(manifest.get("checkpoints"), archived.get("checkpoints")):
        raise ProbeError("The continuation did not preserve all predecessor checkpoints.")
    pin = _terminal_checkpoint(archived, target_tick=old_target)
    if predecessor_ref.get("terminal_checkpoint") != pin:
        raise ProbeError("The continuation terminal pin differs from the predecessor checkpoint.")
    current = repository.read_world(scope, world_id, branch_id)
    new_job = repository.find_job_by_key(scope, world_id, branch_id, manifest["idempotency_key"])
    if new_job is None:
        if (
            manifest.get("job_id") is not None
            or current.state.current_tick != old_target
            or current.branch.state_version != int(pin["state_version"])
            or current.state_hash != pin["state_hash"]
        ):
            raise ProbeError("The branch moved beyond its predecessor before its continuation job was stored.")
        return
    if manifest.get("job_id") not in (None, str(new_job.job_id)):
        raise ProbeError("The continuation manifest refers to another stored job.")
    _verify_stored_world_advance_job(
        new_job, world_id=world_id, branch_id=branch_id, job_id=str(new_job.job_id),
        started_tick=old_target, target_tick=target_tick, require_succeeded=False,
    )
    if new_job.payload.get("initial_state_version") != int(pin["state_version"]):
        raise ProbeError("The continuation job was not queued from the predecessor's exact state version.")
    progress_version = new_job.payload.get("committed_state_version")
    if progress_version is None:
        # A worker may have claimed the queued job and crashed before its
        # first committed window; that boundary remains the reviewed start.
        progress_version = new_job.payload.get("initial_state_version")
        if (
            current.state.current_tick != old_target
            or current.last_seq != int(pin["sequence"])
            or current.branch.head_seq != int(pin["sequence"])
            or current.branch.state_version != int(pin["state_version"])
            or current.state_hash != pin["state_hash"]
        ):
            raise ProbeError("The continuation's uncommitted worker boundary differs from its predecessor pin.")
    if (new_job.committed_through_tick != current.state.current_tick
            or progress_version != current.branch.state_version
            or current.state.current_tick < old_target
            or current.branch.state_version < int(pin["state_version"])
            or current.state.current_tick > target_tick
            or new_job.status == JobStatus.SUCCEEDED and current.state.current_tick != target_tick):
        raise ProbeError("The continuation job progress does not match the current branch head.")


def _verify_existing_continuation_resume(
    directory: Path,
    manifest: dict[str, Any],
    config: dict[str, Any],
    *,
    repository: WorldRepository,
    scope: AccessScope,
    world_id: UUID,
    branch_id: UUID,
) -> bool:
    """Validate an existing continuation on ordinary exact-config resumes."""
    if manifest.get("predecessor") is None:
        return False
    _verify_prepared_continuation(
        directory, manifest, config, repository=repository, scope=scope,
        world_id=world_id, branch_id=branch_id,
    )
    return True


def _state_counts(state: WorldState) -> dict[str, Any]:
    kind_counts = Counter(entity.kind for entity in state.entities.values())
    status_counts = Counter(entity.status for entity in state.entities.values())
    kind_status = Counter((entity.kind, entity.status) for entity in state.entities.values())
    named_people = [
        entity for entity in state.entities.values()
        if entity.kind in {"character", "person"} and entity.name
    ]
    named_by_status = Counter(entity.status for entity in named_people)
    active_named_ids = {entity.id for entity in named_people if entity.status == "active"}
    named_by_id = {str(entity.id): entity for entity in named_people}
    content_index = state.metadata.get("content_index")
    raw_key_ids = content_index.get("key_npc_ids", []) if isinstance(content_index, dict) else []
    key_status = Counter(
        named_by_id[entity_id].status if entity_id in named_by_id else "missing"
        for entity_id in set(item for item in raw_key_ids if isinstance(item, str))
    ) if isinstance(raw_key_ids, list) else Counter()
    institutional_counts: Counter[str] = Counter()
    active_members: set[UUID] = set()
    active_officeholders: set[UUID] = set()
    named_by_life_stage: Counter[str] = Counter()
    population_headcount = 0
    population_unmodeled = 0
    population_records = 0
    population_error_count = 0
    for entity in state.entities.values():
        raw_life = entity.components.get("life")
        if raw_life is not None and entity.kind in {"character", "person"} and entity.name:
            try:
                named_by_life_stage[LifeComponent.model_validate(raw_life).life_stage] += 1
            except Exception:
                named_by_life_stage["invalid_or_unknown"] += 1
        raw_governance = entity.components.get("governance")
        if entity.status == "active" and raw_governance is not None:
            governance = GovernanceComponent.model_validate(raw_governance)
            institutional_counts["active_records"] += 1
            members = active_named_ids.intersection(governance.members)
            active_members.update(members)
            if members:
                institutional_counts["with_active_named_members"] += 1
            if governance.leader_id in active_named_ids:
                institutional_counts["with_active_named_leader"] += 1
                active_officeholders.add(governance.leader_id)
            for office in governance.offices:
                if office.holder_id in active_named_ids:
                    active_officeholders.add(office.holder_id)
                else:
                    institutional_counts["vacant_or_inactive_offices"] += 1
        raw_population = entity.components.get("population")
        if raw_population is not None:
            try:
                population = PopulationComponent.model_validate(raw_population)
                population_headcount += population.headcount
                if population.unmodeled_headcount is not None:
                    population_unmodeled += population.unmodeled_headcount
                population_records += 1
            except Exception:
                population_error_count += 1
    return {
        "entities_by_kind": dict(sorted(kind_counts.items())),
        "entities_by_status": dict(sorted(status_counts.items())),
        "entities_by_kind_and_status": {
            f"{kind}:{status}": count for (kind, status), count in sorted(kind_status.items())
        },
        "named_people_by_status": dict(sorted(named_by_status.items())),
        "named_people_by_life_stage": dict(sorted(named_by_life_stage.items())),
        "named_people_total": len(named_people),
        "authored_key_npcs_by_status": dict(sorted(key_status.items())),
        "institutional_governance": {
            "active_records": institutional_counts["active_records"],
            "with_active_named_members": institutional_counts["with_active_named_members"],
            "with_active_named_leader": institutional_counts["with_active_named_leader"],
            "vacant_or_inactive_offices": institutional_counts["vacant_or_inactive_offices"],
            "distinct_active_named_members": len(active_members),
            "distinct_active_named_officeholders": len(active_officeholders),
        },
        "aggregate_population": {
            "records": population_records,
            "headcount": str(population_headcount),
            "unmodeled_headcount": str(population_unmodeled),
            "includes_named_people": True,
            "invalid_records": population_error_count,
        },
    }


def _storage_metrics(database: Database, world_id: str) -> dict[str, Any]:
    with database.engine.connect() as connection:
        if inspect(connection).has_table("world_event_payloads"):
            grouped = connection.execute(text(
                "SELECT e.event_type AS event_type, COUNT(*) AS count, "
                "COALESCE(SUM(CASE WHEN p.event_id IS NULL "
                "THEN LENGTH(CAST(e.payload_json AS BLOB)) ELSE p.raw_byte_count END), 0) "
                "AS payload_bytes, "
                "COALESCE(SUM(LENGTH(CAST(e.payload_json AS BLOB)) "
                "+ COALESCE(LENGTH(p.compressed_bytes), 0)), 0) AS stored_payload_bytes, "
                "COALESCE(SUM(CASE WHEN p.event_id IS NULL THEN 0 ELSE 1 END), 0) "
                "AS sidecar_count "
                "FROM world_events AS e LEFT JOIN world_event_payloads AS p "
                "ON p.event_id = e.event_id "
                "WHERE e.world_id = :world_id "
                "GROUP BY e.event_type ORDER BY e.event_type"
            ), {"world_id": world_id.replace("-", "")}).all()
        else:
            # Pre-sidecar databases retain the original inline-only layout.
            grouped = connection.execute(text(
                "SELECT e.event_type AS event_type, COUNT(*) AS count, "
                "COALESCE(SUM(LENGTH(CAST(e.payload_json AS BLOB))), 0) AS payload_bytes, "
                "COALESCE(SUM(LENGTH(CAST(e.payload_json AS BLOB))), 0) AS stored_payload_bytes, "
                "0 AS sidecar_count "
                "FROM world_events AS e WHERE e.world_id = :world_id "
                "GROUP BY e.event_type ORDER BY e.event_type"
            ), {"world_id": world_id.replace("-", "")}).all()
    events_by_type = {
        row.event_type: {
            "count": int(row.count),
            # Logical payload bytes include the complete decompressed JSON.
            "payload_bytes": int(row.payload_bytes),
            # Stored bytes count inline JSON plus compressed sidecar bytes only;
            # SQLite row, index, and sidecar metadata overhead is excluded.
            "stored_payload_bytes": int(row.stored_payload_bytes),
            "sidecar_count": int(row.sidecar_count),
        }
        for row in grouped
    }
    return {
        "event_count": sum(row["count"] for row in events_by_type.values()),
        "event_payload_bytes": sum(row["payload_bytes"] for row in events_by_type.values()),
        "event_payload_stored_bytes": sum(
            row["stored_payload_bytes"] for row in events_by_type.values()
        ),
        "event_payload_sidecar_count": sum(row["sidecar_count"] for row in events_by_type.values()),
        "events_by_type": events_by_type,
    }


def _sqlite_integrity(database: Database) -> dict[str, Any]:
    with database.engine.connect() as connection:
        integrity_rows = [row[0] for row in connection.exec_driver_sql("PRAGMA integrity_check").all()]
        foreign_key_rows = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    return {
        "integrity_check": integrity_rows,
        "integrity_ok": integrity_rows == ["ok"],
        "foreign_key_violation_count": len(foreign_key_rows),
        "foreign_keys_ok": not foreign_key_rows,
    }


def _disk_sizes(directory: Path) -> dict[str, int]:
    names = ("world-engine.db", "world-engine.db-wal", "world-engine.db-shm")
    values = {name: (directory / name).stat().st_size if (directory / name).exists() else 0 for name in names}
    values["database_wal_shm_total"] = sum(values.values())
    return values


def _checkpoint(
    repository: WorldRepository,
    scope: AccessScope,
    world_id,
    branch_id,
    *,
    start_tick: int,
    day_seconds: int,
    year_days: int,
    scheduled_year: int | None,
    label: str,
) -> dict[str, Any]:
    expected_tick = (
        start_tick + scheduled_year * year_days * day_seconds if scheduled_year is not None else None
    )
    snapshot = repository.snapshot(scope, world_id, branch_id, at_tick=expected_tick)
    replay = repository.replay(scope, world_id, branch_id, through_seq=snapshot.last_seq)
    if (
        replay.state_hash != snapshot.state_hash
        or replay.last_seq != snapshot.last_seq
        or replay.state.current_tick != snapshot.current_tick
        or expected_tick is not None and snapshot.current_tick != expected_tick
    ):
        raise ProbeError("The branch commit log does not replay to the checkpoint state.")
    elapsed_days = (snapshot.current_tick - start_tick) // day_seconds
    elapsed_years, partial_days = divmod(max(0, elapsed_days), year_days)
    return {
        "label": label,
        "scheduled_year": scheduled_year,
        "tick": str(snapshot.current_tick),
        "sequence": str(snapshot.last_seq),
        "state_version": str(snapshot.state_version),
        "state_hash": snapshot.state_hash,
        "elapsed_whole_years": elapsed_years,
        "elapsed_partial_year_days": partial_days,
        "counts": _state_counts(snapshot.state),
    }


def _add_checkpoint(
    manifest: dict[str, Any],
    manifest_path: Path,
    repository: WorldRepository,
    scope: AccessScope,
    world_id,
    branch_id,
    *,
    start_tick: int,
    day_seconds: int,
    year_days: int,
    scheduled_year: int | None,
    label: str,
) -> dict[str, Any]:
    record = _checkpoint(
        repository, scope, world_id, branch_id, start_tick=start_tick,
        day_seconds=day_seconds, year_days=year_days,
        scheduled_year=scheduled_year, label=label,
    )
    key = (record["label"], record["scheduled_year"], record["tick"], record["sequence"])
    existing = {
        (item.get("label"), item.get("scheduled_year"), item.get("tick"), item.get("sequence"))
        for item in manifest.get("checkpoints", [])
    }
    if key not in existing:
        checkpoints = manifest.setdefault("checkpoints", [])
        if scheduled_year is not None:
            checkpoints[:] = [item for item in checkpoints if (
                item.get("label"), item.get("scheduled_year")
            ) != (label, scheduled_year)]
        checkpoints.append(record)
        _atomic_json(manifest_path, manifest)
    return record


def _emit_progress(value: dict[str, Any]) -> None:
    print(_canonical_json(value), flush=True)


def _advance_job(
    *,
    repository: WorldRepository,
    scope: AccessScope,
    jobs: SimulationJobs,
    job: JobRecord,
    world_id,
    branch_id,
    start_tick: int,
    target_tick: int,
    day_seconds: int,
    year_days: int,
    max_windows: int | None,
    lease_wait_seconds: int,
    manifest: dict[str, Any],
    manifest_path: Path,
    decision_mode: str = "deterministic_no_model",
    model_operations: ModelOperations | None = None,
    model_scope: AccessScope | None = None,
    resume_model_failure: bool = False,
) -> tuple[JobRecord, dict[str, int], list[str], int]:
    if resume_model_failure and decision_mode != "configured_local_model":
        raise ProbeError("--resume-model-failure requires --decision-mode configured_local_model.")
    outcomes: Counter[str] = Counter()
    stop_reasons: list[str] = []
    windows = 0
    resumed_model_failure = False
    worker_id = f"capacity-probe:{os.getpid()}"
    wait_started = time.monotonic()
    lease = None

    while lease is None:
        current = repository.read_job(scope, job.job_id)
        if current.status == JobStatus.PAUSED:
            pause_code = (current.result or {}).get("probe_pause")
            resumable_pause = pause_code in {
                "max_windows_reached",
                "keyboard_interrupt",
                "ten_windows_without_clock_progress",
            }
            resumable_model_failure = (
                pause_code == "model_decision_failed"
                and decision_mode == "configured_local_model"
                and resume_model_failure
                and not resumed_model_failure
            )
            if not resumable_pause and not resumable_model_failure:
                stop_reasons.append(pause_code or "job_paused_at_simulation_boundary")
                return current, dict(outcomes), stop_reasons, windows
            current = repository.control_job(scope, current.job_id, "resume")
            if resumable_model_failure:
                resumed_model_failure = True
                outcomes["model_failure_pause_resumes"] += 1
        if current.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
            return current, dict(outcomes), stop_reasons, windows
        lease = repository.claim_job(worker_id, lease_seconds=LEASE_SECONDS, job_type=JOB_TYPE)
        if lease is None:
            if time.monotonic() - wait_started >= lease_wait_seconds:
                stop_reasons.append("job_lease_unavailable_after_wait_limit")
                return repository.read_job(scope, job.job_id), dict(outcomes), stop_reasons, windows
            time.sleep(1)

    no_progress_windows = 0
    maintenance_path = manifest_path.with_name(MAINTENANCE_REQUEST_NAME)
    seen_decision_boundaries: set[tuple[str, int, int]] = set()
    seen_decade_handoffs: set[tuple[int, int]] = set()
    while True:
        current = repository.read_job(scope, job.job_id)
        if current.status != JobStatus.RUNNING:
            return current, dict(outcomes), stop_reasons, windows
        if max_windows is not None and windows >= max_windows:
            current = repository.finish_job(
                lease, JobStatus.PAUSED,
                result={"probe_pause": "max_windows_reached"},
                committed_through_tick=current.committed_through_tick,
            )
            stop_reasons.append("max_windows_reached")
            return current, dict(outcomes), stop_reasons, windows

        try:
            lease = repository.renew_job(lease, lease_seconds=LEASE_SECONDS)
        except InvalidLease:
            latest = repository.read_job(scope, job.job_id)
            if latest.status != JobStatus.RUNNING:
                return latest, dict(outcomes), stop_reasons, windows
            if latest.fencing_token != lease.fencing_token or lease.expires_at > datetime.now(timezone.utc):
                raise
            recovered = repository.claim_job(worker_id, lease_seconds=LEASE_SECONDS, job_type=JOB_TYPE)
            if recovered is None or recovered.job_id != job.job_id:
                raise
            lease = recovered
            current = repository.read_job(scope, job.job_id)
            outcomes["expired_lease_reclaimed"] += 1
            _emit_progress({"progress": "lease_reclaimed", "committed_tick": str(current.committed_through_tick)})
        if _maintenance_requested(maintenance_path, job.job_id):
            paused = repository.finish_job(
                lease, JobStatus.PAUSED, result={"probe_pause": MAINTENANCE_PAUSE},
                committed_through_tick=current.committed_through_tick,
            )
            return paused, dict(outcomes), [*stop_reasons, MAINTENANCE_PAUSE], windows
        previous_tick = current.committed_through_tick
        previous_version = current.payload.get("committed_state_version")
        if previous_tick is None:
            raise ProbeError("The simulation job has no committed clock progress.")
        try:
            resolution = jobs.advance(scope, lease)
        except KeyboardInterrupt:
            latest = repository.read_job(scope, job.job_id)
            try:
                current = repository.finish_job(
                    lease, JobStatus.PAUSED,
                    result={"probe_pause": "keyboard_interrupt"},
                    committed_through_tick=latest.committed_through_tick,
                )
            except Exception:
                current = repository.read_job(scope, job.job_id)
            stop_reasons.append("keyboard_interrupt")
            return current, dict(outcomes), stop_reasons, windows

        windows += 1
        if hasattr(resolution, "outcome"):
            outcome = str(resolution.outcome)
            outcomes[outcome] += 1
            if getattr(resolution, "interruption", None) is not None:
                reason = resolution.interruption.reason_code
                if reason:
                    stop_reasons.append(reason)
        else:
            outcome = "job_record_returned"

        current = repository.read_job(scope, job.job_id)
        while outcome == "decision_required" and current.status == JobStatus.RUNNING:
            if _maintenance_requested(maintenance_path, job.job_id):
                stop_reasons.append(MAINTENANCE_PAUSE)
                break
            requests = tuple(getattr(resolution, "decision_requests", ()))
            outcomes["decision_requests"] += len(requests)
            if decision_mode != "configured_local_model":
                break
            if len(requests) != 1:
                stop_reasons.append("model_decision_request_set_unsupported")
                break
            decision = requests[0]
            if decision.decision_owner != "model":
                outcomes["user_decision_requests"] += 1
                stop_reasons.append("user_decision_required")
                break
            if not decision.options and decision.kind != "npc.plan_review":
                stop_reasons.append("model_decision_has_no_options")
                break
            if model_operations is None or model_scope is None:
                stop_reasons.append("configured_model_dispatcher_unavailable")
                break
            decision_key = (
                str(decision.request_id), decision.state_version, decision.at_tick
            )
            if decision_key in seen_decision_boundaries:
                stop_reasons.append("repeated_model_decision_boundary")
                break
            seen_decision_boundaries.add(decision_key)
            try:
                for attempt_number in range(1, len(MODEL_DECISION_RETRY_BACKOFF_SECONDS) + 2):
                    if attempt_number > 1:
                        time.sleep(MODEL_DECISION_RETRY_BACKOFF_SECONDS[attempt_number - 2])

                    # Renew before every dispatch attempt. Reusing this exact
                    # decision keeps retries bound to the same request ID,
                    # options, state version, and tick.
                    lease = repository.renew_job(lease, lease_seconds=LEASE_SECONDS)
                    model_job = repository.read_job(scope, job.job_id)
                    if attempt_number > 1:
                        outcomes["model_retry_attempts"] += 1
                    outcomes["model_calls_attempted"] += 1
                    outcomes[f"model_call_attempt_{attempt_number}"] += 1
                    try:
                        answer = asyncio.run(_invoke_probe_model_decision(
                            model_operations, repository, model_scope, lease, model_job, decision,
                        ))
                    except ProviderError as error:
                        is_unbilled_network_failure = (
                            error.code is ProviderErrorCode.NETWORK
                            and error.retryable is True
                            and error.possibly_billed is False
                        )
                        if is_unbilled_network_failure:
                            outcomes["model_unbilled_network_failures"] += 1
                            if attempt_number <= len(MODEL_DECISION_RETRY_BACKOFF_SECONDS):
                                continue
                        raise

                    # A response is accepted at most once, after the last
                    # lease fence and the unchanged pending decision check.
                    lease = repository.renew_job(lease, lease_seconds=LEASE_SECONDS)
                    next_resolution = jobs.advance(scope, lease, model_decision=answer)
                    break
                else:  # pragma: no cover - guarded by the loop's retry policy
                    raise RuntimeError("model decision retry loop ended without a result")
            except InvalidLease:
                stop_reasons.append("lease_lost_during_model_decision")
                return repository.read_job(scope, job.job_id), dict(outcomes), stop_reasons, windows
            except KeyboardInterrupt:
                latest = repository.read_job(scope, job.job_id)
                try:
                    current = repository.finish_job(
                        lease,
                        JobStatus.PAUSED,
                        result={"probe_pause": "keyboard_interrupt"},
                        committed_through_tick=latest.committed_through_tick,
                    )
                except InvalidLease:
                    current = repository.read_job(scope, job.job_id)
                stop_reasons.append("keyboard_interrupt")
                outcome = "decision_interrupted"
                break
            except Exception as error:
                outcomes["model_call_failures"] += 1
                failure = {"probe_pause": "model_decision_failed"}
                if isinstance(error, ProbeModelDeadline):
                    outcomes["model_host_deadline_expirations"] += 1
                    failure["model_host_deadline"] = {
                        "request_id": decision_key[0], "state_version": decision_key[1], "tick": decision_key[2],
                    }
                latest = repository.read_job(scope, job.job_id)
                try:
                    if latest.status == JobStatus.RUNNING:
                        repository.finish_job(
                            lease,
                            JobStatus.PAUSED,
                            result=failure,
                            committed_through_tick=latest.committed_through_tick,
                        )
                except InvalidLease:
                    stop_reasons.append("lease_lost_after_model_decision_failure")
                    return repository.read_job(scope, job.job_id), dict(outcomes), stop_reasons, windows
                current = repository.read_job(scope, job.job_id)
                stop_reasons.append("model_decision_failed")
                outcome = "model_decision_failed"
                break

            accepted = tuple(getattr(next_resolution, "accepted_decisions", ()))
            accepted_response = any(item.source == "model" and item.request_id == decision.request_id for item in accepted)
            if decision.kind == "npc.reflection":
                reflection_job = repository.read_job(scope, job.job_id)
                receipt = reflection_job.payload.get("last_npc_reflection")
                accepted_response = isinstance(receipt, dict) and receipt.get("request_id") == str(decision.request_id)
            if not accepted_response:
                outcomes["model_response_not_accepted"] += 1
                current = repository.read_job(scope, job.job_id)
                if current.status == JobStatus.RUNNING:
                    try:
                        current = repository.finish_job(
                            lease,
                            JobStatus.PAUSED,
                            result={"probe_pause": "model_decision_not_accepted"},
                            committed_through_tick=current.committed_through_tick,
                        )
                    except InvalidLease:
                        current = repository.read_job(scope, job.job_id)
                stop_reasons.append("model_decision_not_accepted")
                outcome = "model_decision_not_accepted"
                break
            outcomes["model_decisions_accepted"] += 1
            resolution = next_resolution
            outcome = str(getattr(resolution, "outcome", "job_record_returned"))
            outcomes[outcome] += 1
            current = repository.read_job(scope, job.job_id)
            if getattr(resolution, "interruption", None) is not None:
                reason = resolution.interruption.reason_code
                if reason:
                    stop_reasons.append(reason)
        if outcome == "decision_required" and current.status == JobStatus.WAITING_INPUT:
            requests = tuple(getattr(resolution, "decision_requests", ()))
            outcomes["decision_requests"] += len(requests)
            if any(request.decision_owner == "user" for request in requests):
                outcomes["user_decision_requests"] += sum(
                    request.decision_owner == "user" for request in requests
                )
                if "user_decision_required" not in stop_reasons:
                    stop_reasons.append("user_decision_required")
        actual_tick = current.committed_through_tick
        if actual_tick is None:
            if hasattr(resolution, "next_tick"):
                actual_tick = int(resolution.next_tick)
            else:
                actual_tick = previous_tick
        elapsed_days = max(0, actual_tick - start_tick) // day_seconds
        elapsed_years = elapsed_days // year_days
        recorded_decades = [
            item.get("scheduled_year", 0)
            for item in manifest.get("checkpoints", [])
            if item.get("label") == "decade"
            and item.get("tick") == str(start_tick + item["scheduled_year"] * year_days * day_seconds)
        ]
        next_decade = max(10, ((max(recorded_decades, default=0) // 10) + 1) * 10)
        while next_decade <= elapsed_years:
            expected_tick = start_tick + next_decade * year_days * day_seconds
            # A window can commit the clock exactly onto a same-tick boundary.
            # The worker must keep resolving due events, lifespan endings and
            # population cleanup before this tick is a settled checkpoint.
            settled_resolution = actual_tick > expected_tick or current.status == JobStatus.SUCCEEDED
            if not settled_resolution:
                break
            checkpoint = _add_checkpoint(
                manifest, manifest_path, repository, scope, world_id, branch_id,
                start_tick=start_tick, day_seconds=day_seconds, year_days=year_days,
                scheduled_year=next_decade, label="decade",
            )
            _emit_progress({
                "progress": "checkpoint",
                "scheduled_year": next_decade,
                "actual_tick": checkpoint["tick"],
                "committed_state_version": checkpoint["state_version"],
                "counts": checkpoint["counts"],
            })
            next_decade += 10

        manifest["status"] = current.status.value
        manifest["last_tick"] = str(actual_tick)
        _atomic_json(manifest_path, manifest)

        if outcome == "decision_required" and current.status == JobStatus.RUNNING:
            if stop_reasons and stop_reasons[-1] == MAINTENANCE_PAUSE:
                pause_code = MAINTENANCE_PAUSE
            elif decision_mode == "configured_local_model":
                pause_code = stop_reasons[-1] if stop_reasons else "model_decision_unresolved"
            else:
                pause_code = "decision_required_no_model_dispatcher"
            current = repository.finish_job(
                lease, JobStatus.PAUSED,
                result={"probe_pause": pause_code},
                committed_through_tick=actual_tick,
            )
            if pause_code not in stop_reasons:
                stop_reasons.append(pause_code)
        if (actual_tick == previous_tick and current.status == JobStatus.RUNNING
                and current.payload.get("committed_state_version") == previous_version):
            no_progress_windows += 1
        else:
            no_progress_windows = 0
        if no_progress_windows >= 10 and current.status == JobStatus.RUNNING:
            current = repository.finish_job(
                lease, JobStatus.PAUSED,
                result={"probe_pause": "ten_windows_without_clock_progress"},
                committed_through_tick=actual_tick,
            )
            stop_reasons.append("ten_windows_without_clock_progress")

        # Persist the exact head before another worker window starts from a
        # decade boundary. This snapshot is only a storage handoff: the decade
        # is registered in the manifest above only after a later tick commits
        # or the job succeeds. Multiple same-tick event commits can therefore
        # produce later snapshots for newer state versions at this boundary.
        if current.status == JobStatus.RUNNING:
            handoff_recorded_decades = [
                item.get("scheduled_year", 0)
                for item in manifest.get("checkpoints", [])
                if item.get("label") == "decade"
                and item.get("tick") == str(
                    start_tick + item["scheduled_year"] * year_days * day_seconds
                )
            ]
            handoff_decade = max(
                10, ((max(handoff_recorded_decades, default=0) // 10) + 1) * 10
            )
            handoff_tick = start_tick + handoff_decade * year_days * day_seconds
            handoff_version = current.payload.get("committed_state_version")
            handoff_key = (actual_tick, handoff_version)
            if (
                actual_tick == handoff_tick
                and type(handoff_version) is int
                and handoff_key not in seen_decade_handoffs
            ):
                snapshot = repository.snapshot(scope, world_id, branch_id)
                if (snapshot.current_tick, snapshot.state_version) != handoff_key:
                    raise ProbeError(
                        "The branch head changed while persisting a decade handoff snapshot."
                    )
                seen_decade_handoffs.add(handoff_key)

        if windows % 100 == 0 or current.status != JobStatus.RUNNING:
            _emit_progress({
                "progress": "worker",
                "windows": windows,
                "job_status": current.status.value,
                "committed_tick": str(actual_tick),
                "target_tick": str(target_tick),
                "elapsed_years": elapsed_years,
                "outcome": outcome,
            })
        if current.status != JobStatus.RUNNING:
            return current, dict(outcomes), stop_reasons, windows


def _capacity_sample(state: WorldState) -> dict[str, int]:
    active = [entity for entity in state.entities.values() if entity.status == "active"]
    named = {str(entity.id) for entity in active if entity.kind in {"character", "person"} and entity.name}
    institutions = [
        entity.components.get("governance") for entity in active
        if entity.kind in {"organization", "polity"}
    ]
    return {
        "active_named_people": len(named),
        "active_countries": sum(entity.kind == "polity" for entity in active),
        "active_other_organizations": sum(entity.kind == "organization" for entity in active),
        "active_locations": sum(entity.kind in {"place", "location"} for entity in active),
        "institutions_with_active_named_members": sum(
            bool(named.intersection(map(str, governance.get("members", ()))))
            for governance in institutions if isinstance(governance, dict)
        ),
    }


def _archive_metrics(
    repository: WorldRepository, scope: AccessScope, world_id: UUID, *, directory: Path, limits: BackupLimits,
) -> dict[str, Any]:
    started = time.perf_counter()
    policy = {
        "configured_compressed_limit_bytes": limits.compressed_bytes,
        "configured_uncompressed_limit_bytes": limits.uncompressed_bytes,
    }
    try:
        with SpooledTemporaryFile(max_size=MIB, mode="w+b", dir=directory) as archive:
            compressed_bytes = BackupApplication(
                repository, limits=limits, scratch_directory=directory,
            ).export_world_to(scope, world_id, archive)
            with ZipFile(archive, "r") as zipped:
                members = zipped.infolist()
                manifest = json.loads(zipped.read("manifest.json"))
        return {
            "status": "exported",
            "format_version": manifest["format_version"],
            "compressed_bytes": compressed_bytes,
            "uncompressed_member_bytes": sum(member.file_size for member in members),
            "member_count": len(members),
            "event_member_count": sum(member.filename.startswith("events/") for member in members),
            **policy,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    except BackupArchiveError as exc:
        return {
            "status": "limit_or_validation_failure",
            "error": str(exc),
            **policy,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:
        return {
            "status": "export_failed",
            "error": f"{type(exc).__name__}: {exc}",
            **policy,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }


def _build_parser(pack_ids: list[str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create or resume one full-catalog world and advance it using the real leased "
            "SQLite simulation worker. One directory is reserved for one pack/seed/duration."
        )
    )
    parser.add_argument("--pack", required=True, choices=pack_ids, help="content pack ID")
    parser.add_argument(
        "--pack-version",
        help="authored content pack version for a new world; resumes use the saved version by default",
    )
    parser.add_argument("--seed", required=True, type=int, help="explicit signed 64-bit catalog seed")
    parser.add_argument(
        "--population-mode", choices=("natural", "sustained"), default="natural",
        help="population policy seeded in a new catalog world (default: natural)",
    )
    parser.add_argument(
        "--decision-mode", choices=DECISION_MODES, default="deterministic_no_model",
        help="pause at model decisions by default; opt into the configured local model route explicitly",
    )
    parser.add_argument(
        "--resume-model-failure",
        action="store_true",
        help="once per invocation, resume only a saved model_decision_failed pause (requires configured_local_model)",
    )
    parser.add_argument(
        "--model-data-directory", type=Path, default=PROJECT_ROOT / ".data",
        help="local model settings/credential scope for configured_local_model (default: project .data)",
    )
    parser.add_argument("--years", required=True, type=int, help="whole declared calendar years (may be zero with days)")
    parser.add_argument("--days", default=0, type=int, help="additional exact calendar days")
    parser.add_argument(
        "--continue-completed", action="store_true",
        help="extend an audited succeeded probe on its existing root branch from its exact terminal tick",
    )
    parser.add_argument("--data-directory", required=True, type=Path, help="dedicated, resumable probe directory")
    parser.add_argument("--compact-ledger", action="store_true",
                        help="select compact physical ledger storage in an empty initialized SQLite database")
    parser.add_argument("--output", type=Path, help="write the final JSON report to this path")
    parser.add_argument("--max-windows", type=int, help="pause after this many worker windows for resumable sampling")
    parser.add_argument("--audit-from-genesis", action="store_true",
                        help="Replay the entire committed history from genesis when reporting.")
    parser.add_argument("--backup-compressed-mib", type=int, default=DEFAULT_LIMITS.compressed_bytes // MIB,
                        help="Compressed archive limit in MiB for the final export measurement.")
    parser.add_argument("--backup-expanded-mib", type=int, default=DEFAULT_LIMITS.uncompressed_bytes // MIB,
                        help="Expanded archive limit in MiB for the final export measurement.")
    parser.add_argument(
        "--lease-wait-seconds", type=int, default=130,
        help="maximum wait to reclaim an expired worker lease (default: 130)",
    )
    return parser


def _resume_model_failure_enabled(args: argparse.Namespace) -> bool:
    requested = bool(getattr(args, "resume_model_failure", False))
    decision_mode = getattr(args, "decision_mode", "deterministic_no_model")
    if requested and decision_mode != "configured_local_model":
        raise ProbeError("--resume-model-failure requires --decision-mode configured_local_model.")
    return requested


def run(args: argparse.Namespace, catalog: ContentCatalog) -> tuple[dict[str, Any], int]:
    if args.years < 0 or args.days < 0:
        raise ProbeError("--years and --days must be nonnegative.")
    if not INT64_MIN <= args.seed <= INT64_MAX:
        raise ProbeError("--seed must fit in a signed 64-bit integer.")
    if args.max_windows is not None and args.max_windows < 1:
        raise ProbeError("--max-windows must be positive when supplied.")
    if args.lease_wait_seconds < 0:
        raise ProbeError("--lease-wait-seconds must be nonnegative.")
    if args.backup_compressed_mib <= 0 or args.backup_expanded_mib <= 0:
        raise ProbeError("Backup limits must be positive.")
    backup_limits = replace(DEFAULT_LIMITS, compressed_bytes=args.backup_compressed_mib * MIB,
                            uncompressed_bytes=args.backup_expanded_mib * MIB)
    decision_mode = getattr(args, "decision_mode", "deterministic_no_model")
    if decision_mode not in DECISION_MODES:
        raise ProbeError("Unsupported model decision mode.")
    resume_model_failure = _resume_model_failure_enabled(args)

    directory = args.data_directory.expanduser().resolve()
    continue_completed = bool(getattr(args, "continue_completed", False))
    saved_manifest = _existing_manifest(directory)
    if continue_completed and saved_manifest is None:
        raise ProbeError("--continue-completed requires an existing completed probe manifest.")
    saved_pack_version = _saved_pack_version(saved_manifest)
    requested_pack_version = getattr(args, "pack_version", None)
    if requested_pack_version is not None and (
        not isinstance(requested_pack_version, str) or not requested_pack_version.strip()
    ):
        raise ProbeError("--pack-version must be a non-empty version string.")
    if saved_manifest is not None and requested_pack_version is not None and (
        requested_pack_version != saved_pack_version
    ):
        raise ProbeError(
            f"The saved probe uses content pack version {saved_pack_version}; "
            "--pack-version cannot change a resumable world."
        )
    if continue_completed and saved_manifest is not None and (
        "population_mode" not in saved_manifest.get("config", {})
        or "catalog_seeded" not in saved_manifest.get("config", {})
    ):
        raise ProbeError("A legacy unseeded probe cannot be extended with --continue-completed.")
    legacy_config = None if continue_completed else _legacy_config_for_resume(saved_manifest, args)
    if legacy_config is not None and saved_pack_version != "1.7.0":
        raise ProbeError("A legacy unseeded probe can only resume with content pack version 1.7.0.")
    effective_pack_version = saved_pack_version if saved_manifest is not None else requested_pack_version
    directory.mkdir(parents=True, exist_ok=True)
    title = f"Capacity probe {args.pack} seed {args.seed}"
    player_name = f"Probe adventurer seed {args.seed}"
    if legacy_config is not None:
        # Preserve the exact old hash, catalog path and idempotency key so an
        # existing no-seed job can be resumed safely under the new runner.
        draft = catalog.build_world(args.pack, title, player_name, pack_version=effective_pack_version)
        config = legacy_config
    else:
        try:
            build_options = {"pack_version": effective_pack_version} if effective_pack_version is not None else {}
            draft = catalog.build_world(
                args.pack,
                title,
                player_name,
                seed=args.seed,
                population_mode=args.population_mode,
                **build_options,
            )
        except ValueError as exc:
            raise ProbeError(f"The requested content pack version is unavailable: {exc}") from exc
        config = _run_config(
            args,
            draft.pack_version,
            year_days=draft.state.calendar.year_days,
            day_seconds=draft.state.calendar.day_seconds,
            catalog_seeded=True,
        )
    model_operations: ModelOperations | None = None
    model_scope: AccessScope | None = None
    if decision_mode == "configured_local_model":
        model_operations, model_scope = _configured_model_operations(
            getattr(args, "model_data_directory", PROJECT_ROOT / ".data")
        )
    with _directory_lock(directory / ".probe-world-advance.lock"):
        if continue_completed:
            manifest = _existing_manifest(directory)
            if manifest is None:
                raise ProbeError("The completed predecessor manifest disappeared before continuation.")
            manifest_path = directory / MANIFEST_NAME
            if (
                manifest.get("world_id") != str(draft.world_id)
                or manifest.get("branch_id") != str(draft.branch_id)
            ):
                raise ProbeError("The probe catalog identity changed; refusing to continue another world.")
        else:
            manifest, manifest_path = _manifest_for(
                directory,
                config,
                draft,
                allow_world_id_drift=legacy_config is not None,
            )
        world_id = UUID(manifest["world_id"])
        branch_id = UUID(manifest["branch_id"])
        scope = _load_or_create_scope(directory)
        database_path = directory / "world-engine.db"
        database_url = URL.create("sqlite", database=database_path.as_posix()).render_as_string(hide_password=False)
        _migrate(database_url)
        database = Database(database_url)
        started = time.perf_counter()
        started_at = datetime.now(timezone.utc)
        try:
            if getattr(args, "compact_ledger", False):
                try:
                    database.enable_compact_ledger()
                except ValueError as exc:
                    raise ProbeError(str(exc)) from None
            repository = WorldRepository(database)
            existing_worlds = repository.list_worlds(scope)
            unexpected = [row for row in existing_worlds if row.world_id != world_id]
            if unexpected:
                raise ProbeError(
                    "The probe database contains a different world. Choose a fresh data directory; "
                    "the runner will not alter unrelated worlds."
                )
            if continue_completed and not existing_worlds:
                raise ProbeError("--continue-completed will not create or recreate the predecessor world.")
            if not existing_worlds:
                if legacy_config is not None:
                    raise ProbeError(
                        "This legacy probe has no persisted world, and its original pack version is unavailable; "
                        "refusing to create a different catalog draft."
                    )
                created = repository.create_world(scope, draft)
                world_id, branch_id = created.world.world_id, created.branch.branch_id
                if world_id != UUID(manifest["world_id"]) or branch_id != UUID(manifest["branch_id"]):
                    raise ProbeError("The created catalog world IDs differ from the saved probe manifest.")
                manifest["world_created"] = True
                _atomic_json(manifest_path, manifest)
            else:
                view = repository.read_world(scope, world_id, branch_id)
                world_id, branch_id = view.world.world_id, view.branch.branch_id
                if not continue_completed:
                    manifest["world_created"] = True
                    _atomic_json(manifest_path, manifest)

            if continue_completed:
                if manifest.get("config_hash") == _config_hash(config):
                    _verify_prepared_continuation(
                        directory, manifest, config, repository=repository, scope=scope,
                        world_id=world_id, branch_id=branch_id,
                    )
                else:
                    manifest, manifest_path = _prepare_completed_continuation(
                        directory, manifest, config, repository=repository, scope=scope,
                        world_id=world_id, branch_id=branch_id, database=database,
                    )
            else:
                _verify_existing_continuation_resume(
                    directory, manifest, config, repository=repository, scope=scope,
                    world_id=world_id, branch_id=branch_id,
                )

            initial_view = repository.read_world(scope, world_id, branch_id)
            start_tick = int(manifest.get("start_tick", str(initial_view.state.current_tick)))
            job_start_tick = int(manifest.get("job_start_tick", str(start_tick)))
            target_tick = start_tick + config["total_seconds"]
            if target_tick > INT64_MAX:
                raise ProbeError("The exact target tick exceeds the supported signed 64-bit range.")
            if manifest.get("target_tick") not in (None, str(target_tick)):
                raise ProbeError("The stored exact target tick does not match the probe configuration.")
            manifest.update({
                "start_tick": str(start_tick),
                "job_start_tick": str(job_start_tick),
                "target_tick": str(target_tick),
                "status": "running",
                "database_file": "world-engine.db",
                "started_at_utc": manifest.get("started_at_utc") or started_at.isoformat(),
            })
            _atomic_json(manifest_path, manifest)
            if target_tick <= start_tick:
                raise ProbeError("The exact target must be later than the current branch tick.")

            jobs = SimulationJobs(repository)
            job_key = manifest["idempotency_key"]
            job = repository.find_job_by_key(scope, world_id, branch_id, job_key)
            if job is None:
                job = jobs.enqueue_world_advance(
                    scope, world_id, branch_id,
                    WorldAdvancePlan(target_tick=target_tick, started_tick=job_start_tick),
                    idempotency_key=job_key,
                    speed="balanced",
                    depth="deep",
                    expected_state_version=initial_view.branch.state_version,
                )
            elif (
                job.payload.get("plan", {}).get("target_tick") != target_tick
                or continue_completed and job.payload.get("plan", {}).get("started_tick") != job_start_tick
                or continue_completed and (
                    job.world_id != world_id or job.branch_id != branch_id or job.job_type != JOB_TYPE
                )
            ):
                raise ProbeError("The persisted simulation job has a different target tick.")
            manifest["job_id"] = str(job.job_id)
            manifest["status"] = job.status.value
            _atomic_json(manifest_path, manifest)

            _emit_progress({
                "progress": "started",
                "world_id": str(world_id),
                "branch_id": str(branch_id),
                "job_id": str(job.job_id),
                "status": job.status.value,
                "start_tick": str(start_tick),
                "job_start_tick": str(job_start_tick),
                "target_tick": str(target_tick),
                "duration": {
                    "declared_years": config["declared_years"],
                    "declared_days": config["declared_days"],
                    "total_days": config["total_days"],
                    "normalized_years": config["normalized_years"],
                    "partial_year_days": config["partial_year_days"],
                },
                "decision_mode": config.get("decision_mode", "deterministic_no_model"),
                "population_mode": config.get("population_mode", "natural"),
                "seed_semantics": config["seed_semantics"],
            })

            if job.status in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED}:
                job, outcomes, stop_reasons, windows = _advance_job(
                    repository=repository,
                    scope=scope,
                    jobs=jobs,
                    job=job,
                    world_id=world_id,
                    branch_id=branch_id,
                    start_tick=start_tick,
                    target_tick=target_tick,
                    day_seconds=config["day_seconds"],
                    year_days=config["year_days"],
                    max_windows=args.max_windows,
                    lease_wait_seconds=args.lease_wait_seconds,
                    manifest=manifest,
                    manifest_path=manifest_path,
                    decision_mode=decision_mode,
                    model_operations=model_operations,
                    model_scope=model_scope,
                    resume_model_failure=resume_model_failure,
                )
            else:
                outcomes, stop_reasons, windows = {}, [], 0

            final_view = repository.read_world(scope, world_id, branch_id)
            final_job = repository.read_job(scope, job.job_id)
            pause_code = (final_job.result or {}).get("probe_pause")
            deferred_verification = final_job.status == JobStatus.PAUSED and pause_code in {
                MAINTENANCE_PAUSE, "model_decision_failed",
            }
            final_elapsed_days = max(0, final_view.state.current_tick - start_tick) // config["day_seconds"]
            for checkpoint_year in range(10, config["normalized_years"] + 1, 10):
                expected_tick = start_tick + checkpoint_year * config["year_days"] * config["day_seconds"]
                if (
                    final_view.state.current_tick > expected_tick
                    or final_view.state.current_tick == expected_tick
                    and final_job.status == JobStatus.SUCCEEDED
                ):
                    already = any(
                        item.get("label") == "decade" and item.get("scheduled_year") == checkpoint_year
                        and item.get("tick") == str(expected_tick)
                        for item in manifest.get("checkpoints", [])
                    )
                    if not already:
                        _add_checkpoint(
                            manifest, manifest_path, repository, scope, world_id, branch_id,
                            start_tick=start_tick, day_seconds=config["day_seconds"],
                            year_days=config["year_days"], scheduled_year=checkpoint_year, label="decade",
                        )
            if deferred_verification:
                if final_job.committed_through_tick != final_view.state.current_tick:
                    raise ProbeError("The paused job does not match the committed world clock.")
                manifest.update({
                    "status": final_job.status.value,
                    "last_tick": str(final_view.state.current_tick),
                    "last_state_version": str(final_view.branch.state_version),
                    "last_state_hash": final_view.state_hash,
                    "last_job_status": final_job.status.value,
                    "outcomes": outcomes,
                    "stop_reasons": list(dict.fromkeys(stop_reasons)),
                    "windows_this_invocation": windows,
                })
                _atomic_json(manifest_path, manifest)
                # Release the directory for maintenance or supervised recovery.
                # The resumed job still performs final verification at its target.
                return {
                    "probe": "world_advance_capacity",
                    "schema": 1,
                    "status": pause_code,
                    "success": False,
                    "simulation_completed": False,
                    "world_id": str(world_id),
                    "branch_id": str(branch_id),
                    "job_id": str(final_job.job_id),
                    "committed_tick": str(final_view.state.current_tick),
                    "last_sequence": str(final_view.last_seq),
                    "state_version": str(final_view.branch.state_version),
                    "state_hash": final_view.state_hash,
                    "manifest": str(manifest_path),
                    "outcomes": outcomes,
                    "stop_reasons": list(dict.fromkeys(stop_reasons)),
                }, 2
            _add_checkpoint(
                manifest, manifest_path, repository, scope, world_id, branch_id,
                start_tick=start_tick, day_seconds=config["day_seconds"],
                year_days=config["year_days"], scheduled_year=None,
                label=(
                    "target"
                    if final_job.status == JobStatus.SUCCEEDED
                    and final_view.state.current_tick == target_tick
                    else "partial"
                ),
            )
            final_view = repository.read_world(scope, world_id, branch_id)
            final_job = repository.read_job(scope, job.job_id)
            storage = _storage_metrics(database, str(world_id))
            integrity = _sqlite_integrity(database)
            simulation_completed = (
                final_job.status == JobStatus.SUCCEEDED
                and final_view.state.current_tick == target_tick
                and final_job.committed_through_tick == target_tick
            )
            audit_from_genesis = args.audit_from_genesis or (simulation_completed and config["normalized_years"] >= 600)
            capacity_minima: dict[str, dict[str, int | str]] = {}
            first_population_shortfall: dict[str, int | str] | None = None
            states_audited = 0

            def observe_commit(sequence: int, state: WorldState) -> None:
                nonlocal states_audited, first_population_shortfall
                states_audited += 1
                sample = _capacity_sample(state)
                for metric, count in sample.items():
                    previous = capacity_minima.get(metric)
                    if previous is None or count < int(previous["count"]):
                        capacity_minima[metric] = {"count": count, "sequence": str(sequence), "tick": str(state.current_tick)}
                if sample["active_named_people"] < 240 and first_population_shortfall is None:
                    first_population_shortfall = {"count": sample["active_named_people"], "sequence": str(sequence),
                                                  "tick": str(state.current_tick)}

            replay_started = time.perf_counter()
            replay = repository.replay(
                scope, world_id, branch_id, from_genesis=audit_from_genesis,
                visit_commit=observe_commit if audit_from_genesis else None,
            )
            replay_seconds = round(time.perf_counter() - replay_started, 3)
            replay_ok = (
                replay.state_hash == final_view.state_hash
                and replay.branch.state_version == final_view.branch.state_version
                and replay.state.current_tick == final_view.state.current_tick
            )
            archive = _archive_metrics(repository, scope, world_id, directory=directory, limits=backup_limits)
            duration_elapsed = time.perf_counter() - started
            success = (
                simulation_completed and replay_ok
                and integrity["integrity_ok"] and integrity["foreign_keys_ok"]
            )
            manifest.update({
                "status": final_job.status.value,
                "last_tick": str(final_view.state.current_tick),
                "last_state_version": str(final_view.branch.state_version),
                "last_state_hash": final_view.state_hash,
                "last_job_status": final_job.status.value,
                "outcomes": outcomes,
                "stop_reasons": list(dict.fromkeys(stop_reasons)),
                "windows_this_invocation": windows,
            })
            _atomic_json(manifest_path, manifest)

            report = {
                "probe": "world_advance_capacity",
                "schema": 1,
                "status": "completed" if success else "incomplete_or_unverified",
                "success": success,
                "success_scope": "exact target reached, final replay matched the head, and SQLite integrity checks passed; archive policy is reported separately",
                "full_600_year_acceptance_certification": False,
                "archive_within_current_policy": archive.get("status") == "exported",
                "simulation_completed": simulation_completed,
                "run_scope": {
                    "pack_id": args.pack,
                    "pack_version": config["pack_version"],
                    "seed": args.seed,
                    "catalog_seeded": config.get("catalog_seeded", False),
                    "population_mode": config.get("population_mode", "natural"),
                    "seed_semantics": config["seed_semantics"],
                    "data_directory": str(directory),
                    "world_id": str(world_id),
                    "branch_id": str(branch_id),
                    "job_id": str(final_job.job_id),
                    "decision_mode": config.get("decision_mode", "deterministic_no_model"),
                },
                "target": {
                    "start_tick": str(start_tick),
                    "target_tick": str(target_tick),
                    "committed_tick": str(final_view.state.current_tick),
                    "committed_through_tick": (
                        str(final_job.committed_through_tick)
                        if final_job.committed_through_tick is not None else None
                    ),
                    "job_status": final_job.status.value,
                    "duration": {
                        "declared_years": config["declared_years"],
                        "declared_days": config["declared_days"],
                        "total_days": config["total_days"],
                        "normalized_years": config["normalized_years"],
                        "partial_year_days": config["partial_year_days"],
                        "day_seconds": config["day_seconds"],
                        "year_days": config["year_days"],
                        "total_seconds": config["total_seconds"],
                    },
                    "elapsed_calendar_days": final_elapsed_days,
                    "elapsed_whole_years": final_elapsed_days // config["year_days"],
                    "elapsed_partial_year_days": final_elapsed_days % config["year_days"],
                    "overshoot_ticks": str(final_view.state.current_tick - target_tick),
                },
                "worker": {
                    "windows_this_invocation": windows,
                    "outcomes_this_invocation": outcomes,
                    "stop_reasons": list(dict.fromkeys(stop_reasons)),
                    "no_model_dispatcher": decision_mode == "deterministic_no_model",
                    "elapsed_seconds": round(duration_elapsed, 3),
                    "windows_per_second": round(windows / duration_elapsed, 4) if duration_elapsed else None,
                },
                "model_decision_dispatch": {
                    "mode": decision_mode,
                    "requests": outcomes.get("decision_requests", 0),
                    "calls_attempted": outcomes.get("model_calls_attempted", 0),
                    "attempts_by_number": {
                        str(number): outcomes.get(f"model_call_attempt_{number}", 0)
                        for number in range(1, len(MODEL_DECISION_RETRY_BACKOFF_SECONDS) + 2)
                    },
                    "retry_attempts": outcomes.get("model_retry_attempts", 0),
                    "unbilled_network_failures": outcomes.get("model_unbilled_network_failures", 0),
                    "decisions_accepted": outcomes.get("model_decisions_accepted", 0),
                    "call_failures": outcomes.get("model_call_failures", 0),
                    "user_owned_requests": outcomes.get("user_decision_requests", 0),
                    "metering_scope": (
                        "project_local_operations_database"
                        if decision_mode == "configured_local_model" else None
                    ),
                    "world_database_isolated": True,
                },
                "checkpoints": manifest.get("checkpoints", []),
                "final_counts": _state_counts(final_view.state),
                "events": storage,
                "verification": {
                    **integrity,
                    "replay_from_genesis": audit_from_genesis,
                    "replay_seconds": replay_seconds,
                    "replay_matches_head": replay_ok,
                    "stored_state_hash": final_view.state_hash,
                    "replayed_state_hash": replay.state_hash,
                    "state_version": str(final_view.branch.state_version),
                    "last_sequence": str(final_view.last_seq),
                },
                "continuous_capacity_audit": {
                    "committed_states_audited": states_audited,
                    "minima": capacity_minima,
                    "first_below_240_active_named_people": first_population_shortfall,
                } if audit_from_genesis else None,
                "sqlite_size_bytes_before_close": _disk_sizes(directory),
                "archive": archive,
                "limits": {
                    "compressed_archive_bytes": backup_limits.compressed_bytes,
                    "uncompressed_archive_bytes": backup_limits.uncompressed_bytes,
                },
                "started_at_utc": started_at.isoformat(),
                "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "manifest": str(manifest_path),
            }
            return report, 0 if success else 2
        finally:
            database.dispose()


def main() -> int:
    catalog = ContentCatalog()
    pack_ids = [item["pack_id"] for item in catalog.list_packs()]
    parser = _build_parser(pack_ids)
    args = parser.parse_args()
    try:
        report, exit_code = run(args, catalog)
        report["sqlite_size_bytes_after_close"] = _disk_sizes(args.data_directory.expanduser().resolve())
        if args.output is not None:
            output_path = args.output.expanduser().resolve()
            report["report_file"] = str(output_path)
            _atomic_json(output_path, report)
        print("FINAL " + _canonical_json(report), flush=True)
        return exit_code
    except ProbeError as exc:
        print(f"probe error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "probe interrupted; the persisted job can be resumed with the same arguments and data directory",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        print(f"probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
