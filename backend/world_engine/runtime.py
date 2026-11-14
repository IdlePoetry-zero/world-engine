"""Explicit local application bootstrap; importing this module changes no state."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from alembic import command
from alembic.config import Config

from world_engine.adapters.models.contracts import RoleTask
from world_engine.adapters.models.gateway import ModelGateway
from world_engine.adapters.models.secrets import EnvironmentSecretResolver
from world_engine.api.app import create_app
from world_engine.api.security import LocalSession
from world_engine.application.operations import ModelOperations, OperationsStore
from world_engine.application.worlds import WorldApplication
from world_engine.domain.models import AccessScope
from world_engine.storage.database import Database
from world_engine.storage.repository import WorldRepository


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
    configuration.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
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
        "speed": "balanced", "depth": "deep", "local_only": False,
        "routes": {role.value: {"provider_id": deepseek["provider_id"], "model": "deepseek-flash"}
                   for role in RoleTask if role is not RoleTask.EMBEDDING},
    })


def build_application(*, data_directory: Path | None = None, catalog=None):
    directory = (data_directory or PROJECT_ROOT / ".data").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    database_url = os.environ.get("WORLD_ENGINE_DATABASE_URL", f"sqlite:///{(directory / 'world-engine.db').as_posix()}")
    if database_url.startswith("sqlite:") and sqlite3.sqlite_version_info < (3, 51, 3):
        raise RuntimeError("SQLite 3.51.3 or newer is required; use the project's patched runtime")
    identity = local_identity(directory)
    migrate(database_url)
    database = Database(database_url)
    try:
        if catalog is None:
            from world_engine.content import ContentCatalog
            catalog = ContentCatalog()
        store = OperationsStore(directory / "operations.db")
        initialize_model_settings(store, identity)
        gateway = ModelGateway(secret_resolver=EnvironmentSecretResolver({"WORLD_ENGINE_DEEPSEEK_API_KEY"}))
        operations = ModelOperations(store, gateway)
        origins = frozenset({"http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:5173", "http://localhost:5173"})
        session = LocalSession(str(identity.user_id), str(identity.tenant_id), origins)
        app = create_app(WorldApplication(WorldRepository(database), catalog), session,
                         operations=operations, static_directory=PROJECT_ROOT / "frontend" / "dist")

        @asynccontextmanager
        async def lifespan(_app):
            try:
                yield
            finally:
                database.dispose()

        app.router.lifespan_context = lifespan
        return app
    except BaseException:
        database.dispose()
        raise


def main():
    import uvicorn
    # One authoritative local API process. Background jobs still require leases;
    # account/network deployment uses a separate explicitly configured entrypoint.
    uvicorn.run(build_application(), host="127.0.0.1", port=8000, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
