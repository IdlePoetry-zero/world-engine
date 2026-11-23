"""Explicit local application bootstrap; importing this module changes no state."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
from dataclasses import replace
import logging
import json
import os
from pathlib import Path
import sqlite3
from threading import Lock
from uuid import uuid4

from alembic import command
from alembic.config import Config

from world_engine.adapters.models.contracts import RoleTask
from world_engine.adapters.models.gateway import ModelGateway
from world_engine.adapters.models.secrets import CompositeSecretResolver, EnvironmentSecretResolver
from world_engine.adapters.credentials import WindowsCredentialStore
from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.operations import ModelOperations, OperationsStore
from world_engine.application.model_decisions import invoke_model_decision
from world_engine.application.pricing import bundled_price
from world_engine.application.worlds import WorldApplication
from world_engine.application.conversation import ConversationApplication, ConversationStore
from world_engine.application.instance_lock import LocalInstanceLock
from world_engine.application.credentials import CredentialService, AccountCredentialResolver
from world_engine.application.authoring import AuthoringApplication
from world_engine.application.actions import RulesActionDispatcher
from world_engine.application.backups import BackupLimits, DEFAULT_LIMITS
from world_engine.application.media_contracts import MediaRegistry
from world_engine.application.media_assets import MediaAssetStore
from world_engine.application.media_authorization import RepositoryMediaAuthorizer
from world_engine.application.media_jobs import MediaJobWorker
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.rules.contracts import ACTION_INTENT_ADAPTER, RuleContext
from world_engine.rules.engine import RuleEngine
from world_engine.domain.models import AccessScope
from world_engine.domain.errors import Conflict, InvalidLease, StorageBusy
from world_engine.domain.models import JobStatus
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository
from world_engine.storage.backup_import import WorldBackupImporter


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def local_identity(directory: Path) -> AccessScope:
    """Publish a complete identity file atomically even with concurrent startup."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "local-identity.json"
    if not target.exists():
        proposed = {"tenant_id": str(uuid4()), "user_id": str(uuid4())}
        temporary = directory / f".identity-{uuid4()}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(proposed, handle)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    # Corruption must fail visibly; never generate a different owner over an
    # existing world database and make a player's saves appear lost.
    return AccessScope.model_validate_json(target.read_text(encoding="utf-8"))


def migrate(database_url: str):
    configuration = Config(str(PROJECT_ROOT / "backend" / "migrations" / "alembic.ini"))
    configuration.attributes["database_url"] = database_url
    command.upgrade(configuration, "head")


def initialize_model_settings(store: OperationsStore, identity: AccessScope):
    if store.list_providers(identity):
        return
    deepseek = store.save_provider(identity, {
        "kind": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-flash", "secret_ref": "env:WORLD_ENGINE_DEEPSEEK_API_KEY", "enabled": True,
    }, "deepseek")
    store.save_provider(identity, {
        "kind": "ollama", "name": "本地 Ollama", "base_url": "http://127.0.0.1:11434",
        "default_model": "qwen3:8b-q4_k_m", "secret_ref": None, "enabled": True,
    }, "ollama")
    store.save_settings(identity, {
        "speed": "balanced", "depth": "deep", "local_only": True,
        "routes": {
            role.value: {
                "provider_id": deepseek["provider_id"], "model": "deepseek-flash",
                "fallbacks": [{"provider_id": "ollama"}],
            }
            for role in RoleTask if role is not RoleTask.EMBEDDING
        },
    })


async def _invoke_worker_model_decision(operations, repository, scope, lease, job, decision):
    """Dispatch ordinary choices and typed NPC plan reviews through their own path.

    Keep the planning import lazy so older frozen probe environments can still
    import this runtime when they never encounter an NPC plan review.
    """
    if decision.kind == "npc.plan_review":
        from world_engine.application.model_decisions import invoke_npc_planning

        return await invoke_npc_planning(operations, repository, scope, lease, job, decision)
    if decision.kind == "npc.reflection":
        from world_engine.application.model_decisions import invoke_npc_reflection

        return await invoke_npc_reflection(operations, repository, scope, lease, job, decision)
    if decision.kind == "npc.meeting_attendance":
        from world_engine.application.model_decisions import invoke_autonomous_meeting_attendance

        return await invoke_autonomous_meeting_attendance(operations, repository, scope, lease, job, decision)
    if decision.kind == "organization.vacancy_vote":
        from world_engine.application.autonomous_vacancy_decisions import invoke_autonomous_vacancy_vote

        return await invoke_autonomous_vacancy_vote(operations, repository, scope, lease, job, decision)
    return await invoke_model_decision(operations, scope, lease, job, decision)


