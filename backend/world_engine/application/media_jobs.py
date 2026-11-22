"""Durable, fenced jobs for registered media plugins.

Plugin execution is asynchronous and bounded; lease renewal follows Python's
documented asyncio task cancellation and timeout semantics. The job row is
fenced independently from branch writes, and asset publication occurs only in
the repository's synchronous guarded callback. See:
https://docs.python.org/3/library/asyncio-task.html
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from world_engine.domain.errors import AccessDenied, InvalidLease
from world_engine.domain.models import (
    AccessScope,
    JobLease,
    JobRecord,
    JobStatus,
    MembershipRole,
    NewJob,
)

from .media_assets import (
    MediaAssetAccessDenied,
    MediaAssetAuth,
    MediaAssetError,
    MediaAssetStore,
    StoredMediaAsset,
)
from .media_contracts import (
    AuthorizedMediaSource,
    GeneratedMediaArtifact,
    MediaRegistry,
    PlannedMediaRequest,
)


MEDIA_JOB_TYPE = "media.generate"


class MediaJobEnvelope(BaseModel):
    """Private job input containing only the host-approved media request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    planned: PlannedMediaRequest
    submitted_by: UUID


@dataclass(frozen=True, slots=True)
class MediaJobClaim:
    """Structural equivalent of the repository's media job claim tuple."""

    job: JobRecord
    lease: JobLease


