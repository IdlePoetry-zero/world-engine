"""Persistent model configuration and call auditing, separate from world facts."""

from __future__ import annotations

from contextlib import contextmanager
import asyncio
from dataclasses import replace
import ipaddress
import json
from pathlib import Path
import sqlite3
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from world_engine.adapters.models.contracts import (
    CallTrace, DataPolicy, FinishReason, ModelRequest, ProviderConfig, ProviderKind, ProviderUsage, RoleTask, utc_now,
)
from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode, TransportFailureKind
from world_engine.domain.errors import Conflict, NotFound
from world_engine.domain.models import AccessScope
from .errors import ApplicationFailure
from .model_scheduler import DEFAULT_ADMISSION_SCHEDULER, EndpointAdmissionScheduler


_NPC_REFLECTION_SCHEMA_REFS = frozenset({
    "npc_reflection_goal_completion_v1",
    "npc_reflection_npc_plan_review_v1",
    "npc_reflection_evidence_conflict_v1",
    "npc_reflection_investigation_case_contested_v1",
})


class OperationsStore:
    """Versioned operations and player discussions; no provider credentials.

    Configuration is scoped to both tenant and account. Network requests happen
    outside connections/transactions. A trace is persisted independently of the
    eventual world commit, so failed world actions still retain incurred usage.
    Conversation tables retain player text and GM answers, not constructed model
    prompts or hidden model reasoning. Call audit tables contain neither.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 4:
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
            if version < 2:
                db.execute("""CREATE TABLE message_requests (
                    tenant TEXT NOT NULL, principal TEXT NOT NULL, id TEXT NOT NULL,
                    world_id TEXT NOT NULL, branch_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL,
                    input_text TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL,
                    PRIMARY KEY(tenant, principal, id)
                )""")
                db.execute("CREATE INDEX message_history ON message_requests(tenant,principal,world_id,branch_id,session_id,created_at)")
                db.execute("PRAGMA user_version = 2")
            if version < 3:
                db.execute("""CREATE TABLE author_previews (
                    tenant TEXT NOT NULL, principal TEXT NOT NULL, id TEXT NOT NULL,
                    world_id TEXT NOT NULL, document TEXT NOT NULL,
                    PRIMARY KEY(tenant, principal, id)
                )""")
                db.execute("PRAGMA user_version = 3")
            if version < 4:
                db.execute("""CREATE TABLE narrative_rewrites (
                    tenant TEXT NOT NULL, principal TEXT NOT NULL, narrative_id TEXT NOT NULL,
                    style TEXT NOT NULL, claim_id TEXT, text TEXT,
                    PRIMARY KEY(tenant, principal, narrative_id, style),
                    CHECK ((claim_id IS NULL) != (text IS NULL))
                )""")
                db.execute("PRAGMA user_version = 4")

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

    @staticmethod
    def _provider_origin(base_url: str) -> tuple[str, str, int]:
        """Return the normalized HTTP origin, excluding the endpoint path."""
        parsed = urlsplit(base_url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        try:
            host = ipaddress.ip_address(host).compressed
        except ValueError:
            host = host.encode("idna").decode("ascii").lower()
        port = parsed.port
        if port is None:
            port = 443 if scheme == "https" else 80
        return scheme, host, port

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

    def save_provider(
        self,
        scope: AccessScope,
        document: dict[str, Any],
        provider_id: str | None = None,
        *,
        public_config: bool = False,
    ):
        # Instantiate the adapter's validated configuration before persistence.
        creating = provider_id is None
        provider_id = provider_id or str(uuid4())
        config = ProviderConfig(provider_id=provider_id, kind=ProviderKind(document["kind"]),
                                base_url=document["base_url"], default_model=document.get("default_model"),
                                secret_ref=document.get("secret_ref"), max_retries=document.get("max_retries", 2),
                                capability_overrides=document.get("capability_overrides", {}),
                                allow_insecure_http=document.get("allow_insecure_http", False),
                                ollama_context_tokens=document.get("ollama_context_tokens"))
        if public_config and creating and config.secret_ref and config.secret_ref.startswith("os:"):
            raise ApplicationFailure(
                "PROVIDER_CREDENTIAL_REFERENCE_CONFLICT",
                "Manage saved credentials through the provider credential endpoint.",
            )
        if not public_config and config.secret_ref and config.secret_ref.startswith("os:"):
            if config.secret_ref != "os:" + self.credential_target(scope, provider_id):
                raise ValueError("Credential reference belongs to a different account or provider")
        result = config.public_dict() | {"name": document["name"], "enabled": document.get("enabled", True)}
        with self.connect() as db:
            # Serialize read/check/write so another save cannot change the row
            # between checking its credential binding and persisting this edit.
            db.execute("BEGIN IMMEDIATE")
            existing_row = db.execute(
                "SELECT document FROM providers WHERE tenant=? AND principal=? AND id=?",
                (*self.identity(scope), provider_id),
            ).fetchone()
            if existing_row is not None:
                existing = json.loads(existing_row[0])
                if public_config:
                    # Config edits may omit a read-only reference, but only
                    # credential operations can add, replace, or clear one.
                    existing_ref = existing.get("secret_ref")
                    if config.secret_ref is not None and config.secret_ref != existing_ref:
                        raise ApplicationFailure(
                            "PROVIDER_CREDENTIAL_REFERENCE_CONFLICT",
                            "Manage saved credentials through the provider credential endpoint.",
                        )
                    result["secret_ref"] = existing_ref
                if existing.get("secret_ref") and (
                    existing["kind"] != config.kind.value
                    or self._provider_origin(existing["base_url"]) != self._provider_origin(config.base_url)
                ):
                    raise ApplicationFailure(
                        "PROVIDER_CREDENTIAL_DESTINATION_CONFLICT",
                        "Remove the saved credential before changing the provider kind or endpoint origin.",
                    )
            elif public_config and config.secret_ref and config.secret_ref.startswith("os:"):
                raise ApplicationFailure(
                    "PROVIDER_CREDENTIAL_REFERENCE_CONFLICT",
                    "Manage saved credentials through the provider credential endpoint.",
                )
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
            primary_provider_id = route.get("provider_id")
            fallbacks = route.get("fallbacks", [])
            if fallbacks and not primary_provider_id:
                raise ValueError("Fallback providers require a primary provider")
            targets = ([route] if primary_provider_id else []) + fallbacks
            seen_targets: set[tuple[str, str | None]] = set()
            providers: dict[str, dict[str, Any]] = {}
            for target in targets:
                provider_id = target["provider_id"]
                provider = providers.get(provider_id)
                if provider is None:
                    provider = self.provider(scope, provider_id)
                    providers[provider_id] = provider
                if not provider["enabled"]:
                    raise Conflict("A disabled provider cannot be selected")
                # A missing model means that provider's configured default. This
                # catches duplicates where one entry names that default explicitly.
                model = target.get("model") or provider.get("default_model")
                identity = (provider_id, model)
                if identity in seen_targets:
                    raise ValueError("A role route cannot repeat a provider/model target")
                seen_targets.add(identity)
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
            "provider_id", "kind", "base_url", "default_model", "secret_ref", "max_retries", "capability_overrides", "allow_insecure_http",
            "ollama_context_tokens",
        ) if key in document})


class ModelOperations:
    def __init__(
        self,
        store: OperationsStore,
        gateway: Any,
        *,
        scheduler: EndpointAdmissionScheduler = DEFAULT_ADMISSION_SCHEDULER,
    ):
        self.store, self.gateway, self.scheduler = store, gateway, scheduler

    async def generate(self, scope: AccessScope, request: ModelRequest):
        settings = self.store.settings(scope)
        routes = settings["routes"]
        role_route_key = request.role_task.value
        if request.role_task is RoleTask.NPC_PLAN_REVIEW and role_route_key not in routes:
            # Existing settings predate the dedicated reviewer route. Keep the
            # request's NPC_PLAN_REVIEW identity while temporarily routing it
            # through the already-configured NPC decision target.
            primary = routes.get(RoleTask.NPC_DECISION.value, {})
        elif request.role_task is RoleTask.NARRATION and role_route_key not in routes:
            primary = routes.get(RoleTask.GM_ANSWER.value, {})
        else:
            primary = routes.get(role_route_key, {})
        if not primary.get("provider_id"):
            raise ApplicationFailure("MODEL_ROUTE_REQUIRED", "尚未配置当前任务的模型。请在模型后台选择供应商和模型后重试。")
        policy = DataPolicy.LOCAL_ONLY if settings["local_only"] else request.data_policy
        target_specs = [primary, *primary.get("fallbacks", [])]
        has_alternatives = len(target_specs) > 1
        eligible: list[tuple[dict[str, Any], dict[str, Any], ProviderConfig]] = []
        unavailable_errors: list[Exception] = []
        for target in target_specs:
            try:
                document = self.store.provider(scope, target["provider_id"])
            except NotFound as error:
                if not has_alternatives:
                    raise
                unavailable_errors.append(error)
                continue
            if not document["enabled"]:
                error = self._disabled_provider_error()
                if not has_alternatives:
                    raise error
                unavailable_errors.append(error)
                continue
            config = self.store.config(document)
            if policy is DataPolicy.LOCAL_ONLY and not config.is_local_endpoint:
                unavailable_errors.append(self._local_policy_error(config, self._target_request(request, target, document, policy)))
                continue
            eligible.append((target, document, config))

        if not eligible:
            # Preserve the primary route's historical error when every saved
            # target has since become unavailable or disallowed.
            first_error = unavailable_errors[0] if unavailable_errors else None
            if isinstance(first_error, ProviderError) and first_error.call_trace is not None:
                self.store.record_trace(scope, first_error.call_trace)
            if first_error is not None:
                raise first_error
            raise ApplicationFailure("MODEL_ROUTE_REQUIRED", "尚未配置当前任务的模型。请在模型后台选择供应商和模型后重试。")

        for index, (target, document, config) in enumerate(eligible):
            target_request = self._target_request(request, target, document, policy)
            if index > 0:
                original_context = target_request.trace_context
                target_request = replace(
                    target_request,
                    trace_context=replace(
                        original_context,
                        request_id=str(uuid4()),
                        parent_trace=original_context.parent_trace or original_context.request_id,
                    ),
                )
            try:
                return await self._generate_target(scope, target_request, config)
            except ProviderError as error:
                if index + 1 >= len(eligible) or not self._allows_cross_provider_fallback(error):
                    raise
        # The list is non-empty and every call either returns or raises at its
        # terminal target, so reaching here means the loop itself was changed.
        raise RuntimeError("Model route had no executable target")

    @staticmethod
    def _disabled_provider_error() -> ApplicationFailure:
        return ApplicationFailure("PROVIDER_DISABLED", "当前任务使用的供应商已停用，请启用它或选择其他供应商。")

    @staticmethod
    def _target_request(request: ModelRequest, target: dict[str, Any], document: dict[str, Any], policy: DataPolicy) -> ModelRequest:
        profile = request.profile
        route_output_limit = target.get("max_output_tokens")
        if request.role_task is not RoleTask.EMBEDDING and route_output_limit is not None:
            is_bounded_npc_reflection = (
                request.role_task is RoleTask.MEMORY_SUMMARY
                and request.schema_ref in _NPC_REFLECTION_SCHEMA_REFS
            )
            if (
                (request.role_task is RoleTask.NPC_PLAN_REVIEW or is_bounded_npc_reflection)
                and profile.max_output_tokens is not None
            ):
                route_output_limit = min(route_output_limit, profile.max_output_tokens)
            profile = replace(profile, max_output_tokens=route_output_limit)
        return replace(
            request,
            model=target.get("model") or document.get("default_model"),
            data_policy=policy,
            profile=profile,
        )

    @staticmethod
    def _local_policy_error(config: ProviderConfig, request: ModelRequest) -> ProviderError:
        error = ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "Data policy forbids sending this request to a non-loopback endpoint",
        )
        context = request.trace_context
        now = utc_now()
        error.call_trace = CallTrace(
            request_id=context.request_id,
            parent_trace=context.parent_trace,
            world_id=context.world_id,
            branch_id=context.branch_id,
            job_id=context.job_id,
            role_task=request.role_task,
            speed_profile=request.profile.speed,
            depth_profile=request.profile.depth,
            data_policy=request.data_policy,
            provider_id=config.provider_id,
            provider_kind=config.kind,
            requested_model=request.model or config.default_model or "unknown",
            reported_model=None,
            started_at=now,
            ended_at=now,
            queue_seconds=None,
            first_token_seconds=None,
            validation_seconds=None,
            attempts=(),
            finish_reason=None,
            usage=ProviderUsage(),
            error_code=error.code.value,
            error_message=error.safe_message,
            end_reason="preflight_rejected",
            max_output_tokens=request.profile.max_output_tokens,
        )
        return error

    @staticmethod
    def _allows_cross_provider_fallback(error: ProviderError) -> bool:
        if error.code is ProviderErrorCode.UNAVAILABLE:
            return True
        if error.code is ProviderErrorCode.NETWORK and error.transport_failure_kind is TransportFailureKind.REMOTE_PROTOCOL:
            return True
        return error.retryable and error.code in {
            ProviderErrorCode.NETWORK,
            ProviderErrorCode.TIMEOUT,
            ProviderErrorCode.RATE_LIMITED,
            ProviderErrorCode.SERVER,
        }

    async def _generate_target(self, scope: AccessScope, request: ModelRequest, config: ProviderConfig):
        enqueued_at = perf_counter()
        queue_seconds = None
        try:
            async with self.scheduler.slot(config, request.profile.speed) as queue_seconds:
                result = await self.gateway.generate(config, request)
        except asyncio.CancelledError as error:
            trace = getattr(error, "call_trace", None)
            if queue_seconds is None:
                trace = self._cancelled_in_queue(config, request, perf_counter() - enqueued_at)
            elif trace is not None:
                trace = replace(trace, queue_seconds=queue_seconds)
            if trace is not None:
                error.call_trace = trace
                self.store.record_trace(scope, trace)
            raise
        except ProviderError as error:
            if error.call_trace is not None:
                error.call_trace = replace(error.call_trace, queue_seconds=queue_seconds)
                self.store.record_trace(scope, error.call_trace)
            raise
        result = replace(result, call_trace=replace(result.call_trace, queue_seconds=queue_seconds))
        self.store.record_trace(scope, result.call_trace)
        return result

    @staticmethod
    def _cancelled_in_queue(config: ProviderConfig, request: ModelRequest, queue_seconds: float) -> CallTrace:
        now = utc_now()
        context = request.trace_context
        return CallTrace(
            request_id=context.request_id, parent_trace=context.parent_trace, world_id=context.world_id,
            branch_id=context.branch_id, job_id=context.job_id, role_task=request.role_task,
            speed_profile=request.profile.speed, depth_profile=request.profile.depth, data_policy=request.data_policy,
            provider_id=config.provider_id, provider_kind=config.kind, requested_model=request.model or "",
            reported_model=None, started_at=now, ended_at=now, queue_seconds=queue_seconds,
            first_token_seconds=None, validation_seconds=None, attempts=(), finish_reason=FinishReason.ABORTED,
            usage=ProviderUsage(), end_reason="cancelled", max_output_tokens=request.profile.max_output_tokens,
        )

    async def discover(self, scope: AccessScope, provider_id: str):
        document = self.store.provider(scope, provider_id)
        if not document["enabled"]:
            raise Conflict("The selected provider is disabled")
        models = await self.gateway.discover(self.store.config(document))
        return [model.to_public_dict() for model in models]