def build_application(*, data_directory: Path | None = None, catalog=None, port: int = 8000,
                      run_simulation_worker: bool = True, media_registry: MediaRegistry | None = None,
                      backup_limits: BackupLimits | None = None, compact_ledger: bool = False):
    directory = (data_directory or PROJECT_ROOT / ".data").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    backup_scratch_directory = directory / "backup-scratch"
    backup_scratch_directory.mkdir(parents=True, exist_ok=True)
    database_url = os.environ.get("WORLD_ENGINE_DATABASE_URL", f"sqlite:///{(directory / 'world-engine.db').as_posix()}")
    if database_url.startswith("sqlite:") and sqlite3.sqlite_version_info < (3, 51, 3):
        raise RuntimeError("SQLite 3.51.3 or newer is required; use the project's patched runtime")
    process_lock = LocalInstanceLock(directory / "application.lock")
    database = None
    try:
        identity = local_identity(directory)
        migrate(database_url)
        database = Database(database_url)
        if compact_ledger:
            database.enable_compact_ledger()
        restore_with_compact_ledger = database.uses_compact_ledger()
        if catalog is None:
            from world_engine.content import ContentCatalog
            catalog = ContentCatalog()
        store = OperationsStore(directory / "operations.db")
        initialize_model_settings(store, identity)
        credentials = CredentialService(store, WindowsCredentialStore())
        gateway = ModelGateway(secret_resolver=CompositeSecretResolver([
            EnvironmentSecretResolver({"WORLD_ENGINE_DEEPSEEK_API_KEY"}),
            AccountCredentialResolver(credentials, identity),
        ]), price_resolver=bundled_price)
        operations = ModelOperations(store, gateway)
        origins = frozenset({f"http://127.0.0.1:{port}", f"http://localhost:{port}", "http://127.0.0.1:5173", "http://localhost:5173"})
        cookie_name = "world_engine_session" if port == 8000 else f"world_engine_session_{port}"
        session = LocalSession(str(identity.user_id), str(identity.tenant_id), origins, cookie_name=cookie_name)
        worlds = WorldApplication(WorldRepository(database), catalog)
        conversation_store = ConversationStore(store)
        conversation_store.recover_inflight()
        actions = RulesActionDispatcher(worlds.repository, operations, RuleEngine(), ACTION_INTENT_ADAPTER, RuleContext)
        conversations = ConversationApplication(worlds, operations, conversation_store, actions=actions)
        authoring = AuthoringApplication(worlds.repository, store)
        restore_lock = Lock()

        def restore_copy(_scope, original, upgrade=None):
            verified = original if upgrade is None else upgrade.bind_copy(original)
            target_version = verified.world.ruleset_version
            migration = {"source_ruleset_version": original.world.ruleset_version} if upgrade is not None else {}
            metadata = {"ruleset_version": target_version} | migration
            directory_key = verified.archive_sha256 if upgrade is None else f"{verified.archive_sha256}-ruleset-{target_version}"
            with restore_lock:
                destination = directory / "restored" / directory_key
                destination.mkdir(parents=True, exist_ok=True)
                target_url = f"sqlite:///{(destination / 'world-engine.db').as_posix()}"
                new_owner = local_identity(destination)
                migrate(target_url)
                target = Database(target_url)
                try:
                    repository = WorldRepository(target)
                    existing = repository.list_worlds(new_owner)
                    if existing:
                        if (len(existing) != 1 or existing[0].world_id != verified.world.world_id
                                or existing[0].ruleset_version != target_version
                                or existing[0].pack_version != verified.world.pack_version):
                            raise Conflict("The restore destination belongs to a different world.")
                        return verified.summary() | {"data_directory": str(destination), "duplicate": True} | metadata
                    if restore_with_compact_ledger:
                        target.enable_compact_ledger()
                    imported = WorldBackupImporter(
                        repository, scratch_directory=backup_scratch_directory,
                    ).import_verified_backup(new_owner, verified)
                    return imported.to_dict() | {"data_directory": str(destination), "duplicate": False} | metadata
                finally:
                    target.dispose()

        media_assets = MediaAssetStore(directory / "media")
        media_worker = (MediaJobWorker(worlds.repository, media_registry, media_assets,
                                      RepositoryMediaAuthorizer(worlds.repository))
                        if media_registry is not None else None)
        app = create_app(worlds, session, operations=operations, credentials=credentials, conversations=conversations, authoring=authoring,
                         static_directory=PROJECT_ROOT / "frontend" / "dist", restore_copy=restore_copy,
                         restore_ruleset_copy=restore_copy,
                         media_registry=media_registry, media_assets=media_assets, backup_limits=backup_limits,
                         backup_scratch_directory=backup_scratch_directory)

        @asynccontextmanager
        async def lifespan(_app):
            stop = asyncio.Event()
            jobs = SimulationJobs(worlds.repository)
            worker_id = f"local:{uuid4()}"

            async def resolve_model_decision(lease, decision):
                job = await asyncio.to_thread(worlds.repository.read_job, identity, lease.job_id)
                try:
                    return await _invoke_worker_model_decision(
                        operations, worlds.repository, identity, lease, job, decision,
                    )
                except StorageBusy:
                    raise
                except Exception:
                    try:
                        await asyncio.to_thread(worlds.repository.finish_job, lease, JobStatus.PAUSED,
                                                result={"error": "MODEL_DECISION_UNAVAILABLE"})
                    except InvalidLease:
                        pass
                    raise

            async def advance_without_model(lease):
                while True:
                    try:
                        return await asyncio.to_thread(jobs.advance, identity, lease)
                    except StorageBusy:
                        if stop.is_set():
                            raise
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=0.5)
                        except TimeoutError:
                            continue
                        raise

            async def run_jobs():
                while not stop.is_set():
                    try:
                        lease = await asyncio.to_thread(
                            worlds.repository.claim_job, worker_id,
                            job_types=("simulation.long_action", "simulation.world_advance"), lease_seconds=120,
                        )
                        while lease is not None and not stop.is_set():
                            outcome = await advance_without_model(lease)
                            pending = getattr(outcome, "decision_requests", ())
                            if (len(pending) == 1 and not outcome.events
                                and pending[0].decision_owner == "model"
                                and (pending[0].kind == "npc.plan_review" or pending[0].options)):
                                lease = await asyncio.to_thread(worlds.repository.renew_job, lease, lease_seconds=120)
                                answer = await resolve_model_decision(lease, pending[0])
                                outcome = await asyncio.to_thread(jobs.advance, identity, lease, model_decision=answer)
                            if getattr(outcome, "outcome", None) != "advanced":
                                break
                            lease = await asyncio.to_thread(worlds.repository.renew_job, lease, lease_seconds=120)
                    except (Conflict, StorageBusy):
                        pass
                    except Exception as error:
                        logging.getLogger(__name__).error("Simulation worker failure: %s", type(error).__name__)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=0.5)
                    except TimeoutError:
                        pass

            async def run_media_jobs():
                while not stop.is_set():
                    try:
                        if await media_worker.process_one(worker_id) is not None:
                            continue
                    except Exception as error:
                        logging.getLogger(__name__).error("Media worker failure: %s", type(error).__name__)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=0.5)
                    except TimeoutError:
                        pass

            worker = asyncio.create_task(run_jobs()) if run_simulation_worker else None
            media_task = asyncio.create_task(run_media_jobs()) if media_worker is not None else None
            try:
                yield
            finally:
                stop.set()
                if worker is not None:
                    await worker
                if media_task is not None:
                    media_task.cancel()
                    await asyncio.gather(media_task, return_exceptions=True)
                database.dispose()
                process_lock.close()

        app.router.lifespan_context = lifespan
        return app
    except BaseException:
        if database is not None:
            database.dispose()
        process_lock.close()
        raise


