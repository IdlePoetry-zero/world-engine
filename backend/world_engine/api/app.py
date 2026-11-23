"""Application composition and HTTP world routes.

The factory takes real application services; tests can inject isolated database
instances. Startup/bootstrap is separate from import to avoid creating files or
running migrations as an import side effect.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import SpooledTemporaryFile
import hashlib
import hmac
from typing import Annotated, BinaryIO, Callable, Literal
from uuid import UUID, uuid4, uuid5
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from world_engine.application.visibility import project_event, project_player_state
from world_engine.application.long_action_readback import current_job_reason_code, work_purchase_affordability
from world_engine.application.work_chat import RoutineWorkScheduleRequest, build_routine_work_plan
from world_engine.application.worlds import WorldApplication
from world_engine.application.world_pack_import import WorldPackApplication
from world_engine.application.ruleset_migrations import (
    RulesetUpgrade, available_ruleset_upgrades, require_ruleset_upgrade,
)
from world_engine.application.operations import ModelOperations
from world_engine.application.credentials import CredentialService
from world_engine.application.conversation import ConversationApplication
from world_engine.application.errors import (
    ApplicationFailure, MODEL_OUTPUT_TRUNCATED_MESSAGE, OLLAMA_CONTEXT_LIMIT_MESSAGE,
    STORAGE_BUSY_CODE, STORAGE_BUSY_MESSAGE,
)
from world_engine.application.authoring import AuthoringApplication
from world_engine.application.author_rule_checks import AuthorRuleConflict
from world_engine.application.backups import (
    BackupApplication, BackupArchiveError, BackupLimits, DEFAULT_LIMITS, MAX_COMPRESSED_ARCHIVE_BYTES, VerifiedWorldBackup,
)
from world_engine.application.media_contracts import AssetScope, AuthorizedMediaSource, MediaRegistry
from world_engine.application.media_assets import MediaAssetAuth, MediaAssetError, MediaAssetNotFound, MediaAssetStore
from world_engine.application.media_authorization import RepositoryMediaAuthorizer
from world_engine.application.media_jobs import MediaJobEnqueuer, MediaJobEnvelope
from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode
from world_engine.adapters.credentials import CredentialStoreError
from world_engine.domain.errors import AccessDenied, Conflict, DomainError, InsufficientStorage, NotFound, StaleVersion, StorageBusy
from world_engine.domain.components import NeedsComponent, PlaceComponent, SkillsComponent
from world_engine.domain.models import MembershipRole
from world_engine.domain.models import AccessScope
from world_engine.domain.work_shift import WorkShiftError
from world_engine.domain.travel_itinerary import (
    plan_travel_itinerary, revalidate_travel_itinerary_leg, travel_itinerary_journey_id,
)
from world_engine.domain.travel_itinerary_progress import itinerary_requires_replan
from world_engine.rules.engine import route_access_reason

from .contracts import (
    BranchRequest, ChallengeRequest, ContestRequest, ContinueAfterDeathRequest, CreateWorldRequest, JobControlRequest, JobDecisionRequest,
    LongActionRequest, MediaJobRequest, WorldAdvanceRequest,
)
from world_engine.application.simulation_jobs import SimulationJobs
from world_engine.application.study_chat import study_safety_stop
from world_engine.simulation import LongActionPlan
from world_engine.simulation.contracts import RoutineWorkSchedule, WorldAdvancePlan
from .security import IdentityProvider, LocalSession, LocalSessionMiddleware
from .serialization import wire
from .world_packs import world_pack_router


def _work_schedule_request(schedule: RoutineWorkSchedule | None) -> RoutineWorkScheduleRequest | None:
    """Recover the submitted form fields from an already-resolved durable schedule."""
    if schedule is None:
        return None
    fields = schedule.model_dump(mode="python", exclude={"purchase_target"})
    if schedule.purchase_target is not None:
        fields["purchase_target"] = schedule.purchase_target.model_dump(
            mode="python", include={"listing_id", "quantity"},
        )
    return RoutineWorkScheduleRequest.model_validate(fields)


class BackupDownload(StreamingResponse):
    """Own the staged archive until the response finishes or disconnects."""

    def __init__(self, archive: BinaryIO, archive_size: int, world_id: UUID):
        self.archive = archive
        super().__init__(
            iter(lambda: self.archive.read(1024 * 1024), b""),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="world-{world_id}.webackup"',
                "Content-Length": str(archive_size),
                "Cache-Control": "no-store",
            },
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.archive.close()


def create_app(
    worlds: WorldApplication,
    session: LocalSession,
    *,
    operations: ModelOperations | None = None,
    credentials: CredentialService | None = None,
    conversations: ConversationApplication | None = None,
    authoring: AuthoringApplication | None = None,
    static_directory: Path | None = None,
    media_registry: MediaRegistry | None = None,
    media_assets: MediaAssetStore | None = None,
    identity_provider: IdentityProvider | None = None,
    restore_copy: Callable[[AccessScope, VerifiedWorldBackup], dict] | None = None,
    restore_ruleset_copy: Callable[[AccessScope, VerifiedWorldBackup, RulesetUpgrade], dict] | None = None,
    backup_limits: BackupLimits | None = None,
    backup_scratch_directory: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="万象世界引擎", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    if backup_scratch_directory is not None:
        backup_scratch_directory = Path(backup_scratch_directory).resolve()
        backup_scratch_directory.mkdir(parents=True, exist_ok=True)
    app.add_middleware(LocalSessionMiddleware, session=session, identity_provider=identity_provider)
    app.state.worlds = worlds
    media = media_registry if media_registry is not None else MediaRegistry()

    def scope(request: Request) -> AccessScope:
        return AccessScope(
            tenant_id=UUID(request.state.tenant_id),
            user_id=UUID(request.state.principal_id),
            correlation_id=uuid4(),
        )

    @app.get("/api/v1/worlds/{world_id}/branches/{branch_id}/media/{content_hash}")
    def read_media(
        request: Request, world_id: UUID, branch_id: UUID, content_hash: str, actor_id: UUID | None = None,
    ):
        if media_assets is None:
            raise NotFound("Media is not configured.")
        identity = scope(request)
        role = worlds.repository.authorize_role(identity, world_id, set(MembershipRole))
        if actor_id is None:
            worlds.repository.read_world(identity, world_id, branch_id)
        else:
            actor_id = worlds.repository.authorize_actor(identity, world_id, actor_id, branch_id=branch_id)
        authorization = MediaAssetAuth(
            scope=identity, world_id=world_id, branch_id=branch_id, membership_role=role,
            verified_subject_ids=frozenset() if actor_id is None else frozenset({actor_id}),
        )
        try:
            data, content_type = media_assets.read_with_content_type(content_hash, authorization)
        except MediaAssetNotFound as error:
            raise NotFound("Media asset not found.") from error
        except MediaAssetError as error:
            raise ApplicationFailure("MEDIA_UNAVAILABLE", "媒体资源暂不可用。", status_code=503) from error
        return Response(data, media_type=content_type, headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError):
        if isinstance(error, StorageBusy):
            return JSONResponse({"error": {"code": STORAGE_BUSY_CODE, "message": STORAGE_BUSY_MESSAGE, "retryable": True}},
                                status_code=503, headers={"Retry-After": "1"})
        status, code, message = 422, "RULE_REJECTED", "当前条件下不能完成这项操作。"
        if isinstance(error, AccessDenied):
            status, code, message = 403, "FORBIDDEN", "当前身份没有操作权限。"
        elif isinstance(error, NotFound):
            status, code, message = 404, "NOT_FOUND", "找不到该资源。"
        elif isinstance(error, InsufficientStorage):
            status, code, message = 507, "INSUFFICIENT_STORAGE", "磁盘空间不足。请释放空间并核对世界状态后重试。"
        elif isinstance(error, StaleVersion):
            status, code, message = 409, "STALE_VERSION", "世界已发生变化，请刷新后重试。"
        elif isinstance(error, Conflict):
            status, code, message = 409, "CONFLICT", "请求与已有记录冲突，请检查操作状态。"
        return JSONResponse({"error": {"code": code, "message": message, "retryable": False}}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        # Do not echo input values: provider configuration may contain a value
        # accidentally pasted into the wrong field.
        fields = [{"path": ".".join(str(p) for p in item["loc"]), "type": item["type"]} for item in error.errors()]
        return JSONResponse(
            {"error": {"code": "INVALID_REQUEST", "message": "输入字段不符合要求。", "field_errors": fields, "retryable": False}},
            status_code=422,
        )

    @app.exception_handler(ProviderError)
    async def provider_error(request: Request, error: ProviderError):
        if error.code is ProviderErrorCode.TRUNCATED:
            message = MODEL_OUTPUT_TRUNCATED_MESSAGE
        elif error.code is ProviderErrorCode.CONTEXT_LIMIT:
            message = OLLAMA_CONTEXT_LIMIT_MESSAGE
        else:
            message = "模型请求未完成，请检查供应商配置与调用记录。"
        return JSONResponse({"error": {"code": error.code.value,
            "message": message, "retryable": error.retryable}}, status_code=502)

    @app.exception_handler(ApplicationFailure)
    async def application_error(request: Request, error: ApplicationFailure):
        details = {"code": error.code, "message": error.public_message, "retryable": error.retryable}
        if isinstance(error, AuthorRuleConflict):
            details["field_errors"] = error.field_errors
        return JSONResponse({"error": details}, status_code=error.status_code)

    @app.exception_handler(CredentialStoreError)
    async def credential_error(request: Request, error: CredentialStoreError):
        return JSONResponse({"error": {"code": "OS_CREDENTIAL_ERROR",
            "message": "Windows 凭据管理器暂时无法完成操作，请检查当前系统账户的访问权限。",
            "retryable": False}}, status_code=503)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "world-engine"}

    @app.get("/api/v1/session")
    def get_session(request: Request):
        display_name = "本地玩家" if identity_provider is None else "已认证玩家"
        return {
            "principal": {"id": request.state.principal_id, "display_name": display_name},
            "permissions": ["world.create", "world.own.manage"],
        }

    @app.get("/api/v1/worlds")
    def list_worlds(request: Request):
        return wire(worlds.list_worlds(scope(request)))

    @app.get("/api/v1/packs")
    def list_packs(request: Request):
        return wire(worlds.list_packs())

    app.include_router(world_pack_router(WorldPackApplication(worlds), scope, authoring))

    @app.get("/api/v1/extensions")
    def list_extensions():
        return {"items": [manifest.model_dump(mode="json") for manifest in media.manifests()]}

    @app.post("/api/v1/worlds", status_code=201)
    def create_world(
        request: Request,
        body: CreateWorldRequest,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        return wire(worlds.create_world(
            scope(request), body.pack_id, body.title, body.player_name, idempotency_key,
            seed=body.seed, population_mode=body.population_mode, pack_version=body.pack_version,
            npc_autonomy_mode=body.npc_autonomy_mode,
        ))

    @app.get("/api/v1/worlds/{world_id}/view")
    def world_view(
        request: Request, world_id: UUID, branch_id: UUID | None = None,
        mode: Literal["player", "author"] = "player",
    ):
        return wire(worlds.view_world(scope(request), world_id, branch_id, author=mode == "author"))

    @app.get("/api/v1/worlds/{world_id}/player-report")
    def download_player_report(request: Request, world_id: UUID, branch_id: UUID | None = None):
        view = worlds.view_world(scope(request), world_id, branch_id)
        return JSONResponse(
            wire({"format": "world-engine-player-report", "version": 1, "view": view}),
            headers={"Content-Disposition": 'attachment; filename="player-report.json"'},
        )

    @app.get("/api/v1/worlds/{world_id}/branches")
    def list_branches(request: Request, world_id: UUID):
        return wire(worlds.list_branches(scope(request), world_id))

    @app.get("/api/v1/worlds/{world_id}/continuation-options")
    def continuation_options(request: Request, world_id: UUID, branch_id: UUID):
        return wire(worlds.continuation_options(scope(request), world_id, branch_id))

    @app.post("/api/v1/worlds/{world_id}/continue-after-death")
    def continue_after_death(
        request: Request, world_id: UUID, body: ContinueAfterDeathRequest,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        return wire(worlds.continue_after_death(
            scope(request), world_id, body.branch_id, body.previous_player_id, body.new_player_id,
            int(body.expected_state_version), idempotency_key,
        ))

    @app.post("/api/v1/worlds/{world_id}/challenges/{challenge_id}/attempt")
    def perform_challenge(
        request: Request, world_id: UUID, challenge_id: UUID, body: ChallengeRequest,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        return wire(worlds.perform_challenge(scope(request), world_id, body.branch_id, body.actor_id, challenge_id,
                                             int(body.expected_state_version), idempotency_key))

    @app.post("/api/v1/worlds/{world_id}/branches", status_code=201)
    def fork_branch(
        request: Request, world_id: UUID, body: BranchRequest,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        return wire(worlds.fork_branch(
            scope(request), world_id, body.branch_id, int(body.sequence), body.label, idempotency_key,
        ))

    @app.post("/api/v1/worlds/{world_id}/contests")
    def change_contest(
        request: Request, world_id: UUID, body: ContestRequest,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ):
        return wire(worlds.change_contest(scope(request), world_id, body.branch_id, body.actor_id, body.action,
                                        int(body.expected_state_version), idempotency_key))

    @app.get("/api/v1/worlds/{world_id}/history")
    def history(request: Request, world_id: UUID, branch_id: UUID,
                after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
                mode: Literal["author", "player", "auto"] = "author"):
        access = scope(request)
        repository = worlds.repository
        # Full ledger payloads include private NPC facts and author revisions.
        author = mode != "player"
        if author:
            try:
                repository.authorize_role(access, world_id, {MembershipRole.OWNER, MembershipRole.EDITOR})
            except AccessDenied:
                if mode != "auto":
                    raise
                author = False
        if author:
            page = repository.list_events(access, world_id, branch_id, after_seq=after)
            items = [event.model_dump() | {"can_fork": event.commit_boundary} for event in page.events]
        else:
            world, default_player_id, entity_names = repository.read_player_history_context(access, world_id, branch_id)
            actor_id = str(worlds.resolve_player_identity(access, world, default_player_id))
            page = repository.list_player_history_events(access, world_id, branch_id, after_seq=after)
            items = [
                projected | {"branch_id": event.branch_id, "can_fork": False}
                for event in page.events if (
                    projected := project_event(event.model_dump(mode="json"), actor_id, entities=entity_names)
                ) is not None
            ]
        result = {"items": items, "next_cursor": page.next_cursor if page.has_more else None}
        if mode != "author":
            result["mode"] = "author" if author else "player"
        if author and after == 0 and repository.has_world_pack_archive(access, world_id):
            result["has_source_world_pack"] = True
        return wire(result)

    @app.get("/api/v1/jobs/{job_id}")
    def job_status(request: Request, job_id: UUID):
        access = scope(request)
        job = worlds.repository.read_job(access, job_id)
        return wire(public_job(job, access))

    def public_job(job, access=None, *, itinerary_player_state=None):
        result = job.result or {}
        interruption = result.get("interruption") or {}
        simulation_job = job.job_type in {"simulation.long_action", "simulation.world_advance"}
        plan = job.payload.get("plan", {}) if simulation_job else {}
        reason_code = interruption.get("reason_code") or result.get("reason_code") or result.get("error")
        view = job.model_dump(include={
            "job_id", "world_id", "branch_id", "job_type", "status",
            "committed_through_tick", "created_at", "updated_at",
        }) | {"reason_code": current_job_reason_code(job, reason_code),
              "started_tick": plan.get("started_tick"),
              "target_tick": job.payload.get("effective_travel_target_tick", plan.get("target_tick"))}
        if simulation_job:
            committed_version = job.payload.get("committed_state_version")
            if committed_version is None:
                committed_version = job.payload.get("initial_state_version")
            if type(committed_version) is int and 0 <= committed_version <= 2**63 - 1:
                view["committed_state_version"] = str(committed_version)
        if (plan.get("activity") == "study" and access is not None
            and job.payload.get("submitted_by") == str(access.user_id)):
            stop_conditions = plan.get("stop_conditions")
            safety_conditions = [
                condition for condition in stop_conditions if (
                    isinstance(condition, dict)
                    and condition.get("kind") == "need_at_or_below"
                    and condition.get("subject_id") == plan.get("actor_id")
                    and condition.get("need") == "safety"
                )
            ] if isinstance(stop_conditions, list) else []
            if len(safety_conditions) == 1:
                threshold = safety_conditions[0].get("threshold")
                if type(threshold) is int and 0 <= threshold <= 100:
                    view["study_stop_safety_at_or_below"] = threshold
        if plan.get("activity") == "work" and access is not None and job.payload.get("submitted_by") == str(access.user_id):
            if job.status.value == "succeeded" and not view["reason_code"]:
                view["reason_code"] = (result.get("metrics") or {}).get("integration_boundary")
            view["work_progress"] = job.payload.get("work_progress")
            if "work_resume" in job.payload:
                view["work_resume"] = job.payload["work_resume"]
            purchase_target = (plan.get("work_schedule") or {}).get("purchase_target")
            if purchase_target is not None:
                view["work_purchase_target"] = {
                    key: purchase_target[key]
                    for key in ("listing_id", "resource_id", "currency_resource_id", "quantity")
                }
                affordability = work_purchase_affordability(job, LongActionPlan.model_validate(plan))
                if affordability is not None:
                    view["work_purchase_affordability"] = affordability
        itinerary = plan.get("travel_itinerary")
        if itinerary is not None and access is not None and job.payload.get("submitted_by") == str(access.user_id):
            cursor = job.payload.get("itinerary_cursor")
            view["itinerary_progress"] = {
                "legs_total": len(itinerary["legs"]),
                "legs_arrived": cursor["leg_index"] + 1 if cursor is not None else 0,
            }
            if itinerary_player_state is None:
                try:
                    itinerary_player_state = worlds.view_world(access, job.world_id, job.branch_id)["state"]
                except (AccessDenied, NotFound):
                    itinerary_player_state = {}
            if itinerary_player_state.get("player_id") == itinerary.get("actor_id"):
                itinerary_plan = LongActionPlan.model_validate(plan).travel_itinerary
                if itinerary_plan is not None:
                    entities = itinerary_player_state["entities"]
                    journey = itinerary_player_state.get("own_active_travel") or {}
                    route_view = []
                    for leg in itinerary_plan.legs:
                        origin = entities.get(str(leg.from_place_id)) or {}
                        destination = entities.get(str(leg.to_place_id)) or {}
                        in_transit = str(travel_itinerary_journey_id(itinerary_plan.itinerary_id, leg.leg_id)) == journey.get("journey_id")
                        route_view.append({
                            "from_name": origin.get("name"), "to_name": destination.get("name"),
                            "travel_ticks": str(leg.travel_ticks),
                            "arrived": leg.leg_index < view["itinerary_progress"]["legs_arrived"],
                            "expected_arrival_tick": journey.get("arrival_tick") if in_transit else None,
                        })
                    view["itinerary_route"] = route_view
            if job.status.value == "paused" and (
                itinerary_requires_replan(interruption.get("reason_code"))
                or itinerary_requires_replan(result.get("reason_code"))
            ):
                view["itinerary_requires_replan"] = True
        if job.job_type == "media.generate":
            try:
                planned = MediaJobEnvelope.model_validate(job.payload).planned
            except ValueError:
                return view
            media_request = planned.request
            view["media_request"] = {
                "source_event_id": media_request.source_event_ids[0], "plugin_id": planned.plugin_id,
                "kind": media_request.kind, "audience": media_request.visibility.policy,
                "actor_id": media_request.recipient_id if media_request.visibility.policy == "participants" else None,
            }
            if job.status.value == "succeeded":
                view["asset"] = result.get("asset")
        return view

    if media_registry is not None and media_assets is not None:
        media_enqueuer = MediaJobEnqueuer(worlds.repository, media_registry)
        media_authorizer = RepositoryMediaAuthorizer(worlds.repository)

        @app.post("/api/v1/worlds/{world_id}/branches/{branch_id}/media-jobs", status_code=202)
        def enqueue_media_job(request: Request, world_id: UUID, branch_id: UUID, body: MediaJobRequest):
            identity = scope(request)
            participant = body.audience == "participants"
            if participant != (body.actor_id is not None):
                raise ApplicationFailure("MEDIA_RECIPIENT_REQUIRED", "请为参与者媒体选择角色。", status_code=422)
            event = worlds.repository.read_branch_event(identity, world_id, branch_id, body.source_event_id)
            try:
                source = AuthorizedMediaSource(
                    event=event, recipient_id=body.actor_id if participant else identity.user_id,
                    authorized_description=body.description,
                    visibility=AssetScope(policy=body.audience, subject_ids=(body.actor_id,) if participant else ()),
                )
            except ValueError as error:
                raise AccessDenied("Media audience exceeds its source event audience.") from error
            plan = next((item for item in media.plan_requests(source)
                         if item.plugin_id == body.plugin_id and item.request.kind == body.kind), None)
            if plan is None:
                raise NotFound("No media subscription matches the recorded source event.")
            media_authorizer.authorize_plan(identity, world_id, branch_id, plan)
            try:
                return wire(public_job(media_enqueuer.enqueue(identity, plan)))
            except ValueError as error:
                raise ApplicationFailure("MEDIA_PLUGIN_UNAVAILABLE", "媒体插件暂不可用。", status_code=422) from error

    @app.get("/api/v1/jobs/{job_id}/decision")
    def job_decision(request: Request, job_id: UUID):
        access = scope(request)
        repository = worlds.repository
        job = repository.read_job(access, job_id)
        allowed_roles = {MembershipRole.OWNER}
        if job.job_type == "simulation.world_advance":
            view = repository.read_world(access, job.world_id, job.branch_id)
            if view.world.owner_id != access.user_id:
                raise AccessDenied("Only the world owner can view world advance decisions.")
        elif job.job_type == "simulation.long_action" and job.payload.get("plan", {}).get("travel_itinerary") is not None:
            if job.payload.get("submitted_by") != str(access.user_id):
                raise AccessDenied("Only the submitting player can view this travel decision.")
            itinerary_plan = LongActionPlan.model_validate(job.payload["plan"])
            repository.authorize_actor_control(access, job.world_id, itinerary_plan.actor_id)
            allowed_roles.update({MembershipRole.EDITOR, MembershipRole.PLAYER})
        else:
            allowed_roles.add(MembershipRole.EDITOR)
        repository.authorize_role(access, job.world_id, allowed_roles)
        decisions = (job.result or {}).get("decision_requests", []) if job.status.value == "waiting_input" else []
        return wire({"items": [
            {key: decision[key] for key in ("request_id", "title", "options")}
            for decision in decisions if decision.get("decision_owner") == "user"
        ]})

    @app.post("/api/v1/jobs/{job_id}/decision")
    def submit_job_decision(request: Request, job_id: UUID, body: JobDecisionRequest):
        access = scope(request)
        job = worlds.repository.submit_job_decision(access, job_id, body.request_id, body.option_id)
        return wire(public_job(job, access))

    @app.get("/api/v1/worlds/{world_id}/jobs")
    def branch_jobs(request: Request, world_id: UUID, branch_id: UUID):
        access = scope(request)
        jobs = worlds.repository.list_jobs(access, world_id, branch_id)
        itinerary_player_state = None
        if any(job.payload.get("submitted_by") == str(access.user_id) and
               (job.payload.get("plan") or {}).get("travel_itinerary") is not None for job in jobs):
            try:
                itinerary_player_state = worlds.view_world(access, world_id, branch_id)["state"]
            except (AccessDenied, NotFound):
                itinerary_player_state = {}
        return wire({"items": [public_job(job, access, itinerary_player_state=itinerary_player_state) for job in jobs]})

    @app.post("/api/v1/worlds/{world_id}/long-actions", status_code=202)
    def enqueue_long_action(
        request: Request, world_id: UUID, body: LongActionRequest,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=120)],
    ):
        access = scope(request)
        view = worlds.repository.read_world(access, world_id, body.branch_id)
        actor = worlds.resolve_player(access, view)
        version = int(body.expected_state_version)
        key = f"long-action:{access.user_id}:{idempotency_key}"
        existing = worlds.repository.find_job_by_key(access, world_id, body.branch_id, key)
        if existing is not None:
            plan = LongActionPlan.model_validate(existing.payload["plan"])
            expected_study_stop = study_safety_stop(actor, body.study_stop_safety_at_or_below)
            itinerary = plan.travel_itinerary
            itinerary_route_ids = tuple(leg.route_id for leg in itinerary.legs) if itinerary else None
            if (plan.actor_id != actor or plan.activity != body.activity
                or plan.route_id != body.route_id
                or itinerary_route_ids != body.route_ids
                or plan.itinerary_auto_care != body.itinerary_auto_care
                or plan.skill_id != body.skill_id
                or plan.stop_conditions != expected_study_stop
                or _work_schedule_request(plan.work_schedule) != body.work_schedule
                or plan.target_tick - plan.started_tick != body.duration_seconds
                or existing.payload["initial_state_version"] != version
                or existing.payload["speed"] != body.profile.speed
                or existing.payload["depth"] != body.profile.depth):
                raise Conflict("The job key already belongs to a different long action.")
            return wire(public_job(existing, access))
        if view.branch.state_version != version:
            raise StaleVersion(version, view.branch.state_version)
        if body.activity == "study":
            skills_raw = view.state.entities[actor].components.get("skills")
            try:
                skills = SkillsComponent.model_validate(skills_raw) if skills_raw is not None else SkillsComponent()
            except Exception as error:
                raise ApplicationFailure(
                    "SKILLS_UNAVAILABLE",
                    "The character's registered skills are invalid.",
                    status_code=422,
                ) from error
            if body.skill_id not in skills.skills:
                raise ApplicationFailure(
                    "SKILL_NOT_REGISTERED",
                    "The selected skill is not registered for this character.",
                    status_code=422,
                )
            if body.study_stop_safety_at_or_below is not None:
                needs_raw = view.state.entities[actor].components.get("needs")
                try:
                    needs = NeedsComponent.model_validate(needs_raw) if needs_raw is not None else None
                except Exception as error:
                    raise ApplicationFailure(
                        "STUDY_SAFETY_UNAVAILABLE",
                        "The character has no valid registered safety metric for this stop condition.",
                        status_code=422,
                    ) from error
                if needs is None or needs.safety is None:
                    raise ApplicationFailure(
                        "STUDY_SAFETY_UNAVAILABLE",
                        "The character has no valid registered safety metric for this stop condition.",
                        status_code=422,
                    )
        start = view.state.current_tick
        target = start + body.duration_seconds
        if target > 2**63 - 1:
            raise ApplicationFailure("TICK_OVERFLOW", "目标时间超过世界时钟范围。", status_code=422)
        itinerary = None
        if body.route_ids is not None:
            itinerary = plan_travel_itinerary(
                view.state, itinerary_id=uuid5(body.branch_id, key), actor_id=actor,
                route_ids=body.route_ids, deadline_tick=target,
            )
            first = itinerary.legs[0]
            inspected = revalidate_travel_itinerary_leg(view.state, itinerary, 0)
            origin = view.state.entities[first.from_place_id]
            routes = PlaceComponent.model_validate(origin.components["place"]).routes
            route = next(item for item in routes if item.route_id == first.route_id)
            if (inspected.outcome != "current"
                or route_access_reason(view.state, view.state.entities[actor], route) is not None):
                raise ApplicationFailure(
                    "ITINERARY_FIRST_LEG_UNAVAILABLE", "首段路线当前不可用，请检查路线和出发地。",
                    status_code=422,
                )
        schedule = body.work_schedule
        if schedule is not None:
            visible_market_offers = ()
            if schedule.purchase_target is not None:
                visible_market_offers = project_player_state(view.state.model_dump(mode="json"), str(actor))[
                    "market_offers"
                ]["offers"]
            try:
                plan = build_routine_work_plan(
                    view.state, actor, schedule, body.duration_seconds, visible_market_offers=visible_market_offers,
                )
            except ValueError as error:
                reason = error.code if isinstance(error, WorkShiftError) else None
                if reason == "purchase_target_unavailable":
                    raise ApplicationFailure(
                        "ROUTINE_WORK_PURCHASE_TARGET_UNAVAILABLE",
                        "请选择工作地点公开在售、可用同一工资币种购买的商品及数量。",
                        status_code=422,
                    ) from error
                if reason == "first_shift_outside_window":
                    raise ApplicationFailure(
                        "ROUTINE_WORK_FIRST_SHIFT_OUTSIDE_WINDOW", "首班不能早于当前时刻，并须在计划截止前完成。",
                        status_code=422,
                    ) from error
                raise ApplicationFailure(
                    "ROUTINE_WORK_ASSIGNMENT_UNAVAILABLE", "请核对本人任职、工作地点、岗位有效期和重复间隔。",
                    status_code=422,
                ) from error
        else:
            plan = LongActionPlan(
                actor_id=actor, started_tick=start, target_tick=target, activity=body.activity, route_id=body.route_id,
                skill_id=body.skill_id, travel_itinerary=itinerary,
                stop_conditions=study_safety_stop(actor, body.study_stop_safety_at_or_below),
                itinerary_auto_care=body.itinerary_auto_care,
            )
        job = SimulationJobs(worlds.repository).enqueue(
            access, world_id, body.branch_id, plan, idempotency_key=key,
            speed=body.profile.speed, depth=body.profile.depth, expected_state_version=version,
        )
        return wire({"job_id": job.job_id, "status": job.status,
                     "committed_through_tick": job.committed_through_tick})

    @app.post("/api/v1/worlds/{world_id}/world-advance", status_code=202)
    def enqueue_world_advance(
        request: Request, world_id: UUID, body: WorldAdvanceRequest,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=120)],
    ):
        access = scope(request)
        repository = worlds.repository
        repository.authorize_role(access, world_id, {MembershipRole.OWNER})
        view = repository.read_world(access, world_id, body.branch_id)
        if view.world.owner_id != access.user_id:
            raise AccessDenied("Only the world owner can queue world advancement.")
        version = int(body.expected_state_version)
        key = f"world-advance:{access.user_id}:{idempotency_key}"
        existing = repository.find_job_by_key(access, world_id, body.branch_id, key)
        if existing is not None:
            plan = WorldAdvancePlan.model_validate(existing.payload["plan"])
            if (plan.target_tick - plan.started_tick != body.duration_seconds
                or existing.payload["initial_state_version"] != version
                or existing.payload["speed"] != body.profile.speed
                or existing.payload["depth"] != body.profile.depth):
                raise Conflict("The job key already belongs to a different world advance.")
            return wire(public_job(existing))
        if view.branch.state_version != version:
            raise StaleVersion(version, view.branch.state_version)
        start = view.state.current_tick
        target = start + body.duration_seconds
        if target > 2**63 - 1:
            raise ApplicationFailure("TICK_OVERFLOW", "目标时间超过世界时钟范围。", status_code=422)
        job = SimulationJobs(repository).enqueue_world_advance(
            access, world_id, body.branch_id, WorldAdvancePlan(started_tick=start, target_tick=target),
            idempotency_key=key, speed=body.profile.speed, depth=body.profile.depth, expected_state_version=version,
        )
        return wire(public_job(job))

    @app.post("/api/v1/jobs/{job_id}/control")
    def control_job(request: Request, job_id: UUID, body: JobControlRequest):
        job = worlds.repository.control_job(scope(request), job_id, body.action)
        return wire({"job_id": job.job_id, "status": job.status,
                     "committed_through_tick": job.committed_through_tick})

    @app.get("/api/v1/worlds/{world_id}/backup")
    def download_world_backup(request: Request, world_id: UUID, format_version: Literal["1.0", "2.0"] | None = None):
        archive = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b", dir=backup_scratch_directory)
        try:
            archive_size = BackupApplication(
                worlds.repository,
                limits=backup_limits or DEFAULT_LIMITS,
                scratch_directory=backup_scratch_directory,
            ).export_world_to(
                scope(request), world_id, archive, format_version=format_version,
            )
            archive.seek(0)
        except BackupArchiveError as error:
            archive.close()
            raise ApplicationFailure("BACKUP_EXPORT_FAILED", "无法生成世界备份，请检查存档和备份限制。", status_code=422) from error
        except BaseException:
            archive.close()
            raise
        return BackupDownload(archive, archive_size, world_id)

    async def read_restore_archive(request: Request) -> VerifiedWorldBackup:
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/zip":
            raise ApplicationFailure("BACKUP_FORMAT_REQUIRED", "请上传 .webackup 归档。", status_code=415)
        backups = BackupApplication(
            worlds.repository,
            limits=backup_limits or DEFAULT_LIMITS,
            scratch_directory=backup_scratch_directory,
        )
        upload_limit = backup_limits.compressed_bytes if backup_limits is not None else MAX_COMPRESSED_ARCHIVE_BYTES
        with SpooledTemporaryFile(
            max_size=1024 * 1024, mode="w+b", dir=backup_scratch_directory,
        ) as archive:
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > upload_limit:
                    raise ApplicationFailure("BACKUP_TOO_LARGE", "归档超过当前大小限制。", status_code=413)
                if chunk:
                    await run_in_threadpool(archive.write, chunk)
            try:
                return await run_in_threadpool(backups.validate_archive_file_streaming, archive)
            except BackupArchiveError as error:
                raise ApplicationFailure("BACKUP_INVALID", "归档无法通过格式、完整性或回放校验。", status_code=422) from error

    def restore_preview_id(verified: VerifiedWorldBackup, target_version: str | None = None) -> str:
        binding = verified.archive_sha256 if target_version is None else f"{verified.archive_sha256}:ruleset:{target_version}"
        return hmac.new(
            session.token.encode(), binding.encode(), hashlib.sha256,
        ).hexdigest()

    @app.post("/api/v1/restores/preview")
    async def preview_world_restore(request: Request):
        with await read_restore_archive(request) as verified:
            upgrades = available_ruleset_upgrades(verified.world.ruleset_version) if restore_ruleset_copy is not None else ()
            return verified.summary() | {
                "preview_id": restore_preview_id(verified),
                "ruleset_version": verified.world.ruleset_version,
                "pack_version": verified.world.pack_version,
                "has_source_world_pack": verified.world_pack_archive is not None,
                "ruleset_upgrades": [
                    {"target_version": upgrade.target_version, "summary": upgrade.summary,
                     "preview_id": restore_preview_id(verified, upgrade.target_version)}
                    for upgrade in upgrades
                ],
            }

    if restore_copy is not None:
        @app.post("/api/v1/restores", status_code=201)
        async def confirm_world_restore(
            request: Request, preview_id: Annotated[str, Header(pattern=r"^[a-f0-9]{64}$")],
            target_ruleset_version: Annotated[str | None, Header(max_length=40)] = None,
        ):
            with await read_restore_archive(request) as verified:
                if target_ruleset_version is not None:
                    if restore_ruleset_copy is None:
                        raise ApplicationFailure("RULESET_MIGRATION_UNAVAILABLE", "此实例未启用规则升级。", status_code=422)
                    upgrade = require_ruleset_upgrade(verified.world.ruleset_version, target_ruleset_version)
                if not hmac.compare_digest(preview_id, restore_preview_id(verified, target_ruleset_version)):
                    raise ApplicationFailure("RESTORE_PREVIEW_MISMATCH", "归档与当前预览不符，请重新预览。")
                if target_ruleset_version is not None:
                    return await run_in_threadpool(restore_ruleset_copy, scope(request), verified, upgrade)
                return await run_in_threadpool(restore_copy, scope(request), verified)

    if operations is not None:
        from .operations import operations_router
        app.include_router(operations_router(operations, scope))

    if credentials is not None:
        from .credentials import credential_router
        app.include_router(credential_router(credentials, scope))

    if conversations is not None:
        from .conversation import conversation_router
        app.include_router(conversation_router(conversations, scope))

    if authoring is not None:
        from world_engine.application.family_authoring import FamilyAuthoringApplication
        from world_engine.application.entity_authoring import EntityAuthoringApplication
        from .authoring import authoring_router
        from .entity_authoring import entity_authoring_router
        from .family_authoring import family_authoring_router
        app.include_router(authoring_router(authoring, scope, operations))
        app.include_router(entity_authoring_router(
            EntityAuthoringApplication(worlds.repository, authoring.operations), scope,
        ))
        app.include_router(family_authoring_router(FamilyAuthoringApplication(worlds.repository), scope))

    if static_directory is not None and static_directory.is_dir():
        app.mount("/", StaticFiles(directory=static_directory, html=True), name="frontend")
    return app
