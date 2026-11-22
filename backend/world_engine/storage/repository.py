"""Transactional repository for worlds, branches, events, and account controls."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from itertools import groupby
from sqlite3 import SQLITE_BUSY, SQLITE_FULL, SQLITE_LOCKED
from typing import Any, Iterator, NamedTuple
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy import Engine, Text, and_, cast, delete, event as sqlalchemy_event, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, aliased, defer

from world_engine.domain.errors import (
    AccessDenied,
    Conflict,
    IdempotencyConflict,
    InsufficientStorage,
    InvalidEvent,
    InvalidLease,
    NotFound,
    StaleVersion,
    StorageBusy,
)
from world_engine.domain.combat_truce import CombatTruceChangedEvent
from world_engine.domain.combat_validation import validate_combat_batch
from world_engine.domain.family_events import KINSHIP_EVENTS, KinshipLinkEndedEvent, KinshipLinkRecordedEvent, validate_kinship_batch
from world_engine.domain.occupation_events import OCCUPATION_EVENTS, validate_occupation_batch
from world_engine.domain.occupation_death_vacancies import OccupationDeathVacanciesResolvedEvent
from world_engine.domain.occupation_death_validation import derive_death_vacancies, validate_persisted_death_vacancies
from world_engine.domain.occupation_legacy_vacancies import (
    OccupationLegacyVacanciesResolvedEvent, OccupationLegacyVacancyError, plan_legacy_occupation_vacancies,
)
from world_engine.domain.npc_attendance_events import NPC_ATTENDANCE_EVENTS, NpcAttendanceStartedEvent
from world_engine.domain.npc_plan_review_events import NpcPlanReviewScheduledEvent, validate_authored_npc_review_scope
from world_engine.domain.relationship_invitation_events import RelationInvitationChangedEvent
from world_engine.domain.private_gifts import PrivateGiftChangedEvent
from world_engine.domain.private_gift_validation import NPC_GIFT_REPLY_COMMAND, validate_private_gift_batch
from world_engine.domain.relationship_reflection_events import (
    MeetingReflectionRecordedEvent, NPC_REFLECTION_COMMAND, validate_meeting_reflection_batch,
)
from world_engine.domain.relationship_partnership_events import (
    NPC_PARTNERSHIP_RESPONSE_COMMAND, RelationshipPartnershipChangedEvent, validate_relationship_partnership_batch,
)
from world_engine.domain.relationship_disclosure_events import (
    RelationshipDisclosureRecordedEvent, validate_relationship_disclosure_batch,
)
from world_engine.domain.shipment_relief_events import (
    SHIPMENT_RELIEF_DISTRIBUTED_EVENT_TYPE,
    ShipmentReliefDistributedEvent,
    validate_shipment_relief_batch,
)
from world_engine.domain.travel_itinerary import TravelItineraryArrivalProof
from world_engine.domain.travel_itinerary_progress import (
    build_travel_itinerary_arrival_proof, inspect_travel_itinerary_job,
    itinerary_requires_replan,
)
from world_engine.domain.travel_journey_events import TravelJourneyChangedEvent, validate_travel_journey_scope
from world_engine.domain.waypoint_opportunity import current_waypoint_opportunity
from world_engine.domain.waypoint_opportunity_events import (
    WAYPOINT_OPPORTUNITY_COMMAND, WaypointOpportunityDecisionRecordedEvent, validate_waypoint_opportunity_batch,
    waypoint_opportunity_request_id,
)
from world_engine.domain.relationship_invitation_state import (
    NPC_ATTENDANCE_COMMAND, NPC_REPLY_COMMAND, validate_relationship_invitation_batch,
)
from world_engine.domain.npc_meeting_attendance import (
    NPC_MEETING_ATTENDANCE_COMMAND,
    NpcMeetingAttendanceDecisionRecordedEvent,
    validate_npc_meeting_attendance_decision_batch,
)
from world_engine.domain.institutional_credit_events import InstitutionalCreditChangedEvent
from world_engine.domain.institutional_credit_extension_events import InstitutionalCreditExtensionChangedEvent
from world_engine.domain.institutional_finance_batch import validate_institutional_finance_batch
from world_engine.domain.institutional_treasury_reserve_events import InstitutionalTreasuryMobilizedEvent
from world_engine.domain.player_continuation import (
    PLAYER_CONTINUE_AFTER_DEATH_COMMAND, validate_player_character_continued_batch,
)
from world_engine.domain.leadership_ballot_state import BALLOT_COMMANDS, NPC_BALLOT_VOTE_COMMAND, validate_leadership_ballot_batch
from world_engine.domain.environmental_source_events import EnvironmentalSourceAccountOpenedEvent
from world_engine.domain.components import (
    KnowledgeRecord, PlaceComponent, ProvenanceReference, SkillsComponent, TreasuryComponent, VisibilityScope,
)
from world_engine.domain.safe_conduct_state import NPC_SAFE_CONDUCT_COMMAND, validate_safe_conduct_batch, validate_safe_conduct_travel_batch
from world_engine.domain.leadership_ballot_events import LeadershipBallotChangedEvent, leadership_ballot_event_id
from world_engine.domain.leadership_ballots import LeadershipBallotError, cancel_invalidated_leadership_ballot
from world_engine.domain.work_shift_events import WorkShiftPaidEvent
from world_engine.domain.world_books import WorldBookImportedEvent
from world_engine.rules.work_shift_validation import validate_completed_work_shift
from world_engine.domain.treatment_validation import validate_treatment_batch
from world_engine.domain.events import (
    event_payload, parse_event, requires_historical_evidence_destruction_validation,
    requires_accusation_repair_validation, requires_investigation_case_validation,
    requires_public_accusation_correction_validation,
    validate_adjudication_batch, validate_background_clock_batch,
    validate_historical_evidence_destruction_batch, validate_investigation_case_batch,
    validate_npc_attendance_scope, validate_public_accusation_correction_batch,
    validate_accusation_repair_batch,
)
from world_engine.domain.promise_events import PromiseDeadlineBreachedEvent
from world_engine.domain.promise_trust_events import PromiseDeadlineBreachedWithTrustEvent
from world_engine.domain.promise_validation import requires_promise_validation, validate_promise_batch
from world_engine.domain.market_pricing_events import MarketPriceReviewResolvedEvent
from world_engine.domain.market_pricing_validation import validate_market_price_review_batch
from world_engine.domain.shipment_validation import validate_shipment_spoilage_batch
from world_engine.domain.events import validate_population_membership_batch
from world_engine.domain.gameplay_events import (
    AuthorComponentRevisedEvent, BackgroundDeprivationResolvedEvent, BackgroundNeedsAdvancedEvent,
    BackgroundRoutineAdvancedEvent, ComponentReplacedEvent,
    GoalCreatedEvent, GovernanceSuccessionEvent, ImprovisedCheckResolvedEvent, KnowledgeAcquiredEvent,
    InvestigationCaseClosedEvent, PublicAccusationCorrectionRecordedEvent,
    AccusationRepairOfferRecordedEvent, AccusationRepairResolvedEvent, AccusationRepairTrustProof,
    PlayerCharacterContinuedEvent, ResourceAccountOpenedEvent,
    RelationDimensionChangedEvent, RelationEstablishedEvent,
)
from world_engine.domain.npc_plans import npc_journey_ids
from world_engine.domain.models import (
    AccessScope,
    BranchRecord,
    CommandEnvelope,
    CommitResult,
    DomainEvent,
    EntityCreatedEvent,
    EventPage,
    EventRecord,
    JobLease,
    JobRecord,
    JobStatus,
    MembershipRole,
    NewJob,
    OutboxRecord,
    SnapshotRecord,
    NewWorld,
    WorldCreated,
    WorldRecord,
    WorldState,
    WorldView,
    normalize_datetime,
    utc_now,
)
from world_engine.domain.reducers import (
    LedgerPosting,
    apply_event,
    ledger_postings,
    state_hash,
    state_json,
    state_payload_hash,
    validate_ledger_postings,
)
from world_engine.simulation.contracts import DecisionResponse, LongActionPlan

from .database import Database
from .event_rows import append_event_row, read_event_payload
from .models import (
    ActorControlRow,
    Base,
    BranchLeaseRow,
    BranchRow,
    CommandRow,
    EventPayloadRow,
    EventRow,
    JobRow,
    LedgerPostingRow,
    MAX_WORLD_PACK_ARCHIVE_BYTES,
    MembershipRow,
    OutboxRow,
    SnapshotRow,
    WorldPackArchiveRow,
    WorldRow,
)
from .validated_state_cache import (
    PendingPlayerHistoryContext,
    PlayerHistoryContextCache,
    StoredBranchStateIntegrityError,
    StoredPromiseTrustIntegrityError,
    ValidatedBranchStateCache,
)


MAX_EVENTS_PER_COMMAND = 10_000
MAX_EVENT_PAGE = 500
# These event types are unconditionally rejected by the player projection when
# their stored visibility is exactly author_only. Keep this small allowlist in
# sync with application.visibility.project_event: other author-only event types
# can have actor-safe special projections and must still be decoded.
PLAYER_HISTORY_PAYLOADLESS_EVENT_TYPES = frozenset({
    "core.background_routine_advanced.v1",
    "core.renewable_resource_harvested.v1",
})


class WorldHead(NamedTuple):
    """World library fields resolved without materializing the active state."""

    world: WorldRecord
    current_tick: int
    state_version: int
MAX_SHIPMENT_RELIEF_HISTORY = 40
MUTATING_ROLES = {MembershipRole.OWNER, MembershipRole.EDITOR, MembershipRole.PLAYER}
AUTHOR_ROLES = {MembershipRole.OWNER, MembershipRole.EDITOR}
MEDIA_JOB_TYPE = "media.generate"
LEGACY_VACANCIES_COMMAND = "author.settle_legacy_occupation_vacancies"


class _TemplateFork(NamedTuple):
    source_branch_id: UUID
    source_seq: int
    source_hash: str
    label: str


class MediaJobClaim(NamedTuple):
    job: JobRecord
    lease: JobLease
EVENT_STORAGE_KEY = "events_storage"
EVENT_STORAGE_KIND = "world_event_rows"
OUTBOX_STORAGE_KEY = "payload_storage"
OUTBOX_STORAGE_KIND = "world_event_record"
SIMULATION_NPC_REFLECTION_COMMAND = "simulation.npc_reflection"
NPC_REFLECTION_MODES = frozenset({
    "salient_events",
    "salient_events_and_plans",
    "salient_events_plans_and_conflicts",
})
NPC_REFLECTION_MODE_KINDS = {
    "salient_events": frozenset({"goal_completion"}),
    "salient_events_and_plans": frozenset({"goal_completion", "npc_plan_review"}),
    "salient_events_plans_and_conflicts": frozenset({
        "goal_completion", "npc_plan_review", "evidence_conflict", "investigation_case_contested",
    }),
}
NPC_REFLECTION_OUTLOOKS = {
    "goal_completion": frozenset({"carry_forward", "reconsider"}),
    "npc_plan_review": frozenset({"carry_forward", "reconsider"}),
    "evidence_conflict": frozenset({"seek_confirmation"}),
    "investigation_case_contested": frozenset({"seek_confirmation"}),
}
NPC_REFLECTION_MEMORY_CLAIMS = {
    "carry_forward": "我愿意沿用有效做法。",
    "reconsider": "我会重新考虑原方法。",
    "seek_confirmation": "我有疑点，先独立核实。",
}


def _npc_reflection_result_document(
    source_kind: str,
    outcome: str,
    outlook: str | None,
    model_call_id: str | None,
) -> dict[str, object]:
    if not isinstance(source_kind, str) or source_kind not in NPC_REFLECTION_MODE_KINDS["salient_events_plans_and_conflicts"]:
        raise InvalidEvent("NPC reflection result refers to an unsupported source kind.")
    if not isinstance(outcome, str) or outcome not in {"proposed", "deferred", "unavailable"}:
        raise InvalidEvent("NPC reflection outcome is not registered.")
    if outcome == "proposed":
        if not isinstance(outlook, str) or outlook not in NPC_REFLECTION_OUTLOOKS[source_kind]:
            raise InvalidEvent("NPC reflection proposal is not allowed for its source kind.")
    elif outcome == "deferred":
        if not isinstance(outlook, str) or outlook != "defer":
            raise InvalidEvent("A deferred NPC reflection must use the defer outlook.")
    elif outlook is not None:
        raise InvalidEvent("An unavailable NPC reflection cannot carry an outlook.")
    if model_call_id is not None and (
        not isinstance(model_call_id, str)
        or not 1 <= len(model_call_id) <= 128
        or model_call_id != model_call_id.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in model_call_id)
    ):
        raise InvalidEvent("NPC reflection call identifiers must be bounded single-line trace IDs.")
    return {"outcome": outcome, "outlook": outlook, "model_call_id": model_call_id}


def _npc_reflection_pending_document(job_id: UUID, value: object) -> dict[str, object]:
    from world_engine.domain.npc_reflection import NpcReflectionSourceEvidence

    if not isinstance(value, dict) or set(value) != {
        "version", "request_id", "source_event_id", "source_kind", "actor_id", "tick",
        "source_state_version", "source_head_seq", "source_sequence", "source_evidence", "result",
    }:
        raise InvalidEvent("The persisted NPC reflection trigger is malformed.")
    if type(value.get("version")) is not int or value.get("version") != 1 or type(value.get("tick")) is not int:
        raise InvalidEvent("The persisted NPC reflection trigger version or tick is malformed.")
    if any(type(value.get(key)) is not int for key in ("source_state_version", "source_head_seq", "source_sequence")):
        raise InvalidEvent("The persisted NPC reflection head pin is malformed.")
    try:
        request_id = UUID(str(value["request_id"]))
        source_event_id = UUID(str(value["source_event_id"]))
        actor_id = UUID(str(value["actor_id"]))
        evidence = NpcReflectionSourceEvidence.model_validate(value["source_evidence"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidEvent("The persisted NPC reflection source reference is malformed.") from exc
    source_kind = value.get("source_kind")
    if (
        not isinstance(source_kind, str)
        or source_kind not in NPC_REFLECTION_MODE_KINDS["salient_events_plans_and_conflicts"]
        or request_id != uuid5(job_id, str(source_event_id))
        or evidence.event_id != source_event_id
        or evidence.actor_id != actor_id
        or evidence.kind != source_kind
        or evidence.tick != value["tick"]
        or value["source_state_version"] < 1
        or value["source_head_seq"] < 1
        or value["source_sequence"] < 1
        or value["source_sequence"] > value["source_head_seq"]
    ):
        raise InvalidEvent("The persisted NPC reflection source does not match its deterministic trigger.")
    raw_result = value.get("result")
    if raw_result is not None:
        if not isinstance(raw_result, dict) or set(raw_result) != {"outcome", "outlook", "model_call_id"}:
            raise InvalidEvent("The persisted NPC reflection result is malformed.")
        result = _npc_reflection_result_document(
            source_kind,
            raw_result.get("outcome"),
            raw_result.get("outlook"),
            raw_result.get("model_call_id"),
        )
    else:
        result = None
    return {
        "version": 1,
        "request_id": str(request_id),
        "source_event_id": str(source_event_id),
        "source_kind": source_kind,
        "actor_id": str(actor_id),
        "tick": value["tick"],
        "source_state_version": value["source_state_version"],
        "source_head_seq": value["source_head_seq"],
        "source_sequence": value["source_sequence"],
        "source_evidence": evidence.model_dump(mode="json"),
        "result": result,
    }


def _npc_reflection_memory_event(pending: Mapping[str, object]) -> KnowledgeAcquiredEvent:
    """Build the sole deterministic private memory for an approved outlook."""
    request_id = UUID(str(pending["request_id"]))
    source_event_id = UUID(str(pending["source_event_id"]))
    actor_id = UUID(str(pending["actor_id"]))
    tick = pending["tick"]
    result = pending.get("result")
    if type(tick) is not int or not isinstance(result, dict) or result.get("outcome") != "proposed":
        raise InvalidEvent("Only an approved NPC reflection can produce private memory.")
    outlook = result.get("outlook")
    if not isinstance(outlook, str) or outlook not in NPC_REFLECTION_MEMORY_CLAIMS:
        raise InvalidEvent("NPC reflection memory has an unsupported outlook.")
    source_text = str(source_event_id)
    record = KnowledgeRecord(
        id=uuid5(request_id, "knowledge-record"),
        owner_id=actor_id,
        holder_id=actor_id,
        subject_id=actor_id,
        claim=NPC_REFLECTION_MEMORY_CLAIMS[outlook],
        source_refs=[source_event_id],
        source_event_id=source_event_id,
        provenance=[
            ProvenanceReference(kind="event", reference_id=source_text),
            ProvenanceReference(kind="rule", reference_id="npc.reflection.v1"),
        ],
        received_tick=tick,
        kind="memory",
        visibility=VisibilityScope(policy="private"),
    )
    return KnowledgeAcquiredEvent(
        event_id=uuid5(request_id, "knowledge-event"),
        record=record,
        visibility={"policy": "author_only"},
    )


class WorldRepository:
    """Synchronous SQLAlchemy repository; each public operation uses a short transaction."""

    def __init__(self, database: Database | Engine | str) -> None:
        self._branch_state_cache = ValidatedBranchStateCache()
        self._player_history_context_cache = PlayerHistoryContextCache()
        if isinstance(database, Database):
            self.database = database
        elif isinstance(database, Engine):
            self.database = Database.__new__(Database)
            self.database.engine = database
            from sqlalchemy.orm import sessionmaker

            self.database.session_factory = sessionmaker(
                bind=database, class_=Session, expire_on_commit=False, autoflush=False
            )
        else:
            self.database = Database(database)

    def create_schema(self) -> None:
        """Create tables for tests and scratch databases; production uses Alembic."""
        Base.metadata.create_all(self.database.engine)

    @contextmanager
    def _transaction(
        self, *, write: bool = False, after_commit: Callable[[], None] | None = None,
    ) -> Iterator[Session]:
        """Open a short transaction; reserve SQLite's single writer up front."""
        try:
            with self.database.session_factory() as session:
                with session.begin():
                    if write and self.database.engine.dialect.name == "sqlite":
                        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    yield session
            if after_commit is not None:
                try:
                    after_commit()
                except Exception:
                    # Optional cache work must not turn a durable commit into
                    # an application-visible failure.
                    pass
        except OperationalError as exc:
            sqlite_code = getattr(exc.orig, "sqlite_errorcode", None)
            if sqlite_code == SQLITE_FULL:
                raise InsufficientStorage("SQLite could not commit because available storage was exhausted.") from exc
            # SQLite stores the primary result code in the low byte of extended codes.
            sqlite_busy = isinstance(sqlite_code, int) and (sqlite_code & 0xFF) in (SQLITE_BUSY, SQLITE_LOCKED)
            # Psycopg reports lock conflicts, serialization failures and deadlocks by SQLSTATE.
            postgres_busy = (
                self.database.engine.dialect.name == "postgresql"
                and getattr(exc.orig, "sqlstate", None) in ("55P03", "40001", "40P01")
            )
            if sqlite_busy or postgres_busy:
                raise StorageBusy("Storage is temporarily busy. Try again.") from exc
            raise

    @staticmethod
    def validate_initial_world_state(state: WorldState) -> WorldState:
        """Revalidate genesis state before using it as the root branch checkpoint."""
        state = WorldState.model_validate(state_json(state))
        if state.npc_plans:
            raise InvalidEvent("A new world cannot seed NPC plans without the registered simulation execution path.")
        if state.promise_trust is not None:
            raise InvalidEvent("A new world cannot seed promise trust receipts without settled events.")
        if state.relationship_invitations:
            raise InvalidEvent("A new world cannot seed relationship agreement without a committed proposal.")
        if state.institutional_credit_contracts:
            raise InvalidEvent("A new world cannot seed institutional credit without committed agreements.")
        if state.private_gifts:
            raise InvalidEvent("A new world cannot seed private gifts without committed proposals.")
        if state.relationship_reflections or state.relationship_partnerships:
            raise InvalidEvent("A new world cannot seed a partnership or meeting reflection without committed actions.")
        if state.public_accusation_corrections:
            raise InvalidEvent("A new world cannot seed public-accusation correction provenance without a committed source closure.")
        if state.accusation_repair_offers:
            raise InvalidEvent("A new world cannot seed accusation repair offers without their committed correction history.")
        if state.leadership_ballots or state.leadership_vacancy_reviews:
            raise InvalidEvent("A new world cannot seed leadership proceedings without committed reviews and proposals.")
        if state.safe_conduct_treaties:
            raise InvalidEvent("A new world cannot seed safe conduct treaties without committed agreements.")
        if state.npc_attendance:
            raise InvalidEvent("A new world cannot seed NPC attendance without a committed start.")
        if state.travel_journeys:
            raise InvalidEvent("A new world cannot seed travel without a committed departure.")
        return state

    def create_world(
        self,
        scope: AccessScope,
        new_world: NewWorld,
        *,
        world_pack_archive: bytes | None = None,
    ) -> WorldCreated:
        if world_pack_archive is not None and not 0 < len(world_pack_archive) <= MAX_WORLD_PACK_ARCHIVE_BYTES:
            raise InvalidEvent("A world package archive must contain between 1 byte and 16 MiB.")
        state = self.validate_initial_world_state(new_world.state)
        now = utc_now()
        payload = state_json(state)
        digest = state_payload_hash(payload)
        try:
            with self._transaction(write=True) as session:
                world_row = WorldRow(
                    world_id=new_world.world_id,
                    tenant_id=scope.tenant_id,
                    owner_id=scope.user_id,
                    title=new_world.title,
                    active_branch_id=new_world.branch_id,
                    ruleset_version=new_world.ruleset_version,
                    pack_version=new_world.pack_version,
                    created_at=now,
                )
                session.add(world_row)
                session.flush()
                if world_pack_archive is not None:
                    session.add(WorldPackArchiveRow(world_id=new_world.world_id, archive_bytes=world_pack_archive))
                session.add(
                    MembershipRow(
                        world_id=new_world.world_id,
                        user_id=scope.user_id,
                        tenant_id=scope.tenant_id,
                        role=MembershipRole.OWNER.value,
                        created_at=now,
                    )
                )
                session.add(
                    BranchRow(
                        branch_id=new_world.branch_id,
                        world_id=new_world.world_id,
                        name="main",
                        parent_branch_id=None,
                        fork_seq=0,
                        head_seq=0,
                        state_version=0,
                        initial_state_version=0,
                        current_tick=state.current_tick,
                        initial_state_hash=digest,
                        state_hash=digest,
                        initial_state_json=payload,
                        state_json=payload,
                        created_at=now,
                    )
                )
                if state.player_id is not None:
                    session.add(
                        ActorControlRow(
                            world_id=new_world.world_id,
                            user_id=scope.user_id,
                            actor_id=state.player_id,
                            granted_by=scope.user_id,
                            created_at=now,
                        )
                    )
                session.flush()
                return WorldCreated(
                    world=self._world_record(world_row),
                    branch=self._branch_record(session.get(BranchRow, new_world.branch_id)),
                )
        except IntegrityError as exc:
            raise Conflict("World or branch identifier already exists.") from exc

    def read_world_pack_archive(self, scope: AccessScope, world_id: UUID) -> bytes | None:
        """Read the preserved import source for an authenticated world author."""
        with self._transaction() as session:
            self._require_world_pack_author(session, scope, world_id)
            return session.scalar(
                select(WorldPackArchiveRow.archive_bytes).where(WorldPackArchiveRow.world_id == world_id)
            )

    def has_world_pack_archive(self, scope: AccessScope, world_id: UUID) -> bool:
        """Check import source availability without reading the archive bytes."""
        with self._transaction() as session:
            self._require_world_pack_author(session, scope, world_id)
            return session.scalar(
                select(WorldPackArchiveRow.world_id).where(WorldPackArchiveRow.world_id == world_id)
            ) is not None

    def _require_world_pack_author(self, session: Session, scope: AccessScope, world_id: UUID) -> None:
        _, role = self._require_membership(session, scope, world_id)
        if role not in AUTHOR_ROLES:
            raise AccessDenied("World package archives require an owner or editor.")

    def read_world(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID | None = None
    ) -> WorldView:
        return self._read_world_view(scope, world_id, branch_id, detached=True)

    def project_verified_player_view(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID | None,
        project: Callable[[WorldView], dict[str, Any]],
    ) -> dict[str, Any]:
        """Run a trusted synchronous player projector after the verified read.

        The projector must treat the borrowed view as read-only, returning a
        plain response dictionary with no references to mutable state.
        """
        view = self._read_world_view(scope, world_id, branch_id, detached=False)
        return project(view)

    def _read_world_view(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID | None, *, detached: bool,
    ) -> WorldView:
        cache = self._branch_state_cache
        read_state = (
            cache.get_or_validate_detached_state_from_text
            if detached else cache.borrow_verified_state_from_text
        )
        with self._transaction() as session:
            world, _ = self._require_membership(session, scope, world_id)
            target_branch_id = branch_id or world.active_branch_id
            branch, raw_state_json = self._get_branch_with_state_text(
                session, world_id, target_branch_id,
            )
            try:
                state = read_state(
                    world_id=world_id,
                    branch_id=target_branch_id,
                    raw_state_json=raw_state_json,
                    stored_digest=branch.state_hash,
                    state_version=branch.state_version,
                    current_tick=branch.current_tick,
                )
            except StoredPromiseTrustIntegrityError as exc:
                raise Conflict("Stored promise trust projection belongs to another world.") from exc
            except StoredBranchStateIntegrityError as exc:
                raise Conflict("Stored branch projection failed its integrity check.") from exc
            record = self._branch_record(branch)
            # Every field is already validated. Revalidating the envelope would
            # rerun the world graph and component checks even on a cache hit.
            return WorldView.model_construct(
                world=self._world_record(world),
                branch=record,
                state=state,
                last_seq=record.head_seq,
                state_hash=record.state_hash,
            )

    def read_player_history_context(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID,
    ) -> tuple[WorldRecord, UUID | None, dict[UUID, str]]:
        """Read the validated player and entity-name projection for history views."""
        with self._transaction() as session:
            world, _ = self._require_membership(session, scope, world_id)
            branch, raw_state_json = self._get_branch_with_state_text(session, world_id, branch_id)
            sqlite_context_cache = self.database.engine.dialect.name == "sqlite"
            if sqlite_context_cache:
                cached_context = self._player_history_context_cache.lookup(
                    world_id=world_id, branch_id=branch_id,
                    head_seq=branch.head_seq, state_version=branch.state_version,
                    current_tick=branch.current_tick, state_hash=branch.state_hash,
                    raw_state_json=raw_state_json,
                )
                if cached_context is not None:
                    self._player_history_context_cache.note_authorized_read(world_id, branch_id)
                    return self._world_record(world), cached_context[0], cached_context[1]
            try:
                player_id, entity_names = self._branch_state_cache.get_or_validate_history_context_from_text(
                    world_id=world_id,
                    branch_id=branch_id,
                    raw_state_json=raw_state_json,
                    stored_digest=branch.state_hash,
                    state_version=branch.state_version,
                    current_tick=branch.current_tick,
                )
            except StoredPromiseTrustIntegrityError as exc:
                raise Conflict("Stored promise trust projection belongs to another world.") from exc
            except StoredBranchStateIntegrityError as exc:
                raise Conflict("Stored branch projection failed its integrity check.") from exc
            if sqlite_context_cache:
                self._player_history_context_cache.note_authorized_read(world_id, branch_id)
                self._player_history_context_cache.remember_verified_context(
                    world_id=world_id, branch_id=branch_id,
                    head_seq=branch.head_seq, state_version=branch.state_version,
                    current_tick=branch.current_tick, state_hash=branch.state_hash,
                    raw_state_json=raw_state_json, player_id=player_id, entity_names=entity_names,
                )
            return self._world_record(world), player_id, entity_names

    def publish_terminal_player_history_context_candidate(
        self, scope: AccessScope, job: JobRecord,
    ) -> bool:
        """Promote a terminal long-action projection after exact head readback.

        This is deliberately an optional SQLite-only cache path. A missing,
        stale, oversized, unsupported, or malformed candidate simply falls
        through to the normal verified history read.
        """
        cache = self._player_history_context_cache
        candidate = cache.take_terminal_candidate(job.job_id)
        if candidate is None:
            return False
        if self.database.engine.dialect.name != "sqlite":
            return False
        if job.job_type != "simulation.long_action" or job.status not in {
            JobStatus.SUCCEEDED, JobStatus.WAITING_INPUT,
        }:
            return False
        if job.status is JobStatus.WAITING_INPUT and not self._job_waits_for_player_choice(job.result):
            return False
        projection = candidate.projection
        if (projection.world_id, projection.branch_id) != (job.world_id, job.branch_id):
            return False
        if not cache.has_recent_authorized_read(job.world_id, job.branch_id):
            return False
        try:
            with self._transaction() as session:
                self._require_membership(session, scope, job.world_id)
                branch, raw_state_json = self._get_branch_with_state_text(
                    session, job.world_id, job.branch_id,
                )
            if not isinstance(raw_state_json, str):
                return False
            raw_fingerprint = hashlib.sha256(raw_state_json.encode("utf-8")).hexdigest()
            if (
                branch.head_seq != projection.head_seq
                or branch.state_version != projection.state_version
                or branch.current_tick != projection.current_tick
                or branch.state_hash != projection.state_hash
                or raw_fingerprint != projection.raw_fingerprint
            ):
                return False
            return cache.promote_verified_candidate(candidate)
        except Exception:
            return False

    def discard_terminal_player_history_context_candidate(self, job_id: UUID) -> None:
        self._player_history_context_cache.discard_terminal_candidate(job_id)

    @staticmethod
    def _job_waits_for_player_choice(result: Mapping[str, Any] | None) -> bool:
        if not isinstance(result, Mapping):
            return False
        requests = result.get("decision_requests")
        if not isinstance(requests, list):
            return False
        return any(
            isinstance(request, Mapping)
            and request.get("decision_owner") == "user"
            and isinstance(request.get("options"), list)
            and bool(request["options"])
            for request in requests
        )

    def read_verified_player_head(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID | None = None,
    ) -> tuple[WorldRecord, BranchRecord, UUID | None]:
        """Read verified player-head metadata without materializing or copying its graph."""
        with self._transaction() as session:
            world, _ = self._require_membership(session, scope, world_id)
            target_branch_id = branch_id or world.active_branch_id
            branch, raw_state_json = self._get_branch_with_state_text(session, world_id, target_branch_id)
            try:
                player_id = self._branch_state_cache.get_or_validate_player_id_from_text(
                    world_id=world_id,
                    branch_id=target_branch_id,
                    raw_state_json=raw_state_json,
                    stored_digest=branch.state_hash,
                    state_version=branch.state_version,
                    current_tick=branch.current_tick,
                )
            except StoredPromiseTrustIntegrityError as exc:
                raise Conflict("Stored promise trust projection belongs to another world.") from exc
            except StoredBranchStateIntegrityError as exc:
                raise Conflict("Stored branch projection failed its integrity check.") from exc
            return self._world_record(world), self._branch_record(branch), player_id

    def list_worlds(self, scope: AccessScope) -> list[WorldRecord]:
        with self._transaction() as session:
            rows = session.scalars(
                select(WorldRow)
                .join(MembershipRow, MembershipRow.world_id == WorldRow.world_id)
                .where(
                    WorldRow.tenant_id == scope.tenant_id,
                    MembershipRow.tenant_id == scope.tenant_id,
                    MembershipRow.user_id == scope.user_id,
                )
                .order_by(WorldRow.created_at, WorldRow.world_id)
            ).all()
            return [self._world_record(row) for row in rows]

    def list_world_heads(self, scope: AccessScope) -> list[WorldHead]:
        """List visible worlds and active-head counters without loading state JSON."""
        with self._transaction() as session:
            rows = session.execute(
                select(WorldRow, BranchRow.current_tick, BranchRow.state_version)
                .join(
                    MembershipRow,
                    and_(
                        MembershipRow.world_id == WorldRow.world_id,
                        MembershipRow.tenant_id == WorldRow.tenant_id,
                    ),
                )
                .outerjoin(
                    BranchRow,
                    and_(
                        BranchRow.world_id == WorldRow.world_id,
                        BranchRow.branch_id == WorldRow.active_branch_id,
                    ),
                )
                .where(
                    WorldRow.tenant_id == scope.tenant_id,
                    MembershipRow.tenant_id == scope.tenant_id,
                    MembershipRow.user_id == scope.user_id,
                )
                .order_by(WorldRow.created_at, WorldRow.world_id)
            ).all()
            heads = []
            for world, current_tick, state_version in rows:
                if current_tick is None or state_version is None:
                    # Match read_world's behavior for a missing/wrong-world active branch.
                    raise NotFound("Branch does not exist in this world.")
                heads.append(WorldHead(
                    self._world_record(world), int(current_tick), int(state_version),
                ))
            return heads

    def list_branches(self, scope: AccessScope, world_id: UUID) -> list[BranchRecord]:
        with self._transaction() as session:
            self._require_membership(session, scope, world_id)
            rows = session.scalars(
                select(BranchRow).where(BranchRow.world_id == world_id).order_by(BranchRow.created_at, BranchRow.name)
            ).all()
            return [self._branch_record(row) for row in rows]

    def list_events(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID,
        *,
        after_seq: int = 0,
        limit: int = 100,
        include_ancestors: bool = True,
    ) -> EventPage:
        if after_seq < 0:
            raise ValueError("after_seq must be nonnegative")
        if not 1 <= limit <= MAX_EVENT_PAGE:
            raise ValueError(f"limit must be between 1 and {MAX_EVENT_PAGE}")
        with self._transaction() as session:
            self._require_membership(session, scope, world_id)
            selected, has_more = self._select_event_page_rows(
                session, world_id, branch_id, after_seq=after_seq, limit=limit,
                include_ancestors=include_ancestors,
            )
            events = [self._event_record(row) for row in selected]
            next_cursor = events[-1].sequence if events else after_seq
            return EventPage(branch_id=branch_id, events=events, next_cursor=next_cursor, has_more=has_more)

    def list_player_history_events(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID,
        *,
        after_seq: int = 0,
        limit: int = 100,
        include_ancestors: bool = True,
    ) -> EventPage:
        """Read a player-history page, avoiding payloads known to be wholly hidden.

        The raw event rows are selected and paginated exactly like list_events.
        Only the two allowlisted author-only event types are materialized with
        an empty payload; their normal player projection is always ``None``.
        Other visibility shapes and event types retain the full decode and
        verification path, including actor-safe author-only projections.
        """
        if after_seq < 0:
            raise ValueError("after_seq must be nonnegative")
        if not 1 <= limit <= MAX_EVENT_PAGE:
            raise ValueError(f"limit must be between 1 and {MAX_EVENT_PAGE}")
        with self._transaction() as session:
            self._require_membership(session, scope, world_id)
            selected, has_more = self._select_event_page_rows(
                session, world_id, branch_id, after_seq=after_seq, limit=limit,
                include_ancestors=include_ancestors,
            )
            events = [
                self._event_record(row, decode_payload=not (
                    row.event_type in PLAYER_HISTORY_PAYLOADLESS_EVENT_TYPES
                    and row.visibility_json == {"policy": "author_only"}
                ))
                for row in selected
            ]
            next_cursor = selected[-1].sequence if selected else after_seq
            return EventPage(branch_id=branch_id, events=events, next_cursor=next_cursor, has_more=has_more)

    def _select_event_page_rows(
        self,
        session: Session,
        world_id: UUID,
        branch_id: UUID,
        *,
        after_seq: int,
        limit: int,
        include_ancestors: bool,
    ) -> tuple[list[EventRow], bool]:
        """Share the raw row selection so player pages keep ledger cursors."""
        branch = self._get_branch(
            session, world_id, branch_id, defer_state_json=True, defer_initial_state_json=True,
        )
        chain = self._branch_chain(
            session, branch, branch.head_seq, include_ancestors, defer_state_json=True,
        )
        rows: list[EventRow] = []
        # Ancestor segments precede descendant segments and do not overlap in
        # sequence. Fetch one extra raw row so hidden rows still count toward
        # the page boundary and determine has_more exactly as before.
        for row_branch_id, max_seq in reversed(chain):
            if max_seq <= after_seq:
                continue
            rows.extend(
                session.scalars(
                    select(EventRow).where(
                        EventRow.branch_id == row_branch_id,
                        EventRow.sequence > after_seq,
                        EventRow.sequence <= max_seq,
                    ).order_by(EventRow.sequence).limit(limit + 1 - len(rows))
                ).all()
            )
            if len(rows) > limit:
                break
        has_more = len(rows) > limit
        return rows[:limit], has_more

    def list_shipment_relief_history(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID,
        actor_id: UUID,
        *,
        limit: int = MAX_SHIPMENT_RELIEF_HISTORY,
        through_seq: int | None = None,
    ) -> list[EventRecord]:
        """Return a small, branch-visible history of relief events involving an actor.

        Filter to this event type and participant in SQL before reading payloads;
        a consumed delivered lot therefore does not erase its receipt, and the
        query never materializes unrelated event history.
        """
        if not isinstance(actor_id, UUID):
            raise ValueError("actor_id must be a UUID")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_SHIPMENT_RELIEF_HISTORY:
            raise ValueError(f"limit must be between 1 and {MAX_SHIPMENT_RELIEF_HISTORY}")
        with self._transaction() as session:
            self._require_membership(session, scope, world_id)
            branch = self._get_branch(
                session, world_id, branch_id, defer_state_json=True, defer_initial_state_json=True,
            )
            sequence = branch.head_seq if through_seq is None else through_seq
            if (
                not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or sequence < branch.fork_seq
                or sequence > branch.head_seq
            ):
                raise ValueError("through_seq must be within the branch's committed history")
            recipient_id = EventRow.payload_json["plan"]["recipient_id"].as_string()
            rows: list[EventRow] = []
            remaining = limit
            # _branch_chain is child first. Query newest local records first,
            # then continue into the parent only if the limit is not filled.
            for source_branch_id, cutoff in self._branch_chain(
                session, branch, sequence, include_ancestors=True, defer_state_json=True,
            ):
                if remaining <= 0:
                    break
                selected = session.scalars(
                    select(EventRow)
                    .where(
                        EventRow.world_id == world_id,
                        EventRow.branch_id == source_branch_id,
                        EventRow.sequence <= cutoff,
                        EventRow.event_type == SHIPMENT_RELIEF_DISTRIBUTED_EVENT_TYPE,
                        or_(
                            EventRow.actor_id == actor_id,
                            recipient_id == str(actor_id),
                        ),
                    )
                    .order_by(EventRow.sequence.desc())
                    .limit(remaining)
                ).all()
                rows.extend(selected)
                remaining -= len(selected)
            rows.sort(key=lambda row: (row.sequence, str(row.branch_id)))
            return [self._event_record(row) for row in rows]

    def list_actor_rest_history(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, actor_id: UUID, *, through_seq: int,
    ) -> list[dict[str, str | int]]:
        """Return up to six committed rest periods for the authorized actor."""
        with self._transaction() as session:
            _, role = self._require_membership(session, scope, world_id)
            if role not in AUTHOR_ROLES and session.get(ActorControlRow, (world_id, scope.user_id, actor_id)) is None:
                raise AccessDenied("This account does not control the requested actor.")
            branch = self._get_branch(
                session, world_id, branch_id, defer_state_json=True, defer_initial_state_json=True,
            )
            if not branch.fork_seq <= through_seq <= branch.head_seq:
                raise ValueError("through_seq must be within the branch's committed history")
            history = []
            for source_branch_id, cutoff in self._branch_chain(
                session, branch, through_seq, include_ancestors=True, defer_state_json=True,
            ):
                remaining = 6 - len(history)
                if not remaining:
                    break
                actions = session.execute(
                    select(EventRow.event_id, EventRow.tick, CommandRow.payload_json["duration_ticks"].as_integer())
                    .join(CommandRow, CommandRow.command_id == EventRow.command_id)
                    .where(
                        EventRow.branch_id == source_branch_id,
                        EventRow.sequence <= cutoff,
                        EventRow.actor_id == actor_id,
                        EventRow.event_type == "core.time_advanced.v1",
                        CommandRow.branch_id == source_branch_id,
                        CommandRow.actor_id == actor_id,
                        CommandRow.command_type == "action.rest",
                    )
                    .order_by(EventRow.sequence.desc())
                    .limit(remaining)
                )
                for event_id, end_tick, duration in actions:
                    if type(duration) is not int or duration < 60:
                        continue
                    history.append({
                        "event_id": str(event_id), "started_tick": end_tick - duration,
                        "ended_tick": end_tick, "duration_seconds": duration,
                    })
            return list(reversed(history))

    def list_actor_training_history(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, actor_id: UUID, *, through_seq: int,
    ) -> list[dict[str, str | int]]:
        """Read recent training slices from the actor's skill and clock events."""
        with self._transaction() as session:
            _, role = self._require_membership(session, scope, world_id)
            if role not in AUTHOR_ROLES and session.get(ActorControlRow, (world_id, scope.user_id, actor_id)) is None:
                raise AccessDenied("This account does not control the requested actor.")
            branch = self._get_branch(
                session, world_id, branch_id, defer_state_json=True, defer_initial_state_json=True,
            )
            if not branch.fork_seq <= through_seq <= branch.head_seq:
                raise ValueError("through_seq must be within the branch's committed history")
            history = []
            for source_branch_id, cutoff in self._branch_chain(
                session, branch, through_seq, include_ancestors=True, defer_state_json=True,
            ):
                remaining = 6 - len(history)
                if not remaining:
                    break
                slices = session.execute(
                    self._training_slice_query(world_id, source_branch_id, actor_id, cutoff)
                    .order_by(EventRow.sequence.desc())
                    .limit(remaining)
                )
                for event_id, start_tick, end_tick, replacement, _job_id in slices:
                    receipt = self._training_slice(event_id, start_tick, end_tick, replacement)
                    if receipt is not None:
                        history.append(receipt)
            return list(reversed(history))

    def list_actor_breakthrough_history(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, actor_id: UUID, *, through_seq: int,
    ) -> list[EventRecord]:
        """Read up to six committed breakthrough outcomes in the actor's branch history."""
        with self._transaction() as session:
            _, role = self._require_membership(session, scope, world_id)
            if role not in AUTHOR_ROLES and session.get(ActorControlRow, (world_id, scope.user_id, actor_id)) is None:
                raise AccessDenied("This account does not control the requested actor.")
            branch = self._get_branch(
                session, world_id, branch_id, defer_state_json=True, defer_initial_state_json=True,
            )
            if not branch.fork_seq <= through_seq <= branch.head_seq:
                raise ValueError("through_seq must be within the branch's committed history")
            history: list[EventRecord] = []
            for source_branch_id, cutoff in self._branch_chain(
                session, branch, through_seq, include_ancestors=True, defer_state_json=True,
            ):
                remaining = 6 - len(history)
                if not remaining:
                    break
                rows = session.scalars(
                    select(EventRow)
                    .where(
                        EventRow.world_id == world_id, EventRow.branch_id == source_branch_id,
                        EventRow.sequence <= cutoff, EventRow.actor_id == actor_id,
                        EventRow.event_type == "core.cultivation_breakthrough_resolved.v1",
                    )
                    .order_by(EventRow.sequence.desc())
                    .limit(remaining)
                )
                history.extend(self._event_record(row) for row in rows)
            return list(reversed(history))

    def list_actor_study_totals(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, actor_id: UUID, *, through_seq: int,
    ) -> list[dict[str, str | int]]:
        """Aggregate all committed training slices for the latest recorded study jobs."""
        with self._transaction() as session:
            _, role = self._require_membership(session, scope, world_id)
            if role not in AUTHOR_ROLES and session.get(ActorControlRow, (world_id, scope.user_id, actor_id)) is None:
                raise AccessDenied("This account does not control the requested actor.")
            branch = self._get_branch(
                session, world_id, branch_id, defer_state_json=True, defer_initial_state_json=True,
            )
            if not branch.fork_seq <= through_seq <= branch.head_seq:
                raise ValueError("through_seq must be within the branch's committed history")
            jobs = session.scalars(
                select(JobRow)
                .where(
                    JobRow.world_id == world_id, JobRow.branch_id == branch_id,
                    JobRow.job_type == "simulation.long_action",
                    JobRow.payload_json["plan"]["actor_id"].as_string() == str(actor_id),
                    JobRow.payload_json["plan"]["activity"].as_string() == "study",
                )
                .order_by(JobRow.created_at.desc(), JobRow.job_id.desc())
                .limit(4)
            )
            totals: dict[str, dict[str, str | int]] = {}
            for job in jobs:
                plan = LongActionPlan.model_validate(job.payload_json["plan"])
                if plan.started_tick is None or plan.skill_id is None:
                    continue
                totals[str(job.job_id)] = {
                    "job_id": str(job.job_id), "skill_id": plan.skill_id,
                    "planned_start_tick": plan.started_tick, "planned_target_tick": plan.target_tick,
                    "actual_training_seconds": 0, "committed_slice_count": 0,
                }
            if not totals:
                return []
            slices = session.execute(
                self._training_slice_query(world_id, branch_id, actor_id, through_seq)
                .where(CommandRow.command_type == "simulation.window",
                       CommandRow.payload_json["job_id"].as_string().in_(list(totals)))
            )
            for event_id, start_tick, end_tick, replacement, job_id in slices:
                receipt = self._training_slice(event_id, start_tick, end_tick, replacement)
                total = totals[job_id]
                if receipt is None or receipt["skill_id"] != total["skill_id"]:
                    continue
                if not int(total["planned_start_tick"]) <= start_tick < end_tick <= int(total["planned_target_tick"]):
                    continue
                total["actual_training_seconds"] = int(total["actual_training_seconds"]) + end_tick - start_tick
                total["committed_slice_count"] = int(total["committed_slice_count"]) + 1
            return list(totals.values())

    @staticmethod
    def _training_slice_query(world_id: UUID, branch_id: UUID, actor_id: UUID, through_seq: int):
        clock = aliased(EventRow, name="training_clock")
        command_clock = aliased(EventRow, name="training_command_clock")
        previous = aliased(EventRow, name="before_training_clock")
        # Resolve the clock event by command ID before joining the surrounding history.
        clock_id = select(command_clock.event_id).where(
            command_clock.command_id == EventRow.command_id,
            command_clock.event_type == "core.time_advanced.v1",
            command_clock.tick == EventRow.tick,
        ).correlate(EventRow).scalar_subquery()
        return (
            select(clock.event_id,
                   func.coalesce(previous.tick, BranchRow.initial_state_json["current_tick"].as_integer()),
                   clock.tick, EventRow.payload_json["replacement"], CommandRow.payload_json["job_id"].as_string())
            .select_from(EventRow)
            .join(CommandRow, CommandRow.command_id == EventRow.command_id)
            .join(BranchRow, BranchRow.branch_id == EventRow.branch_id)
            .join(clock, and_(
                clock.event_id == clock_id, clock.branch_id == EventRow.branch_id,
                clock.sequence < EventRow.sequence,
            ))
            .outerjoin(previous, and_(
                previous.branch_id == clock.branch_id, previous.sequence == clock.sequence - 1,
            ))
            .where(
                EventRow.world_id == world_id, EventRow.branch_id == branch_id,
                EventRow.sequence <= through_seq, EventRow.actor_id == actor_id,
                EventRow.event_type == "core.component_replaced.v1",
                EventRow.payload_json["entity_id"].as_string() == str(actor_id),
                EventRow.payload_json["component_name"].as_string() == "skills",
                CommandRow.command_type.in_(("action.train", "simulation.window", "backup.restored_event_batch")),
            )
        )

    @staticmethod
    def _training_slice(event_id: UUID, start_tick: int | None, end_tick: int, replacement) -> dict[str, str | int] | None:
        if type(start_tick) is not int or not 0 < end_tick - start_tick <= 8 * 3600:
            return None
        duration = end_tick - start_tick
        skills = SkillsComponent.model_validate(replacement).skills
        trained = [skill_id for skill_id, value in skills.items()
                   if value.last_practiced_tick == end_tick and value.trained_ticks >= duration]
        if len(trained) != 1:
            return None
        return {
            "event_id": str(event_id), "skill_id": trained[0],
            "started_tick": start_tick, "ended_tick": end_tick, "duration_seconds": duration,
        }

    def list_actor_movement_history(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, actor_id: UUID, *, through_seq: int,
    ) -> list[dict[str, str | int]]:
        """Read recent direct moves of the authorized actor at a branch checkpoint."""
        with self._transaction() as session:
            _, role = self._require_membership(session, scope, world_id)
            if role not in AUTHOR_ROLES and session.get(ActorControlRow, (world_id, scope.user_id, actor_id)) is None:
                raise AccessDenied("This account does not control the requested actor.")
            branch = self._get_branch(
                session, world_id, branch_id, defer_state_json=True, defer_initial_state_json=True,
            )
            if not branch.fork_seq <= through_seq <= branch.head_seq:
                raise ValueError("through_seq must be within the branch's committed history")
            history = []
            for source_branch_id, cutoff in self._branch_chain(
                session, branch, through_seq, include_ancestors=True, defer_state_json=True,
            ):
                remaining = 4 - len(history)
                if not remaining:
                    break
                rows = session.scalars(
                    select(EventRow)
                    .where(
                        EventRow.world_id == world_id,
                        EventRow.branch_id == source_branch_id,
                        EventRow.sequence <= cutoff,
                        EventRow.event_type == "core.movement_completed.v1",
                        EventRow.payload_json["actor_id"].as_string() == str(actor_id),
                    )
                    .order_by(EventRow.sequence.desc())
                    .limit(remaining)
                )
                for row in rows:
                    move = row.payload_json
                    history.append({
                        "event_id": str(row.event_id),
                        "from_place_id": move["from_place_id"], "to_place_id": move["to_place_id"],
                        "started_tick": move["started_tick"], "arrived_tick": move["arrived_tick"],
                    })
            return history

    def read_branch_event(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, event_id: UUID,
    ) -> EventRecord:
        """Read one event from its originating branch for host-side authorization."""
        with self._transaction() as session:
            self._require_membership(session, scope, world_id)
            row = session.scalar(select(EventRow).where(
                EventRow.world_id == world_id, EventRow.branch_id == branch_id, EventRow.event_id == event_id,
            ))
            if row is None:
                raise NotFound("Event does not exist in this branch.")
            return self._event_record(row)

    def read_command_receipt(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID,
        command_id: UUID, actor_id: UUID, idempotency_key: str,
    ) -> CommitResult | None:
        with self._transaction() as session:
            _, role = self._require_membership(session, scope, world_id)
            self._require_actor_control(session, scope, world_id, role, actor_id)
            row = session.get(CommandRow, command_id)
            if row is None or (row.world_id, row.branch_id, row.actor_id, row.idempotency_key) != (
                world_id, branch_id, actor_id, idempotency_key,
            ):
                return None
            return self._load_commit_result(session, row)

    def find_death_events(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, subject_ids: set[UUID],
    ) -> dict[UUID, UUID]:
        if not subject_ids:
            return {}
        with self._transaction() as session:
            _, role = self._require_membership(session, scope, world_id)
            if role not in {MembershipRole.OWNER, MembershipRole.EDITOR}:
                raise AccessDenied("Only an owner or editor can inspect death evidence.")
            branch = self._get_branch(session, world_id, branch_id)
            return self._find_death_events_in_session(session, world_id, branch, subject_ids)

    def unavailable_continuation_actor_ids(self, scope: AccessScope, world_id: UUID) -> set[UUID]:
        """Return actor IDs held by another user, for owner-only continuation choices."""
        with self._transaction() as session:
            world, role = self._require_membership(session, scope, world_id)
            if role != MembershipRole.OWNER or world.owner_id != scope.user_id:
                raise AccessDenied("Only the world owner can inspect player-continuation targets.")
            return set(session.scalars(
                select(ActorControlRow.actor_id).where(
                    ActorControlRow.world_id == world_id,
                    ActorControlRow.user_id != scope.user_id,
                )
            ).all())

    def find_journey_resolution_events(self, scope, world_id, branch_id, journey_ids):
        """Read actual arrival/interruption receipts within this branch's ancestry."""
        requested = set(journey_ids)
        if any(not isinstance(item, UUID) for item in requested) or len(requested) > 1000:
            raise InvalidEvent("Journey receipt lookup requires at most 1000 UUIDs.")
        if not requested:
            return {}
        with self._transaction() as session:
            _, role = self._require_membership(session, scope, world_id)
            if role not in {MembershipRole.OWNER, MembershipRole.EDITOR}:
                raise AccessDenied("Only an owner or editor can inspect NPC journey evidence.")
            branch = self._get_branch(session, world_id, branch_id)
            return self._find_journey_resolution_events_in_session(session, world_id, branch, requested)

    def record_model_decision(self, lease, response, expected_state_version):
        """Durably pin a worker response before its world transaction.

        The lease is the internal host authority. A different answer cannot
        overwrite a pending or already-consumed response with the same ID.
        This does not claim provider exactly-once billing across network loss.
        """
        from world_engine.simulation.contracts import DecisionResponse

        parsed = DecisionResponse.model_validate(response.model_dump(mode="python"))
        if parsed.source not in {"model", "policy"}:
            raise InvalidEvent("Only the trusted worker can record a model or policy response.")
        document = parsed.model_dump(mode="json")
        with self._transaction(write=True) as session:
            job = self._validate_worker_lease(session, lease, lease.world_id, lease.branch_id)
            if job.job_type not in {"simulation.world_advance", "simulation.long_action"}:
                raise InvalidEvent("Model decision receipts require a simulation job.")
            payload = job.payload_json
            for key in ("pending_decision", "last_decision"):
                previous = payload.get(key)
                if previous is not None and previous.get("request_id") == str(parsed.request_id):
                    previous = DecisionResponse.model_validate(previous).model_dump(mode="json")
                    if previous != document:
                        raise Conflict("A recorded decision cannot be replaced by another answer.")
                    return "pending" if key == "pending_decision" else "committed"
            if payload.get("pending_decision") is not None:
                raise Conflict("Another decision is still pending for this simulation job.")
            branch = self._get_branch(session, lease.world_id, lease.branch_id)
            if branch.state_version != expected_state_version or parsed.state_version != expected_state_version:
                raise StaleVersion(parsed.state_version, branch.state_version)
            if parsed.at_tick != branch.current_tick or job.committed_through_tick != branch.current_tick:
                raise Conflict("The model response no longer matches the simulation clock.")
            job.payload_json = {**payload, "pending_decision": document}
            job.updated_at = utc_now()
            return "pending"

    @staticmethod
    def _require_npc_reflection_job(world: WorldRow, role: MembershipRole, scope: AccessScope, job: JobRow) -> None:
        if role != MembershipRole.OWNER or world.owner_id != scope.user_id:
            raise AccessDenied("NPC reflection requires the canonical world owner host.")
        if job.job_type not in {"simulation.world_advance", "simulation.long_action"}:
            raise InvalidLease("NPC reflection receipts require a simulation worker lease.")

    @staticmethod
    def _validate_npc_meeting_attendance_in_session(
        session, world, branch, job, command, events, marker, state,
    ) -> None:
        from world_engine.simulation.contracts import DecisionResponse
        from world_engine.simulation.npc_meeting_attendance import next_npc_meeting_attendance_candidate

        if (marker is None or command.causation_ids
                or command.command_id != uuid5(job.job_id, f"window:{branch.state_version}")
                or job.committed_through_tick != state.current_tick):
            raise InvalidEvent("The meeting decision must match the active simulation window.")
        validate_npc_meeting_attendance_decision_batch(
            events, pre_state=state, world_id=world.world_id, branch_id=branch.branch_id,
            command_id=command.command_id, actor_id=marker.actor_id, ruleset_version=world.ruleset_version,
            command_type=command.command_type, command_payload=command.payload,
        )
        controlled = set(session.scalars(select(ActorControlRow.actor_id).where(
            ActorControlRow.world_id == world.world_id,
        )))
        if job.job_type == "simulation.long_action":
            controlled.add(LongActionPlan.model_validate(job.payload_json["plan"]).actor_id)
        candidate = next_npc_meeting_attendance_candidate(state, externally_controlled_actor_ids=controlled)
        record = marker.record
        if candidate is None or (candidate.actor_id, candidate.invitation_id, candidate.expected_invitation_revision) != (
            record.actor_id, record.invitation_id, record.source_invitation_revision,
        ):
            raise InvalidEvent("The NPC is no longer eligible for this meeting decision.")
        raw_response = job.payload_json.get("pending_decision")
        response = DecisionResponse.model_validate(raw_response) if raw_response is not None else None
        if response is None or response.npc_planning_status is not None or (
            response.request_id, response.at_tick, response.state_version, response.source, response.option_id,
        ) != (record.request_id, record.as_of_tick, branch.state_version, "model", record.choice):
            raise InvalidEvent("The leased worker has no matching model decision receipt.")

    def _npc_reflection_source_in_session(
        self,
        session: Session,
        world: WorldRow,
        branch: BranchRow,
        job: JobRow,
        pending: Mapping[str, object],
    ) -> tuple[dict[str, object], dict[str, object] | None, frozenset[UUID]]:
        """Re-read and re-project the exact pinned source under the live lease."""
        from world_engine.domain.npc_reflection import (
            NpcReflectionSourceEvidence,
            NpcReflectionTrigger,
            project_npc_reflection_source,
            select_npc_reflection_trigger,
        )

        mode = job.payload_json.get("npc_reflection_mode")
        if not isinstance(mode, str) or mode not in NPC_REFLECTION_MODES:
            raise InvalidEvent("This simulation job has no supported NPC reflection profile.")
        if not isinstance(pending, Mapping):
            raise InvalidEvent("The persisted NPC reflection trigger is malformed.")
        parsed = _npc_reflection_pending_document(job.job_id, dict(pending))
        if parsed["source_kind"] not in NPC_REFLECTION_MODE_KINDS[mode]:
            raise InvalidEvent("The pending NPC reflection source is outside its pinned profile.")
        if (
            branch.state_version != parsed["source_state_version"]
            or branch.head_seq != parsed["source_head_seq"]
            or branch.current_tick != parsed["tick"]
        ):
            return parsed, None, frozenset()
        event_row = session.scalar(select(EventRow).where(
            EventRow.world_id == world.world_id,
            EventRow.branch_id == branch.branch_id,
            EventRow.event_id == UUID(str(parsed["source_event_id"])),
            EventRow.sequence == parsed["source_sequence"],
        ))
        if (
            event_row is None
            or event_row.state_version_after != parsed["source_state_version"]
            or event_row.tick != parsed["tick"]
            or event_row.sequence > branch.head_seq
        ):
            return parsed, None, frozenset()
        event = self._typed_event_from_row(event_row)
        state = self._checked_branch_state(branch)
        controlled_ids = frozenset(session.scalars(select(ActorControlRow.actor_id).where(
            ActorControlRow.world_id == world.world_id,
        )))
        trigger = select_npc_reflection_trigger(
            [event], state, mode=mode, externally_controlled_actor_ids=controlled_ids,
        )
        expected_trigger = NpcReflectionTrigger(
            event_id=UUID(str(parsed["source_event_id"])),
            actor_id=UUID(str(parsed["actor_id"])),
            tick=parsed["tick"],
            kind=parsed["source_kind"],
        )
        if trigger != expected_trigger:
            return parsed, None, controlled_ids
        evidence = project_npc_reflection_source(
            event, state, externally_controlled_actor_ids=controlled_ids,
        )
        if evidence is None:
            return parsed, None, controlled_ids
        checked_evidence = NpcReflectionSourceEvidence.model_validate(evidence.model_dump(mode="python"))
        if checked_evidence.model_dump(mode="json") != parsed["source_evidence"]:
            return parsed, None, controlled_ids
        return parsed, {
            "status": checked_evidence.status,
            "title": checked_evidence.title,
            "predicate": checked_evidence.predicate,
            "effective_tick": checked_evidence.effective_tick,
        }, controlled_ids

    def _validate_npc_reflection_memory_commit_in_session(
        self,
        session: Session,
        world: WorldRow,
        branch: BranchRow,
        job: JobRow,
        role: MembershipRole,
        scope: AccessScope,
        command: CommandEnvelope,
        events: Sequence[DomainEvent],
    ) -> dict[str, object]:
        if len(events) != 1 or not isinstance(events[0], KnowledgeAcquiredEvent):
            raise InvalidEvent("NPC reflection memory requires exactly one typed knowledge event.")
        if (
            job.job_type not in {"simulation.world_advance", "simulation.long_action"}
            or not isinstance(job.payload_json.get("npc_reflection_mode"), str)
            or job.payload_json.get("npc_reflection_mode") not in NPC_REFLECTION_MODES
            or job.payload_json.get("pending_decision") is not None
        ):
            raise InvalidLease("NPC reflection memory does not match an active opted-in simulation job.")
        if role != MembershipRole.OWNER or world.owner_id != scope.user_id:
            raise AccessDenied("NPC reflection memory requires the canonical world owner host.")
        raw_pending = job.payload_json.get("pending_npc_reflection")
        if raw_pending is None:
            raise Conflict("NPC reflection memory has no pending approved outlook.")
        pending = _npc_reflection_pending_document(job.job_id, raw_pending)
        if pending["result"] is None or pending["result"]["outcome"] != "proposed":
            raise Conflict("NPC reflection memory requires a persisted proposed outlook.")
        expected_event = _npc_reflection_memory_event(pending)
        if events[0] != expected_event:
            raise InvalidEvent("NPC reflection memory must match the deterministic private record.")
        request_id = UUID(str(pending["request_id"]))
        source_event_id = UUID(str(pending["source_event_id"]))
        outlook = pending["result"]["outlook"]
        if (
            command.command_id != uuid5(request_id, "knowledge-command")
            or command.idempotency_key != f"npc-reflection:{request_id}"
            or command.expected_state_version != pending["source_state_version"]
            or command.command_type != SIMULATION_NPC_REFLECTION_COMMAND
            or command.actor_id != scope.user_id
            or command.payload != {
                "kind": "npc_reflection_memory",
                "job_id": str(job.job_id),
                "request_id": str(request_id),
                "source_event_id": str(source_event_id),
                "outlook": outlook,
            }
            or command.causation_ids != [source_event_id]
            or branch.state_version != pending["source_state_version"]
            or branch.head_seq != pending["source_head_seq"]
        ):
            raise InvalidEvent("NPC reflection memory command is not bound to its exact trigger receipt.")
        verified_pending, evidence, controlled_ids = self._npc_reflection_source_in_session(
            session, world, branch, job, raw_pending,
        )
        if verified_pending != pending:
            raise Conflict("The pending NPC reflection source changed before memory commit.")
        if evidence is None:
            if UUID(str(pending["actor_id"])) in controlled_ids:
                raise AccessDenied("The source NPC is now controlled and cannot receive an automatic reflection.")
            raise Conflict("The NPC reflection source is no longer eligible for private memory.")
        if UUID(str(pending["actor_id"])) in controlled_ids:
            raise AccessDenied("The source NPC is now controlled and cannot receive an automatic reflection.")
        return pending

    def read_pending_npc_reflection_source(
        self, scope: AccessScope, lease: JobLease,
    ) -> dict[str, object] | None:
        """Return one validated actor-safe reflection source, or stale evidence as None.

        The evidence value is deliberately small. A None evidence value means
        the source no longer matches the current exact branch head and must be
        persisted as unavailable without a model request.
        """
        with self._transaction() as session:
            world, role = self._require_membership(session, scope, lease.world_id)
            job = self._validate_worker_lease(session, lease, lease.world_id, lease.branch_id)
            self._require_npc_reflection_job(world, role, scope, job)
            raw_pending = job.payload_json.get("pending_npc_reflection")
            if raw_pending is None:
                return None
            branch = self._get_branch(session, lease.world_id, lease.branch_id)
            pending, evidence, _ = self._npc_reflection_source_in_session(
                session, world, branch, job, raw_pending,
            )
            result = pending["result"]
            return {
                "request_id": pending["request_id"],
                "source_event_id": pending["source_event_id"],
                "source_kind": pending["source_kind"],
                "actor_id": pending["actor_id"],
                "tick": pending["tick"],
                "source_state_version": pending["source_state_version"],
                "evidence": evidence,
                "result": result,
            }

    def persist_npc_reflection_result(
        self,
        scope: AccessScope,
        lease: JobLease,
        *,
        request_id: UUID,
        source_event_id: UUID,
        outcome: str,
        outlook: str | None,
        model_call_id: str | None,
    ) -> str:
        """Pin one closed model outlook or durable no-event outcome to a leased job."""
        if not isinstance(request_id, UUID) or not isinstance(source_event_id, UUID):
            raise InvalidEvent("NPC reflection receipt references must be UUIDs.")
        with self._transaction(write=True) as session:
            world, role = self._require_membership(session, scope, lease.world_id)
            job = self._validate_worker_lease(session, lease, lease.world_id, lease.branch_id)
            self._require_npc_reflection_job(world, role, scope, job)
            payload = job.payload_json
            raw_pending = payload.get("pending_npc_reflection")
            if raw_pending is None:
                previous = payload.get("last_npc_reflection")
                if isinstance(previous, dict) and (
                    previous.get("request_id") == str(request_id)
                    and previous.get("source_event_id") == str(source_event_id)
                ):
                    expected = _npc_reflection_result_document(
                        previous.get("source_kind"), outcome, outlook, model_call_id,
                    )
                    prior_response = previous.get("response")
                    if not isinstance(prior_response, dict):
                        prior_response = {key: previous.get(key) for key in expected}
                    if {key: prior_response.get(key) for key in expected} == expected:
                        return "duplicate"
                raise Conflict("There is no matching pending NPC reflection to resolve.")
            branch = self._get_branch(session, lease.world_id, lease.branch_id)
            pending, evidence, _ = self._npc_reflection_source_in_session(
                session, world, branch, job, raw_pending,
            )
            if pending["request_id"] != str(request_id) or pending["source_event_id"] != str(source_event_id):
                raise Conflict("The NPC reflection response does not match the pending source request.")
            result = _npc_reflection_result_document(
                pending["source_kind"], outcome, outlook, model_call_id,
            )
            current_result = pending["result"]
            if current_result is not None:
                if current_result != result:
                    if (
                        result["outcome"] == "unavailable"
                        and current_result.get("outcome") == "proposed"
                        and evidence is None
                        and model_call_id in {None, current_result.get("model_call_id")}
                    ):
                        unavailable = _npc_reflection_result_document(
                            pending["source_kind"], "unavailable", None,
                            current_result.get("model_call_id"),
                        )
                        durable = self._npc_reflection_receipt(
                            pending, unavailable, response=current_result,
                        )
                        job.payload_json = {
                            key: value for key, value in payload.items() if key != "pending_npc_reflection"
                        } | {"last_npc_reflection": durable}
                        job.updated_at = utc_now()
                        return "cleared"
                    raise Conflict("A recorded NPC reflection response cannot be replaced.")
                if result["outcome"] == "proposed" and evidence is None:
                    durable = self._npc_reflection_receipt(pending, {
                        "outcome": "unavailable", "outlook": None,
                        "model_call_id": result["model_call_id"],
                    }, response=result)
                    job.payload_json = {
                        key: value for key, value in payload.items() if key != "pending_npc_reflection"
                    } | {"last_npc_reflection": durable}
                    job.updated_at = utc_now()
                    return "cleared"
                return "pending" if result["outcome"] == "proposed" else "duplicate"
            if result["outcome"] == "proposed" and evidence is None:
                requested_result = result
                result = _npc_reflection_result_document(
                    pending["source_kind"], "unavailable", None, result["model_call_id"],
                )
            else:
                requested_result = result
            if result["outcome"] == "proposed":
                updated = {**pending, "result": result}
                job.payload_json = {**payload, "pending_npc_reflection": updated}
                job.updated_at = utc_now()
                return "pending"
            durable = self._npc_reflection_receipt(pending, result, response=requested_result)
            job.payload_json = {
                key: value for key, value in payload.items() if key != "pending_npc_reflection"
            } | {"last_npc_reflection": durable}
            job.updated_at = utc_now()
            return "cleared"

    @staticmethod
    def _npc_reflection_receipt(
        pending: Mapping[str, object],
        result: Mapping[str, object],
        *,
        response: Mapping[str, object] | None = None,
        memory_event_id: UUID | None = None,
    ) -> dict[str, object]:
        receipt: dict[str, object] = {
            "request_id": pending["request_id"],
            "source_event_id": pending["source_event_id"],
            "source_kind": pending["source_kind"],
            "actor_id": pending["actor_id"],
            "tick": pending["tick"],
            "outcome": result["outcome"],
            "outlook": result["outlook"],
            "model_call_id": result["model_call_id"],
            "response": dict(response if response is not None else result),
        }
        if memory_event_id is not None:
            receipt["memory_event_id"] = str(memory_event_id)
        return receipt

    def commit_pending_npc_reflection(
        self, scope: AccessScope, lease: JobLease,
    ) -> CommitResult | None:
        """Commit one accepted outlook as deterministic actor-private memory."""
        source = self.read_pending_npc_reflection_source(scope, lease)
        if source is None or source.get("result") is None:
            return None
        result = source["result"]
        if not isinstance(result, dict) or result.get("outcome") != "proposed":
            return None
        if source.get("evidence") is None:
            self.persist_npc_reflection_result(
                scope, lease,
                request_id=UUID(str(source["request_id"])),
                source_event_id=UUID(str(source["source_event_id"])),
                outcome="proposed", outlook=result.get("outlook"),
                model_call_id=result.get("model_call_id"),
            )
            return None
        request_id = UUID(str(source["request_id"]))
        source_event_id = UUID(str(source["source_event_id"]))
        event = _npc_reflection_memory_event({
            "request_id": source["request_id"],
            "source_event_id": source["source_event_id"],
            "source_kind": source["source_kind"],
            "actor_id": source["actor_id"],
            "tick": source["tick"],
            "result": result,
        })
        command = CommandEnvelope(
            command_id=uuid5(request_id, "knowledge-command"),
            actor_id=scope.user_id,
            idempotency_key=f"npc-reflection:{request_id}",
            expected_state_version=source["source_state_version"],
            command_type=SIMULATION_NPC_REFLECTION_COMMAND,
            payload={
                "kind": "npc_reflection_memory",
                "job_id": str(lease.job_id),
                "request_id": str(request_id),
                "source_event_id": str(source_event_id),
                "outlook": result["outlook"],
            },
            causation_ids=[source_event_id],
        )
        try:
            return self.commit_command(
                scope, lease.world_id, lease.branch_id, command, [event], lease=lease,
            )
        except AccessDenied:
            # A control assignment may change between source verification and
            # this write. Downgrade only after a fresh leased read proves the
            # same request is now ineligible; otherwise preserve the failure.
            current = self.read_pending_npc_reflection_source(scope, lease)
            if (
                current is None
                or current.get("request_id") != str(request_id)
                or current.get("source_event_id") != str(source_event_id)
                or current.get("evidence") is not None
            ):
                raise
            self.persist_npc_reflection_result(
                scope, lease, request_id=request_id, source_event_id=source_event_id,
                outcome="proposed", outlook=result.get("outlook"),
                model_call_id=result.get("model_call_id"),
            )
            return None

    def _validate_npc_plan_command_in_session(self, session, world, branch, command, events, lease, state):
        from world_engine.simulation.contracts import DecisionResponse, SimulationContext, WorldAdvancePlan
        from world_engine.simulation.npc_plan_validation import validate_npc_plan_simulation_batch

        job = self._validate_worker_lease(session, lease, world.world_id, branch.branch_id)
        if job.job_type == "simulation.world_advance":
            plan = WorldAdvancePlan.model_validate(job.payload_json["plan"])
            if command.actor_id != world.owner_id:
                raise AccessDenied("NPC world advancement must retain the authenticated host actor.")
        elif job.job_type == "simulation.long_action":
            plan = LongActionPlan.model_validate(job.payload_json["plan"])
            if command.actor_id != plan.actor_id:
                raise AccessDenied("NPC background execution must retain the focal plan's actor.")
        else:
            raise InvalidEvent("NPC plan batches require a registered simulation worker lease.")
        if job.committed_through_tick != state.current_tick or command.payload != {
            "job_id": str(job.job_id), "next_tick": str(state.current_tick),
        }:
            raise InvalidEvent("NPC plan batches must match their exact zero-time leased window.")
        controlled = frozenset(session.scalars(select(ActorControlRow.actor_id).where(
            ActorControlRow.world_id == world.world_id,
        )))
        journey_ids = npc_journey_ids(state)
        receipts = self._find_journey_resolution_events_in_session(session, world.world_id, branch, journey_ids) if journey_ids else {}
        pending = job.payload_json.get("pending_decision")
        responses = (DecisionResponse.model_validate(pending),) if pending else ()
        for event in events:
            if event.type == "core.npc_plan_review_resolved.v1" and event.source == "model" and not responses:
                raise InvalidEvent("A model-owned NPC review requires its durable worker decision receipt.")
        seed = state.metadata.get("generation_seed")
        if seed is None:
            seed = state.metadata.get("seed")
        context = SimulationContext(
            world_id=world.world_id, branch_id=branch.branch_id, command_id=command.command_id,
            command_actor_id=command.actor_id, state_version=command.expected_state_version,
            world_seed=str(world.world_id if seed is None or seed == "" else seed),
            ruleset_version=world.ruleset_version, speed=job.payload_json.get("speed", "balanced"),
            depth=job.payload_json.get("depth", "deep"), externally_controlled_actor_ids=controlled,
            decision_responses=responses, journey_resolution_receipts=receipts,
            journey_receipt_branch_ids=frozenset(receipt.branch_id for receipt in receipts.values()),
        )
        return validate_npc_plan_simulation_batch(state, events, plan, context,
                                                journey_resolution_receipts=receipts)

    def _find_journey_resolution_events_in_session(self, session, world_id, branch, requested, *, through_seq=None):
        remaining = {str(item) for item in requested}
        found = {}
        cutoff = branch.head_seq if through_seq is None else through_seq
        for source_id, source_cutoff in self._branch_chain(session, branch, cutoff, True):
            rows = session.execute(
                select(EventRow, EventPayloadRow)
                .outerjoin(EventPayloadRow, EventPayloadRow.event_id == EventRow.event_id)
                .where(
                    EventRow.world_id == world_id, EventRow.branch_id == source_id,
                    EventRow.sequence <= source_cutoff,
                    EventRow.event_type == "core.travel_journey_changed.v1",
                    or_(EventRow.payload_json["journey"]["journey_id"].as_string().in_(remaining),
                        EventPayloadRow.event_id.is_not(None)),
                ).order_by(EventRow.sequence.desc()).execution_options(yield_per=32)
            )
            for row, sidecar in rows:
                payload = read_event_payload(row, prefetched_sidecar=sidecar)
                journey_id = payload.get("journey", {}).get("journey_id")
                if journey_id not in remaining or payload.get("operation") not in {"arrive", "interrupt"}:
                    continue
                receipt = parse_event(payload | {
                    "type": row.event_type, "event_id": str(row.event_id), "visibility": row.visibility_json,
                })
                if not isinstance(receipt, TravelJourneyChangedEvent):
                    raise InvalidEvent("Stored journey receipt has an unexpected event type.")
                found[UUID(journey_id)] = receipt
                remaining.remove(journey_id)
                if not remaining:
                    return found
        return found

    def find_public_accusation_closures(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID,
        actor_id: UUID,
        case_ids: Sequence[UUID],
    ) -> dict[UUID, UUID]:
        """Resolve only this actor's actual, uncorrected public-disconfirmation event IDs."""
        if not isinstance(case_ids, Sequence) or isinstance(case_ids, (str, bytes)):
            raise InvalidEvent("Public-accusation case IDs must be a sequence of UUIDs.")
        if any(not isinstance(case_id, UUID) for case_id in case_ids):
            raise InvalidEvent("Public-accusation case IDs must be a sequence of UUIDs.")
        requested = set(case_ids)
        with self._transaction() as session:
            _, role = self._require_membership(session, scope, world_id)
            self._require_actor_control(session, scope, world_id, role, actor_id)
            branch = self._get_branch(session, world_id, branch_id)
            if not requested:
                return {}
            state = self._checked_branch_state(branch)
            owned_cases = {
                item.id: item for item in state.investigation_cases
                if item.id in requested
                and item.owner_id == actor_id
                and item.status == "closed"
                and item.public_accusation
                and item.outcome == "disconfirmed"
                and not any(correction.case_file_id == item.id for correction in state.public_accusation_corrections)
            }
            if not owned_cases:
                return {}
            closures = self._find_public_accusation_closures_in_session(
                session, world_id, branch, actor_id, set(owned_cases), through_seq=branch.head_seq,
            )
            return {
                case_id: event_id for case_id, event_id in closures.items()
                if (case_file := owned_cases[case_id]).suspect_id is not None
                and (closure := self._read_public_accusation_closure_in_lineage(
                    session, world_id, branch, event_id, through_seq=branch.head_seq,
                )) is not None
                and closure.case_file_id == case_file.id
                and closure.suspect_id == case_file.suspect_id
                and closure.closed_tick == case_file.closed_tick
                and closure.expected_revision + 1 == case_file.revision
            }

    def find_accusation_repair_trust_proof(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID,
        offer_id: UUID,
        target_id: UUID,
        *,
        requested_by: UUID | None = None,
    ) -> AccusationRepairTrustProof | None:
        """Resolve verified prior harm only for this authorized target and pending offer."""
        with self._transaction() as session:
            world, role = self._require_membership(session, scope, world_id)
            if requested_by is None:
                self._require_actor_control(session, scope, world_id, role, target_id)
            else:
                self._require_actor_control(session, scope, world_id, role, requested_by)
            branch = self._get_branch(session, world_id, branch_id)
            state = self._checked_branch_state(branch)
            offer = next((item for item in state.accusation_repair_offers if item.offer_id == offer_id), None)
            if (
                offer is None or offer.status != "pending" or offer.suspect_id != target_id
                or requested_by is not None and requested_by != offer.accuser_id
            ):
                return None
            if requested_by is not None:
                self._require_uncontrolled_npc(session, scope, world, role, target_id)

            stored_offer = self._read_accusation_repair_offer_in_lineage(
                session, world_id, branch, offer_id, through_seq=branch.head_seq,
            )
            if stored_offer is None or not self._same_accusation_repair_offer(stored_offer[0].record, offer):
                return None
            correction = next((
                item for item in state.public_accusation_corrections
                if item.event_id == offer.correction_event_id
            ), None)
            if (
                correction is None
                or correction.actor_id != offer.accuser_id
                or correction.case_file_id != offer.case_file_id
                or correction.suspect_id != offer.suspect_id
                or correction.source_closure_event_id != offer.source_closure_event_id
            ):
                return None
            correction_event = self._read_public_accusation_correction_in_lineage(
                session, world_id, branch, correction.event_id, through_seq=branch.head_seq,
            )
            if correction_event is None or correction_event.record != correction:
                return None
            return self._find_accusation_repair_trust_proof_for_closure(
                session, world_id, branch, correction, through_seq=branch.head_seq,
            )

    def authorize_hosted_npc(self, scope: AccessScope, world_id: UUID, actor_id: UUID) -> None:
        """Check hosted character control before sending a private decision to a model."""
        with self._transaction() as session:
            world, role = self._require_membership(session, scope, world_id)
            self._require_uncontrolled_npc(session, scope, world, role, actor_id)

    def world_actor_control_ids(self, scope: AccessScope, world_id: UUID) -> frozenset[UUID]:
        with self._transaction() as session:
            world, role = self._require_membership(session, scope, world_id)
            if role != MembershipRole.OWNER or world.owner_id != scope.user_id:
                raise AccessDenied("Only the world owner can inspect controls for whole-world simulation.")
            return frozenset(session.scalars(select(ActorControlRow.actor_id).where(ActorControlRow.world_id == world_id)))

    @staticmethod
    def _require_uncontrolled_npc(session: Session, scope: AccessScope, world: WorldRow, role: MembershipRole, actor_id: UUID) -> None:
        if role != MembershipRole.OWNER or world.owner_id != scope.user_id:
            raise AccessDenied("Only the world owner can solicit a hosted NPC response.")
        controlled = session.scalar(select(ActorControlRow.actor_id).where(
            ActorControlRow.world_id == world.world_id, ActorControlRow.actor_id == actor_id,
        ).limit(1))
        if controlled is not None:
            raise AccessDenied("A user-controlled character must respond for themselves.")

    def cancel_invalidated_ballot(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, ballot_id: UUID, *,
        command_id: UUID, expected_state_version: int, expected_ballot_revision: int,
    ) -> CommitResult:
        command = CommandEnvelope(
            command_id=command_id, actor_id=scope.user_id,
            idempotency_key=f"invalidated-ballot:{command_id}", command_type=BALLOT_COMMANDS["cancel"],
            expected_state_version=expected_state_version,
            payload={"kind": "cancel_invalidated_leadership_ballot", "ballot_id": str(ballot_id),
                     "expected_ballot_revision": expected_ballot_revision},
        )
        with self._transaction() as session:
            world, role = self._require_membership(session, scope, world_id)
            if role not in AUTHOR_ROLES:
                raise AccessDenied("Leadership ballot maintenance requires an owner or editor.")
            branch = self._get_branch(session, world_id, branch_id)
            previous = session.get(CommandRow, command_id)
            if previous is not None:
                if (previous.world_id, previous.branch_id, previous.actor_id, previous.idempotency_key) != (
                    world_id, branch_id, scope.user_id, command.idempotency_key,
                ):
                    raise IdempotencyConflict("Command ID is already in use.")
                return self._idempotent_result(session, previous, command.payload_hash())
            if branch.state_version != expected_state_version:
                raise StaleVersion(expected_state_version, branch.state_version)
            state = self._checked_branch_state(branch)
            ballot = next((item for item in state.leadership_ballots if item.ballot_id == ballot_id), None)
            if ballot is None:
                raise NotFound("Leadership ballot does not exist on this branch.")
            if ballot.revision != expected_ballot_revision:
                raise StaleVersion(expected_ballot_revision, ballot.revision)
            try:
                cancelled = cancel_invalidated_leadership_ballot(state, ballot, expected_ballot_revision=expected_ballot_revision)
            except LeadershipBallotError as exc:
                raise InvalidEvent(str(exc)) from exc
            event = LeadershipBallotChangedEvent(
                event_id=leadership_ballot_event_id(command_id, world.ruleset_version, "cancel", ballot_id),
                world_id=world_id, branch_id=branch_id, command_id=command_id, actor_id=scope.user_id,
                ruleset_version=world.ruleset_version, expected_revision=expected_ballot_revision,
                operation="cancel", resulting_ballot=cancelled,
            )
        return self.commit_command(scope, world_id, branch_id, command, [event])

    def settle_legacy_occupation_vacancies(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, *,
        command_id: UUID, expected_state_version: int,
    ) -> CommitResult | None:
        """Append an explicit, version-checked repair for a legacy branch head."""
        command = CommandEnvelope(
            command_id=command_id, actor_id=scope.user_id,
            idempotency_key=f"legacy-vacancies:{command_id}", command_type=LEGACY_VACANCIES_COMMAND,
            expected_state_version=expected_state_version,
        )
        with self._transaction() as session:
            world, role = self._require_membership(session, scope, world_id)
            if role not in AUTHOR_ROLES:
                raise AccessDenied("Employment maintenance requires an owner or editor.")
            branch = self._get_branch(session, world_id, branch_id)
            previous = session.get(CommandRow, command_id)
            if previous is not None:
                if (previous.world_id, previous.branch_id, previous.actor_id, previous.idempotency_key) != (
                    world_id, branch_id, scope.user_id, command.idempotency_key,
                ):
                    raise IdempotencyConflict("Command ID is already in use.")
                return self._idempotent_result(session, previous, command.payload_hash())
            if branch.state_version != expected_state_version:
                raise StaleVersion(expected_state_version, branch.state_version)
            state = self._checked_branch_state(branch)
            try:
                event = plan_legacy_occupation_vacancies(
                    state, world_id=world_id, branch_id=branch_id, command_id=command_id,
                    actor_id=scope.user_id, ruleset_version=world.ruleset_version,
                )
            except OccupationLegacyVacancyError as exc:
                raise InvalidEvent(str(exc)) from exc
        if event is None:
            return None
        return self.commit_command(scope, world_id, branch_id, command, [event])

    def commit_template_fork(
        self,
        scope: AccessScope,
        world_id: UUID,
        source_branch_id: UUID,
        source_seq: int,
        source_hash: str,
        branch_id: UUID,
        label: str,
        command: CommandEnvelope,
        events: Sequence[AuthorComponentRevisedEvent],
    ) -> CommitResult:
        """Create a branch at a reviewed source head and commit its first author revision batch."""
        normalized_label = self._validate_fork_input(source_seq, label)
        return self.commit_command(
            scope, world_id, branch_id, command, events,
            _template_fork=_TemplateFork(source_branch_id, source_seq, source_hash, normalized_label),
        )

    def commit_command(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID,
        command: CommandEnvelope,
        events: Sequence[DomainEvent | Mapping[str, object]],
        *,
        lease: object | None = None,
        itinerary_care_receipt: object | None = None,
        itinerary_transit_projection_receipt: object | None = None,
        capture_player_history_candidate: bool = False,
        capture_npc_reflection: bool = False,
        _template_fork: _TemplateFork | None = None,
    ) -> CommitResult:
        """Atomically append typed events, update projection, ledger, command, and outbox."""
        if not events:
            raise InvalidEvent("A committed command must contain at least one domain event.")
        if len(events) > MAX_EVENTS_PER_COMMAND:
            raise InvalidEvent(f"A command may contain no more than {MAX_EVENTS_PER_COMMAND} events.")
        typed_events = tuple(parse_event(event) for event in events)
        credit_maturity = any(isinstance(event, InstitutionalCreditChangedEvent) and event.operation == "overdue"
                              for event in typed_events)
        if credit_maturity and lease is None:
            raise AccessDenied("Institutional credit maturity requires a current simulation worker lease.")
        waypoint_choice = next((event for event in typed_events
                                if isinstance(event, WaypointOpportunityDecisionRecordedEvent)), None)
        if (waypoint_choice is not None or command.command_type == WAYPOINT_OPPORTUNITY_COMMAND) and lease is None:
            raise AccessDenied("A waypoint choice requires the current itinerary worker lease.")
        npc_meeting_decision = next((event for event in typed_events
                                     if isinstance(event, NpcMeetingAttendanceDecisionRecordedEvent)), None)
        npc_meeting_batch = npc_meeting_decision is not None or command.command_type == NPC_MEETING_ATTENDANCE_COMMAND
        if npc_meeting_batch and lease is None:
            raise AccessDenied("An autonomous meeting decision requires the current simulation worker lease.")
        npc_plan_batch = any(event.type in {
            "core.npc_plan_created.v1", "core.npc_plan_revised.v1", "core.npc_plan_review_resolved.v1",
        } for event in typed_events)
        if npc_plan_batch and (lease is None or command.command_type != "simulation.window"):
            raise InvalidEvent("NPC plan commits require the registered simulation execution path.")
        npc_reflection_memory_batch = command.command_type == SIMULATION_NPC_REFLECTION_COMMAND
        if npc_reflection_memory_batch and (
            lease is None or len(typed_events) != 1 or not isinstance(typed_events[0], KnowledgeAcquiredEvent)
        ):
            raise InvalidEvent("NPC reflection memory requires one leased private knowledge event.")
        if any(isinstance(event, OccupationDeathVacanciesResolvedEvent) for event in typed_events):
            raise InvalidEvent("Employment closures are derived by the repository from committed deaths.")
        event_ids = [event.event_id for event in typed_events]
        if len(set(event_ids)) != len(event_ids):
            raise InvalidEvent("Event IDs must be unique within a command.")
        request_hash = command.payload_hash()
        if _template_fork is not None:
            if command.command_type != "author.revise_component" or any(
                not isinstance(event, AuthorComponentRevisedEvent) for event in typed_events
            ):
                raise InvalidEvent("Template forks require an author component revision batch.")
            request_hash = self._hash_json({
                "operation": "template_fork",
                "command_hash": request_hash,
                "command_id": str(command.command_id),
                "correlation_id": str(command.correlation_id) if command.correlation_id is not None else None,
                "source_branch_id": str(_template_fork.source_branch_id),
                "source_seq": _template_fork.source_seq,
                "source_hash": _template_fork.source_hash,
                "label": _template_fork.label,
                "events": [event.model_dump(mode="json") for event in typed_events],
            })

        capture_history_candidate_requested = (
            capture_player_history_candidate
            and lease is not None
            and self.database.engine.dialect.name == "sqlite"
            and self._player_history_context_cache.has_recent_authorized_read(world_id, branch_id)
        )
        capture_history_candidate_allowed = False
        captured_history_state: WorldState | None = None
        captured_history_raw_fingerprint: str | None = None
        captured_history_job_id: UUID | None = None
        committed_history_result: CommitResult | None = None
        capture_npc_reflection_allowed = False
        npc_reflection_commit_pending: dict[str, object] | None = None

        def retain_history_context_after_commit() -> None:
            if (not capture_history_candidate_allowed or captured_history_state is None
                    or captured_history_raw_fingerprint is None or captured_history_job_id is None
                    or committed_history_result is None):
                return
            try:
                player_id, entity_names = self._branch_state_cache._history_context(
                    world_id, captured_history_state,
                )
                candidate = self._player_history_context_cache.build_terminal_candidate(
                    job_id=captured_history_job_id,
                    world_id=world_id,
                    branch_id=branch_id,
                    head_seq=committed_history_result.last_seq,
                    state_version=committed_history_result.state_version,
                    current_tick=captured_history_state.current_tick,
                    state_hash=committed_history_result.state_hash,
                    raw_fingerprint=captured_history_raw_fingerprint,
                    player_id=player_id,
                    entity_names=entity_names,
                )
                if candidate is not None:
                    self._player_history_context_cache.retain_terminal_candidate(candidate)
            except Exception:
                # Optional cache work must not alter an already durable commit.
                return

        try:
            with self._transaction(
                write=True, after_commit=retain_history_context_after_commit,
            ) as session:
                world, role = self._require_membership(session, scope, world_id)
                branch = (
                    self._get_branch(session, world_id, branch_id, for_update=True)
                    if _template_fork is None else None
                )
                self._require_mutation_role(role)
                validate_institutional_finance_batch(
                    typed_events, world_id=world_id, branch_id=branch_id, command_id=command.command_id,
                    actor_id=command.actor_id, ruleset_version=world.ruleset_version,
                    command_type=command.command_type, command_payload=command.payload,
                )
                self._authorize_author_events(scope, role, command.command_type, command.actor_id, typed_events)
                if _template_fork is not None:
                    target = self._prepare_template_fork(
                        session, world_id, branch_id, _template_fork, command, request_hash,
                    )
                    if isinstance(target, CommitResult):
                        return target
                    branch = target
                enrollments = [event for event in typed_events if isinstance(event, NpcPlanReviewScheduledEvent)]
                if enrollments:
                    validate_authored_npc_review_scope(
                        enrollments, world_id=world_id, branch_id=branch_id, command_id=command.command_id,
                        command_actor_id=command.actor_id,
                    )
                    for enrollment in enrollments:
                        self._require_uncontrolled_npc(session, scope, world, role, enrollment.actor_id)
                npc_validation = None
                npc_before_state = None
                if npc_plan_batch:
                    if role != MembershipRole.OWNER or world.owner_id != scope.user_id:
                        raise AccessDenied("NPC plan execution requires the canonical world owner host.")
                    previous = session.scalar(select(CommandRow).where(
                        CommandRow.branch_id == branch_id, CommandRow.idempotency_key == command.idempotency_key,
                    ))
                    if previous is not None:
                        return self._idempotent_result(session, previous, request_hash)
                    if command.expected_state_version != branch.state_version:
                        raise StaleVersion(command.expected_state_version, branch.state_version)
                    npc_before_state = self._checked_branch_state(branch)
                    npc_validation = self._validate_npc_plan_command_in_session(
                        session, world, branch, command, typed_events, lease, npc_before_state,
                    )
                validation_events = npc_validation.core_rule_events if npc_validation is not None else typed_events
                validation_actor_id = npc_validation.actual_npc_actor_id if npc_validation is not None else command.actor_id
                validation_command_type = (npc_validation.action_command_type or command.command_type) if npc_validation is not None else command.command_type
                validation_command_payload = (npc_validation.action_command_payload or {}) if npc_validation is not None else command.payload
                continuation_events = [
                    event for event in typed_events if isinstance(event, PlayerCharacterContinuedEvent)
                ]
                continuation_requested = (
                    command.command_type == PLAYER_CONTINUE_AFTER_DEATH_COMMAND or bool(continuation_events)
                )
                if continuation_requested:
                    if role != MembershipRole.OWNER or world.owner_id != scope.user_id:
                        raise AccessDenied("Only the world owner can continue the player character after death.")
                    if (
                        len(typed_events) == 1
                        and len(continuation_events) == 1
                        and continuation_events[0].actor_id == command.actor_id
                        and continuation_events[0].previous_player_id == command.actor_id
                        and continuation_events[0].world_id == world_id
                        and continuation_events[0].branch_id == branch_id
                        and continuation_events[0].command_id == command.command_id
                        and continuation_events[0].ruleset_version == world.ruleset_version
                    ):
                        prior_continuation = session.scalar(
                            select(CommandRow).where(
                                CommandRow.branch_id == branch_id,
                                CommandRow.idempotency_key == command.idempotency_key,
                            )
                        )
                        if prior_continuation is not None:
                            return self._idempotent_result(session, prior_continuation, request_hash)
                validate_kinship_batch(validation_events, world_id=world_id, branch_id=branch_id,
                                      command_id=command.command_id, actor_id=validation_actor_id,
                                      ruleset_version=world.ruleset_version, command_type=validation_command_type)
                validate_occupation_batch(validation_events, world_id=world_id, branch_id=branch_id,
                                         command_id=command.command_id, actor_id=validation_actor_id, ruleset_version=world.ruleset_version,
                                         command_type=validation_command_type, command_payload=validation_command_payload)
                if not npc_meeting_batch:
                    validate_relationship_invitation_batch(
                        validation_events, world_id=world_id, branch_id=branch_id, command_id=command.command_id,
                        actor_id=validation_actor_id, ruleset_version=world.ruleset_version,
                        command_type=validation_command_type, command_payload=validation_command_payload,
                        require_work_calendar_checked=True,
                    )
                validate_meeting_reflection_batch(
                    validation_events, world_id=world_id, branch_id=branch_id, command_id=command.command_id,
                    actor_id=validation_actor_id, ruleset_version=world.ruleset_version,
                    command_type=validation_command_type, command_payload=validation_command_payload,
                )
                validate_relationship_partnership_batch(
                    validation_events, world_id=world_id, branch_id=branch_id, command_id=command.command_id,
                    actor_id=validation_actor_id, ruleset_version=world.ruleset_version,
                    command_type=validation_command_type, command_payload=validation_command_payload,
                )
                validate_shipment_relief_batch(
                    validation_events, world_id=world_id, branch_id=branch_id, command_id=command.command_id,
                    actor_id=validation_actor_id, ruleset_version=world.ruleset_version,
                    command_type=validation_command_type, command_payload=validation_command_payload,
                )
                validate_leadership_ballot_batch(
                    validation_events, world_id=world_id, branch_id=branch_id, command_id=command.command_id,
                    actor_id=validation_actor_id, ruleset_version=world.ruleset_version,
                    command_type=validation_command_type, command_payload=validation_command_payload,
                )
                validate_safe_conduct_batch(
                    validation_events, world_id=world_id, branch_id=branch_id, command_id=command.command_id,
                    actor_id=validation_actor_id, ruleset_version=world.ruleset_version,
                    command_type=validation_command_type, command_payload=validation_command_payload,
                )
                if command.command_type == BALLOT_COMMANDS["cancel"] and (
                    role not in AUTHOR_ROLES or command.actor_id != scope.user_id
                ):
                    raise AccessDenied("Leadership ballot maintenance requires an authenticated owner or editor.")
                if command.command_type in {
                    NPC_REPLY_COMMAND, NPC_ATTENDANCE_COMMAND, NPC_REFLECTION_COMMAND, NPC_PARTNERSHIP_RESPONSE_COMMAND,
                    NPC_BALLOT_VOTE_COMMAND, NPC_SAFE_CONDUCT_COMMAND,
                }:
                    reply = typed_events[0]
                    self._require_uncontrolled_npc(session, scope, world, role, reply.actor_id)
                repair_responses = [
                    event for event in typed_events if isinstance(event, AccusationRepairResolvedEvent)
                ]
                for response in repair_responses:
                    if response.requested_by is None:
                        if command.actor_id != response.actor_id:
                            raise AccessDenied("A direct accusation-repair response must use its target as actor.")
                        self._require_actor_control(session, scope, world_id, role, response.actor_id)
                    else:
                        if command.actor_id != response.requested_by:
                            raise AccessDenied("A hosted accusation-repair request must use its accuser as actor.")
                        self._require_actor_control(session, scope, world_id, role, response.requested_by)
                        self._require_uncontrolled_npc(session, scope, world, role, response.actor_id)
                if command.command_type == LEGACY_VACANCIES_COMMAND:
                    maintenance = typed_events[0]
                    if (maintenance.world_id, maintenance.branch_id, maintenance.command_id, maintenance.ruleset_version) != (
                        world_id, branch_id, command.command_id, world.ruleset_version,
                    ) or command.payload or command.causation_ids:
                        raise InvalidEvent("Employment maintenance must match its exact persisted command context.")
                self._require_actor_control(session, scope, world_id, role, command.actor_id)

                previous = session.scalar(
                    select(CommandRow).where(
                        CommandRow.branch_id == branch_id,
                        CommandRow.idempotency_key == command.idempotency_key,
                    )
                )
                if previous is not None:
                    return self._idempotent_result(session, previous, request_hash)

                now = utc_now()
                if lease is None:
                    self._ensure_no_active_branch_lease(session, branch_id, now)

                if command.expected_state_version != branch.state_version:
                    raise StaleVersion(command.expected_state_version, branch.state_version)
                if lease is not None:
                    leased_job = self._validate_worker_lease(session, lease, world_id, branch_id)
                    if credit_maturity and leased_job.job_type not in {"simulation.world_advance", "simulation.long_action"}:
                        raise InvalidLease("Institutional credit maturity requires a simulation job.")
                    if (capture_history_candidate_requested
                            and leased_job.job_type == "simulation.long_action"):
                        capture_history_candidate_allowed = True
                        captured_history_job_id = lease.job_id
                    if leased_job.job_type == "simulation.world_advance" and (
                        role != MembershipRole.OWNER or scope.user_id != world.owner_id or command.actor_id != scope.user_id
                    ):
                        raise AccessDenied("World advancement must be committed as the world owner.")
                    if npc_reflection_memory_batch:
                        if leased_job.job_type not in {"simulation.world_advance", "simulation.long_action"}:
                            raise InvalidLease("NPC reflection memory requires a matching simulation worker lease.")
                        if (
                            role != MembershipRole.OWNER or scope.user_id != world.owner_id
                            or command.actor_id != scope.user_id
                        ):
                            raise AccessDenied("NPC reflection memory requires the canonical world owner host.")
                    pending_decision = leased_job.payload_json.get("pending_decision")
                    user_choice_pending = False
                    if pending_decision is not None:
                        try:
                            user_choice_pending = DecisionResponse.model_validate(pending_decision).source == "user"
                        except (TypeError, ValueError, ValidationError):
                            user_choice_pending = True
                    if (
                        capture_npc_reflection
                        and command.command_type == "simulation.window"
                        and waypoint_choice is None
                        and not user_choice_pending
                        and leased_job.job_type in {"simulation.world_advance", "simulation.long_action"}
                        and role == MembershipRole.OWNER
                        and world.owner_id == scope.user_id
                        and command.actor_id == scope.user_id
                        and leased_job.status == JobStatus.RUNNING.value
                        and isinstance(leased_job.payload_json.get("npc_reflection_mode"), str)
                        and leased_job.payload_json.get("npc_reflection_mode") in NPC_REFLECTION_MODES
                        and leased_job.payload_json.get("pending_npc_reflection") is None
                    ):
                        capture_npc_reflection_allowed = True
                if command.command_type == "simulation.autonomous_vacancy":
                    if (lease is None or leased_job.job_type not in {"simulation.world_advance", "simulation.long_action"}
                        or role != MembershipRole.OWNER or scope.user_id != world.owner_id or command.actor_id != scope.user_id):
                        raise AccessDenied("Autonomous vacancy proceedings require a simulation lease held by the world owner.")
                    self._validate_autonomous_vacancy_in_session(session, world, branch, command, typed_events)
                if waypoint_choice is not None or command.command_type == WAYPOINT_OPPORTUNITY_COMMAND:
                    validate_waypoint_opportunity_batch(
                        typed_events, world_id=world_id, branch_id=branch_id,
                        command_id=command.command_id, actor_id=command.actor_id,
                        ruleset_version=world.ruleset_version, command_type=command.command_type,
                        command_payload=command.payload, state_version=branch.state_version,
                    )
                    if leased_job.payload_json.get("submitted_by") != str(scope.user_id):
                        raise AccessDenied("The itinerary decision must commit for its submitting player.")
                    self._validate_waypoint_opportunity_choice(branch, leased_job, waypoint_choice)
                if any(isinstance(event, BackgroundRoutineAdvancedEvent) or (
                    isinstance(event, (BackgroundNeedsAdvancedEvent, BackgroundDeprivationResolvedEvent))
                    and event.include_actor
                ) for event in typed_events) and (
                    lease is None or leased_job.job_type != "simulation.world_advance"
                ):
                    raise AccessDenied("All-character settlement requires a world advancement job.")
                if any(isinstance(event, NPC_ATTENDANCE_EVENTS) for event in typed_events):
                    if lease is None or leased_job.job_type != "simulation.world_advance" or command.command_type != "simulation.window":
                        raise AccessDenied("NPC attendance recording requires a world-advance worker lease.")
                    validate_npc_attendance_scope(
                        typed_events, world_id=world_id, branch_id=branch_id, command_id=command.command_id,
                        actor_id=command.actor_id, ruleset_version=world.ruleset_version,
                    )
                    for event in typed_events:
                        if isinstance(event, NpcAttendanceStartedEvent):
                            self._require_uncontrolled_npc(session, scope, world, role, event.attendance.actor_id)
                travel_events = [event for event in typed_events if isinstance(event, TravelJourneyChangedEvent)]
                itinerary_cursor_to_write = None
                itinerary_care_projection = None
                single_itinerary_care_delay = (
                    len(travel_events) == 1
                    and sum(event.operation == "delay" for event in travel_events) == 1
                )
                if travel_events:
                    validate_travel_journey_scope(
                        typed_events, world_id=world_id, branch_id=branch_id,
                        command_id=command.command_id, ruleset_version=world.ruleset_version,
                    )
                    job_plan = leased_job.payload_json.get("plan", {}) if lease is not None else {}
                    itinerary_step = None
                    itinerary = None
                    if job_plan.get("travel_itinerary") is not None:
                        itinerary = LongActionPlan.model_validate(job_plan).travel_itinerary
                        itinerary_state = self._checked_branch_state(branch)
                        saved_cursor = leased_job.payload_json.get("itinerary_cursor")
                        cursor = TravelItineraryArrivalProof.model_validate(saved_cursor) if saved_cursor is not None else None
                        itinerary_step = inspect_travel_itinerary_job(itinerary_state, itinerary, cursor=cursor)
                    for travel in travel_events:
                        if travel.operation == "delay":
                            stationary_command = (
                                command.command_type in {"action.rest", "action.consume", "action.wait"} if lease is None else
                                leased_job.job_type == "simulation.long_action"
                                and command.command_type == "simulation.window"
                                and job_plan.get("actor_id") == str(travel.actor_id)
                                and job_plan.get("activity") != "travel"
                            )
                            itinerary_care = (
                                itinerary_step is not None and itinerary_step.outcome == "resume_in_transit"
                                and leased_job.job_type == "simulation.long_action"
                                and job_plan.get("itinerary_auto_care") is True
                                and command.command_type == "simulation.window"
                                and command.payload == {
                                    "job_id": str(leased_job.job_id), "next_tick": str(travel.stationary_until_tick),
                                }
                                and itinerary.actor_id == travel.actor_id
                                and itinerary_step.journey_id == travel.journey.journey_id
                            )
                            if travel.actor_id != command.actor_id or not (stationary_command or itinerary_care):
                                raise AccessDenied("Travel delay requires the traveler's stationary action.")
                            if itinerary_care:
                                from world_engine.rules.itinerary_care_validation import validate_itinerary_care_batch

                                remaining_travel = sum(
                                    leg.travel_ticks for leg in itinerary.legs[itinerary_step.leg_index + 1:]
                                )
                                if travel.journey.arrival_tick + remaining_travel > itinerary.deadline_tick:
                                    raise InvalidEvent("Itinerary care must leave the current journey inside its authorized deadline.")
                                validated_projection = validate_itinerary_care_batch(
                                    itinerary_state, typed_events, world_id=world_id, branch_id=branch_id,
                                    command_id=command.command_id, actor_id=command.actor_id,
                                    ruleset_version=world.ruleset_version,
                                    receipt=itinerary_care_receipt,
                                    authoritative_prestate_hash=branch.state_hash,
                                    return_projection=single_itinerary_care_delay,
                                )
                                if single_itinerary_care_delay:
                                    itinerary_care_projection = validated_projection
                            continue
                        if (lease is None or leased_job.job_type not in {"simulation.long_action", "simulation.world_advance"}
                            or command.command_type != "simulation.window"):
                            raise AccessDenied("Travel journey transitions require a simulation worker lease.")
                        if travel.routine_choice is not None and npc_validation is not None:
                            raise InvalidEvent("An NPC plan departure cannot carry a routine choice.")
                        if travel.operation == "depart" and npc_validation is None:
                            # A hosted routine shares the active job lease, but it
                            # cannot act as the account's current long-action actor.
                            if (leased_job.job_type == "simulation.world_advance"
                                    or job_plan.get("actor_id") != str(travel.actor_id)):
                                from world_engine.rules.contracts import RuleContext
                                from world_engine.rules.engine import RuleEngine
                                from world_engine.simulation.npc_routine_departure import plan_npc_routine_departure

                                self._require_uncontrolled_npc(session, scope, world, role, travel.actor_id)
                                routine_state = self._checked_branch_state(branch)
                                if job_plan.get("consumption") is not None:
                                    raise AccessDenied("An exact consumption job cannot host a routine departure.")
                                if (command.actor_id != scope.user_id or command.payload != {
                                    "job_id": str(leased_job.job_id), "next_tick": str(routine_state.current_tick),
                                } or len(typed_events) != 1):
                                    raise AccessDenied("A routine departure must be the sole event in its leased world window.")
                                controlled = frozenset(session.scalars(select(ActorControlRow.actor_id).where(
                                    ActorControlRow.world_id == world_id,
                                )))
                                expected = plan_npc_routine_departure(
                                    routine_state, RuleContext(
                                        world_id=world_id, branch_id=branch_id, command_id=command.command_id,
                                        actor_id=command.actor_id, ruleset_version=world.ruleset_version,
                                    ), excluded_actor_ids=controlled, rules=RuleEngine(),
                                )
                                if expected is None or list(expected.events) != [travel]:
                                    raise InvalidEvent("The departure must match the currently due NPC routine and its registered route.")
                                continue
                            if travel.routine_choice is not None:
                                raise InvalidEvent("A player departure cannot carry an NPC routine choice.")
                            if (leased_job.job_type != "simulation.long_action"
                                or job_plan.get("activity") != "travel"
                                or job_plan.get("actor_id") != str(travel.actor_id)
                                or command.actor_id != travel.actor_id):
                                raise AccessDenied("A departure must match the travelling character's leased plan.")
                            if itinerary_step is None:
                                if job_plan.get("route_id") != str(travel.journey.route_id):
                                    raise AccessDenied("A departure must match the travelling character's leased route.")
                            elif (itinerary_step.outcome != "ready_to_run"
                                  or itinerary_step.leg.route_id != travel.journey.route_id
                                  or itinerary_step.journey_id != travel.journey.journey_id):
                                raise AccessDenied("A departure must match the next authorized itinerary leg.")
                        if (itinerary_step is not None and travel.operation == "arrive"
                            and travel.journey.journey_id == itinerary_step.journey_id):
                            if itinerary_step.outcome != "resume_in_transit" or itinerary_cursor_to_write is not None:
                                raise InvalidEvent("Only the active itinerary journey can advance its arrival cursor.")
                            itinerary_cursor_to_write = build_travel_itinerary_arrival_proof(
                                itinerary, leg_index=itinerary_step.leg_index, prior_state=itinerary_state,
                                arrival_event=travel, world_id=world_id, branch_id=branch_id, cursor=cursor,
                            )
                self._validate_causation(session, world_id, branch, command.causation_ids)
                self._validate_succession_causes(
                    session, world_id, branch, command.causation_ids, typed_events
                )

                state = npc_before_state if npc_before_state is not None else self._checked_branch_state(branch)
                if npc_reflection_memory_batch:
                    npc_reflection_commit_pending = self._validate_npc_reflection_memory_commit_in_session(
                        session, world, branch, leased_job, role, scope, command, typed_events,
                    )
                if continuation_requested:
                    validate_player_character_continued_batch(
                        state,
                        command.actor_id,
                        typed_events,
                        world_id=world_id,
                        branch_id=branch_id,
                        command_id=command.command_id,
                        ruleset_version=world.ruleset_version,
                        command_type=command.command_type,
                        command_payload=command.payload,
                    )
                    for event in continuation_events:
                        controlled_by_other = session.scalar(
                            select(ActorControlRow.user_id).where(
                                ActorControlRow.world_id == world_id,
                                ActorControlRow.actor_id == event.new_player_id,
                                ActorControlRow.user_id != scope.user_id,
                            ).limit(1)
                        )
                        if controlled_by_other is not None:
                            raise AccessDenied("The continuation target is controlled by another user.")
                    self._validate_player_character_continuation_provenance(
                        session,
                        world_id,
                        branch,
                        typed_events,
                        command.causation_ids,
                        through_seq=branch.head_seq,
                    )
                validate_relationship_disclosure_batch(
                    typed_events, state=state, world_id=world_id, branch_id=branch_id,
                    command_id=command.command_id, actor_id=command.actor_id,
                    ruleset_version=world.ruleset_version, command_type=command.command_type,
                    command_payload=command.payload,
                )
                if command.command_type in {
                    NPC_REPLY_COMMAND, NPC_ATTENDANCE_COMMAND, NPC_REFLECTION_COMMAND, NPC_PARTNERSHIP_RESPONSE_COMMAND,
                    NPC_BALLOT_VOTE_COMMAND, NPC_SAFE_CONDUCT_COMMAND,
                } and typed_events[0].actor_id == state.player_id:
                    raise AccessDenied("The player character must respond directly.")
                for response in typed_events:
                    if isinstance(response, CombatTruceChangedEvent) and response.requested_by is not None:
                        if response.actor_id == state.player_id:
                            raise AccessDenied("The player character must respond to a truce directly.")
                        self._require_uncontrolled_npc(session, scope, world, role, response.actor_id)
                validate_background_clock_batch(state.current_tick, command.actor_id, typed_events)
                if npc_meeting_batch:
                    if (leased_job.job_type not in {"simulation.world_advance", "simulation.long_action"}
                            or role != MembershipRole.OWNER or scope.user_id != world.owner_id
                            or command.actor_id != scope.user_id):
                        raise AccessDenied("Autonomous NPC attendance requires the canonical owner and an active simulation lease.")
                    self._validate_npc_meeting_attendance_in_session(
                        session, world, branch, leased_job, command, typed_events, npc_meeting_decision, state,
                    )
                validate_private_gift_batch(
                    typed_events, world_id=world_id, branch_id=branch_id, command_id=command.command_id,
                    actor_id=command.actor_id, ruleset_version=world.ruleset_version,
                    command_type=command.command_type, command_payload=command.payload,
                    day_seconds=state.calendar.day_seconds,
                )
                if command.command_type == NPC_GIFT_REPLY_COMMAND:
                    if typed_events[0].actor_id == state.player_id:
                        raise AccessDenied("The branch's player character must answer its own private gifts.")
                    self._require_uncontrolled_npc(session, scope, world, role, typed_events[0].actor_id)
                validate_safe_conduct_travel_batch(state, typed_events)
                paid = None
                if lease is not None and command.command_type == "action.work_shift":
                    if leased_job.job_type != "simulation.long_action":
                        raise AccessDenied("A leased wage settlement requires a recurring work plan.")
                    work_plan = LongActionPlan.model_validate(leased_job.payload_json["plan"])
                    schedule = work_plan.work_schedule
                    paid = next((event.plan for event in typed_events if isinstance(event, WorkShiftPaidEvent)), None)
                    if (work_plan.activity != "work" or work_plan.actor_id != command.actor_id
                        or schedule is None or paid is None or paid.assignment_id != schedule.assignment_id):
                        raise AccessDenied("A leased wage settlement must match the assigned job and character.")
                    elapsed = state.current_tick - schedule.first_shift_tick
                    cadence = schedule.repeat_every_days * state.calendar.day_seconds
                    if elapsed < 0 or elapsed % cadence or paid.completed_tick > work_plan.target_tick:
                        raise InvalidEvent("The paid shift falls outside the authorized schedule.")
                    if (schedule.balance_target is not None
                        and paid.ledger_entries[0].balance_before >= schedule.balance_target):
                        raise InvalidEvent("The scheduled work balance target was already reached.")
                validate_completed_work_shift(state, typed_events, world_id=world_id, branch_id=branch_id,
                                          command_id=command.command_id, actor_id=command.actor_id,
                                          ruleset_version=world.ruleset_version,
                                          command_type=command.command_type, command_payload=command.payload)
                validate_population_membership_batch(state, typed_events)
                validate_adjudication_batch(state.current_tick, command.actor_id, typed_events, world_id=world_id,
                                            branch_id=branch_id, command_id=command.command_id,
                                            ruleset_version=world.ruleset_version, state=state)
                validate_investigation_case_batch(
                    state, command.actor_id, typed_events, world_id=world_id, branch_id=branch_id,
                    command_id=command.command_id, ruleset_version=world.ruleset_version,
                    command_type=command.command_type, command_payload=command.payload,
                )
                if requires_public_accusation_correction_validation(command.command_type, typed_events):
                    validate_public_accusation_correction_batch(
                        state, command.actor_id, typed_events, world_id=world_id, branch_id=branch_id,
                        command_id=command.command_id, ruleset_version=world.ruleset_version,
                        command_type=command.command_type, command_payload=command.payload,
                    )
                    self._validate_public_accusation_correction_provenance(
                        session, world_id, branch, typed_events, command.causation_ids,
                        through_seq=branch.head_seq,
                    )
                if requires_accusation_repair_validation(command.command_type, typed_events):
                    validate_accusation_repair_batch(
                        state, command.actor_id, typed_events, world_id=world_id, branch_id=branch_id,
                        command_id=command.command_id, ruleset_version=world.ruleset_version,
                        command_type=command.command_type, command_payload=command.payload,
                        causation_ids=command.causation_ids,
                    )
                    self._validate_accusation_repair_provenance(
                        session, world_id, branch, state, typed_events, command.causation_ids,
                        through_seq=branch.head_seq,
                    )
                validate_historical_evidence_destruction_batch(
                    state, command.actor_id, typed_events, world_id=world_id, branch_id=branch_id,
                    command_id=command.command_id, ruleset_version=world.ruleset_version,
                    command_type=command.command_type, command_payload=command.payload,
                )
                validate_promise_batch(
                    typed_events, world_id=world_id, branch_id=branch_id, command_id=command.command_id,
                    actor_id=command.actor_id, ruleset_version=world.ruleset_version,
                    command_type=command.command_type, command_payload=command.payload, causation_ids=command.causation_ids,
                )
                validate_shipment_spoilage_batch(
                    state, command.actor_id, typed_events, world_id=world_id, branch_id=branch_id,
                    command_id=command.command_id, ruleset_version=world.ruleset_version,
                    command_type=command.command_type, command_payload=command.payload,
                )
                validate_market_price_review_batch(
                    typed_events, world_id=world_id, branch_id=branch_id, command_id=command.command_id,
                    ruleset_version=world.ruleset_version, command_type=command.command_type,
                    command_payload=command.payload,
                )
                if any(isinstance(event, (PromiseDeadlineBreachedEvent, PromiseDeadlineBreachedWithTrustEvent,
                                          MarketPriceReviewResolvedEvent))
                       or event.type == "core.shipment_spoilage_resolved.v1" for event in typed_events):
                    if lease is None or leased_job.job_type not in {"simulation.long_action", "simulation.world_advance"}:
                        raise InvalidLease("These scheduled events require a current simulation job lease.")
                    if command.payload != {"job_id": str(leased_job.job_id), "next_tick": str(state.current_tick)}:
                        raise InvalidEvent("Scheduled event must match its leased job and exact committed tick.")
                validate_combat_batch(
                    typed_events, world_id=world_id, branch_id=branch_id,
                    command_id=command.command_id, ruleset_version=world.ruleset_version,
                    actor_id=command.actor_id, command_type=command.command_type,
                    command_payload=command.payload, state=state, causation_ids=command.causation_ids,
                )
                validate_treatment_batch(
                    typed_events, world_id=world_id, branch_id=branch_id,
                    command_id=command.command_id, ruleset_version=world.ruleset_version,
                    actor_id=command.actor_id, command_type=command.command_type,
                    command_payload=command.payload, state=state,
                )
                for audit in typed_events:
                    if isinstance(audit, ImprovisedCheckResolvedEvent) and (
                        command.command_type != "action.improvised_check"
                        or command.payload != {"kind": "improvised_check", "target_id": str(audit.target_id), "skill_id": audit.skill_id}
                    ):
                        raise InvalidEvent("An improvised check must match its command proposal.")
                vacancies = derive_death_vacancies(
                    state, typed_events, world_id=world_id, branch_id=branch_id, command_id=command.command_id,
                    actor_id=command.actor_id, ruleset_version=world.ruleset_version,
                )
                if vacancies is not None:
                    if len(typed_events) == MAX_EVENTS_PER_COMMAND or vacancies.event_id in event_ids:
                        raise InvalidEvent("Derived employment closure exceeds the batch limit or repeats an event ID.")
                    typed_events = (*typed_events, vacancies)
                version_after = branch.state_version + 1
                first_seq = branch.head_seq + 1
                event_records: list[EventRecord] = []
                postings: list[LedgerPosting] = []
                event_postings_by_event: list[tuple[LedgerPosting, ...]] = []
                next_state = state
                next_state_tick = state.current_tick
                projection_reused = False
                itinerary_transit_projection_reused = False
                projection_payload = None
                projection_hash = None
                if (
                    itinerary_care_projection is not None
                    and vacancies is None
                    and lease is not None
                    and leased_job.job_type == "simulation.long_action"
                    and command.command_type == "simulation.window"
                    and single_itinerary_care_delay
                    and len(typed_events) == len(itinerary_care_projection.event_ticks)
                    and len(typed_events) == len(itinerary_care_projection.event_serializations)
                ):
                    projected_state = itinerary_care_projection.projected_state
                    try:
                        candidate_payload = state_json(projected_state)
                        candidate_hash = state_payload_hash(candidate_payload)
                    except Exception:
                        candidate_payload = None
                        candidate_hash = None
                    if (
                        candidate_payload is not None
                        and candidate_hash == itinerary_care_projection.projected_state_hash
                        and itinerary_care_projection.matches_projected_payload(candidate_payload)
                        and projected_state.current_tick == itinerary_care_projection.event_ticks[-1]
                        and itinerary_care_projection.event_ticks[0] == state.current_tick
                    ):
                        next_state = projected_state
                        next_state_tick = projected_state.current_tick
                        projection_payload = candidate_payload
                        projection_hash = candidate_hash
                        projection_reused = True
                if not projection_reused and itinerary_transit_projection_receipt is not None:
                    from world_engine.simulation.engine import (
                        _ItineraryTransitProjectionReceipt,
                        _is_ordinary_itinerary_transit_batch,
                    )

                    receipt = itinerary_transit_projection_receipt
                    receipt_candidate = type(receipt) is _ItineraryTransitProjectionReceipt
                    job_plan_payload = leased_job.payload_json.get("plan", {}) if lease is not None else {}
                    persisted_plan = None
                    itinerary = None
                    itinerary_step = None
                    if receipt_candidate and isinstance(job_plan_payload, dict):
                        try:
                            persisted_plan = LongActionPlan.model_validate(job_plan_payload)
                            itinerary = persisted_plan.travel_itinerary
                            if itinerary is not None:
                                saved_cursor = leased_job.payload_json.get("itinerary_cursor")
                                cursor = (
                                    TravelItineraryArrivalProof.model_validate(saved_cursor)
                                    if saved_cursor is not None else None
                                )
                                itinerary_step = inspect_travel_itinerary_job(state, itinerary, cursor=cursor)
                        except (TypeError, ValueError, ValidationError):
                            itinerary = None
                            itinerary_step = None
                    identity_matches = bool(
                        receipt_candidate
                        and lease is not None
                        and leased_job.job_type == "simulation.long_action"
                        and command.command_type == "simulation.window"
                        and itinerary is not None
                        and itinerary_step is not None
                        and itinerary_step.outcome == "resume_in_transit"
                        and persisted_plan is not None
                        and persisted_plan.activity == "travel"
                        and persisted_plan.actor_id == receipt.actor_id
                        and not leased_job.payload_json.get("pending_decision")
                        and not travel_events
                        and vacancies is None
                        and _is_ordinary_itinerary_transit_batch(
                            tuple(typed_events), actor_id=receipt.actor_id,
                            to_tick=receipt.projected_current_tick,
                        )
                        and receipt.matches_execution(
                            world_id=world_id,
                            branch_id=branch_id,
                            job_id=lease.job_id,
                            worker_id=lease.worker_id,
                            fencing_token=lease.fencing_token,
                            state_version=branch.state_version,
                            command_id=command.command_id,
                            actor_id=command.actor_id,
                            itinerary_journey_id=itinerary_step.journey_id,
                            route_id=itinerary_step.leg.route_id,
                            ruleset_version=world.ruleset_version,
                            prestate_hash=branch.state_hash,
                        )
                        and command.payload == {
                            "job_id": str(leased_job.job_id),
                            "next_tick": str(receipt.projected_current_tick),
                        }
                        and receipt.event_ticks
                        and len(receipt.event_ticks) == len(typed_events)
                        and all(event_tick == receipt.projected_current_tick for event_tick in receipt.event_ticks)
                        and state.current_tick < receipt.projected_current_tick <= itinerary.deadline_tick
                    )
                    if identity_matches:
                        try:
                            candidate_payload = receipt.projected_payload
                            candidate_hash = state_payload_hash(candidate_payload)
                            candidate_tick = candidate_payload.get("current_tick")
                        except Exception:
                            candidate_payload = None
                            candidate_hash = None
                            candidate_tick = None
                        if (
                            candidate_payload is not None
                            and type(candidate_tick) is int
                            and candidate_tick == receipt.projected_current_tick
                            and candidate_hash == receipt.projected_state_hash
                            and receipt.matches_projected_payload(candidate_payload)
                            and receipt.matches_prestate(state)
                            and receipt.matches_events(tuple(typed_events))
                        ):
                            next_state_tick = receipt.projected_current_tick
                            projection_payload = candidate_payload
                            projection_hash = candidate_hash
                            projection_reused = True
                            itinerary_transit_projection_reused = True
                for index, event in enumerate(typed_events):
                    if not projection_reused:
                        next_state = apply_event(next_state, event)
                    event_tick = (
                        itinerary_transit_projection_receipt.event_ticks[index]
                        if itinerary_transit_projection_reused
                        else itinerary_care_projection.event_ticks[index]
                        if projection_reused
                        else next_state.current_tick
                    )
                    event_postings = ledger_postings(event)
                    validate_ledger_postings(event_postings)
                    postings.extend(event_postings)
                    event_postings_by_event.append(event_postings)
                    event_records.append(
                        EventRecord(
                            event_id=event.event_id,
                            world_id=world_id,
                            branch_id=branch_id,
                            sequence=first_seq + index,
                            tick=event_tick,
                            type=event.type,
                            command_id=command.command_id,
                            actor_id=command.actor_id,
                            causation_ids=command.causation_ids,
                            correlation_id=command.correlation_id or scope.correlation_id,
                            ruleset_version=world.ruleset_version,
                            payload=event_payload(event),
                            visibility=event.visibility,
                            state_version_after=version_after,
                            commit_boundary=(index == len(typed_events) - 1),
                            committed_at=now,
                        )
                    )
                validate_ledger_postings(tuple(postings))
                next_payload = projection_payload if projection_reused else state_json(next_state)
                next_hash = projection_hash if projection_reused else state_payload_hash(next_payload)
                if not projection_reused:
                    next_state_tick = next_state.current_tick
                last_seq = first_seq + len(typed_events) - 1
                queued_npc_reflection: dict[str, object] | None = None
                if (
                    capture_npc_reflection_allowed
                    and leased_job.payload_json.get("pending_npc_reflection") is None
                    and any(event.type in {
                        "core.npc_plan_revised.v1",
                        "core.npc_plan_review_resolved.v1",
                        "core.knowledge_acquired.v1",
                        "core.investigation_case_closed.v1",
                    } for event in typed_events)
                ):
                    try:
                        from world_engine.domain.npc_reflection import (
                            NpcReflectionSourceEvidence,
                            project_npc_reflection_source,
                            select_npc_reflection_trigger,
                        )

                        reflection_state = next_state
                        if projection_reused and projection_payload is not None:
                            reflection_state = WorldState.model_validate(projection_payload)
                        controlled_ids = frozenset(session.scalars(select(ActorControlRow.actor_id).where(
                            ActorControlRow.world_id == world_id,
                        )))
                        reflection_mode = leased_job.payload_json.get("npc_reflection_mode")
                        trigger = select_npc_reflection_trigger(
                            typed_events,
                            reflection_state,
                            mode=reflection_mode,
                            externally_controlled_actor_ids=controlled_ids,
                        )
                        if trigger is not None:
                            source_event = next((
                                event for event in typed_events if event.event_id == trigger.event_id
                            ), None)
                            source_record = next((
                                record for record in event_records if record.event_id == trigger.event_id
                            ), None)
                            evidence = (
                                project_npc_reflection_source(
                                    source_event, reflection_state,
                                    externally_controlled_actor_ids=controlled_ids,
                                )
                                if source_event is not None else None
                            )
                            if (
                                source_event is not None
                                and source_record is not None
                                and source_record.tick == trigger.tick
                                and evidence is not None
                            ):
                                checked_evidence = NpcReflectionSourceEvidence.model_validate(
                                    evidence.model_dump(mode="python"),
                                )
                                request_id = uuid5(leased_job.job_id, str(trigger.event_id))
                                queued_npc_reflection = _npc_reflection_pending_document(
                                    leased_job.job_id,
                                    {
                                        "version": 1,
                                        "request_id": str(request_id),
                                        "source_event_id": str(trigger.event_id),
                                        "source_kind": trigger.kind,
                                        "actor_id": str(trigger.actor_id),
                                        "tick": trigger.tick,
                                        "source_state_version": version_after,
                                        "source_head_seq": last_seq,
                                        "source_sequence": source_record.sequence,
                                        "source_evidence": checked_evidence.model_dump(mode="json"),
                                        "result": None,
                                    },
                                )
                    except Exception:
                        # Reflection is optional work. If projection cannot
                        # reproduce a typed trigger, the committed world event
                        # remains authoritative and no model request is queued.
                        queued_npc_reflection = None
                if lease is not None:
                    leased_job.committed_through_tick = next_state_tick
                    if leased_job.job_type in {"simulation.long_action", "simulation.world_advance"}:
                        decision = leased_job.payload_json.get("pending_decision")
                        leased_job.payload_json = {
                            key: value for key, value in leased_job.payload_json.items()
                            if key != "pending_decision"
                        } | {"committed_state_version": version_after}
                        if decision is not None:
                            leased_job.payload_json = {**leased_job.payload_json, "last_decision": decision}
                        if waypoint_choice is not None and waypoint_choice.record.choice == "stop":
                            leased_job.status = JobStatus.PAUSED.value
                            leased_job.result_json = {"reason_code": "waypoint_opportunity_stop"}
                            leased_job.updated_at = leased_job.lease_expires_at = now
                            current_lease = session.get(BranchLeaseRow, branch_id)
                            current_lease.lease_expires_at = leased_job.lease_expires_at
                        if itinerary_cursor_to_write is not None:
                            leased_job.payload_json = {
                                **leased_job.payload_json,
                                "itinerary_cursor": itinerary_cursor_to_write.model_dump(mode="json"),
                            }
                        if paid is not None:
                            verified = leased_job.payload_json.get("work_progress", {})
                            worker_credit = paid.ledger_entries[0]
                            wages = verified.get("wages_by_resource", {})
                            with localcontext() as decimal_context:
                                decimal_context.prec = 60
                                earned = Decimal(wages.get(worker_credit.resource_id, "0")) + worker_credit.amount
                            leased_job.payload_json = {
                                **leased_job.payload_json,
                                "work_progress": {
                                    "shifts_paid": verified.get("shifts_paid", 0) + 1,
                                    "wages_by_resource": {**wages, worker_credit.resource_id: format(earned, "f")},
                                    "last_paid_tick": str(paid.completed_tick),
                                    "balance_resource_id": worker_credit.resource_id,
                                    "balance_after_last_pay": format(worker_credit.balance_after, "f"),
                                },
                            }
                        if queued_npc_reflection is not None:
                            leased_job.payload_json = {
                                **leased_job.payload_json,
                                "pending_npc_reflection": queued_npc_reflection,
                            }
                        if npc_reflection_commit_pending is not None:
                            committed_memory = next(
                                item for item in typed_events if isinstance(item, KnowledgeAcquiredEvent)
                            )
                            receipt = self._npc_reflection_receipt(
                                npc_reflection_commit_pending,
                                npc_reflection_commit_pending["result"],
                                memory_event_id=committed_memory.event_id,
                            )
                            leased_job.payload_json = {
                                key: value for key, value in leased_job.payload_json.items()
                                if key != "pending_npc_reflection"
                            } | {"last_npc_reflection": receipt}
                    leased_job.updated_at = now

                branch_update = (
                    update(BranchRow)
                    .where(
                        BranchRow.branch_id == branch_id,
                        BranchRow.world_id == world_id,
                        BranchRow.state_version == command.expected_state_version,
                    )
                    .values(
                        head_seq=last_seq,
                        state_version=version_after,
                        current_tick=next_state_tick,
                        state_hash=next_hash,
                        state_json=next_payload,
                    )
                )
                capture_bound_json = (
                    capture_history_candidate_allowed
                    and not projection_reused
                    and self._player_history_context_cache.has_recent_authorized_read(world_id, branch_id)
                )
                if capture_bound_json:
                    captured_history_state = next_state
                    raw_fingerprint_holder: dict[str, str | None] = {"value": None}

                    def capture_stored_json_parameter(
                        _connection, _cursor, statement, parameters, context, executemany,
                    ) -> None:
                        if executemany or not isinstance(statement, str):
                            return
                        normalized = statement.lstrip().lower()
                        if not normalized.startswith("update") or "world_branches" not in normalized:
                            return
                        try:
                            if not isinstance(parameters, (tuple, list)):
                                return
                            index = context.compiled.positiontup.index("state_json")
                            raw_text = parameters[index]
                            if isinstance(raw_text, str):
                                raw_fingerprint_holder["value"] = hashlib.sha256(
                                    raw_text.encode("utf-8"),
                                ).hexdigest()
                        except Exception:
                            return

                    connection = session.connection()
                    sqlalchemy_event.listen(connection, "before_cursor_execute", capture_stored_json_parameter)
                    try:
                        result = session.execute(branch_update)
                    finally:
                        sqlalchemy_event.remove(connection, "before_cursor_execute", capture_stored_json_parameter)
                    captured_history_raw_fingerprint = raw_fingerprint_holder["value"]
                else:
                    result = session.execute(branch_update)
                if result.rowcount != 1:
                    concurrent = session.scalar(
                        select(CommandRow).where(
                            CommandRow.branch_id == branch_id,
                            CommandRow.idempotency_key == command.idempotency_key,
                        )
                    )
                    if concurrent is not None:
                        return self._idempotent_result(session, concurrent, request_hash)
                    current = session.get(BranchRow, branch_id)
                    raise StaleVersion(command.expected_state_version, current.state_version if current else -1)

                for event, record, event_postings in zip(
                    typed_events, event_records, event_postings_by_event, strict=True,
                ):
                    append_event_row(session,
                        EventRow(
                            branch_id=branch_id,
                            sequence=record.sequence,
                            event_id=record.event_id,
                            world_id=world_id,
                            tick=record.tick,
                            event_type=record.type,
                            command_id=record.command_id,
                            actor_id=record.actor_id,
                            causation_json=[str(item) for item in record.causation_ids],
                            correlation_id=record.correlation_id,
                            ruleset_version=record.ruleset_version,
                            payload_json=record.payload,
                            visibility_json=record.visibility,
                            state_version_after=record.state_version_after,
                            commit_boundary=record.commit_boundary,
                            committed_at=now,
                        )
                    )
                    for posting_index, posting in enumerate(event_postings):
                        session.add(
                            LedgerPostingRow(
                                posting_id=uuid4(),
                                event_id=event.event_id,
                                posting_index=posting_index,
                                account_id=posting.account_id,
                                resource_id=posting.resource_id,
                                delta=format(posting.delta, "f"),
                                created_at=now,
                            )
                        )
                    session.add(
                        OutboxRow(
                            outbox_id=uuid4(),
                            event_id=record.event_id,
                            world_id=world_id,
                            branch_id=branch_id,
                            topic=record.type,
                            payload_json={OUTBOX_STORAGE_KEY: {
                                "kind": OUTBOX_STORAGE_KIND,
                                "version": 1,
                                "event_id": str(record.event_id),
                            }},
                            attempts=0,
                            created_at=now,
                            published_at=None,
                        )
                    )

                commit_result = CommitResult(
                    command_id=command.command_id,
                    branch_id=branch_id,
                    duplicate=False,
                    state_version=version_after,
                    first_seq=first_seq,
                    last_seq=last_seq,
                    events=event_records,
                    state_hash=next_hash,
                )
                session.add(
                    CommandRow(
                        command_id=command.command_id,
                        tenant_id=scope.tenant_id,
                        world_id=world_id,
                        branch_id=branch_id,
                        actor_id=command.actor_id,
                        idempotency_key=command.idempotency_key,
                        payload_hash=request_hash,
                        expected_state_version=command.expected_state_version,
                        command_type=command.command_type,
                        payload_json=command.payload,
                        result_json=self._compact_commit_result(commit_result),
                        created_at=now,
                    )
                )
                session.flush()
                if capture_bound_json and captured_history_raw_fingerprint is not None:
                    committed_history_result = commit_result
                return commit_result
        except IntegrityError as exc:
            # Unique event/command/outbox constraints indicate a replay collision;
            # the whole transaction is rolled back so no partial state survives.
            raise Conflict("The command conflicts with an existing event or idempotency record.") from exc

    def authorize_role(
        self, scope: AccessScope, world_id: UUID, roles: set[MembershipRole]
    ) -> MembershipRole:
        with self._transaction() as session:
            _, role = self._require_membership(session, scope, world_id)
            if role not in roles:
                raise AccessDenied("The membership role does not allow this operation.")
            return role

    def authorize_actor(
        self,
        scope: AccessScope,
        world_id: UUID,
        actor_id: UUID,
        *,
        require_control: bool = True,
        branch_id: UUID | None = None,
    ) -> UUID:
        """Authorize a POV actor before the API builds an audience projection."""
        with self._transaction() as session:
            world, role = self._require_membership(session, scope, world_id)
            branch = self._get_branch(session, world_id, branch_id or world.active_branch_id)
            state = self._checked_branch_state(branch)
            actor = state.entities.get(actor_id)
            if actor is None or actor.kind not in {"character", "person"}:
                raise NotFound("Actor does not exist in this branch.")
            if require_control:
                if role == MembershipRole.OBSERVER:
                    raise AccessDenied("Observers cannot select a controlled actor POV.")
                if role == MembershipRole.PLAYER:
                    control = session.get(ActorControlRow, (world_id, scope.user_id, actor_id))
                    if control is None:
                        raise AccessDenied("This player does not control the selected actor.")
            return actor_id

    def authorize_actor_control(self, scope: AccessScope, world_id: UUID, actor_id: UUID) -> None:
        """Check a current control grant for an actor from a verified branch view."""
        with self._transaction() as session:
            _, role = self._require_membership(session, scope, world_id)
            self._require_mutation_role(role)
            self._require_actor_control(session, scope, world_id, role, actor_id)

    def controlled_actors(self, scope: AccessScope, world_id: UUID) -> list[UUID]:
        with self._transaction() as session:
            world, role = self._require_membership(session, scope, world_id)
            if role in AUTHOR_ROLES:
                branch = self._get_branch(session, world_id, world.active_branch_id)
                state = self._checked_branch_state(branch)
                return sorted(
                    (entity_id for entity_id, entity in state.entities.items() if entity.kind in {"character", "person"}),
                    key=str,
                )
            rows = session.scalars(
                select(ActorControlRow.actor_id)
                .where(ActorControlRow.world_id == world_id, ActorControlRow.user_id == scope.user_id)
                .order_by(ActorControlRow.actor_id)
            ).all()
            return list(rows)

    def grant_membership(
        self,
        scope: AccessScope,
        world_id: UUID,
        user_id: UUID,
        role: MembershipRole,
        *,
        controlled_actor_ids: Sequence[UUID] = (),
    ) -> None:
        """Create/update a member and replace that member's controlled actor set."""
        now = utc_now()
        with self._transaction(write=True) as session:
            world, caller_role = self._require_membership(session, scope, world_id)
            if caller_role != MembershipRole.OWNER:
                raise AccessDenied("Only a world owner may grant memberships.")
            active_branch = self._get_branch(session, world_id, world.active_branch_id)
            state = self._checked_branch_state(active_branch)
            for actor_id in controlled_actor_ids:
                actor = state.entities.get(actor_id)
                if actor is None or actor.kind not in {"character", "person"}:
                    raise NotFound(f"Controlled actor {actor_id} does not exist in the active branch.")
            membership = session.get(MembershipRow, (world_id, user_id))
            if membership is None:
                membership = MembershipRow(
                    world_id=world_id,
                    user_id=user_id,
                    tenant_id=scope.tenant_id,
                    role=role.value,
                    created_at=now,
                )
                session.add(membership)
            else:
                membership.role = role.value
                membership.tenant_id = scope.tenant_id
            session.execute(
                delete(ActorControlRow).where(
                    ActorControlRow.world_id == world_id,
                    ActorControlRow.user_id == user_id,
                )
            )
            if role == MembershipRole.PLAYER:
                for actor_id in set(controlled_actor_ids):
                    session.add(
                        ActorControlRow(
                            world_id=world_id,
                            user_id=user_id,
                            actor_id=actor_id,
                            granted_by=scope.user_id,
                            created_at=now,
                        )
                    )

    def grant_actor_control(
        self, scope: AccessScope, world_id: UUID, user_id: UUID, actor_id: UUID
    ) -> None:
        with self._transaction(write=True) as session:
            world, role = self._require_membership(session, scope, world_id)
            if role != MembershipRole.OWNER:
                raise AccessDenied("Only a world owner may grant actor control.")
            member = session.get(MembershipRow, (world_id, user_id))
            if member is None or member.role != MembershipRole.PLAYER.value:
                raise AccessDenied("Actor control can only be granted to a player membership.")
            branch = self._get_branch(session, world_id, world.active_branch_id)
            state = self._checked_branch_state(branch)
            actor = state.entities.get(actor_id)
            if actor is None or actor.kind not in {"character", "person"}:
                raise NotFound("Actor does not exist in the active branch.")
            if session.get(ActorControlRow, (world_id, user_id, actor_id)) is None:
                session.add(
                    ActorControlRow(
                        world_id=world_id,
                        user_id=user_id,
                        actor_id=actor_id,
                        granted_by=scope.user_id,
                        created_at=utc_now(),
                    )
                )

    def snapshot(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, *, at_tick: int | None = None,
    ) -> SnapshotRecord:
        with self._transaction(write=True) as session:
            self._require_membership(session, scope, world_id)
            branch = self._get_branch(session, world_id, branch_id)
            sequence = branch.head_seq
            if at_tick is not None and at_tick != branch.current_tick:
                sequence = session.scalar(
                    select(EventRow.sequence).where(
                        EventRow.world_id == world_id,
                        EventRow.branch_id == branch_id,
                        EventRow.tick == at_tick,
                        EventRow.commit_boundary.is_(True),
                    ).order_by(EventRow.sequence.desc()).limit(1)
                )
                if sequence is None:
                    raise Conflict("No branch-local commit boundary exists at the requested snapshot tick.")
            existing = session.scalar(
                select(SnapshotRow).where(
                    SnapshotRow.branch_id == branch_id,
                    SnapshotRow.last_seq == sequence,
                )
            )
            if sequence == branch.head_seq:
                state = self._checked_branch_state(branch)
                state_version = branch.state_version
                digest = branch.state_hash
            elif existing is None:
                state, state_version = self._state_at_seq(session, world_id, branch, sequence)
                digest = state_hash(state)
            if existing is not None:
                return self._snapshot_record(existing)
            row = SnapshotRow(
                snapshot_id=uuid4(),
                world_id=world_id,
                branch_id=branch_id,
                last_seq=sequence,
                state_version=state_version,
                current_tick=state.current_tick,
                state_hash=digest,
                state_json=state_json(state),
                created_at=utc_now(),
            )
            session.add(row)
            session.flush()
            return self._snapshot_record(row)

    @staticmethod
    def _validate_fork_input(fork_seq: int, label: str) -> str:
        if fork_seq < 0:
            raise ValueError("fork_seq must be nonnegative")
        normalized_label = label.strip()
        if not normalized_label or len(normalized_label) > 160:
            raise ValueError("label must be non-empty text of at most 160 characters")
        return normalized_label

    @staticmethod
    def _check_fork_identity(
        branch: BranchRow, world_id: UUID, source_branch_id: UUID, fork_seq: int, label: str,
    ) -> None:
        if (branch.world_id, branch.parent_branch_id, branch.fork_seq, branch.name) != (
            world_id, source_branch_id, fork_seq, label,
        ):
            raise Conflict("Idempotency key already identifies a different branch.")

    def _insert_fork(
        self, session: Session, source: BranchRow, fork_seq: int, label: str, branch_id: UUID,
    ) -> BranchRow:
        if fork_seq > source.head_seq:
            raise Conflict("Cannot fork after the source branch head.")
        world_id = source.world_id
        state, state_version = self._state_at_seq(session, world_id, source, fork_seq)
        payload = state_json(state)
        digest = state_payload_hash(payload)
        now = utc_now()
        row = BranchRow(
            branch_id=branch_id,
            world_id=world_id,
            name=label,
            parent_branch_id=source.branch_id,
            fork_seq=fork_seq,
            head_seq=fork_seq,
            state_version=state_version,
            initial_state_version=state_version,
            current_tick=state.current_tick,
            initial_state_hash=digest,
            state_hash=digest,
            initial_state_json=payload,
            state_json=payload,
            created_at=now,
        )
        session.add(row)
        session.flush()
        # The fork checkpoint makes the exact replay boundary explicit.
        session.add(
            SnapshotRow(
                snapshot_id=uuid4(),
                world_id=world_id,
                branch_id=branch_id,
                last_seq=fork_seq,
                state_version=state_version,
                current_tick=state.current_tick,
                state_hash=digest,
                state_json=payload,
                created_at=now,
            )
        )
        session.flush()
        return row

    def _prepare_template_fork(
        self,
        session: Session,
        world_id: UUID,
        branch_id: UUID,
        fork: _TemplateFork,
        command: CommandEnvelope,
        request_hash: str,
    ) -> BranchRow | CommitResult:
        source = self._get_branch(session, world_id, fork.source_branch_id, for_update=True, defer_state_json=True)
        existing = session.get(BranchRow, branch_id)
        if existing is not None:
            self._check_fork_identity(existing, world_id, fork.source_branch_id, fork.source_seq, fork.label)
            if (existing.initial_state_version, existing.initial_state_hash) != (
                command.expected_state_version, fork.source_hash,
            ):
                raise Conflict("The existing branch starts from a different source checkpoint.")
            previous = session.scalar(select(CommandRow).where(
                CommandRow.branch_id == branch_id,
                CommandRow.idempotency_key == command.idempotency_key,
            ))
            if previous is None:
                raise Conflict("The branch already exists without this template migration receipt.")
            return self._idempotent_result(session, previous, request_hash)

        if command.expected_state_version != source.state_version:
            raise StaleVersion(command.expected_state_version, source.state_version)
        if (source.head_seq, source.state_hash) != (fork.source_seq, fork.source_hash):
            raise Conflict("The source branch no longer matches the reviewed template checkpoint.")
        branch = self._insert_fork(session, source, fork.source_seq, fork.label, branch_id)
        if (branch.state_version, branch.state_hash, branch.current_tick) != (
            source.state_version, source.state_hash, source.current_tick,
        ):
            raise Conflict("The source branch head differs from its event replay.")
        return branch

    def fork(
        self,
        scope: AccessScope,
        world_id: UUID,
        source_branch_id: UUID,
        fork_seq: int,
        label: str,
        *,
        branch_id: UUID | None = None,
    ) -> BranchRecord:
        normalized_label = self._validate_fork_input(fork_seq, label)
        branch_id = branch_id or uuid4()
        try:
            with self._transaction(write=True) as session:
                _, role = self._require_membership(session, scope, world_id)
                self._require_mutation_role(role)
                source = self._get_branch(session, world_id, source_branch_id)
                existing = session.get(BranchRow, branch_id)
                if existing is not None:
                    self._check_fork_identity(existing, world_id, source_branch_id, fork_seq, normalized_label)
                    return self._branch_record(existing)
                return self._branch_record(self._insert_fork(session, source, fork_seq, normalized_label, branch_id))
        except IntegrityError as exc:
            raise Conflict("Branch label already exists in this world.") from exc

    def replay(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID,
        through_seq: int | None = None,
        *,
        from_genesis: bool = False,
        visit_commit: Callable[[int, WorldState], None] | None = None,
    ) -> WorldView:
        """Replay a commit boundary; a genesis audit may observe each committed state."""
        if visit_commit is not None and not from_genesis:
            raise ValueError("A commit observer requires replay from genesis.")
        with self._transaction() as session:
            world, _ = self._require_membership(session, scope, world_id)
            branch = self._get_branch(session, world_id, branch_id)
            target_seq = branch.head_seq if through_seq is None else through_seq
            if target_seq < 0 or target_seq > branch.head_seq:
                raise Conflict("Replay sequence must be between zero and the branch head.")
            state, version = self._state_at_seq(
                session, world_id, branch, target_seq, from_genesis=from_genesis, visit_commit=visit_commit,
            )
            digest = state_hash(state)
            if target_seq == branch.head_seq and (
                digest != branch.state_hash or version != branch.state_version or state.current_tick != branch.current_tick
            ):
                raise Conflict("Branch head differs from its event replay.")
            return WorldView(
                world=self._world_record(world),
                branch=self._branch_record(branch),
                state=state,
                last_seq=target_seq,
                state_hash=digest,
            )

    def replay_from_checkpoint(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID,
        *,
        checkpoint_seq: int,
        through_seq: int,
        checkpoint_hash: str,
        visit_commit: Callable[[int, WorldState], None] | None = None,
    ) -> WorldView:
        """Replay a bounded range from one exact, persisted snapshot.

        The caller must pin the snapshot by both sequence and hash. Snapshots on
        an ancestor branch are usable only when that sequence is visible through
        the requested branch's fork lineage.
        """
        if (not isinstance(checkpoint_seq, int) or isinstance(checkpoint_seq, bool)
            or not isinstance(through_seq, int) or isinstance(through_seq, bool)):
            raise ValueError("Replay sequences must be integers.")
        if not isinstance(checkpoint_hash, str) or not checkpoint_hash:
            raise ValueError("checkpoint_hash must be non-empty text.")

        with self._transaction() as session:
            world, _ = self._require_membership(session, scope, world_id)
            branch = self._get_branch(session, world_id, branch_id)
            if through_seq < 0 or through_seq > branch.head_seq:
                raise Conflict("Replay sequence must be between zero and the branch head.")
            if checkpoint_seq < 0 or checkpoint_seq > through_seq:
                raise Conflict("Checkpoint sequence must be between zero and the replay boundary.")

            visible_chain = self._branch_chain(session, branch, through_seq, include_ancestors=True)
            checkpoint_snapshot = None
            checkpoint_branch = None
            for source_branch_id, visible_through in visible_chain:
                if checkpoint_seq > visible_through:
                    continue
                candidate = session.scalar(
                    select(SnapshotRow).where(
                        SnapshotRow.world_id == world_id,
                        SnapshotRow.branch_id == source_branch_id,
                        SnapshotRow.last_seq == checkpoint_seq,
                    )
                )
                if candidate is not None:
                    checkpoint_snapshot = candidate
                    checkpoint_branch = session.get(BranchRow, source_branch_id)
                    break
            if checkpoint_snapshot is None or checkpoint_branch is None:
                raise Conflict("No persisted checkpoint exists at the requested sequence in this branch history.")
            if checkpoint_snapshot.state_hash != checkpoint_hash:
                raise Conflict("Checkpoint hash does not match the persisted snapshot.")
            self._validate_replay_snapshot(
                session, world_id, checkpoint_branch, checkpoint_snapshot, checkpoint_seq,
            )

            state, version = self._state_at_seq(
                session,
                world_id,
                branch,
                through_seq,
                checkpoint_snapshot=checkpoint_snapshot,
                visit_commit=visit_commit,
            )
            digest = state_hash(state)

            # A head replay is checked against the authoritative branch
            # projection. If the requested boundary has its own stored snapshot,
            # check that hash and metadata as an independent end pin as well.
            if through_seq == branch.head_seq and (
                digest != branch.state_hash or version != branch.state_version or state.current_tick != branch.current_tick
            ):
                raise Conflict("Branch head differs from its event replay.")
            for source_branch_id, visible_through in visible_chain:
                if through_seq > visible_through:
                    continue
                end_snapshot = session.scalar(
                    select(SnapshotRow).where(
                        SnapshotRow.world_id == world_id,
                        SnapshotRow.branch_id == source_branch_id,
                        SnapshotRow.last_seq == through_seq,
                    )
                )
                if end_snapshot is None:
                    continue
                end_branch = session.get(BranchRow, source_branch_id)
                if end_branch is None:
                    raise Conflict("Checkpoint branch lineage is incomplete.")
                end_state = self._validate_replay_snapshot(
                    session, world_id, end_branch, end_snapshot, through_seq,
                )
                if (
                    state_hash(end_state) != digest
                    or end_snapshot.state_version != version
                    or end_snapshot.current_tick != state.current_tick
                ):
                    raise Conflict("Replay result differs from the persisted end checkpoint.")
                break

            return WorldView(
                world=self._world_record(world),
                branch=self._branch_record(branch),
                state=state,
                last_seq=through_seq,
                state_hash=digest,
            )

    def _validate_replay_snapshot(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        snapshot: SnapshotRow,
        sequence: int,
    ) -> WorldState:
        """Check a pinned snapshot's payload, sequence, tick, and version metadata."""
        if (
            branch.world_id != world_id
            or snapshot.world_id != world_id
            or snapshot.branch_id != branch.branch_id
            or snapshot.last_seq != sequence
            or sequence < branch.fork_seq
        ):
            raise Conflict("Persisted checkpoint metadata does not match its branch and sequence.")
        try:
            state = WorldState.model_validate(snapshot.state_json)
            digest = state_hash(state)
        except (TypeError, ValueError) as exc:
            raise Conflict("Persisted checkpoint state is invalid.") from exc
        if digest != snapshot.state_hash or state.current_tick != snapshot.current_tick:
            raise Conflict("Persisted checkpoint failed its integrity check.")

        if sequence == branch.fork_seq:
            try:
                initial_state = WorldState.model_validate(branch.initial_state_json)
                initial_digest = state_hash(initial_state)
            except (TypeError, ValueError) as exc:
                raise Conflict("Branch fork checkpoint is invalid.") from exc
            if (
                initial_digest != branch.initial_state_hash
                or snapshot.state_version != branch.initial_state_version
                or digest != branch.initial_state_hash
                or state.current_tick != initial_state.current_tick
            ):
                raise Conflict("Persisted checkpoint differs from the branch fork metadata.")
            return state

        event = session.scalar(
            select(EventRow).where(
                EventRow.world_id == world_id,
                EventRow.branch_id == branch.branch_id,
                EventRow.sequence == sequence,
            )
        )
        if (
            event is None
            or event.commit_boundary is not True
            or snapshot.state_version != event.state_version_after
            or snapshot.current_tick != event.tick
        ):
            raise Conflict("Persisted checkpoint does not match a complete commit boundary.")
        return state

    def find_job_by_key(self, scope, world_id, branch_id, key):
        with self._transaction() as session:
            self._require_membership(session, scope, world_id)
            row = session.scalar(select(JobRow).where(
                JobRow.world_id == world_id, JobRow.branch_id == branch_id,
                JobRow.idempotency_key == key,
            ))
            return self._job_record(row) if row is not None and self._job_visible_to(row, scope) else None

    def enqueue_job(self, scope: AccessScope, new_job: NewJob, *, expected_state_version: int | None = None) -> JobRecord:
        if new_job.job_type == MEDIA_JOB_TYPE and new_job.payload.get("submitted_by") != str(scope.user_id):
            raise AccessDenied("The submitting account must own the media job.")
        now = utc_now()
        request_payload = {
            "world_id": str(new_job.world_id),
            "branch_id": str(new_job.branch_id),
            "job_type": new_job.job_type,
            "payload": new_job.payload,
        }
        request_hash = self._hash_json(request_payload)
        try:
            with self._transaction(write=True) as session:
                world, role = self._require_membership(session, scope, new_job.world_id)
                self._require_mutation_role(role)
                if new_job.job_type == "simulation.world_advance" and (
                    role != MembershipRole.OWNER or scope.user_id != world.owner_id
                ):
                    raise AccessDenied("Only the world owner can queue world advancement.")
                branch = self._get_branch(session, new_job.world_id, new_job.branch_id, for_update=True)
                existing = session.scalar(
                    select(JobRow).where(
                        JobRow.branch_id == new_job.branch_id,
                        JobRow.idempotency_key == new_job.idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.request_hash != request_hash:
                        raise IdempotencyConflict("Job idempotency key was reused with a different payload.")
                    return self._job_record(existing)
                if expected_state_version is not None and branch.state_version != expected_state_version:
                    raise StaleVersion(expected_state_version, branch.state_version)
                row = JobRow(
                    job_id=new_job.job_id,
                    tenant_id=scope.tenant_id,
                    world_id=new_job.world_id,
                    branch_id=new_job.branch_id,
                    command_id=None,
                    job_type=new_job.job_type,
                    idempotency_key=new_job.idempotency_key,
                    request_hash=request_hash,
                    status=JobStatus.QUEUED.value,
                    payload_json=new_job.payload,
                    attempt_count=0,
                    worker_id=None,
                    lease_expires_at=None,
                    fencing_token=0,
                    committed_through_tick=branch.current_tick,
                    result_json=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
                return self._job_record(row)
        except IntegrityError as exc:
            raise Conflict("Job identifier or idempotency key already exists.") from exc

    def claim_job(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
        job_type: str | None = None,
        job_types: tuple[str, ...] | None = None,
    ) -> JobLease | None:
        if not worker_id.strip() or len(worker_id) > 200:
            raise ValueError("worker_id must be non-empty text of at most 200 characters")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        if job_type is not None and job_types is not None:
            raise ValueError("Specify job_type or job_types.")
        eligible_types = job_types if job_type is None else (job_type,)
        effective_now = normalize_datetime(now or utc_now())
        expires_at = effective_now + timedelta(seconds=lease_seconds)
        with self._transaction(write=True) as session:
            candidates = session.scalars(
                select(JobRow)
                .where(JobRow.job_type != MEDIA_JOB_TYPE)
                .where(JobRow.job_type.in_(eligible_types) if eligible_types is not None else True)
                .where(
                    (JobRow.status == JobStatus.QUEUED.value)
                    | (
                        (JobRow.status == JobStatus.RUNNING.value)
                        & (JobRow.lease_expires_at <= effective_now)
                    )
                )
                .order_by(JobRow.created_at, JobRow.job_id)
                .limit(1000)
            ).all()
            eligible = (JobRow.status == JobStatus.QUEUED.value) | (
                (JobRow.status == JobStatus.RUNNING.value) & (JobRow.lease_expires_at <= effective_now)
            )
            for candidate in candidates:
                # Lock the branch before its job and lease. The branch row exists
                # even when the first lease row does not, closing that insert race.
                branch = session.scalar(
                    select(BranchRow).where(BranchRow.branch_id == candidate.branch_id).with_for_update()
                )
                if branch is None:
                    continue
                job = session.scalar(
                    select(JobRow)
                    .where(JobRow.job_id == candidate.job_id, eligible)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                if job is None:
                    continue
                current_lease = session.scalar(
                    select(BranchLeaseRow)
                    .where(BranchLeaseRow.branch_id == job.branch_id)
                    .with_for_update()
                )
                if (current_lease is not None
                        and normalize_datetime(current_lease.lease_expires_at) > effective_now):
                    continue
                next_token = (current_lease.fencing_token if current_lease is not None else 0) + 1
                job.status = JobStatus.RUNNING.value
                job.worker_id = worker_id
                job.lease_expires_at = expires_at
                job.fencing_token = next_token
                job.attempt_count += 1
                job.updated_at = effective_now
                if current_lease is None:
                    current_lease = BranchLeaseRow(
                        branch_id=job.branch_id,
                        job_id=job.job_id,
                        worker_id=worker_id,
                        fencing_token=next_token,
                        lease_expires_at=expires_at,
                    )
                    session.add(current_lease)
                else:
                    current_lease.job_id = job.job_id
                    current_lease.worker_id = worker_id
                    current_lease.fencing_token = next_token
                    current_lease.lease_expires_at = expires_at
                session.flush()
                return JobLease(
                    job_id=job.job_id,
                    world_id=job.world_id,
                    branch_id=job.branch_id,
                    worker_id=worker_id,
                    fencing_token=next_token,
                    expires_at=expires_at,
                )
            return None

    def claim_media_job(
        self, worker_id: str, *, lease_seconds: int = 60, now: datetime | None = None,
    ) -> MediaJobClaim | None:
        if not worker_id.strip() or len(worker_id) > 200:
            raise ValueError("worker_id must be non-empty text of at most 200 characters")
        effective_now = normalize_datetime(now or utc_now())
        expires_at = self._media_lease_expiry(effective_now, lease_seconds)
        with self._transaction(write=True) as session:
            row = session.scalar(select(JobRow).where(
                JobRow.job_type == MEDIA_JOB_TYPE,
                or_(JobRow.status == JobStatus.QUEUED.value, and_(
                    JobRow.status == JobStatus.RUNNING.value, JobRow.lease_expires_at <= effective_now,
                )),
            ).order_by(JobRow.created_at, JobRow.job_id).limit(1).with_for_update(skip_locked=True))
            if row is None:
                return None
            row.status = JobStatus.RUNNING.value
            row.worker_id = worker_id
            row.fencing_token += 1
            row.attempt_count += 1
            row.lease_expires_at = expires_at
            row.updated_at = effective_now
            session.flush()
            return MediaJobClaim(self._job_record(row), JobLease(
                job_id=row.job_id, world_id=row.world_id, branch_id=row.branch_id,
                worker_id=worker_id, fencing_token=row.fencing_token, expires_at=expires_at,
            ))

    def renew_media_job(
        self, lease: JobLease, *, lease_seconds: int = 60, now: datetime | None = None,
    ) -> JobLease:
        effective_now = normalize_datetime(now or utc_now())
        expires_at = self._media_lease_expiry(effective_now, lease_seconds)
        with self._transaction(write=True) as session:
            row = self._locked_media_job(session, lease, effective_now)
            row.lease_expires_at = expires_at
            row.updated_at = effective_now
            return lease.model_copy(update={"expires_at": expires_at})

    def finish_media_job(
        self, lease: JobLease, status: JobStatus, *, result: dict[str, Any] | None = None,
        publish: Callable[[], dict[str, Any]] | None = None, now: datetime | None = None,
    ) -> JobRecord:
        if status not in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
            raise ValueError("A media worker may finish only with succeeded or failed status.")
        if publish is not None and (status != JobStatus.SUCCEEDED or result is not None):
            raise ValueError("Media publication supplies the successful result.")
        effective_now = normalize_datetime(now or utc_now())
        with self._transaction(write=True) as session:
            row = self._locked_media_job(session, lease, effective_now, allow_succeeded=status == JobStatus.SUCCEEDED)
            if row.status == JobStatus.SUCCEEDED.value:
                if result is not None and result != row.result_json:
                    raise Conflict("The media job has already finished with another result.")
                return self._job_record(row)
            if status == JobStatus.SUCCEEDED:
                try:
                    submitter = UUID(row.payload_json["submitted_by"])
                except (KeyError, ValueError, TypeError) as error:
                    raise InvalidEvent("Media job has no valid submitting account.") from error
                membership = session.scalar(select(MembershipRow).where(
                    MembershipRow.world_id == row.world_id, MembershipRow.user_id == submitter,
                    MembershipRow.tenant_id == row.tenant_id,
                ).with_for_update())
                if membership is None or membership.role not in {role.value for role in MUTATING_ROLES}:
                    raise AccessDenied("The media submitter no longer has publishing access.")
                if publish is not None:
                    result = publish()
            row.status = status.value
            row.result_json = result
            row.updated_at = effective_now
            row.lease_expires_at = row.updated_at
            session.flush()
            return self._job_record(row)

    @staticmethod
    def _media_lease_expiry(now: datetime, lease_seconds: int) -> datetime:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        return now + timedelta(seconds=lease_seconds)

    @staticmethod
    def _locked_media_job(session: Session, lease: JobLease, now: datetime, *, allow_succeeded=False) -> JobRow:
        row = session.scalar(select(JobRow).where(
            JobRow.job_id == lease.job_id, JobRow.job_type == MEDIA_JOB_TYPE,
            JobRow.world_id == lease.world_id, JobRow.branch_id == lease.branch_id,
            JobRow.worker_id == lease.worker_id, JobRow.fencing_token == lease.fencing_token,
        ).with_for_update())
        if row is not None:
            if allow_succeeded and row.status == JobStatus.SUCCEEDED.value:
                return row
            if (row.status == JobStatus.RUNNING.value and row.lease_expires_at is not None
                    and normalize_datetime(row.lease_expires_at) > now):
                return row
        raise InvalidLease("The media lease has expired or no longer owns this job.")

    def renew_job(
        self,
        lease: JobLease,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> JobLease:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        effective_now = normalize_datetime(now or utc_now())
        expires_at = effective_now + timedelta(seconds=lease_seconds)
        with self._transaction(write=True) as session:
            self._validate_worker_lease(session, lease, lease.world_id, lease.branch_id, now=effective_now)
            job = session.get(JobRow, lease.job_id)
            current = session.get(BranchLeaseRow, lease.branch_id)
            if job is None or current is None or job.status != JobStatus.RUNNING.value:
                raise InvalidLease("Only a running job may renew its lease.")
            job.lease_expires_at = expires_at
            job.updated_at = effective_now
            current.lease_expires_at = expires_at
            return JobLease(
                job_id=lease.job_id,
                world_id=lease.world_id,
                branch_id=lease.branch_id,
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
                expires_at=expires_at,
            )

    def finish_job(
        self,
        lease: JobLease,
        status: JobStatus,
        *,
        result: dict[str, Any] | None = None,
        committed_through_tick: int | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        if status not in {JobStatus.WAITING_INPUT, JobStatus.PAUSED, JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
            raise ValueError("A running job may finish only to a waiting, paused, or terminal state.")
        effective_now = normalize_datetime(now or utc_now())
        with self._transaction(write=True) as session:
            self._validate_worker_lease(session, lease, lease.world_id, lease.branch_id, now=effective_now)
            job = session.get(JobRow, lease.job_id)
            current = session.get(BranchLeaseRow, lease.branch_id)
            if job is None or current is None or job.status != JobStatus.RUNNING.value:
                raise InvalidLease("The job is no longer running under this lease.")
            job.status = status.value
            if status is JobStatus.PAUSED and (result or {}).get("reason_code") == "waypoint_opportunity_stale":
                job.payload_json = {key: value for key, value in job.payload_json.items() if key != "pending_decision"}
            job.result_json = result
            if committed_through_tick is not None and committed_through_tick != job.committed_through_tick:
                raise InvalidLease("Job progress must match the last committed event batch.")
            job.updated_at = effective_now
            job.lease_expires_at = effective_now
            current.lease_expires_at = effective_now
            session.flush()
            return self._job_record(job)

    def control_job(self, scope: AccessScope, job_id: UUID, action: str) -> JobRecord:
        transitions = {
            "pause": ({JobStatus.QUEUED, JobStatus.RUNNING}, JobStatus.PAUSED),
            "resume": ({JobStatus.PAUSED}, JobStatus.QUEUED),
            "cancel": ({JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.WAITING_INPUT}, JobStatus.CANCELLED),
        }
        if action not in transitions:
            raise ValueError("Unknown job control action.")
        allowed, destination = transitions[action]
        with self._transaction(write=True) as session:
            job = session.get(JobRow, job_id)
            if job is None:
                raise NotFound("Job does not exist.")
            world, role = self._require_membership(session, scope, job.world_id)
            if not self._job_visible_to(job, scope):
                raise NotFound("Job does not exist.")
            if role == MembershipRole.PLAYER and job.job_type == "simulation.long_action":
                plan_payload = job.payload_json.get("plan") if isinstance(job.payload_json, dict) else None
                if not isinstance(plan_payload, dict):
                    raise AccessDenied("The long action plan is not valid for player control.")
                try:
                    plan = LongActionPlan.model_validate(plan_payload)
                except ValidationError as error:
                    raise AccessDenied("The long action plan is not valid for player control.") from error
                self._require_actor_control(session, scope, job.world_id, role, plan.actor_id)
            else:
                allowed_roles = MUTATING_ROLES if job.job_type == MEDIA_JOB_TYPE else AUTHOR_ROLES
                if role not in allowed_roles:
                    raise AccessDenied("The membership role cannot control this job.")
            if job.job_type == "simulation.world_advance" and (
                role != MembershipRole.OWNER or scope.user_id != world.owner_id
            ):
                raise AccessDenied("The membership role cannot control this job.")
            branch = self._get_branch(session, job.world_id, job.branch_id, for_update=True)
            session.refresh(job, with_for_update=True)
            if job.status == destination.value:
                return self._job_record(job)
            if JobStatus(job.status) not in allowed:
                raise Conflict("The job cannot perform this transition from its current state.")
            now = utc_now()
            if action == "resume" and job.job_type == "simulation.long_action":
                plan = job.payload_json.get("plan")
                if isinstance(plan, dict) and plan.get("activity") == "study":
                    self._resume_study_from_current_checkpoint(session, branch, job, plan, now)
                elif isinstance(plan, dict) and plan.get("activity") == "work":
                    self._resume_work_from_current_checkpoint(session, branch, job, plan, now)
                else:
                    self._resume_travel_from_current_checkpoint(session, branch, job, now)
            current = session.get(BranchLeaseRow, job.branch_id)
            if current is not None and current.job_id == job_id:
                current.lease_expires_at = now
            job.lease_expires_at = now
            job.updated_at = now
            job.status = destination.value
            session.flush()
            return self._job_record(job)

    def _resume_work_from_current_checkpoint(
        self, session: Session, branch: BranchRow, job: JobRow, raw_plan: dict, now: datetime,
    ) -> None:
        from world_engine.simulation.work_resume import plan_work_resume

        payload = job.payload_json
        committed_version = payload.get("committed_state_version", payload.get("initial_state_version"))
        job_tick = job.committed_through_tick
        if committed_version == branch.state_version and job_tick == branch.current_tick:
            return
        self._ensure_no_active_branch_lease(session, branch.branch_id, now)
        if (
            type(committed_version) is not int or committed_version > branch.state_version
            or job_tick is None or payload.get("pending_decision")
        ):
            raise Conflict("The paused work job has no compatible committed checkpoint.")
        plan = LongActionPlan.model_validate(raw_plan)
        state = self._checked_branch_state(branch)
        try:
            checkpoint = plan_work_resume(state, plan, previous_committed_tick=job_tick)
        except (InvalidEvent, ValueError) as error:
            raise Conflict("The paused job no longer matches its assignment and authorized work schedule.") from error
        resume = payload.get("work_resume", {})
        next_shift_tick = checkpoint.next_shift_tick
        job.committed_through_tick = state.current_tick
        job.payload_json = {
            **payload,
            "committed_state_version": branch.state_version,
            "work_resume": {
                "skipped_shifts": resume.get("skipped_shifts", 0) + checkpoint.skipped_shifts,
                "next_shift_tick": str(next_shift_tick) if next_shift_tick is not None else None,
            },
        }

    def _resume_study_from_current_checkpoint(
        self, session: Session, branch: BranchRow, job: JobRow, raw_plan: dict, now: datetime,
    ) -> None:
        payload = job.payload_json
        committed_version = payload.get("committed_state_version", payload.get("initial_state_version"))
        job_tick = job.committed_through_tick
        if committed_version == branch.state_version and job_tick == branch.current_tick:
            return
        self._ensure_no_active_branch_lease(session, branch.branch_id, now)
        plan = LongActionPlan.model_validate(raw_plan)
        if (
            type(committed_version) is not int or committed_version > branch.state_version
            or plan.started_tick is None or job_tick is None
            or not plan.started_tick <= job_tick <= branch.current_tick < plan.target_tick
            or payload.get("pending_decision")
        ):
            raise Conflict("The paused study is outside its original time window or has a pending decision.")
        state = self._checked_branch_state(branch)
        actor = state.entities.get(plan.actor_id)
        if actor is None or actor.status != "active" or actor.components.get("skills") is None:
            raise Conflict("The paused study actor is no longer available to practice.")
        if plan.skill_id not in SkillsComponent.model_validate(actor.components["skills"]).skills:
            raise Conflict("The paused study skill is no longer registered for this actor.")

        # Recovery activities consume world time outside this job. Resume the
        # remaining plan from the committed head without moving its deadline.
        job.committed_through_tick = branch.current_tick
        job.payload_json = {**payload, "committed_state_version": branch.state_version}

    def _resume_travel_from_current_checkpoint(
        self, session: Session, branch: BranchRow, job: JobRow, now: datetime,
    ) -> None:
        payload = job.payload_json
        plan = payload.get("plan")
        committed_version = payload.get("committed_state_version")
        if not isinstance(plan, dict) or plan.get("activity") != "travel":
            return
        if plan.get("travel_itinerary") is not None:
            self._resume_itinerary_from_current_checkpoint(session, branch, job, now)
            return
        job_tick = job.committed_through_tick
        if committed_version is None or job_tick is None:
            return
        if committed_version == branch.state_version and job_tick == branch.current_tick:
            return

        self._ensure_no_active_branch_lease(session, branch.branch_id, now)
        state = self._checked_branch_state(branch)
        travel = LongActionPlan.model_validate(plan)
        actor = state.entities.get(travel.actor_id)
        journey = next((row for row in state.travel_journeys
                        if row.actor_id == travel.actor_id and row.status == "in_transit"), None)
        if (
            actor is None or actor.status != "active" or actor.location_id is not None
            or journey is None or journey.route_id != travel.route_id or travel.started_tick is None
            or not journey.departed_tick <= travel.started_tick <= job_tick <= state.current_tick
            or journey.arrival_tick - journey.stationary_ticks > travel.target_tick or payload.get("pending_decision")
        ):
            raise Conflict("The paused job no longer matches an active journey at this checkpoint.")
        resolution_due = journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick
        if state.current_tick > resolution_due:
            raise Conflict("The active journey already has an overdue resolution.")

        # Self-care can lawfully change the same traveler's state and time while
        # travel is paused. Adopt that committed checkpoint under the branch
        # lock. Keep the submitted plan and use the journey's current arrival.
        job.committed_through_tick = state.current_tick
        job.payload_json = {
            **payload,
            "committed_state_version": branch.state_version,
            "effective_travel_target_tick": max(travel.target_tick, journey.arrival_tick),
        }

    def _resume_itinerary_from_current_checkpoint(
        self, session: Session, branch: BranchRow, job: JobRow, now: datetime,
    ) -> None:
        payload = job.payload_json
        committed_version = payload.get("committed_state_version", payload.get("initial_state_version"))
        result = job.result_json
        interruption = result.get("interruption") if isinstance(result, Mapping) else None
        interruption_reason = interruption.get("reason_code") if isinstance(interruption, Mapping) else None
        result_reason = result.get("reason_code") if isinstance(result, Mapping) else None
        if itinerary_requires_replan(interruption_reason) or itinerary_requires_replan(result_reason):
            raise Conflict(
                "This itinerary cannot resume after the recorded interruption. "
                "Plan a new itinerary from the current place."
            )
        if committed_version == branch.state_version and job.committed_through_tick == branch.current_tick:
            return
        self._ensure_no_active_branch_lease(session, branch.branch_id, now)
        itinerary = LongActionPlan.model_validate(payload["plan"]).travel_itinerary
        tick = job.committed_through_tick
        if (
            type(committed_version) is not int or committed_version > branch.state_version or tick is None
            or not itinerary.created_tick <= tick <= branch.current_tick < itinerary.deadline_tick
            or payload.get("pending_decision")
        ):
            raise Conflict("The paused itinerary is outside its authorized window or has a pending decision.")
        state = self._checked_branch_state(branch)
        raw_cursor = payload.get("itinerary_cursor")
        cursor = TravelItineraryArrivalProof.model_validate(raw_cursor) if raw_cursor is not None else None
        step = inspect_travel_itinerary_job(state, itinerary, cursor=cursor)
        if step.outcome not in {"ready_to_run", "resume_in_transit"}:
            raise Conflict("The paused itinerary no longer matches the recorded route and journey.")
        if step.outcome == "resume_in_transit":
            remaining_travel = sum(leg.travel_ticks for leg in itinerary.legs[step.leg_index + 1:])
            if step.journey.arrival_tick + remaining_travel > itinerary.deadline_tick:
                raise Conflict("The active journey no longer fits the itinerary's original deadline.")
        job.committed_through_tick = state.current_tick
        job.payload_json = {**payload, "committed_state_version": branch.state_version}

    def read_job(self, scope: AccessScope, job_id: UUID) -> JobRecord:
        with self._transaction() as session:
            job = session.get(JobRow, job_id)
            if job is None:
                raise NotFound("Job does not exist.")
            self._require_membership(session, scope, job.world_id)
            if not self._job_visible_to(job, scope):
                raise NotFound("Job does not exist.")
            return self._job_record(job)

    @staticmethod
    def _waypoint_chat_receipt_for_job(job: JobRow, message_id: UUID) -> dict[str, object] | None:
        if "waypoint_chat_receipts" not in job.payload_json:
            return None
        receipts = job.payload_json.get("waypoint_chat_receipts")
        if not isinstance(receipts, dict):
            raise Conflict("Stored waypoint chat receipts are invalid.")
        message_key = str(message_id)
        if message_key not in receipts:
            return None
        receipt = receipts[message_key]
        expected_keys = {"request_id", "option_id", "accepted", "job_id"}
        if (
            not isinstance(receipt, dict)
            or set(receipt) != expected_keys
            or not isinstance(receipt.get("request_id"), str)
            or receipt.get("option_id") not in ("stop", "continue")
            or not isinstance(receipt.get("accepted"), bool)
            or receipt.get("job_id") != str(job.job_id)
        ):
            raise Conflict("A stored waypoint chat receipt is invalid.")
        try:
            request_id = UUID(receipt["request_id"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise Conflict("A stored waypoint chat receipt is invalid.") from exc
        if str(request_id) != receipt["request_id"]:
            raise Conflict("A stored waypoint chat receipt is invalid.")
        return {
            "request_id": receipt["request_id"],
            "option_id": receipt["option_id"],
            "accepted": receipt["accepted"],
            "job_id": receipt["job_id"],
        }

    @staticmethod
    def _store_waypoint_chat_receipt(
        job: JobRow, message_id: UUID, request_id: UUID, option_id: str, *, accepted: bool,
    ) -> None:
        payload = dict(job.payload_json)
        receipts = payload.get("waypoint_chat_receipts", {})
        if not isinstance(receipts, dict):
            raise Conflict("Stored waypoint chat receipts are invalid.")
        receipts = dict(receipts)
        message_key = str(message_id)
        if message_key in receipts:
            raise Conflict("This chat message already has a waypoint receipt.")
        receipts[message_key] = {
            "request_id": str(request_id),
            "option_id": option_id,
            "accepted": accepted,
            "job_id": str(job.job_id),
        }
        payload["waypoint_chat_receipts"] = receipts
        job.payload_json = payload

    def submit_job_decision(
        self,
        scope: AccessScope,
        job_id: UUID,
        request_id: UUID,
        option_id: str,
        *,
        waypoint_message_id: UUID | None = None,
    ) -> JobRecord:
        with self._transaction(write=True) as session:
            job = session.get(JobRow, job_id)
            if job is None:
                raise NotFound("Job does not exist.")
            world, role = self._require_membership(session, scope, job.world_id)
            if not self._job_visible_to(job, scope):
                raise NotFound("Job does not exist.")
            itinerary_job = job.job_type == "simulation.long_action" and job.payload_json.get("plan", {}).get("travel_itinerary") is not None
            if itinerary_job:
                if job.payload_json.get("submitted_by") != str(scope.user_id):
                    raise AccessDenied("Only the submitting player can answer this travel decision.")
                self._require_mutation_role(role)
                plan = LongActionPlan.model_validate(job.payload_json["plan"])
                self._require_actor_control(session, scope, job.world_id, role, plan.actor_id)
            elif role not in AUTHOR_ROLES or (
                job.job_type == "simulation.world_advance"
                and (role != MembershipRole.OWNER or scope.user_id != world.owner_id)
            ):
                raise AccessDenied("The membership role cannot answer this job.")
            branch = self._get_branch(session, job.world_id, job.branch_id, for_update=True)
            session.refresh(job, with_for_update=True)
            request_key = str(request_id)

            if waypoint_message_id is not None:
                if (
                    not itinerary_job
                    or job.payload_json.get("submitted_by") != str(scope.user_id)
                ):
                    raise AccessDenied("A waypoint chat choice must belong to the submitting player's itinerary.")
                # The branch row lock serializes message receipt assignment across
                # all jobs on this branch, including jobs older than list_jobs' window.
                matching_rows = session.scalars(
                    select(JobRow).where(
                        JobRow.world_id == job.world_id,
                        JobRow.branch_id == job.branch_id,
                        JobRow.tenant_id == scope.tenant_id,
                        JobRow.payload_json["submitted_by"].as_string() == str(scope.user_id),
                        JobRow.payload_json["waypoint_chat_receipts"][str(waypoint_message_id)]
                        .as_string().is_not(None),
                    )
                ).all()
                found_receipts = [
                    (row, self._waypoint_chat_receipt_for_job(row, waypoint_message_id))
                    for row in matching_rows
                ]
                found_receipts = [(row, receipt) for row, receipt in found_receipts if receipt is not None]
                if len(found_receipts) > 1:
                    raise Conflict("This chat message is already assigned to multiple waypoint jobs.")
                if found_receipts:
                    receipt_job, receipt = found_receipts[0]
                    try:
                        receipt_plan = LongActionPlan.model_validate(receipt_job.payload_json["plan"])
                    except (KeyError, ValidationError) as exc:
                        raise Conflict("The stored waypoint receipt owner is invalid.") from exc
                    if (
                        receipt_job.job_type != "simulation.long_action"
                        or receipt_job.tenant_id != scope.tenant_id
                        or receipt_job.payload_json.get("submitted_by") != str(scope.user_id)
                        or receipt_plan.actor_id != plan.actor_id
                        or receipt_plan.travel_itinerary is None
                    ):
                        raise Conflict("This chat message is already assigned to another waypoint choice.")
                    if (receipt["request_id"], receipt["option_id"]) != (request_key, option_id):
                        raise Conflict("This chat message was already used for a different waypoint choice.")
                    return self._job_record(receipt_job)

            previous = next((job.payload_json[key] for key in ("pending_decision", "last_decision")
                             if job.payload_json.get(key, {}).get("request_id") == request_key), None)
            if previous is not None:
                if previous.get("option_id") != option_id:
                    raise Conflict("This decision was already answered with a different option.")
                return self._job_record(job)
            if job.status != JobStatus.WAITING_INPUT.value or job.job_type not in {"simulation.long_action", "simulation.world_advance"}:
                raise Conflict("This job is not accepting a decision.")
            decision = next((item for item in (job.result_json or {}).get("decision_requests", [])
                             if item.get("request_id") == request_key and item.get("decision_owner") == "user"), None)
            if decision is None or option_id not in {item.get("option_id") for item in decision.get("options", [])}:
                raise Conflict("The requested option is unavailable.")
            if waypoint_message_id is not None and (
                decision.get("kind") != "waypoint_opportunity"
                or decision.get("actor_id") != str(plan.actor_id)
            ):
                raise Conflict("A chat receipt can only be attached to the submitting actor's waypoint decision.")
            if branch.state_version != decision["state_version"] or branch.current_tick != decision["at_tick"]:
                if itinerary_job and decision.get("kind") == "waypoint_opportunity":
                    job.status = JobStatus.PAUSED.value
                    job.result_json = {"reason_code": "waypoint_opportunity_stale"}
                    job.updated_at = utc_now()
                    if waypoint_message_id is not None:
                        self._store_waypoint_chat_receipt(
                            job, waypoint_message_id, request_id, option_id, accepted=False,
                        )
                    session.flush()
                    return self._job_record(job)
                raise StaleVersion(decision["state_version"], branch.state_version)
            job.payload_json = {**job.payload_json, "pending_decision": {
                "request_id": request_key, "option_id": option_id, "state_version": branch.state_version,
                "at_tick": branch.current_tick, "source": "user",
            }}
            if waypoint_message_id is not None:
                self._store_waypoint_chat_receipt(
                    job, waypoint_message_id, request_id, option_id, accepted=True,
                )
            job.status = JobStatus.QUEUED.value
            job.updated_at = utc_now()
            session.flush()
            return self._job_record(job)

    def list_waiting_waypoint_jobs(
        self, scope: AccessScope, world_id: UUID, branch_id: UUID, actor_id: UUID,
    ) -> list[JobRecord]:
        with self._transaction() as session:
            _, role = self._require_membership(session, scope, world_id)
            self._get_branch(session, world_id, branch_id)
            self._require_mutation_role(role)
            self._require_actor_control(session, scope, world_id, role, actor_id)
            rows = session.scalars(
                select(JobRow).where(
                    JobRow.world_id == world_id,
                    JobRow.branch_id == branch_id,
                    JobRow.tenant_id == scope.tenant_id,
                    JobRow.job_type == "simulation.long_action",
                    JobRow.status == JobStatus.WAITING_INPUT.value,
                    JobRow.payload_json["submitted_by"].as_string() == str(scope.user_id),
                    JobRow.payload_json["plan"]["actor_id"].as_string() == str(actor_id),
                    JobRow.result_json["reason_code"].as_string() == "waypoint_opportunity",
                ).order_by(JobRow.created_at, JobRow.job_id).limit(2)
            ).all()
            return [self._job_record(row) for row in rows]

    def find_waypoint_chat_receipt(
        self,
        scope: AccessScope,
        world_id: UUID,
        branch_id: UUID,
        actor_id: UUID,
        message_id: UUID,
    ) -> dict[str, object] | None:
        with self._transaction() as session:
            _, role = self._require_membership(session, scope, world_id)
            self._get_branch(session, world_id, branch_id)
            self._require_mutation_role(role)
            self._require_actor_control(session, scope, world_id, role, actor_id)
            rows = session.scalars(
                select(JobRow).where(
                    JobRow.world_id == world_id,
                    JobRow.branch_id == branch_id,
                    JobRow.tenant_id == scope.tenant_id,
                    JobRow.job_type == "simulation.long_action",
                    JobRow.payload_json["submitted_by"].as_string() == str(scope.user_id),
                    JobRow.payload_json["plan"]["actor_id"].as_string() == str(actor_id),
                    JobRow.payload_json["waypoint_chat_receipts"][str(message_id)]
                    .as_string().is_not(None),
                ).order_by(JobRow.created_at, JobRow.job_id)
            ).all()
            matches: list[dict[str, object]] = []
            for row in rows:
                if (
                    row.tenant_id != scope.tenant_id
                    or row.payload_json.get("submitted_by") != str(scope.user_id)
                    or row.job_type != "simulation.long_action"
                ):
                    continue
                try:
                    plan = LongActionPlan.model_validate(row.payload_json["plan"])
                except (KeyError, ValidationError) as exc:
                    raise Conflict("The stored waypoint receipt owner is invalid.") from exc
                if plan.actor_id != actor_id or plan.travel_itinerary is None:
                    continue
                receipt = self._waypoint_chat_receipt_for_job(row, message_id)
                if receipt is not None:
                    matches.append(receipt)
            if len(matches) > 1:
                raise Conflict("This chat message has multiple waypoint receipts.")
            return matches[0] if matches else None

    def list_jobs(self, scope: AccessScope, world_id: UUID, branch_id: UUID) -> list[JobRecord]:
        with self._transaction() as session:
            self._require_membership(session, scope, world_id)
            self._get_branch(session, world_id, branch_id)
            rows = session.scalars(
                select(JobRow).where(JobRow.world_id == world_id, JobRow.branch_id == branch_id)
                .where(or_(JobRow.job_type != MEDIA_JOB_TYPE,
                           JobRow.payload_json["submitted_by"].as_string() == str(scope.user_id)))
                .order_by(JobRow.updated_at.desc(), JobRow.job_id.desc()).limit(40)
            ).all()
            return [self._job_record(row) for row in rows]

    @staticmethod
    def _job_visible_to(job: JobRow, scope: AccessScope) -> bool:
        return job.job_type != MEDIA_JOB_TYPE or job.payload_json.get("submitted_by") == str(scope.user_id)

    def pending_outbox(self, *, limit: int = 100) -> list[OutboxRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._transaction() as session:
            rows = session.scalars(
                select(OutboxRow)
                .where(OutboxRow.published_at.is_(None))
                .order_by(OutboxRow.created_at, OutboxRow.outbox_id)
                .limit(limit)
            ).all()
            referenced_ids = [
                row.event_id
                for row in rows
                if isinstance(row.payload_json, dict) and OUTBOX_STORAGE_KEY in row.payload_json
            ]
            event_rows = session.scalars(
                select(EventRow).where(EventRow.event_id.in_(referenced_ids))
            ).all() if referenced_ids else []
            events_by_id = {row.event_id: row for row in event_rows}
            return [self._outbox_record(row, events_by_id.get(row.event_id)) for row in rows]

    def mark_outbox_delivery(self, outbox_id: UUID, *, succeeded: bool) -> OutboxRecord:
        now = utc_now()
        with self._transaction(write=True) as session:
            row = session.get(OutboxRow, outbox_id)
            if row is None:
                raise NotFound("Outbox message does not exist.")
            row.attempts += 1
            if succeeded:
                row.published_at = now.replace(tzinfo=None)
            session.flush()
            has_reference = isinstance(row.payload_json, dict) and OUTBOX_STORAGE_KEY in row.payload_json
            event_row = session.scalar(select(EventRow).where(EventRow.event_id == row.event_id)) if has_reference else None
            return self._outbox_record(row, event_row)

    def _require_membership(
        self, session: Session, scope: AccessScope, world_id: UUID
    ) -> tuple[WorldRow, MembershipRole]:
        world = session.scalar(
            select(WorldRow).where(WorldRow.world_id == world_id, WorldRow.tenant_id == scope.tenant_id)
        )
        if world is None:
            raise NotFound("World does not exist in this tenant.")
        membership = session.get(MembershipRow, (world_id, scope.user_id))
        if membership is None or membership.tenant_id != scope.tenant_id:
            raise AccessDenied("The principal is not a member of this world.")
        try:
            role = MembershipRole(membership.role)
        except ValueError as exc:
            raise AccessDenied("The membership has an unsupported role.") from exc
        return world, role

    def _require_mutation_role(self, role: MembershipRole) -> None:
        if role not in MUTATING_ROLES:
            raise AccessDenied("This membership role cannot commit world mutations.")

    def _authorize_author_events(
        self,
        scope: AccessScope,
        role: MembershipRole,
        command_type: str,
        actor_id: UUID,
        events: Sequence[DomainEvent],
    ) -> None:
        book_events = [event for event in events if isinstance(event, WorldBookImportedEvent)]
        if book_events or command_type == "author.import_world_book":
            if command_type != "author.import_world_book" or role not in AUTHOR_ROLES or actor_id != scope.user_id:
                raise AccessDenied("World-book imports require the authenticated owner or editor.")
            if len(book_events) != 1 or len(events) != 1:
                raise InvalidEvent("A world-book import requires exactly one author event.")
            return
        card_source_events = [
            event for event in events
            if isinstance(event, EntityCreatedEvent) and event.character_card_source is not None
        ]
        if card_source_events:
            if command_type != "author.create_character":
                raise AccessDenied("Character-card source data requires an explicit character import commit.")
            if role not in AUTHOR_ROLES or actor_id != scope.user_id:
                raise AccessDenied("Character-card source data requires the authenticated owner or editor.")
            if len(card_source_events) != 1 or not events or events[0] is not card_source_events[0]:
                raise InvalidEvent("Character-card source data must attach only to the created character event.")
        if command_type == LEGACY_VACANCIES_COMMAND or any(
            isinstance(event, OccupationLegacyVacanciesResolvedEvent) for event in events
        ):
            if role not in AUTHOR_ROLES or actor_id != scope.user_id:
                raise AccessDenied("Employment maintenance requires an owner or editor.")
            if (command_type != LEGACY_VACANCIES_COMMAND or len(events) != 1
                or not isinstance(events[0], OccupationLegacyVacanciesResolvedEvent) or events[0].actor_id != actor_id):
                raise InvalidEvent("Employment maintenance requires one matching author event.")
            return
        family_commands = {"author.record_kinship_link", "author.end_kinship_link"}
        family_events = [event for event in events if isinstance(event, KINSHIP_EVENTS)]
        if family_events or command_type in family_commands:
            if role not in AUTHOR_ROLES or actor_id != scope.user_id or command_type not in family_commands:
                raise AccessDenied("Kinship editing requires the authenticated owner or editor.")
            expected = KinshipLinkRecordedEvent if command_type == "author.record_kinship_link" else KinshipLinkEndedEvent
            if len(events) != 1 or not isinstance(events[0], expected) or events[0].actor_id != actor_id:
                raise InvalidEvent("A kinship author command requires one matching event and authenticated actor.")
            return
        if command_type in {"author.create_place", "author.create_organization"}:
            if role not in AUTHOR_ROLES or actor_id != scope.user_id:
                raise AccessDenied("Entity creation requires the authenticated owner or editor.")
            if not events or not isinstance(events[0], EntityCreatedEvent):
                raise InvalidEvent("Entity creation must begin with the new entity.")
            entity = events[0].entity
            expected_kinds = (
                {"place", "location"} if command_type == "author.create_place"
                else {"polity", "organization", "faction", "company", "government"}
            )
            if entity.kind not in expected_kinds:
                raise InvalidEvent("The new entity kind does not match the author command.")
            if len(events) == 1:
                return
            component = "place" if command_type == "author.create_place" else "treasury"
            revision = events[-1]
            if (
                not isinstance(revision, AuthorComponentRevisedEvent)
                or revision.entity_id != entity.id or revision.author_id != scope.user_id
                or revision.component_name != component
                or component in entity.components
            ):
                raise InvalidEvent("Entity account initialization must finish with its component revision.")
            expected_account_event = (
                EnvironmentalSourceAccountOpenedEvent if component == "place" else ResourceAccountOpenedEvent
            )
            if len(events) < 3 or any(
                not isinstance(event, expected_account_event) or event.owner_id != entity.id
                for event in events[1:-1]
            ):
                raise InvalidEvent("Entity creation can open only accounts owned by the new entity.")
            replacement = revision.replacement
            if not isinstance(replacement, dict):
                raise InvalidEvent("Entity account initialization requires a typed component.")
            try:
                definition = (
                    PlaceComponent.model_validate(replacement) if component == "place"
                    else TreasuryComponent.model_validate(replacement)
                )
            except ValueError as error:
                raise InvalidEvent("Entity account initialization component is invalid.") from error
            if component == "place":
                expected_accounts = {
                    (str(policy.source_account_id), policy.resource_id)
                    for policy in definition.renewable_harvests
                }
            else:
                expected_accounts = {
                    (str(account_id), resource_id)
                    for resource_id, account_id in definition.accounts.items()
                }
            actual_accounts = {(str(event.account_id), event.resource_id) for event in events[1:-1]}
            if actual_accounts != expected_accounts or len(actual_accounts) != len(events) - 2:
                raise InvalidEvent("New accounts must exactly match the initialized component.")
            return
        if any(isinstance(event, EnvironmentalSourceAccountOpenedEvent) for event in events):
            raise AccessDenied("Environmental source accounts require explicit place authorship.")
        if any(isinstance(event, NpcPlanReviewScheduledEvent) for event in events) and command_type not in {
            "author.create_character", "author.schedule_npc_review",
        }:
            raise AccessDenied("NPC enrollment requires its registered author command.")
        if command_type == "author.schedule_npc_review":
            if role is not MembershipRole.OWNER or actor_id != scope.user_id:
                raise AccessDenied("NPC enrollment requires the authenticated owner.")
            if len(events) not in {1, 2} or not isinstance(events[-1], NpcPlanReviewScheduledEvent):
                raise InvalidEvent("NPC enrollment requires its schedule and optionally one destination goal.")
            if len(events) == 2:
                creation, enrollment = events
                if not isinstance(creation, GoalCreatedEvent):
                    raise InvalidEvent("NPC enrollment can add only a destination goal.")
                goal = creation.goal
                if (goal.owner_id != enrollment.actor_id or not goal.auto_complete or goal.status != "active"
                        or goal.progress or goal.evidence_refs or len(goal.conditions) != 1
                        or goal.conditions[0].kind != "reach_location" or goal.conditions[0].subject_id != goal.owner_id
                        or creation.visibility != {"policy": "author_only"}):
                    raise InvalidEvent("NPC enrollment goal must be a private, unfulfilled destination goal for the enrolled actor.")
            return
        if command_type == "author.create_character":
            if role not in AUTHOR_ROLES or actor_id != scope.user_id:
                raise AccessDenied("Character authorship requires the authenticated owner or editor.")
            creation = events[0]
            if not isinstance(creation, EntityCreatedEvent) or creation.entity.kind != "character":
                raise InvalidEvent("Character authorship must begin with a character creation event.")
            character_id = creation.entity.id
            if not all(
                (isinstance(event, GoalCreatedEvent) and event.goal.owner_id == character_id)
                or (isinstance(event, KnowledgeAcquiredEvent) and event.record.holder_id == character_id
                    and event.record.source_event_id == creation.event_id)
                or (isinstance(event, NpcPlanReviewScheduledEvent) and event.actor_id == character_id)
                for event in events[1:]
            ):
                raise InvalidEvent("Character authorship can initialize only the new character's goals and knowledge.")
            return
        author_events = [event for event in events if isinstance(event, AuthorComponentRevisedEvent)]
        if not author_events:
            if command_type.startswith("author."):
                raise AccessDenied("Author command types require an author revision event.")
            return
        if command_type != "author.revise_component":
            raise AccessDenied("Author revisions require command_type 'author.revise_component'.")
        if role not in AUTHOR_ROLES:
            raise AccessDenied("Only world owners and editors can author entity definitions.")
        if actor_id != scope.user_id:
            raise AccessDenied("Author commands must use the authenticated user as their actor_id.")
        if any(event.author_id != scope.user_id for event in author_events):
            raise AccessDenied("Author revision attribution must match the authenticated user.")
        if len(author_events) != len(events):
            raise AccessDenied("Author revision commands cannot mix author and gameplay events.")

    def _require_actor_control(
        self,
        session: Session,
        scope: AccessScope,
        world_id: UUID,
        role: MembershipRole,
        actor_id: UUID,
    ) -> None:
        if role == MembershipRole.PLAYER:
            control = session.get(ActorControlRow, (world_id, scope.user_id, actor_id))
            if control is None:
                raise AccessDenied("This player does not control the requested actor.")

    def _get_branch(
        self,
        session: Session,
        world_id: UUID,
        branch_id: UUID,
        *,
        for_update: bool = False,
        defer_state_json: bool = False,
        defer_initial_state_json: bool = False,
    ) -> BranchRow:
        statement = select(BranchRow).where(BranchRow.branch_id == branch_id, BranchRow.world_id == world_id)
        deferred_columns = []
        if defer_state_json:
            deferred_columns.append(defer(BranchRow.state_json, raiseload=True))
        if defer_initial_state_json:
            deferred_columns.append(defer(BranchRow.initial_state_json, raiseload=True))
        if deferred_columns:
            statement = statement.options(*deferred_columns)
        if for_update:
            statement = statement.with_for_update()
        branch = session.scalar(statement)
        if branch is None:
            raise NotFound("Branch does not exist in this world.")
        return branch

    def _get_branch_with_state_text(
        self, session: Session, world_id: UUID, branch_id: UUID,
    ) -> tuple[BranchRow, str]:
        """Read branch metadata and raw state text without ORM JSON decoding."""
        row = session.execute(
            select(BranchRow, cast(BranchRow.state_json, Text))
            .where(BranchRow.branch_id == branch_id, BranchRow.world_id == world_id)
            .options(
                defer(BranchRow.state_json, raiseload=True),
                defer(BranchRow.initial_state_json, raiseload=True),
            )
        ).one_or_none()
        if row is None:
            raise NotFound("Branch does not exist in this world.")
        branch, raw_state_json = row
        return branch, raw_state_json

    def _ensure_no_active_branch_lease(self, session: Session, branch_id: UUID, now: datetime) -> None:
        lease = session.scalar(
            select(BranchLeaseRow).where(BranchLeaseRow.branch_id == branch_id).with_for_update()
        )
        if lease is not None and normalize_datetime(lease.lease_expires_at) > normalize_datetime(now):
            raise Conflict("Branch has an active writer lease; pause or finish its job before writing.")

    def _validate_causation(
        self, session: Session, world_id: UUID, branch: BranchRow, causation_ids: Sequence[UUID]
    ) -> None:
        if not causation_ids:
            return
        chain = self._branch_chain(session, branch, branch.head_seq, include_ancestors=True)
        missing = set(causation_ids)
        for branch_id, max_seq in chain:
            missing.difference_update(
                session.scalars(
                    select(EventRow.event_id).where(
                        EventRow.world_id == world_id,
                        EventRow.branch_id == branch_id,
                        EventRow.sequence <= max_seq,
                        EventRow.event_id.in_(missing),
                    )
                ).all()
            )
            if not missing:
                return
        if missing:
            raise InvalidEvent(f"Causation references must point to committed events in this branch lineage: {missing}.")

    def _validate_succession_causes(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        causation_ids: Sequence[UUID],
        events: Sequence[DomainEvent],
    ) -> None:
        successions = [event for event in events if isinstance(event, GovernanceSuccessionEvent)]
        if not successions:
            return
        for event in successions:
            if event.source_death_event_id not in causation_ids:
                raise InvalidEvent("A succession command must causally reference the predecessor's death event.")
        actual = self._find_death_events_in_session(
            session, world_id, branch, {event.predecessor_id for event in successions},
        )
        for event in successions:
            if actual.get(event.predecessor_id) != event.source_death_event_id:
                raise InvalidEvent("Succession must reference the latest actual terminal event for its predecessor.")

    def _validate_player_character_continuation_provenance(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        events: Sequence[DomainEvent],
        causation_ids: Sequence[UUID],
        *,
        through_seq: int,
    ) -> None:
        continuations = [event for event in events if isinstance(event, PlayerCharacterContinuedEvent)]
        if not continuations:
            return
        if len(events) != 1 or len(continuations) != 1:
            raise InvalidEvent("A player continuation must contain exactly one continuation event.")
        event = continuations[0]
        if tuple(causation_ids) != (event.source_death_event_id,):
            raise InvalidEvent("A player continuation must causally reference exactly its source death event.")
        actual_deaths = self._find_death_events_in_session(
            session,
            world_id,
            branch,
            {event.previous_player_id},
            through_seq=through_seq,
        )
        if actual_deaths.get(event.previous_player_id) != event.source_death_event_id:
            raise InvalidEvent("Player continuation must reference the latest actual death in this branch lineage.")

    def _find_death_events_in_session(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        subject_ids: set[UUID],
        *,
        through_seq: int | None = None,
    ) -> dict[UUID, UUID]:
        remaining = {str(subject_id) for subject_id in subject_ids}
        found: dict[UUID, UUID] = {}
        target_id = EventRow.payload_json["target_id"].as_string()
        entity_id = EventRow.payload_json["entity_id"].as_string()
        sequence = branch.head_seq if through_seq is None else through_seq
        # SQLite can prefilter the batch subjects. Keep the row-kind filter on
        # other backends and match the typed payload below.
        for source_id, cutoff in self._branch_chain(session, branch, sequence, True):
            deprivation = EventRow.event_type == "core.background_deprivation_resolved.v1"
            if self.database.engine.dialect.name == "sqlite":
                subjects = func.json_each(EventRow.payload_json, "$.subjects").table_valued("value")
                deprivation = and_(deprivation, exists(select(1).select_from(subjects).where(
                    func.json_extract(subjects.c.value, "$.entity_id").in_(remaining),
                    func.json_extract(subjects.c.value, "$.terminal_status") == "dead",
                )))
            rows = session.scalars(
                select(EventRow)
                .where(
                    EventRow.branch_id == source_id,
                    EventRow.sequence <= cutoff,
                    or_(
                        and_(EventRow.event_type == "core.life_ended.v1", entity_id.in_(remaining)),
                        and_(EventRow.event_type == "core.damage_applied.v1", target_id.in_(remaining)),
                        deprivation,
                        and_(
                            EventRow.event_type == "core.entity_field_changed.v1",
                            entity_id.in_(remaining),
                            EventRow.payload_json["field_name"].as_string() == "status",
                            EventRow.payload_json["value"].as_string().in_(("dead", "retired")),
                        ),
                    ),
                )
                .order_by(EventRow.sequence.desc())
            ).all()
            for row in rows:
                if row.event_type == "core.background_deprivation_resolved.v1":
                    for subject in row.payload_json["subjects"]:
                        subject_id = subject["entity_id"]
                        if subject["terminal_status"] == "dead" and subject_id in remaining:
                            remaining.remove(subject_id)
                            found[UUID(subject_id)] = row.event_id
                    if not remaining:
                        return found
                    continue
                subject_raw = row.payload_json.get("entity_id") or row.payload_json.get("target_id")
                if subject_raw not in remaining:
                    continue
                if row.event_type == "core.damage_applied.v1" and not self._damage_event_was_terminal(
                    session, world_id, row
                ):
                    continue
                remaining.remove(subject_raw)
                found[UUID(subject_raw)] = row.event_id
                if not remaining:
                    return found
        return found

    def _find_public_accusation_closures_in_session(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        actor_id: UUID,
        case_file_ids: set[UUID],
        *,
        through_seq: int,
    ) -> dict[UUID, UUID]:
        remaining = {str(case_file_id) for case_file_id in case_file_ids}
        found: dict[UUID, UUID] = {}
        payload_case_id = EventRow.payload_json["case_file_id"].as_string()
        for source_branch_id, cutoff in self._branch_chain(session, branch, through_seq, include_ancestors=True):
            pending = sorted(remaining)
            for offset in range(0, len(pending), 512):
                chunk = pending[offset:offset + 512]
                rows = session.scalars(
                    select(EventRow)
                    .where(
                        EventRow.world_id == world_id,
                        EventRow.branch_id == source_branch_id,
                        EventRow.sequence <= cutoff,
                        EventRow.actor_id == actor_id,
                        EventRow.event_type == "core.investigation_case_closed.v1",
                        payload_case_id.in_(chunk),
                    )
                    .order_by(EventRow.sequence.desc())
                ).all()
                for row in rows:
                    event = self._public_accusation_closure_from_row(row)
                    if (
                        event.actor_id != actor_id
                        or not event.public_accusation
                        or event.outcome != "disconfirmed"
                        or event.suspect_id is None
                        or str(event.case_file_id) not in remaining
                    ):
                        continue
                    found[event.case_file_id] = event.event_id
                    remaining.remove(str(event.case_file_id))
                if not remaining:
                    break
            if not remaining:
                break
        return found

    def _read_public_accusation_closure_in_lineage(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        event_id: UUID,
        *,
        through_seq: int,
    ) -> InvestigationCaseClosedEvent | None:
        entry = self._read_public_accusation_closure_entry_in_lineage(
            session, world_id, branch, event_id, through_seq=through_seq,
        )
        return None if entry is None else entry[0]

    def _read_public_accusation_closure_entry_in_lineage(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        event_id: UUID,
        *,
        through_seq: int,
    ) -> tuple[InvestigationCaseClosedEvent, EventRow] | None:
        row = self._read_event_row_in_lineage(
            session, world_id, branch, event_id, through_seq=through_seq,
        )
        if row is None or row.event_type != "core.investigation_case_closed.v1":
            return None
        return self._public_accusation_closure_from_row(row), row

    def _read_public_accusation_correction_in_lineage(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        event_id: UUID,
        *,
        through_seq: int,
    ) -> PublicAccusationCorrectionRecordedEvent | None:
        row = self._read_event_row_in_lineage(
            session, world_id, branch, event_id, through_seq=through_seq,
        )
        event = None if row is None else self._typed_event_from_row(row)
        if not isinstance(event, PublicAccusationCorrectionRecordedEvent):
            return None
        if (
            event.world_id != row.world_id
            or event.branch_id != row.branch_id
            or event.command_id != row.command_id
            or event.ruleset_version != row.ruleset_version
            or event.actor_id != row.actor_id
            or event.record.actor_id != row.actor_id
            or row.causation_json != [str(event.record.source_closure_event_id)]
        ):
            return None
        return event

    def _resolve_public_accusation_correction_source(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        state: WorldState,
        correction_event_id: UUID,
        *,
        through_seq: int,
    ) -> tuple[object, PublicAccusationCorrectionRecordedEvent, InvestigationCaseClosedEvent] | None:
        correction = next((
            item for item in state.public_accusation_corrections
            if item.event_id == correction_event_id
        ), None)
        if correction is None:
            return None
        correction_event = self._read_public_accusation_correction_in_lineage(
            session, world_id, branch, correction_event_id, through_seq=through_seq,
        )
        if correction_event is None or correction_event.record != correction:
            return None
        closure = self._read_public_accusation_closure_in_lineage(
            session, world_id, branch, correction.source_closure_event_id, through_seq=through_seq,
        )
        if closure is None or (
            closure.actor_id != correction.actor_id
            or closure.case_file_id != correction.case_file_id
            or closure.suspect_id != correction.suspect_id
            or not closure.public_accusation
            or closure.outcome != "disconfirmed"
            or correction.expected_revision != closure.expected_revision + 1
            or correction.corrected_tick < closure.closed_tick
        ):
            return None
        return correction, correction_event, closure

    def _read_accusation_repair_offer_in_lineage(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        offer_id: UUID,
        *,
        through_seq: int,
    ) -> tuple[AccusationRepairOfferRecordedEvent, EventRow] | None:
        row = self._read_event_row_in_lineage(session, world_id, branch, offer_id, through_seq=through_seq)
        event = None if row is None else self._typed_event_from_row(row)
        if not isinstance(event, AccusationRepairOfferRecordedEvent):
            return None
        if (
            event.world_id != row.world_id
            or event.branch_id != row.branch_id
            or event.command_id != row.command_id
            or event.ruleset_version != row.ruleset_version
            or event.actor_id != row.actor_id
            or event.record.offer_id != event.event_id
            or event.record.accuser_id != row.actor_id
            or row.causation_json != [str(event.record.correction_event_id)]
        ):
            return None
        return event, row

    def _read_event_row_in_lineage(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        event_id: UUID,
        *,
        through_seq: int,
    ) -> EventRow | None:
        for source_branch_id, cutoff in self._branch_chain(session, branch, through_seq, include_ancestors=True):
            row = session.scalar(
                select(EventRow).where(
                    EventRow.world_id == world_id,
                    EventRow.branch_id == source_branch_id,
                    EventRow.sequence <= cutoff,
                    EventRow.event_id == event_id,
                )
            )
            if row is not None:
                return row
        return None

    @staticmethod
    def _typed_event_from_row(row: EventRow) -> DomainEvent:
        try:
            event = parse_event({
                "type": row.event_type,
                "event_id": str(row.event_id),
                "visibility": row.visibility_json,
                **read_event_payload(row),
            })
        except (InvalidEvent, ValueError, TypeError) as exc:
            raise Conflict(f"Stored event {row.event_id} is invalid.") from exc
        return event

    @staticmethod
    def _same_accusation_repair_offer(left: object, right: object) -> bool:
        return (
            getattr(left, "offer_id", None) == getattr(right, "offer_id", None)
            and getattr(left, "correction_event_id", None) == getattr(right, "correction_event_id", None)
            and getattr(left, "source_closure_event_id", None) == getattr(right, "source_closure_event_id", None)
            and getattr(left, "case_file_id", None) == getattr(right, "case_file_id", None)
            and getattr(left, "case_revision", None) == getattr(right, "case_revision", None)
            and getattr(left, "accuser_id", None) == getattr(right, "accuser_id", None)
            and getattr(left, "suspect_id", None) == getattr(right, "suspect_id", None)
            and getattr(left, "statement", None) == getattr(right, "statement", None)
            and getattr(left, "offered_tick", None) == getattr(right, "offered_tick", None)
        )

    def _find_accusation_repair_trust_proof_for_closure(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        correction: object,
        *,
        through_seq: int,
    ) -> AccusationRepairTrustProof | None:
        source_closure_event_id = getattr(correction, "source_closure_event_id", None)
        if not isinstance(source_closure_event_id, UUID):
            return None
        closure_entry = self._read_public_accusation_closure_entry_in_lineage(
            session, world_id, branch, source_closure_event_id, through_seq=through_seq,
        )
        if closure_entry is None:
            return None
        closure, closure_row = closure_entry
        if (
            closure.case_file_id != getattr(correction, "case_file_id", None)
            or closure.actor_id != getattr(correction, "actor_id", None)
            or closure.suspect_id != getattr(correction, "suspect_id", None)
            or not closure.public_accusation
            or closure.outcome != "disconfirmed"
            or getattr(correction, "expected_revision", None) != closure.expected_revision + 1
            or getattr(correction, "corrected_tick", closure.closed_tick) < closure.closed_tick
        ):
            return None

        lineage_cutoffs = dict(self._branch_chain(session, branch, through_seq, include_ancestors=True))
        source_rows = session.scalars(
            select(EventRow)
            .where(
                EventRow.world_id == world_id,
                EventRow.branch_id == closure_row.branch_id,
                EventRow.command_id == closure_row.command_id,
                EventRow.sequence <= lineage_cutoffs.get(closure_row.branch_id, -1),
            )
            .order_by(EventRow.sequence)
        ).all()
        relation_entries: list[tuple[EventRow, RelationEstablishedEvent | RelationDimensionChangedEvent]] = []
        for row in source_rows:
            if row.event_type not in {"core.relation_established.v1", "core.relation_dimension_changed.v1"}:
                continue
            try:
                event = self._typed_event_from_row(row)
            except Conflict:
                return None
            if not isinstance(event, (RelationEstablishedEvent, RelationDimensionChangedEvent)):
                return None
            references = event.relation.evidence_refs if isinstance(event, RelationEstablishedEvent) else event.evidence_refs
            if source_closure_event_id in references:
                relation_entries.append((row, event))
        if len(relation_entries) != 1:
            return None

        relation_row, relation_event = relation_entries[0]
        pair_visibility = {
            "policy": "subject_ids",
            "subject_ids": sorted((str(closure.actor_id), str(closure.suspect_id))),
        }
        if (
            relation_row.actor_id != closure.actor_id
            or relation_row.ruleset_version != closure_row.ruleset_version
            or relation_row.sequence <= closure_row.sequence
            or relation_row.commit_boundary is not True
            or relation_row.visibility_json != pair_visibility
        ):
            return None

        try:
            before_state, _ = self._state_at_seq(
                session, world_id, self._get_branch(session, world_id, closure_row.branch_id),
                closure_row.sequence - 1, from_genesis=True,
            )
        except (Conflict, InvalidEvent):
            return None

        if isinstance(relation_event, RelationEstablishedEvent):
            relation = relation_event.relation
            if (
                relation.from_id != closure.suspect_id
                or relation.to_id != closure.actor_id
                or relation.visibility.policy != "subject_ids"
                or relation.visibility.subject_ids != [
                    UUID(value) for value in pair_visibility["subject_ids"]
                ]
                or relation.evidence_refs != [closure.event_id]
                or relation_event.visibility != pair_visibility
                or any(
                    current.from_id == closure.suspect_id and current.to_id == closure.actor_id
                    for current in before_state.relations
                )
            ):
                return None
            post_effect_trust = relation.dimensions.get("trust")
            baseline_trust = Decimal("0")
        else:
            if (
                relation_event.dimension != "trust"
                or relation_event.evidence_refs != [closure.event_id]
                or relation_event.visibility != pair_visibility
            ):
                return None
            prior_relations = [item for item in before_state.relations if item.id == relation_event.relation_id]
            if len(prior_relations) != 1:
                return None
            prior_relation = prior_relations[0]
            if prior_relation.from_id != closure.suspect_id or prior_relation.to_id != closure.actor_id:
                return None
            prior_value = prior_relation.dimensions.get("trust")
            if relation_event.expected_previous != prior_value:
                return None
            baseline_trust = prior_value if prior_value is not None else Decimal("0")
            post_effect_trust = relation_event.value

        if post_effect_trust is None or baseline_trust <= post_effect_trust:
            return None
        return AccusationRepairTrustProof(
            case_file_id=closure.case_file_id,
            source_closure_event_id=closure.event_id,
            source_relation_effect_event_id=relation_row.event_id,
            accuser_id=closure.actor_id,
            suspect_id=closure.suspect_id,
            baseline_trust=baseline_trust,
            post_effect_trust=post_effect_trust,
        )

    @staticmethod
    def _public_accusation_closure_from_row(row: EventRow) -> InvestigationCaseClosedEvent:
        try:
            event = parse_event({
                "type": row.event_type,
                "event_id": str(row.event_id),
                "visibility": row.visibility_json,
                **read_event_payload(row),
            })
        except (InvalidEvent, ValueError, TypeError) as exc:
            raise Conflict(f"Stored public-accusation closure {row.event_id} is invalid.") from exc
        if not isinstance(event, InvestigationCaseClosedEvent) or (
            event.world_id != row.world_id
            or event.branch_id != row.branch_id
            or event.command_id != row.command_id
            or event.ruleset_version != row.ruleset_version
            or event.actor_id != row.actor_id
            or event.closed_tick != row.tick
        ):
            raise Conflict(f"Stored public-accusation closure {row.event_id} does not match its event row.")
        return event

    def _validate_public_accusation_correction_provenance(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        events: Sequence[DomainEvent],
        causation_ids: Sequence[UUID],
        *,
        through_seq: int,
    ) -> None:
        corrections = [
            event for event in events if isinstance(event, PublicAccusationCorrectionRecordedEvent)
        ]
        if not corrections:
            return
        if len(corrections) != 1:
            raise InvalidEvent("A public-accusation correction requires one source closure.")
        correction = corrections[0]
        record = correction.record
        if tuple(causation_ids) != (record.source_closure_event_id,):
            raise InvalidEvent("A public-accusation correction must causally reference exactly its source closure.")
        closure = self._read_public_accusation_closure_in_lineage(
            session, world_id, branch, record.source_closure_event_id, through_seq=through_seq,
        )
        if closure is None or (
            closure.actor_id != record.actor_id
            or closure.case_file_id != record.case_file_id
            or closure.suspect_id != record.suspect_id
            or not closure.public_accusation
            or closure.outcome != "disconfirmed"
            or record.expected_revision != closure.expected_revision + 1
            or record.corrected_tick < closure.closed_tick
        ):
            raise InvalidEvent("Public-accusation correction does not match its actual ancestor closure event.")

    def _validate_accusation_repair_provenance(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        state: WorldState,
        events: Sequence[DomainEvent],
        causation_ids: Sequence[UUID],
        *,
        through_seq: int,
    ) -> None:
        offers = [event for event in events if isinstance(event, AccusationRepairOfferRecordedEvent)]
        responses = [event for event in events if isinstance(event, AccusationRepairResolvedEvent)]
        if not offers and not responses:
            return
        if len(offers) + len(responses) != 1:
            raise InvalidEvent("Accusation repair must bind one exact correction and offer lineage.")

        if offers:
            event = offers[0]
            record = event.record
            source = self._resolve_public_accusation_correction_source(
                session, world_id, branch, state, record.correction_event_id, through_seq=through_seq,
            )
            if source is None:
                raise InvalidEvent("Accusation repair offer requires its actual correction and closure ancestors.")
            correction, correction_event, closure = source
            if (
                record.offer_id != event.event_id
                or record.correction_event_id != correction_event.event_id
                or record.source_closure_event_id != correction.source_closure_event_id
                or record.case_file_id != correction.case_file_id
                or record.case_revision != correction.expected_revision
                or record.accuser_id != correction.actor_id
                or record.suspect_id != correction.suspect_id
                or closure.event_id != correction.source_closure_event_id
                or tuple(causation_ids) != (correction_event.event_id,)
            ):
                raise InvalidEvent("Accusation repair offer does not match its exact correction ancestors.")
            return

        event = responses[0]
        resulting_offer = event.resulting_offer
        current_offer = next((
            item for item in state.accusation_repair_offers
            if item.offer_id == resulting_offer.offer_id
        ), None)
        if current_offer is None or current_offer.status != "pending":
            raise InvalidEvent("Accusation repair response has no pending ancestor offer.")
        offer_entry = self._read_accusation_repair_offer_in_lineage(
            session, world_id, branch, resulting_offer.offer_id, through_seq=through_seq,
        )
        if offer_entry is None or not self._same_accusation_repair_offer(
            offer_entry[0].record, current_offer,
        ) or not self._same_accusation_repair_offer(resulting_offer, current_offer):
            raise InvalidEvent("Accusation repair response does not match its actual offer ancestor.")

        source = self._resolve_public_accusation_correction_source(
            session, world_id, branch, state, current_offer.correction_event_id, through_seq=through_seq,
        )
        if source is None:
            raise InvalidEvent("Accusation repair response requires its actual correction and closure ancestors.")
        correction, correction_event, closure = source
        if (
            current_offer.source_closure_event_id != correction.source_closure_event_id
            or current_offer.case_file_id != correction.case_file_id
            or current_offer.case_revision != correction.expected_revision
            or current_offer.accuser_id != correction.actor_id
            or current_offer.suspect_id != correction.suspect_id
            or closure.event_id != current_offer.source_closure_event_id
        ):
            raise InvalidEvent("Accusation repair offer ancestry does not match the correction and closure.")

        recovery = event.trust_recovery
        expected_causes = [current_offer.offer_id]
        if recovery is not None:
            proof = self._find_accusation_repair_trust_proof_for_closure(
                session, world_id, branch, correction, through_seq=through_seq,
            )
            if proof is None or proof != recovery.proof:
                raise InvalidEvent("Accusation repair trust proof does not match its actual closure harm.")
            if recovery.applied_delta > 0:
                expected_causes.append(proof.source_relation_effect_event_id)
        elif any(isinstance(item, RelationDimensionChangedEvent) for item in events):
            raise InvalidEvent("Positive accusation-repair trust recovery requires a verified source proof.")
        if tuple(causation_ids) != tuple(expected_causes):
            raise InvalidEvent("Accusation repair causation must match its exact offer and source proof.")

    def _damage_event_was_terminal(self, session: Session, world_id: UUID, damage_row: EventRow) -> bool:
        """Check whether this damage record made its target terminal in its command."""
        source_branch = session.get(BranchRow, damage_row.branch_id)
        if source_branch is None:
            raise Conflict("A death event references a missing branch.")
        command_rows = session.scalars(
            select(EventRow)
            .where(EventRow.branch_id == damage_row.branch_id, EventRow.command_id == damage_row.command_id)
            .order_by(EventRow.sequence)
        ).all()
        if not command_rows:
            return False
        state, _ = self._state_at_seq(session, world_id, source_branch, command_rows[0].sequence - 1)
        target_id = UUID(str(damage_row.payload_json.get("target_id")))
        for row in command_rows:
            raw = {
                "type": row.event_type,
                "event_id": str(row.event_id),
                "visibility": row.visibility_json,
                **read_event_payload(row),
            }
            before = state.entities.get(target_id)
            before_terminal = before is None or before.status in {"dead", "retired"}
            try:
                state = apply_event(state, parse_event(raw))
            except InvalidEvent as exc:
                raise Conflict(f"Stored event {row.event_id} failed death-evidence replay: {exc}") from exc
            if row.event_id == damage_row.event_id:
                after = state.entities.get(target_id)
                return (
                    not before_terminal
                    and after is not None
                    and after.status in {"dead", "retired"}
                )
        return False

    def _idempotent_result(self, session: Session, row: CommandRow, request_hash: str) -> CommitResult:
        if row.payload_hash != request_hash:
            raise IdempotencyConflict("Idempotency key was already used with a different command payload.")
        previous = self._load_commit_result(session, row)
        return previous.model_copy(update={"duplicate": True})

    @staticmethod
    def _compact_commit_result(result: CommitResult) -> dict[str, Any]:
        """Persist the small receipt fields and an exact reference to event rows."""
        if result.first_seq is None or result.last_seq is None or not result.events:
            raise Conflict("A committed command receipt must reference a nonempty event range.")
        payload = result.model_dump(mode="json", exclude={"events"})
        payload["events"] = []
        payload[EVENT_STORAGE_KEY] = {
            "kind": EVENT_STORAGE_KIND,
            "version": 1,
            "branch_id": str(result.branch_id),
            "first_seq": result.first_seq,
            "last_seq": result.last_seq,
            "count": len(result.events),
        }
        return payload

    def _load_commit_result(self, session: Session, row: CommandRow) -> CommitResult:
        """Read both legacy inline receipts and compact event-row references."""
        payload = dict(row.result_json)
        if EVENT_STORAGE_KEY not in payload:
            return CommitResult.model_validate(payload)
        reference = payload.pop(EVENT_STORAGE_KEY)
        if (
            not isinstance(reference, dict)
            or set(reference) != {"kind", "version", "branch_id", "first_seq", "last_seq", "count"}
            or reference.get("kind") != EVENT_STORAGE_KIND
            or type(reference.get("version")) is not int
            or reference.get("version") != 1
            or reference.get("branch_id") != str(row.branch_id)
            or payload.get("branch_id") != str(row.branch_id)
            or payload.get("command_id") != str(row.command_id)
            or payload.get("events") != []
        ):
            raise Conflict("Stored command receipt has an invalid event-storage reference.")
        first_seq, last_seq, count = (reference.get(key) for key in ("first_seq", "last_seq", "count"))
        if (
            type(first_seq) is not int
            or type(last_seq) is not int
            or type(count) is not int
            or first_seq < 1
            or last_seq < first_seq
            or count != last_seq - first_seq + 1
            or payload.get("first_seq") != first_seq
            or payload.get("last_seq") != last_seq
        ):
            raise Conflict("Stored command receipt has an invalid event sequence range.")
        event_rows = session.scalars(
            select(EventRow)
            .where(
                EventRow.branch_id == row.branch_id,
                EventRow.sequence >= first_seq,
                EventRow.sequence <= last_seq,
            )
            .order_by(EventRow.sequence)
        ).all()
        if (
            len(event_rows) != count
            or [item.sequence for item in event_rows] != list(range(first_seq, last_seq + 1))
            or any(
                item.command_id != row.command_id
                or item.world_id != row.world_id
                or item.state_version_after != payload.get("state_version")
                for item in event_rows
            )
            or not event_rows[-1].commit_boundary
            or any(item.commit_boundary for item in event_rows[:-1])
        ):
            raise Conflict("Stored command receipt event rows do not match the referenced batch.")
        payload["events"] = [self._event_record(item).model_dump(mode="json") for item in event_rows]
        return CommitResult.model_validate(payload)

    def _validate_autonomous_vacancy_in_session(self, session, world, branch, command, events) -> None:
        from world_engine.simulation.autonomous_vacancy import (
            VacancyNomination, VacancyVote, next_autonomous_vacancy_step, resolve_autonomous_vacancy_step,
        )
        from world_engine.simulation.contracts import SimulationContext

        state = self._checked_branch_state(branch)
        controlled_ids = frozenset(session.scalars(select(ActorControlRow.actor_id).where(
            ActorControlRow.world_id == world.world_id,
        )))
        context = SimulationContext(
            world_id=world.world_id, branch_id=branch.branch_id, command_id=command.command_id,
            command_actor_id=command.actor_id, world_seed=str(world.world_id), ruleset_version=world.ruleset_version,
            state_version=branch.state_version, externally_controlled_actor_ids=controlled_ids,
            allow_autonomous_vacancy=True,
        )
        step = next_autonomous_vacancy_step(state, context)
        expected = None
        if isinstance(step, VacancyNomination):
            option_id = "defer"
            if isinstance(events[0], LeadershipBallotChangedEvent):
                option_id = f"nominate_{events[0].resulting_ballot.nominee_id.hex}"
            if option_id in {option.option_id for option in step.request.options}:
                expected = resolve_autonomous_vacancy_step(state, context, step, option_id)
        elif isinstance(step, VacancyVote) and isinstance(events[0], LeadershipBallotChangedEvent):
            choice = next((vote.choice for vote in events[0].resulting_ballot.votes if vote.voter_id == step.voter_id), None)
            if choice is not None:
                expected = resolve_autonomous_vacancy_step(state, context, step, choice)
        elif isinstance(step, LeadershipBallotChangedEvent):
            expected = (step,)
        if expected != tuple(events):
            raise InvalidEvent("The autonomous vacancy command no longer matches the authorized world boundary.")

    def _validate_waypoint_opportunity_choice(self, branch, job, event) -> None:
        if job.job_type != "simulation.long_action" or job.status != JobStatus.RUNNING.value:
            raise InvalidLease("A waypoint decision requires a running itinerary.")
        plan = LongActionPlan.model_validate(job.payload_json["plan"])
        itinerary = plan.travel_itinerary
        raw_cursor = job.payload_json.get("itinerary_cursor")
        raw_decision = job.payload_json.get("pending_decision")
        if itinerary is None or raw_cursor is None or raw_decision is None:
            raise InvalidEvent("A waypoint decision requires the itinerary arrival cursor and a captured player answer.")
        state = self._checked_branch_state(branch)
        cursor = TravelItineraryArrivalProof.model_validate(raw_cursor)
        inspect_travel_itinerary_job(state, itinerary, cursor=cursor)
        opportunity = current_waypoint_opportunity(state, itinerary, cursor)
        response = DecisionResponse.model_validate(raw_decision)
        record = event.record
        if (
            opportunity is None or record.opportunity_id != opportunity.opportunity_id
            or (record.journey_id, record.actor_id, record.itinerary_started_tick)
            != (cursor.journey_id, itinerary.actor_id, itinerary.created_tick)
            or (response.request_id, response.state_version, response.at_tick, response.source, response.option_id)
            != (waypoint_opportunity_request_id(record.journey_id, record.opportunity_id, branch.state_version),
                branch.state_version, state.current_tick, "user", record.choice)
        ):
            raise InvalidEvent("The waypoint choice no longer matches the authorized itinerary decision.")

    def _validate_worker_lease(
        self,
        session: Session,
        lease: JobLease,
        world_id: UUID,
        branch_id: UUID,
        *,
        now: datetime | None = None,
    ) -> JobRow:
        """Validated lazily to keep the domain contract independent of job storage."""
        if lease.branch_id != branch_id:
            raise InvalidLease("The worker lease does not match this branch.")
        branch = session.scalar(
            select(BranchRow).where(BranchRow.branch_id == branch_id, BranchRow.world_id == world_id).with_for_update()
        )
        current = session.scalar(
            select(BranchLeaseRow).where(BranchLeaseRow.branch_id == branch_id).with_for_update()
        )
        current_time = normalize_datetime(now or utc_now())
        if (
            branch is None
            or current is None
            or current.job_id != lease.job_id
            or current.worker_id != lease.worker_id
            or current.fencing_token != lease.fencing_token
            or normalize_datetime(current.lease_expires_at) <= current_time
        ):
            raise InvalidLease("The worker lease is expired or fenced by a newer worker.")
        job = session.scalar(select(JobRow).where(JobRow.job_id == lease.job_id).with_for_update())
        if job is None or job.world_id != world_id or job.branch_id != branch_id:
            raise InvalidLease("The worker job does not belong to this world branch.")
        return job

    def _branch_chain(
        self,
        session: Session,
        branch: BranchRow,
        through_seq: int,
        include_ancestors: bool,
        *,
        defer_state_json: bool = False,
    ) -> list[tuple[UUID, int]]:
        chain = [(branch.branch_id, min(through_seq, branch.head_seq))]
        if not include_ancestors:
            return chain
        cursor = branch
        cutoff = min(through_seq, cursor.head_seq)
        while cursor.parent_branch_id is not None:
            cutoff = min(cutoff, cursor.fork_seq)
            if defer_state_json:
                parent = session.scalar(
                    select(BranchRow)
                    .where(BranchRow.branch_id == cursor.parent_branch_id)
                    .options(
                        defer(BranchRow.state_json, raiseload=True),
                        defer(BranchRow.initial_state_json, raiseload=True),
                    )
                )
            else:
                parent = session.get(BranchRow, cursor.parent_branch_id)
            if parent is None:
                raise Conflict("Branch lineage is incomplete.")
            chain.append((parent.branch_id, cutoff))
            cursor = parent
        return chain

    def _state_at_seq(
        self,
        session: Session,
        world_id: UUID,
        branch: BranchRow,
        through_seq: int,
        *,
        from_genesis: bool = False,
        visit_commit: Callable[[int, WorldState], None] | None = None,
        checkpoint_snapshot: SnapshotRow | None = None,
    ) -> tuple[WorldState, int]:
        if from_genesis and checkpoint_snapshot is not None:
            raise ValueError("A replay cannot start from both genesis and a pinned checkpoint.")
        if through_seq < 0 or through_seq > branch.head_seq:
            raise Conflict("Replay sequence is outside the branch history.")
        if through_seq < branch.fork_seq:
            if branch.parent_branch_id is None:
                raise Conflict("Replay sequence precedes the world genesis state.")
            parent = self._get_branch(session, world_id, branch.parent_branch_id)
            return self._state_at_seq(
                session,
                world_id,
                parent,
                through_seq,
                from_genesis=from_genesis,
                visit_commit=visit_commit,
                checkpoint_snapshot=checkpoint_snapshot,
            )
        if through_seq > branch.fork_seq:
            boundary = session.scalar(
                select(EventRow.commit_boundary).where(
                    EventRow.branch_id == branch.branch_id,
                    EventRow.sequence == through_seq,
                )
            )
            if boundary is not True:
                raise Conflict("Replay and fork positions must be complete command commit boundaries.")

        base_seq = branch.fork_seq
        base_version = branch.initial_state_version
        state = WorldState.model_validate(branch.initial_state_json)
        digest = state_hash(state)
        if digest != branch.initial_state_hash:
            raise Conflict("Branch fork checkpoint failed its integrity check.")

        if checkpoint_snapshot is not None and checkpoint_snapshot.branch_id == branch.branch_id:
            if checkpoint_snapshot.last_seq < base_seq or checkpoint_snapshot.last_seq > through_seq:
                raise Conflict("Pinned checkpoint is outside the replay range.")
            state = WorldState.model_validate(checkpoint_snapshot.state_json)
            if (
                state_hash(state) != checkpoint_snapshot.state_hash
                or state.current_tick != checkpoint_snapshot.current_tick
            ):
                raise Conflict("Pinned checkpoint failed its integrity check.")
            base_seq = checkpoint_snapshot.last_seq
            base_version = checkpoint_snapshot.state_version
            if visit_commit is not None:
                visit_commit(base_seq, state)
        elif checkpoint_snapshot is not None:
            if branch.parent_branch_id is None:
                raise Conflict("Pinned checkpoint is not in the branch lineage.")
            parent = self._get_branch(session, world_id, branch.parent_branch_id)
            parent_state, parent_version = self._state_at_seq(
                session,
                world_id,
                parent,
                base_seq,
                visit_commit=visit_commit,
                checkpoint_snapshot=checkpoint_snapshot,
            )
            if parent_version != base_version or state_hash(parent_state) != digest:
                raise Conflict("Branch fork checkpoint differs from its ancestor replay.")
            state = parent_state
        elif from_genesis and branch.parent_branch_id is not None:
            parent = self._get_branch(session, world_id, branch.parent_branch_id)
            parent_state, parent_version = self._state_at_seq(
                session, world_id, parent, base_seq, from_genesis=True, visit_commit=visit_commit,
            )
            if parent_version != base_version or state_hash(parent_state) != digest:
                raise Conflict("Branch fork checkpoint differs from its ancestor replay.")
            state = parent_state
        elif visit_commit is not None:
            visit_commit(base_seq, state)

        snapshot = None
        if checkpoint_snapshot is None and not from_genesis:
            snapshot = session.scalar(
                select(SnapshotRow)
                .where(
                    SnapshotRow.branch_id == branch.branch_id,
                    SnapshotRow.last_seq >= branch.fork_seq,
                    SnapshotRow.last_seq <= through_seq,
                )
                .order_by(SnapshotRow.last_seq.desc())
                .limit(1)
            )
        if snapshot is not None:
            state = WorldState.model_validate(snapshot.state_json)
            if state_hash(state) != snapshot.state_hash or state.current_tick != snapshot.current_tick:
                raise Conflict("Snapshot failed its integrity check.")
            base_seq = snapshot.last_seq
            base_version = snapshot.state_version

        rows = session.execute(
            select(EventRow, EventPayloadRow)
            .outerjoin(EventPayloadRow, EventPayloadRow.event_id == EventRow.event_id)
            .where(
                EventRow.world_id == world_id,
                EventRow.branch_id == branch.branch_id,
                EventRow.sequence > base_seq,
                EventRow.sequence <= through_seq,
            )
            .order_by(EventRow.sequence)
            .execution_options(yield_per=32)
        )
        state_version = base_version
        expected_seq = base_seq + 1
        for _, command_rows in groupby(rows, key=lambda pair: pair[0].command_id):
            prefetched_batch = list(command_rows)
            batch = [row for row, _ in prefetched_batch]
            try:
                stored_events = [parse_event({
                    "type": row.event_type, "event_id": str(row.event_id),
                    "visibility": row.visibility_json,
                    **read_event_payload(row, prefetched_sidecar=sidecar),
                }) for row, sidecar in prefetched_batch]
                events = validate_persisted_death_vacancies(
                    state, stored_events, world_id=world_id, branch_id=branch.branch_id,
                    command_id=batch[0].command_id, actor_id=batch[0].actor_id,
                    ruleset_version=batch[0].ruleset_version,
                )
                npc_replay_validation = None
                replay_actor_id = batch[0].actor_id
                if any(event.type in {"core.npc_plan_created.v1", "core.npc_plan_revised.v1",
                                      "core.npc_plan_review_resolved.v1"} for event in events):
                    from .npc_plan_replay import replay_npc_plan_batch
                    source_command = session.get(CommandRow, batch[0].command_id)
                    if source_command is None:
                        raise InvalidEvent("NPC plan replay is missing its committed command.")
                    journey_ids = npc_journey_ids(state)
                    receipts = self._find_journey_resolution_events_in_session(
                        session, world_id, branch, journey_ids, through_seq=batch[0].sequence - 1,
                    ) if journey_ids else {}
                    npc_replay_validation = replay_npc_plan_batch(
                        state, [self._event_record(row) for row in batch],
                        source_command_type=source_command.command_type,
                        source_command_payload=source_command.payload_json,
                        journey_resolution_receipts=receipts,
                    )
                    events = npc_replay_validation.core_rule_events
                    replay_actor_id = npc_replay_validation.actual_npc_actor_id
                validate_npc_attendance_scope(
                    events, world_id=world_id, branch_id=branch.branch_id, command_id=batch[0].command_id,
                    actor_id=replay_actor_id, ruleset_version=batch[0].ruleset_version,
                )
                validate_authored_npc_review_scope(
                    events, world_id=world_id, branch_id=branch.branch_id,
                    command_id=batch[0].command_id, command_actor_id=batch[0].actor_id,
                )
                validate_background_clock_batch(state.current_tick, replay_actor_id, events)
                validate_safe_conduct_travel_batch(state, events)
                validate_population_membership_batch(state, events)
                validate_adjudication_batch(state.current_tick, replay_actor_id, events, world_id=world_id,
                                            branch_id=branch.branch_id, command_id=batch[0].command_id,
                                            ruleset_version=batch[0].ruleset_version, state=state)
                if (requires_investigation_case_validation(None, events)
                    or requires_public_accusation_correction_validation(None, events)
                    or requires_accusation_repair_validation(None, events)
                    or requires_historical_evidence_destruction_validation(None, events)
                    or requires_promise_validation(None, events)
                    or any(isinstance(event, (MarketPriceReviewResolvedEvent, WorkShiftPaidEvent, RelationInvitationChangedEvent,
                                              PrivateGiftChangedEvent,
                                              MeetingReflectionRecordedEvent, RelationshipPartnershipChangedEvent, ShipmentReliefDistributedEvent,
                                              RelationshipDisclosureRecordedEvent,
                                              PlayerCharacterContinuedEvent,
                                              TravelJourneyChangedEvent, WaypointOpportunityDecisionRecordedEvent,
                                              NpcMeetingAttendanceDecisionRecordedEvent, InstitutionalCreditChangedEvent,
                                              InstitutionalTreasuryMobilizedEvent,
                                              InstitutionalCreditExtensionChangedEvent,
                                              *KINSHIP_EVENTS, *OCCUPATION_EVENTS))
                           or event.type == "core.shipment_spoilage_resolved.v1" for event in events)):
                    first = batch[0]
                    command = session.get(CommandRow, first.command_id)
                    if command is None or (command.world_id, command.branch_id, command.actor_id) != (
                        world_id, branch.branch_id, first.actor_id,
                    ):
                        raise InvalidEvent("Stored action has no matching command record.")
                    restored = (
                        command.command_type == "backup.restored_event_batch"
                        and command.payload_json.get("kind") == "verified_backup_event_batch"
                        and command.payload_json.get("source_request_payload_available") is False
                        and command.payload_json.get("source_command_id") == str(first.command_id)
                        and command.payload_json.get("source_event_count") == len(batch)
                    )
                    replay_command_type = (npc_replay_validation.action_command_type or "simulation.window") if npc_replay_validation is not None else (None if restored else command.command_type)
                    replay_command_payload = (npc_replay_validation.action_command_payload or {}) if npc_replay_validation is not None else (None if restored else command.payload_json)
                    validate_institutional_finance_batch(
                        events, world_id=world_id, branch_id=branch.branch_id, command_id=first.command_id,
                        actor_id=first.actor_id, ruleset_version=first.ruleset_version,
                        command_type=replay_command_type, command_payload=replay_command_payload,
                    )
                    validate_waypoint_opportunity_batch(
                        events, world_id=world_id, branch_id=branch.branch_id, command_id=first.command_id,
                        actor_id=first.actor_id, ruleset_version=first.ruleset_version, command_type=replay_command_type,
                        command_payload=replay_command_payload, state_version=state_version,
                    )
                    npc_meeting_decision = next((event for event in events
                                                 if isinstance(event, NpcMeetingAttendanceDecisionRecordedEvent)), None)
                    if npc_meeting_decision is not None or replay_command_type == NPC_MEETING_ATTENDANCE_COMMAND:
                        validate_npc_meeting_attendance_decision_batch(
                            events, pre_state=state, world_id=world_id, branch_id=branch.branch_id,
                            command_id=first.command_id,
                            actor_id=npc_meeting_decision.actor_id if npc_meeting_decision is not None else replay_actor_id,
                            ruleset_version=first.ruleset_version,
                            command_type=replay_command_type, command_payload=replay_command_payload,
                        )
                    else:
                        validate_relationship_invitation_batch(
                            events, world_id=world_id, branch_id=branch.branch_id, command_id=first.command_id,
                            actor_id=replay_actor_id, ruleset_version=first.ruleset_version,
                            command_type=replay_command_type, command_payload=replay_command_payload,
                        )
                    validate_private_gift_batch(
                        events, world_id=world_id, branch_id=branch.branch_id, command_id=first.command_id,
                        actor_id=first.actor_id, ruleset_version=first.ruleset_version,
                        command_type=None if restored else command.command_type,
                        command_payload=None if restored else command.payload_json,
                        day_seconds=state.calendar.day_seconds,
                    )
                    validate_meeting_reflection_batch(
                        events, world_id=world_id, branch_id=branch.branch_id, command_id=first.command_id,
                        actor_id=replay_actor_id, ruleset_version=first.ruleset_version,
                        command_type=replay_command_type,
                        command_payload=replay_command_payload,
                    )
                    validate_relationship_partnership_batch(
                        events, world_id=world_id, branch_id=branch.branch_id, command_id=first.command_id,
                        actor_id=replay_actor_id, ruleset_version=first.ruleset_version,
                        command_type=replay_command_type,
                        command_payload=replay_command_payload,
                    )
                    validate_relationship_disclosure_batch(
                        events, state=state, world_id=world_id, branch_id=branch.branch_id,
                        command_id=first.command_id, actor_id=replay_actor_id,
                        ruleset_version=first.ruleset_version,
                        command_type=replay_command_type,
                        command_payload=replay_command_payload,
                    )
                    validate_shipment_relief_batch(
                        events, world_id=world_id, branch_id=branch.branch_id, command_id=first.command_id,
                        actor_id=replay_actor_id, ruleset_version=first.ruleset_version,
                        command_type=replay_command_type,
                        command_payload=replay_command_payload,
                    )
                    validate_travel_journey_scope(
                        events, world_id=world_id, branch_id=branch.branch_id,
                        command_id=first.command_id, ruleset_version=first.ruleset_version,
                    )
                    validate_leadership_ballot_batch(
                        events, world_id=world_id, branch_id=branch.branch_id, command_id=first.command_id,
                        actor_id=replay_actor_id, ruleset_version=first.ruleset_version,
                        command_type=replay_command_type,
                        command_payload=replay_command_payload,
                    )
                    validate_safe_conduct_batch(
                        events, world_id=world_id, branch_id=branch.branch_id, command_id=first.command_id,
                        actor_id=replay_actor_id, ruleset_version=first.ruleset_version,
                        command_type=replay_command_type,
                        command_payload=replay_command_payload,
                    )
                    validate_kinship_batch(events, world_id=world_id, branch_id=branch.branch_id,
                                          command_id=first.command_id, actor_id=replay_actor_id,
                                          ruleset_version=first.ruleset_version,
                                          command_type=replay_command_type)
                    validate_occupation_batch(events, world_id=world_id, branch_id=branch.branch_id,
                                              command_id=first.command_id, actor_id=replay_actor_id, ruleset_version=first.ruleset_version,
                                              command_type=replay_command_type,
                                              command_payload=replay_command_payload)
                    validate_completed_work_shift(state, events, world_id=world_id, branch_id=branch.branch_id,
                                              command_id=first.command_id, actor_id=replay_actor_id,
                                              ruleset_version=first.ruleset_version,
                                              command_type=replay_command_type,
                                              command_payload=replay_command_payload)
                    for validate in (validate_investigation_case_batch, validate_historical_evidence_destruction_batch,
                                     validate_shipment_spoilage_batch):
                        validate(
                            state, replay_actor_id, events, world_id=world_id, branch_id=branch.branch_id,
                            command_id=first.command_id, ruleset_version=first.ruleset_version,
                            command_type=replay_command_type,
                            command_payload=replay_command_payload,
                        )
                    if requires_public_accusation_correction_validation(None, events):
                        validate_public_accusation_correction_batch(
                            state, replay_actor_id, events, world_id=world_id, branch_id=branch.branch_id,
                            command_id=first.command_id, ruleset_version=first.ruleset_version,
                            command_type=replay_command_type,
                            command_payload=replay_command_payload,
                        )
                        try:
                            correction_causes = [UUID(str(value)) for value in first.causation_json]
                        except (TypeError, ValueError) as exc:
                            raise InvalidEvent("Stored public-accusation correction causation is invalid.") from exc
                        self._validate_public_accusation_correction_provenance(
                            session, world_id, branch, events, correction_causes,
                            through_seq=batch[0].sequence - 1,
                        )
                    if requires_accusation_repair_validation(None, events):
                        try:
                            repair_causes = [UUID(str(value)) for value in first.causation_json]
                        except (TypeError, ValueError) as exc:
                            raise InvalidEvent("Stored accusation-repair causation is invalid.") from exc
                        validate_accusation_repair_batch(
                            state, replay_actor_id, events, world_id=world_id, branch_id=branch.branch_id,
                            command_id=first.command_id, ruleset_version=first.ruleset_version,
                            command_type=replay_command_type,
                            command_payload=replay_command_payload,
                            causation_ids=repair_causes,
                        )
                        self._validate_accusation_repair_provenance(
                            session, world_id, branch, state, events, repair_causes,
                            through_seq=batch[0].sequence - 1,
                        )
                    if any(isinstance(event, PlayerCharacterContinuedEvent) for event in events):
                        try:
                            continuation_causes = [UUID(str(value)) for value in first.causation_json]
                        except (TypeError, ValueError) as exc:
                            raise InvalidEvent("Stored player-continuation causation is invalid.") from exc
                        validate_player_character_continued_batch(
                            state,
                            first.actor_id,
                            events,
                            world_id=world_id,
                            branch_id=branch.branch_id,
                            command_id=first.command_id,
                            ruleset_version=first.ruleset_version,
                            command_type=replay_command_type,
                            command_payload=replay_command_payload,
                        )
                        self._validate_player_character_continuation_provenance(
                            session,
                            world_id,
                            branch,
                            events,
                            continuation_causes,
                            through_seq=batch[0].sequence - 1,
                        )
                    validate_promise_batch(
                        events, world_id=world_id, branch_id=branch.branch_id, command_id=first.command_id,
                        actor_id=replay_actor_id, ruleset_version=first.ruleset_version,
                        command_type=replay_command_type,
                        command_payload=replay_command_payload, causation_ids=first.causation_json,
                    )
                    validate_market_price_review_batch(
                        events, world_id=world_id, branch_id=branch.branch_id, command_id=first.command_id,
                        ruleset_version=first.ruleset_version, command_type=replay_command_type,
                        command_payload=replay_command_payload,
                    )
                validate_combat_batch(
                    events, world_id=world_id, branch_id=branch.branch_id,
                    command_id=batch[0].command_id, ruleset_version=batch[0].ruleset_version,
                    actor_id=replay_actor_id, state=state, causation_ids=batch[0].causation_json,
                )
                validate_treatment_batch(
                    events, world_id=world_id, branch_id=branch.branch_id,
                    command_id=batch[0].command_id, ruleset_version=batch[0].ruleset_version,
                    actor_id=replay_actor_id, state=state,
                )
            except InvalidEvent as exc:
                raise Conflict(f"Stored command {batch[0].command_id} cannot be replayed: {exc}") from exc
            for index, (row, event) in enumerate(zip(batch, stored_events, strict=True)):
                if row.sequence != expected_seq:
                    raise Conflict("Branch event sequence is not contiguous after its replay checkpoint.")
                if (row.state_version_after != state_version + 1
                    or row.commit_boundary != (index == len(batch) - 1) or row.actor_id != batch[0].actor_id):
                    raise Conflict("Stored command has inconsistent commit metadata.")
                try:
                    state = apply_event(state, event)
                except InvalidEvent as exc:
                    raise Conflict(f"Stored event {row.event_id} cannot be replayed: {exc}") from exc
                if state.current_tick != row.tick:
                    raise Conflict(f"Stored event {row.event_id} has an inconsistent tick.")
                expected_seq += 1
            state_version += 1
            if visit_commit is not None:
                visit_commit(expected_seq - 1, state)
        if expected_seq != through_seq + 1:
            raise Conflict("Branch event sequence ended before its replay boundary.")
        return state, state_version

    def _checked_branch_state(self, branch: BranchRow) -> WorldState:
        try:
            state, _ = self._branch_state_cache.get_or_validate(
                world_id=branch.world_id,
                branch_id=branch.branch_id,
                raw_payload=branch.state_json,
                stored_digest=branch.state_hash,
                state_version=branch.state_version,
                current_tick=branch.current_tick,
            )
        except StoredBranchStateIntegrityError as exc:
            raise Conflict("Stored branch projection failed its integrity check.") from exc
        if state.promise_trust is not None and state.promise_trust.get("world_id") != str(branch.world_id):
            raise Conflict("Stored promise trust projection belongs to another world.")
        return state

    def _world_record(self, row: WorldRow) -> WorldRecord:
        return WorldRecord(
            world_id=row.world_id,
            tenant_id=row.tenant_id,
            owner_id=row.owner_id,
            title=row.title,
            active_branch_id=row.active_branch_id,
            ruleset_version=row.ruleset_version,
            pack_version=row.pack_version,
            created_at=normalize_datetime(row.created_at),
        )

    def _branch_record(self, row: BranchRow | None) -> BranchRecord:
        if row is None:
            raise NotFound("Branch does not exist.")
        return BranchRecord(
            branch_id=row.branch_id,
            world_id=row.world_id,
            name=row.name,
            parent_branch_id=row.parent_branch_id,
            fork_seq=row.fork_seq,
            head_seq=row.head_seq,
            state_version=row.state_version,
            current_tick=row.current_tick,
            state_hash=row.state_hash,
            created_at=normalize_datetime(row.created_at),
        )

    def _event_record(self, row: EventRow, *, decode_payload: bool = True) -> EventRecord:
        return EventRecord(
            event_id=row.event_id,
            world_id=row.world_id,
            branch_id=row.branch_id,
            sequence=row.sequence,
            tick=row.tick,
            type=row.event_type,
            command_id=row.command_id,
            actor_id=row.actor_id,
            causation_ids=[UUID(value) for value in row.causation_json],
            correlation_id=row.correlation_id,
            ruleset_version=row.ruleset_version,
            payload=read_event_payload(row) if decode_payload else {},
            visibility=row.visibility_json,
            state_version_after=row.state_version_after,
            commit_boundary=row.commit_boundary,
            committed_at=normalize_datetime(row.committed_at),
        )

    def _snapshot_record(self, row: SnapshotRow) -> SnapshotRecord:
        return SnapshotRecord(
            snapshot_id=row.snapshot_id,
            world_id=row.world_id,
            branch_id=row.branch_id,
            last_seq=row.last_seq,
            state_version=row.state_version,
            current_tick=row.current_tick,
            state_hash=row.state_hash,
            state=WorldState.model_validate(row.state_json),
            created_at=normalize_datetime(row.created_at),
        )

    def _job_record(self, row: JobRow) -> JobRecord:
        return JobRecord(
            job_id=row.job_id,
            tenant_id=row.tenant_id,
            world_id=row.world_id,
            branch_id=row.branch_id,
            command_id=row.command_id,
            job_type=row.job_type,
            status=JobStatus(row.status),
            payload=row.payload_json,
            attempt_count=row.attempt_count,
            fencing_token=row.fencing_token,
            committed_through_tick=row.committed_through_tick,
            result=row.result_json,
            created_at=normalize_datetime(row.created_at),
            updated_at=normalize_datetime(row.updated_at),
        )

    def _outbox_record(self, row: OutboxRow, event_row: EventRow | None = None) -> OutboxRecord:
        payload = dict(row.payload_json)
        if OUTBOX_STORAGE_KEY in payload:
            reference = payload.pop(OUTBOX_STORAGE_KEY)
            if (
                payload
                or not isinstance(reference, dict)
                or set(reference) != {"kind", "version", "event_id"}
                or reference.get("kind") != OUTBOX_STORAGE_KIND
                or type(reference.get("version")) is not int
                or reference.get("version") != 1
                or reference.get("event_id") != str(row.event_id)
                or event_row is None
                or event_row.event_id != row.event_id
                or event_row.world_id != row.world_id
                or event_row.branch_id != row.branch_id
                or event_row.event_type != row.topic
            ):
                raise Conflict("Stored outbox message has an invalid event-record reference.")
            payload = self._event_record(event_row).model_dump(mode="json")
        return OutboxRecord(
            outbox_id=row.outbox_id,
            event_id=row.event_id,
            world_id=row.world_id,
            branch_id=row.branch_id,
            topic=row.topic,
            payload=payload,
            attempts=row.attempts,
            created_at=normalize_datetime(row.created_at),
            published_at=normalize_datetime(row.published_at) if row.published_at else None,
        )

    def _hash_json(self, payload: dict[str, Any]) -> str:
        import hashlib

        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
