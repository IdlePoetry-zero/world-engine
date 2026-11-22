import base64
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field, StrictInt, StringConstraints
from starlette.concurrency import run_in_threadpool

from world_engine.adapters.models.contracts import DepthProfile, SpeedProfile
from world_engine.application.character_cards import (
    MAX_CARD_JSON_BYTES,
    MAX_CARD_PNG_BYTES,
    load_character_card,
    load_character_card_png,
)
from world_engine.application.errors import ApplicationFailure
from world_engine.application.npc_rehearsal import NpcRehearsalApplication, RehearsalScenario
from world_engine.domain.json_types import JsonValue
from world_engine.domain.models import CharacterCardSource, MembershipRole
from .contracts import RequestModel, Version
from .serialization import wire


class ComponentRevisionProposal(RequestModel):
    entity_id: UUID
    component_name: str = Field(min_length=1, max_length=80)
    replacement: JsonValue
    reason: str = Field(min_length=1, max_length=1000)


class RevisionPreviewRequest(RequestModel):
    branch_id: UUID | None = None
    patch_proposal: ComponentRevisionProposal


class RevisionCommitRequest(RequestModel):
    preview_id: UUID
    expected_version: Version


CharacterTrait = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class CharacterDraftRequest(RequestModel):
    branch_id: UUID
    expected_state_version: Version
    name: str = Field(min_length=1, max_length=120)
    location_id: UUID
    public_role: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=1000)
    values: list[CharacterTrait] = Field(default_factory=list, max_length=16)
    traits: list[CharacterTrait] = Field(default_factory=list, max_length=16)
    secret: str = Field(default="", max_length=1000)
    goal: str = Field(default="", max_length=1000)
    goal_destination_id: UUID | None = None
    review_count: StrictInt = Field(default=0, ge=0, le=1_000_000)
    review_interval_days: StrictInt = Field(default=7, ge=1, le=2**53 - 1)


class CharacterCardImportRequest(RequestModel):
    branch_id: UUID
    expected_state_version: Version
    location_id: UUID
    card_json: Annotated[str, StringConstraints(strip_whitespace=False, min_length=1, max_length=MAX_CARD_JSON_BYTES)]


class WorldBookPreviewRequest(RequestModel):
    branch_id: UUID
    expected_state_version: Version
    title: str = Field(min_length=1, max_length=120)
    book_json: Annotated[str, StringConstraints(strip_whitespace=False, min_length=1, max_length=256 * 1024)]


class WorldBookCommitRequest(RequestModel):
    preview_id: UUID
    public_keys: list[Annotated[str, StringConstraints(strip_whitespace=False, min_length=1, max_length=80)]] = Field(
        max_length=128,
    )


class LegacyVacanciesRequest(RequestModel):
    command_id: UUID
    expected_state_version: Version


class InvalidatedBallotRequest(RequestModel):
    command_id: UUID
    expected_state_version: Version
    expected_ballot_revision: StrictInt = Field(ge=1)


class CharacterCommitRequest(RequestModel):
    preview_id: UUID


class NpcReviewDraftRequest(RequestModel):
    branch_id: UUID
    expected_state_version: Version
    actor_id: UUID
    review_count: StrictInt = Field(ge=1, le=1_000_000)
    review_interval_days: StrictInt = Field(default=7, ge=1, le=2**53 - 1)
    goal_description: str = Field(default="", max_length=1000)
    goal_destination_id: UUID | None = None


class NpcRehearsalRequest(RequestModel):
    request_id: UUID
    branch_id: UUID
    expected_state_version: Version
    actor_id: UUID
    scenario: RehearsalScenario