class MediaJobRepository(Protocol):
    """Minimal storage API used by enqueue and worker application services."""

    def enqueue_job(
        self,
        scope: AccessScope,
        new_job: NewJob,
        *,
        expected_state_version: int | None = None,
    ) -> JobRecord: ...

    def claim_media_job(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> MediaJobClaim | None: ...

    def renew_media_job(
        self,
        lease: JobLease,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> JobLease: ...

    def finish_media_job(
        self,
        lease: JobLease,
        status: JobStatus,
        *,
        result: dict[str, Any] | None = None,
        publish: Callable[[], dict[str, Any]] | None = None,
        now: datetime | None = None,
    ) -> JobRecord: ...


class MediaPublicationAuthorizer(Protocol):
    """Rechecks exact branch source, current role, recipient control and audience."""

    def authorize(self, job: JobRecord, plan: PlannedMediaRequest) -> MediaAssetAuth: ...


class MediaJobEnqueuer:
    """Persist deterministic media plans as idempotent jobs."""

    def __init__(self, repository: MediaJobRepository, registry: MediaRegistry):
        self.repository = repository
        self.registry = registry

    def enqueue(
        self,
        scope: AccessScope,
        plan: PlannedMediaRequest,
        *,
        expected_state_version: int | None = None,
    ) -> JobRecord:
        self.registry.validate_plan(plan)
        request = plan.request
        envelope = MediaJobEnvelope(planned=plan, submitted_by=scope.user_id)
        return self.repository.enqueue_job(
            scope,
            NewJob(
                world_id=request.world_id,
                branch_id=request.branch_id,
                job_type=MEDIA_JOB_TYPE,
                idempotency_key=request.idempotency_key,
                payload=envelope.model_dump(mode="json"),
            ),
            expected_state_version=expected_state_version,
        )

    def enqueue_source(
        self,
        scope: AccessScope,
        source: AuthorizedMediaSource,
        *,
        expected_state_version: int | None = None,
    ) -> list[JobRecord]:
        """Persist all explicit subscription matches for one host-authorized source."""
        return [
            self.enqueue(scope, plan, expected_state_version=expected_state_version)
            for plan in self.registry.plan_requests(source)
        ]


class MediaJobWorker:
    """Execute registered media work with lease renewal and crash-safe publication."""

    def __init__(
        self,
        repository: MediaJobRepository,
        registry: MediaRegistry,
        asset_store: MediaAssetStore,
        authorizer: MediaPublicationAuthorizer,
        *,
        lease_seconds: int = 60,
        plugin_timeout_seconds: float = 180.0,
        heartbeat_interval_seconds: float | None = None,
    ):
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        if not 0 < plugin_timeout_seconds <= 3600:
            raise ValueError("plugin_timeout_seconds must be positive and at most 3600")
        interval = heartbeat_interval_seconds or max(0.1, lease_seconds / 3)
        if interval <= 0 or interval >= lease_seconds:
            raise ValueError("heartbeat_interval_seconds must be positive and shorter than the lease")
        self.repository = repository
        self.registry = registry
        self.asset_store = asset_store
        self.authorizer = authorizer
        self.lease_seconds = lease_seconds
        self.plugin_timeout_seconds = plugin_timeout_seconds
        self.heartbeat_interval_seconds = interval

    async def process_one(self, worker_id: str) -> JobRecord | None:
        claim = await asyncio.to_thread(
            self.repository.claim_media_job,
            worker_id,
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            return None
        return await self.process_claim(claim)

    async def process_claim(self, claim: MediaJobClaim) -> JobRecord:
        """Process an already claimed row, mainly useful to host worker loops."""
        job = claim.job
        lease_ref = [claim.lease]
        try:
            if job.job_type != MEDIA_JOB_TYPE:
                raise ValueError("The claimed row is not a media generation job")
            envelope = MediaJobEnvelope.model_validate(job.payload)
            plan = envelope.planned
            request = plan.request
            if (
                request.world_id != job.world_id
                or request.branch_id != job.branch_id
                or str(envelope.submitted_by) != job.payload.get("submitted_by")
            ):
                raise ValueError("The media job payload does not match its durable scope or key")
            self.registry.validate_plan(plan)
            auth = await self._authorize(job, plan)
            recovered = await asyncio.to_thread(self.asset_store.lookup_request, request, auth)
        except InvalidLease:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._finish_failed(lease_ref[0], self._reason_code(exc))

        if recovered is not None:
            # Do not convert a transient job-database error into terminal
            # failure: the committed asset remains available for retry lookup.
            return await asyncio.to_thread(
                self.repository.finish_media_job,
                lease_ref[0],
                JobStatus.SUCCEEDED,
                result=self._asset_result(recovered),
            )

        try:
            artifact = await self._generate_with_heartbeat(plan, lease_ref)
            # Resolve membership, exact source branch, and audience again after
            # plugin work; a revoked request must not get a new asset grant.
            auth = await self._authorize(job, plan)
        except InvalidLease:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self._finish_failed(lease_ref[0], self._reason_code(exc))

        publication_error: list[Exception] = []

        def publish() -> dict[str, Any]:
            try:
                # A competing or recovered attempt may already have committed
                # this exact request while generation was in flight.
                existing = self.asset_store.lookup_request(request, auth)
                stored = existing or self.asset_store.store(
                    request,
                    artifact.receipt,
                    artifact.data,
                    auth,
                )
                return self._asset_result(stored)
            except Exception as exc:
                publication_error.append(exc)
                raise

        try:
            return await asyncio.to_thread(
                self.repository.finish_media_job,
                lease_ref[0],
                JobStatus.SUCCEEDED,
                publish=publish,
            )
        except InvalidLease:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            if not publication_error:
                # The callback may have committed the external asset and the
                # database transaction may then have failed. Leave the job
                # reclaimable so lookup_request can reconcile it on retry.
                raise
            return await self._finish_failed(
                lease_ref[0], self._reason_code(publication_error[0])
            )

    async def _authorize(self, job: JobRecord, plan: PlannedMediaRequest) -> MediaAssetAuth:
        try:
            raw_auth = await asyncio.to_thread(self.authorizer.authorize, job, plan)
            auth = MediaAssetAuth.model_validate(raw_auth.model_dump(mode="python"))
        except (AccessDenied, MediaAssetAccessDenied):
            raise
        except Exception as exc:
            raise MediaJobAuthorizationError("Current media authorization failed") from exc
        if (
            auth.scope.tenant_id != job.tenant_id
            or auth.scope.user_id != UUID(job.payload["submitted_by"])
            or auth.world_id != job.world_id
            or auth.branch_id != job.branch_id
        ):
            raise MediaJobAuthorizationError("The media authorization proof is bound to another scope")
        request = plan.request
        if request.visibility.policy == "participants":
            if (
                auth.membership_role == MembershipRole.OBSERVER
                or request.recipient_id is None
                or request.recipient_id not in request.visibility.subject_ids
                or request.recipient_id not in auth.verified_subject_ids
            ):
                raise MediaJobAuthorizationError("The current principal cannot publish for this recipient")
        elif auth.membership_role not in {MembershipRole.OWNER, MembershipRole.EDITOR} and not auth.trusted_host:
            raise MediaJobAuthorizationError("The current principal cannot publish this media visibility")
        if request.visibility.policy == "author_only" and auth.scope.user_id != UUID(job.payload["submitted_by"]):
            raise MediaJobAuthorizationError("Author-only media must remain bound to its submitting author")
        return auth

    async def _generate_with_heartbeat(
        self,
        plan: PlannedMediaRequest,
        lease_ref: list[JobLease],
    ) -> GeneratedMediaArtifact:
        async def keep_lease() -> None:
            while True:
                await asyncio.sleep(self.heartbeat_interval_seconds)
                lease_ref[0] = await asyncio.to_thread(
                    self.repository.renew_media_job,
                    lease_ref[0],
                    lease_seconds=self.lease_seconds,
                )

        generate_task = asyncio.create_task(
            asyncio.wait_for(
                self.registry.generate_artifact(plan),
                timeout=self.plugin_timeout_seconds,
            )
        )
        heartbeat_task = asyncio.create_task(keep_lease())
        try:
            done, _pending = await asyncio.wait(
                {generate_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                error = heartbeat_task.exception()
                generate_task.cancel()
                await asyncio.gather(generate_task, return_exceptions=True)
                if error is not None:
                    raise error
                raise InvalidLease("The media lease heartbeat stopped unexpectedly")
            return await generate_task
        finally:
            if not generate_task.done():
                generate_task.cancel()
            if not heartbeat_task.done():
                heartbeat_task.cancel()
            await asyncio.gather(generate_task, heartbeat_task, return_exceptions=True)

    async def _finish_failed(self, lease: JobLease, reason_code: str) -> JobRecord:
        return await asyncio.to_thread(
            self.repository.finish_media_job,
            lease,
            JobStatus.FAILED,
            result={"reason_code": reason_code},
        )

    @staticmethod
    def _asset_result(asset: StoredMediaAsset) -> dict[str, Any]:
        return {"asset": asset.model_dump(mode="json")}

    @staticmethod
    def _reason_code(error: BaseException) -> str:
        if isinstance(error, (AccessDenied, MediaAssetAccessDenied, MediaJobAuthorizationError)):
            return "MEDIA_AUTHORIZATION_REVOKED"
        if isinstance(error, MediaAssetError):
            return "MEDIA_ASSET_STORE_FAILED"
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return "MEDIA_PLUGIN_TIMEOUT"
        if isinstance(error, LookupError):
            return "MEDIA_PLUGIN_UNAVAILABLE"
        if isinstance(error, (ValueError, TypeError)):
            return "MEDIA_REQUEST_REJECTED"
        return "MEDIA_GENERATION_FAILED"


class MediaJobAuthorizationError(RuntimeError):
    """Current branch, creator, or audience authorization no longer matches."""