def main():
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description="Start the local world engine.")
    parser.add_argument("--data-directory", type=Path, help="Open an independent local data directory.")
    parser.add_argument("--compact-ledger", action="store_true", help="Use compact ledger storage in a new data directory.")
    parser.add_argument("--port", type=int, default=8000, help="Loopback HTTP port.")
    parser.add_argument("--windows-speech", action="store_true", help="Enable the local Windows speech plugin.")
    parser.add_argument("--windows-speech-voice", help="Use a named installed Windows voice.")
    mib = 1024 * 1024
    parser.add_argument("--backup-compressed-mib", type=int, default=DEFAULT_LIMITS.compressed_bytes // mib,
                        help="Compressed upload and download limit in MiB.")
    parser.add_argument("--backup-expanded-mib", type=int, default=DEFAULT_LIMITS.uncompressed_bytes // mib,
                        help="Expanded archive limit in MiB.")
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if arguments.data_directory is not None and os.environ.get("WORLD_ENGINE_DATABASE_URL"):
        parser.error("Unset WORLD_ENGINE_DATABASE_URL before opening an independent data directory.")
    if arguments.compact_ledger and arguments.data_directory is None:
        parser.error("--compact-ledger requires an explicit --data-directory.")
    if arguments.windows_speech_voice and not arguments.windows_speech:
        parser.error("--windows-speech-voice requires --windows-speech.")
    if arguments.backup_compressed_mib <= 0 or arguments.backup_expanded_mib <= 0:
        parser.error("Backup limits must be positive.")
    backup_limits = replace(DEFAULT_LIMITS, compressed_bytes=arguments.backup_compressed_mib * mib,
                            uncompressed_bytes=arguments.backup_expanded_mib * mib)
    media_registry = None
    if arguments.windows_speech:
        from world_engine.adapters.media.windows_speech import WindowsSpeechError, register_windows_speech

        media_registry = MediaRegistry()
        try:
            register_windows_speech(media_registry, voice_name=arguments.windows_speech_voice)
        except WindowsSpeechError as error:
            parser.error(str(error))
    # One authoritative local API process. Background jobs still require leases;
    # account/network deployment uses a separate explicitly configured entrypoint.
    uvicorn.run(build_application(data_directory=arguments.data_directory, port=arguments.port,
                                  media_registry=media_registry, backup_limits=backup_limits,
                                  compact_ledger=arguments.compact_ledger),
                host="127.0.0.1", port=arguments.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