def authoring_router(service, scope, model_operations=None):
    router = APIRouter(prefix="/api/v1/worlds")
    if model_operations is not None:
        rehearsal = NpcRehearsalApplication(service.repository, model_operations)

        @router.post("/{world_id}/author/npc-rehearsals")
        async def npc_rehearsal(request: Request, world_id: UUID, body: NpcRehearsalRequest):
            actor_scope = scope(request)
            settings = service.operations.settings(actor_scope)
            return await rehearsal.rehearse(
                actor_scope, world_id, body.branch_id, body.actor_id, body.scenario, int(body.expected_state_version),
                request_id=body.request_id, speed=SpeedProfile(settings["speed"]), depth=DepthProfile(settings["depth"]),
            )

    @router.post("/{world_id}/interventions/preview")
    def preview(request: Request, world_id: UUID, body: RevisionPreviewRequest):
        proposal = body.patch_proposal
        result = service.preview(scope(request), world_id, body.branch_id, proposal.entity_id,
                                 proposal.component_name, proposal.replacement, proposal.reason)
        return wire(result) if proposal.component_name in {"place", "population"} else result

    @router.post("/{world_id}/interventions")
    def commit(request: Request, world_id: UUID, body: RevisionCommitRequest):
        return service.commit(scope(request), world_id, body.preview_id, body.expected_version)

    @router.post("/{world_id}/author/characters/preview")
    def character_preview(request: Request, world_id: UUID, body: CharacterDraftRequest):
        preview = service.preview_character(
            scope(request), world_id, body.branch_id, int(body.expected_state_version),
            body.model_dump(mode="json", exclude={"branch_id", "expected_state_version"}, exclude_none=True),
        )
        if schedule := preview.get("review_schedule"):
            return preview | {"review_schedule": _review_schedule_wire(schedule)}
        return preview

    @router.post("/{world_id}/author/characters/import/preview")
    def character_card_preview(request: Request, world_id: UUID, body: CharacterCardImportRequest):
        return preview_imported_card(
            scope(request), world_id, body.branch_id, body.expected_state_version,
            load_character_card(body.card_json, body.location_id),
        )

    def preview_imported_card(access, world_id, branch_id, expected_state_version, prepared):
        draft, report, source = prepared
        raw_bytes = source["original_json"].encode("utf-8")
        archived_source = CharacterCardSource(
            source_format=source["source_format"], card_version=source["card_version"],
            original_json_base64=base64.b64encode(raw_bytes).decode("ascii"), sha256=source["sha256"],
        )
        preview = service.preview_character(
            access, world_id, branch_id, int(expected_state_version), draft,
            character_card_source=archived_source,
        )
        return preview | {"import_report": report | {"source_retained_on_commit": True}}

    @router.post("/{world_id}/author/characters/import/png/preview")
    async def character_card_png_preview(
        request: Request, world_id: UUID, branch_id: UUID, expected_state_version: Version, location_id: UUID,
    ):
        access = scope(request)
        await run_in_threadpool(
            service.repository.authorize_role, access, world_id, {MembershipRole.OWNER, MembershipRole.EDITOR},
        )
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "image/png":
            raise ApplicationFailure("CHARACTER_CARD_PNG_REQUIRED", "请选择 PNG 角色卡。", status_code=415)
        card = bytearray()
        async for chunk in request.stream():
            if len(card) + len(chunk) > MAX_CARD_PNG_BYTES:
                raise ApplicationFailure("CHARACTER_CARD_PNG_TOO_LARGE", "PNG 角色卡不能超过 8 MiB。", status_code=413)
            card.extend(chunk)
        prepared = await run_in_threadpool(load_character_card_png, bytes(card), location_id)
        return await run_in_threadpool(
            preview_imported_card, access, world_id, branch_id, expected_state_version, prepared,
        )

    @router.post("/{world_id}/author/characters/commit", status_code=201)
    def character_commit(request: Request, world_id: UUID, body: CharacterCommitRequest):
        return service.commit_character(scope(request), world_id, body.preview_id)

    @router.post("/{world_id}/author/world-books/preview")
    def world_book_preview(request: Request, world_id: UUID, body: WorldBookPreviewRequest):
        return service.preview_world_book(
            scope(request), world_id, body.branch_id, int(body.expected_state_version), body.title, body.book_json,
        )

    @router.post("/{world_id}/author/world-books/commit", status_code=201)
    def world_book_commit(request: Request, world_id: UUID, body: WorldBookCommitRequest):
        return service.commit_world_book(scope(request), world_id, body.preview_id, body.public_keys)

    @router.post("/{world_id}/author/npc-reviews/preview")
    def npc_review_preview(request: Request, world_id: UUID, body: NpcReviewDraftRequest):
        preview = service.preview_npc_review(
            scope(request), world_id, body.branch_id, int(body.expected_state_version), body.actor_id,
            body.review_count, body.review_interval_days,
            goal_description=body.goal_description, goal_destination_id=body.goal_destination_id,
        )
        return preview | {"review_schedule": _review_schedule_wire(preview["review_schedule"])}

    @router.post("/{world_id}/author/npc-reviews/commit", status_code=201)
    def npc_review_commit(request: Request, world_id: UUID, body: CharacterCommitRequest):
        return service.commit_npc_review(scope(request), world_id, body.preview_id)

    @router.post("/{world_id}/branches/{branch_id}/author/occupations/settle-legacy-vacancies")
    def settle_legacy_vacancies(request: Request, world_id: UUID, branch_id: UUID, body: LegacyVacanciesRequest):
        result = service.repository.settle_legacy_occupation_vacancies(
            scope(request), world_id, branch_id, command_id=body.command_id,
            expected_state_version=int(body.expected_state_version),
        )
        return wire({"settled": result is not None, "commit": result})

    @router.post("/{world_id}/branches/{branch_id}/author/leadership-ballots/{ballot_id}/cancel-invalidated")
    def cancel_invalidated_ballot(
        request: Request, world_id: UUID, branch_id: UUID, ballot_id: UUID, body: InvalidatedBallotRequest,
    ):
        result = service.repository.cancel_invalidated_ballot(
            scope(request), world_id, branch_id, ballot_id, command_id=body.command_id,
            expected_state_version=int(body.expected_state_version), expected_ballot_revision=body.expected_ballot_revision,
        )
        return wire({"commit": result})

    return router


def _review_schedule_wire(schedule):
    return wire(schedule) | {
        "repeat_ticks": None if schedule["repeat_ticks"] is None else str(schedule["repeat_ticks"]),
    }
