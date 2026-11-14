"""Persistent model configuration and call auditing, separate from world facts."""

from __future__ import annotations

from contextlib import contextmanager
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from world_engine.adapters.models.contracts import (
    CallTrace, DataPolicy, ModelRequest, ProviderConfig, ProviderKind, RoleTask,
)
from world_engine.adapters.models.errors import ProviderError
from world_engine.domain.errors import Conflict, NotFound
from world_engine.domain.models import AccessScope


class OperationsStore:
    """Small versioned operational database; never contains prompts or secrets.

    Configuration is scoped to both tenant and account. Network requests happen
    outside connections/transactions. A trace is persisted independently of the
    eventual world commit, so failed world actions still retain incurred usage.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("Operational database requires a newer application")
            if version == 0:
                for statement in """
                    CREATE TABLE providers (
                        tenant TEXT NOT NULL, principal TEXT NOT NULL, id TEXT NOT NULL,
                        document TEXT NOT NULL, PRIMARY KEY(tenant, principal, id)
                    );
                    CREATE TABLE preferences (
                        tenant TEXT NOT NULL, principal TEXT NOT NULL,
                        document TEXT NOT NULL, PRIMARY KEY(tenant, principal)
                    );
                    CREATE TABLE calls (
                        tenant TEXT NOT NULL, principal TEXT NOT NULL, id TEXT NOT NULL,
                        started_at TEXT NOT NULL, document TEXT NOT NULL,
                        PRIMARY KEY(tenant, principal, id)
                    );
                    CREATE INDEX calls_recent ON calls(tenant, principal, started_at DESC);
                    PRAGMA user_version = 1;
                """.split(";"):
                    if statement.strip():
                        db.execute(statement)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def identity(scope: AccessScope):
        return str(scope.tenant_id), str(scope.user_id)

    @staticmethod
    def credential_target(scope: AccessScope, provider_id: str) -> str:
        return f"world-engine/{scope.tenant_id}/{scope.user_id}/{provider_id}"

    def list_providers(self, scope: AccessScope) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT document FROM providers WHERE tenant=? AND principal=? ORDER BY id", self.identity(scope))
            return [json.loads(row[0]) for row in rows]

    def provider(self, scope: AccessScope, provider_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT document FROM providers WHERE tenant=? AND principal=? AND id=?", (*self.identity(scope), provider_id)).fetchone()
        if row is None:
            raise NotFound("Provider does not exist")
        return json.loads(row[0])

    def save_provider(self, scope: AccessScope, document: dict[str, Any], provider_id: str | None = None):
        # Instantiate the adapter's validated configuration before persistence.
        provider_id = provider_id or str(uuid4())
        config = ProviderConfig(provider_id=provider_id, kind=ProviderKind(document["kind"]),
                                base_url=document["base_url"], default_model=document.get("default_model"),
                                secret_ref=document.get("secret_ref"))
        if config.secret_ref and config.secret_ref.startswith("os:"):
            if config.secret_ref != "os:" + self.credential_target(scope, provider_id):
                raise ValueError("Credential reference belongs to a different account or provider")
        result = config.public_dict() | {"name": document["name"], "enabled": document.get("enabled", True)}
        with self.connect() as db:
            db.execute("INSERT INTO providers VALUES(?,?,?,?) ON CONFLICT(tenant,principal,id) DO UPDATE SET document=excluded.document",
                       (*self.identity(scope), provider_id, json.dumps(result, ensure_ascii=False, allow_nan=False)))
        return result

    def settings(self, scope: AccessScope) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT document FROM preferences WHERE tenant=? AND principal=?", self.identity(scope)).fetchone()
        return json.loads(row[0]) if row else {"speed": "balanced", "depth": "deep", "routes": {}, "local_only": False}

    def save_settings(self, scope: AccessScope, settings: dict[str, Any]):
        # Role and target validation also prevents silently accepting misspelled routes.
        for role, route in settings["routes"].items():
            RoleTask(role)
            if route.get("provider_id"):
                provider = self.provider(scope, route["provider_id"])
                if not provider["enabled"]:
                    raise Conflict("A disabled provider cannot be selected")
        with self.connect() as db:
            db.execute("INSERT INTO preferences VALUES(?,?,?) ON CONFLICT(tenant,principal) DO UPDATE SET document=excluded.document",
                       (*self.identity(scope), json.dumps(settings, allow_nan=False)))
        return settings

    def record_trace(self, scope: AccessScope, trace: CallTrace):
        data = trace.to_public_dict()
        with self.connect() as db:
            db.execute("INSERT INTO calls VALUES(?,?,?,?,?) ON CONFLICT(tenant,principal,id) DO UPDATE SET document=excluded.document",
                       (*self.identity(scope), trace.request_id, trace.started_at.isoformat(), json.dumps(data, allow_nan=False)))

    def list_calls(self, scope: AccessScope, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT document FROM calls WHERE tenant=? AND principal=? ORDER BY started_at DESC LIMIT ?",
                              (*self.identity(scope), min(500, max(1, limit))))
            return [json.loads(row[0]) for row in rows]

    @staticmethod
    def config(document: dict[str, Any]) -> ProviderConfig:
        return ProviderConfig(**{key: document[key] for key in (
            "provider_id", "kind", "base_url", "default_model", "secret_ref", "max_retries", "capability_overrides", "allow_insecure_http"
        ) if key in document})


class ModelOperations:
    def __init__(self, store: OperationsStore, gateway: Any):
        self.store, self.gateway = store, gateway

    async def generate(self, scope: AccessScope, request: ModelRequest):
        settings = self.store.settings(scope)
        target = settings["routes"].get(request.role_task.value, {})
        if not target.get("provider_id"):
            raise Conflict("Configure a model route before playing")
        document = self.store.provider(scope, target["provider_id"])
        if not document["enabled"]:
            raise Conflict("The selected provider is disabled")
        policy = DataPolicy.LOCAL_ONLY if settings["local_only"] else request.data_policy
        request = replace(request, model=target.get("model") or document.get("default_model"), data_policy=policy)
        try:
            result = await self.gateway.generate(self.store.config(document), request)
        except asyncio.CancelledError as error:
            trace = getattr(error, "call_trace", None)
            if trace is not None:
                self.store.record_trace(scope, trace)
            raise
        except ProviderError as error:
            if error.call_trace is not None:
                self.store.record_trace(scope, error.call_trace)
            raise
        self.store.record_trace(scope, result.call_trace)
        return result

    async def discover(self, scope: AccessScope, provider_id: str):
        document = self.store.provider(scope, provider_id)
        if not document["enabled"]:
            raise Conflict("The selected provider is disabled")
        models = await self.gateway.discover(self.store.config(document))
        return [model.to_public_dict() for model in models]
