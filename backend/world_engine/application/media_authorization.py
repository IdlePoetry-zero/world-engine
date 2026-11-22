"""Authorize media against current membership and recorded source audiences."""

from uuid import UUID

from world_engine.domain.errors import AccessDenied
from world_engine.domain.models import AccessScope, JobRecord, MembershipRole

from .media_assets import MediaAssetAuth
from .media_contracts import AuthorizedMediaSource, PlannedMediaRequest


class RepositoryMediaAuthorizer:
    def __init__(self, repository):
        self.repository = repository

    def authorize(self, job: JobRecord, plan: PlannedMediaRequest) -> MediaAssetAuth:
        try:
            submitted_by = UUID(job.payload["submitted_by"])
        except (KeyError, ValueError, TypeError) as error:
            raise AccessDenied("Media job has no authorized submitting account.") from error
        scope = AccessScope(tenant_id=job.tenant_id, user_id=submitted_by)
        return self.authorize_plan(scope, job.world_id, job.branch_id, plan)

    def authorize_plan(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, plan: PlannedMediaRequest,
    ) -> MediaAssetAuth:
        request = plan.request
        if request.world_id != world_id or request.branch_id != branch_id or request.recipient_id is None:
            raise AccessDenied("Media request scope does not match its job.")
        role = self.repository.authorize_role(scope, world_id, {
            MembershipRole.OWNER, MembershipRole.EDITOR, MembershipRole.PLAYER,
        })
        subjects: frozenset[UUID] = frozenset()
        if request.visibility.policy == "participants":
            if set(request.visibility.subject_ids) != {request.recipient_id}:
                raise AccessDenied("Participant media must be scoped to its selected recipient.")
            actor = self.repository.authorize_actor(scope, world_id, request.recipient_id, branch_id=branch_id)
            subjects = frozenset({actor})
        elif role not in {MembershipRole.OWNER, MembershipRole.EDITOR} or request.recipient_id != scope.user_id:
            raise AccessDenied("Public and author media require an author acting in their own account.")

        for event_id in request.source_event_ids:
            event = self.repository.read_branch_event(scope, world_id, branch_id, event_id)
            if event.type != plan.subscription.event_type or request.kind != plan.subscription.kind:
                raise AccessDenied("Media subscription does not match the source event and requested kind.")
            try:
                AuthorizedMediaSource(
                    event=event, recipient_id=request.recipient_id,
                    authorized_description=request.authorized_description, asset_refs=request.asset_refs,
                    style=request.style, visibility=request.visibility,
                )
            except ValueError as error:
                raise AccessDenied("Media audience exceeds its source event audience.") from error
        return MediaAssetAuth(
            scope=scope, world_id=world_id, branch_id=branch_id, membership_role=role,
            verified_subject_ids=subjects,
        )
