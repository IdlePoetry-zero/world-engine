"""Validated public data envelopes shared by domain, storage, and API code."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationInfo, field_validator, model_validator
from pydantic_core import to_jsonable_python

from .components import (
    COMPONENT_REGISTRY,
    COMPONENT_NAME,
    CombatEncounterComponent,
    DailyRoutineComponent,
    RoutineMobilityComponent,
    GovernanceComponent,
    GoalCondition,
    GoalRecord,
    HistoricalEvidenceArtifact,
    InvestigationCaseDefinition,
    HealthComponent,
    InventoryComponent,
    KnowledgeRecord,
    LifeComponent,
    MarketComponent,
    NeedsComponent,
    PersonalityComponent,
    PlaceComponent,
    PopulationComponent,
    ProductionComponent,
    ProgressionComponent,
    RelationRecord,
    ScheduledEventRecord,
    SkillsComponent,
    TreasuryComponent,
    parse_component,
    validate_components,
)
from .json_types import JsonObject, JsonScalar, JsonValue
from .investigation_cases import AccusationRepairOffer, InvestigationCaseFile, PublicAccusationCorrection
from .family_kinship import MAX_KINSHIP_LINKS, KinshipLink, validate_kinship_graph
from .family_events import KinshipLinkEndedEvent, KinshipLinkRecordedEvent
from .occupations import WorkAssignment, WorkPosting
from .occupation_events import OccupationAppointedEvent, OccupationResignedEvent
from .occupation_death_vacancies import OccupationDeathVacanciesResolvedEvent
from .occupation_legacy_vacancies import OccupationLegacyVacanciesResolvedEvent
from .npc_attendance import NpcAttendance
from .npc_commitments import validate_meeting_commitment_schedules
from .npc_plans import NpcPlan
from .npc_plan_events import NpcPlanCreatedEvent, NpcPlanRevisedEvent
from .npc_plan_review_events import NpcPlanReviewResolvedEvent, NpcPlanReviewScheduledEvent
from .npc_attendance_events import NpcAttendanceCancelledEvent, NpcAttendanceSettledEvent, NpcAttendanceStartedEvent
from .npc_meeting_attendance import (
    NpcMeetingAttendanceDecision,
    NpcMeetingAttendanceDecisionRecordedEvent,
    validate_npc_meeting_attendance_decisions_state,
)
from .institutional_credit import InstitutionalCreditContract
from .institutional_credit_events import InstitutionalCreditChangedEvent
from .institutional_credit_extension_events import InstitutionalCreditExtensionChangedEvent
from .institutional_treasury_reserve_events import InstitutionalTreasuryMobilizedEvent
from .institutional_credit_state import validate_institutional_credit_state
from .work_shift import registered_work_shift_terms
from .work_shift_events import WorkShiftPaidEvent
from .market_pricing import validate_market_pricing_config
from .market_pricing_events import MarketPriceReviewResolvedEvent
from .promise_events import (
    PromiseDeadlineBreachedEvent, PromiseItemReturnedEvent, PromiseReleasedEvent, RelationshipPromiseRecordedEvent,
)
from .promise_reducers import validate_promise_state
from .promise_late_return_event import PromiseItemReturnedLateEvent
from .promise_trust_events import PromiseDeadlineBreachedWithTrustEvent, PromiseItemReturnedWithTrustEvent
from .promise_trust_settlement import validate_promise_trust_state
from .relationship_promises import RelationshipPromise
from .relationship_invitations import (
    RelationshipInvitation,
    relationship_invitation_blocks_pair_at_tick,
)
from .leadership_ballots import LeadershipBallot
from .leadership_ballot_events import LeadershipBallotChangedEvent
from .autonomous_vacancy_events import LeadershipVacancyReviewRecord, LeadershipVacancyReviewRecordedEvent
from .safe_conduct import SafeConductTreaty
from .safe_conduct_events import SafeConductChangedEvent
from .relationship_invitation_events import RelationInvitationChangedEvent
from .private_gifts import PrivateGiftChangedEvent, PrivateGiftOffer
from .relationship_reflection_events import MeetingReflectionRecordedEvent
from .combat_truce import CombatTruceChangedEvent, CombatTruceOffer
from .relationship_reflections import MeetingReflection
from .relationship_partnership_events import RelationshipPartnershipChangedEvent
from .relationship_partnerships import RelationshipPartnership
from .relationship_disclosure_events import RelationshipDisclosureRecordedEvent
from .shipment import validate_shipment_state
from .shipment_relief_events import ShipmentReliefDistributedEvent
from .travel_journeys import (
    TRAVEL_RESOLUTION_HANDLER_ID, TRAVEL_RESOLUTION_HANDLER_VERSION, TravelJourney, travel_resolution_schedule_id,
)
from .travel_journey_events import TravelJourneyChangedEvent
from .world_books import WorldBookEntry, WorldBookImportedEvent
from .story_nodes import StoryFollowup, StoryMilestone, StoryMilestoneResolvedEvent, StoryReaction, validate_story_state
from .challenges import ChallengeAttempt, ChallengeDefinition, ChallengeResolvedEvent, validate_challenge_state
from .contests import ContestChangedEvent, ContestDefinition, ContestRecord, validate_contest_state
from .waypoint_opportunity_events import WaypointOpportunityDecision, WaypointOpportunityDecisionRecordedEvent

ResourceId = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")]
EntityKind = Annotated[str, Field(min_length=1, max_length=96, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")]


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        allow_inf_nan=False,
    )


class MembershipRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    PLAYER = "player"
    OBSERVER = "observer"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AccessScope(ContractModel):
    tenant_id: UUID
    user_id: UUID
    correlation_id: UUID | None = None


class CalendarConfig(ContractModel):
    day_seconds: int = Field(default=86_400, ge=1, le=2**31 - 1)
    year_days: int = Field(default=360, ge=1, le=2**31 - 1)
    calendar_id: str = Field(default="core.standard", min_length=1, max_length=160)


class ResourceAccount(ContractModel):
    resource_id: ResourceId
    balance: Decimal
    owner_id: UUID | None = None
    account_type: Literal[
        "holder", "production_source", "environmental_source", "consumption_sink", "issuance_source", "destruction_sink"
    ] = "holder"
    credit_limit: Decimal | None = None

    @field_validator("balance", mode="before")
    @classmethod
    def parse_exact_decimal(cls, value: object) -> Decimal:
        if isinstance(value, float):
            raise ValueError("resource balances must use decimal strings or integers, not binary floats")
        if not isinstance(value, (str, int, Decimal)):
            raise ValueError("resource balances must be decimal strings or integers")
        try:
            amount = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("resource balance is not a valid decimal") from exc
        if not amount.is_finite():
            raise ValueError("resource balance must be finite")
        scale, integer_digits = _decimal_shape(amount)
        if scale > 6:
            raise ValueError("resource balance supports at most 6 fractional digits")
        if integer_digits > 30:
            raise ValueError("resource balance supports at most 30 integer digits")
        return amount

    @field_validator("credit_limit", mode="before")
    @classmethod
    def parse_credit_limit(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, float) or not isinstance(value, (str, int, Decimal)):
            raise ValueError("credit limits must use decimal strings or integers, not binary floats")
        try:
            amount = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("credit limit is not a valid decimal") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError("credit limit must be a finite nonnegative decimal")
        scale, integer_digits = _decimal_shape(amount)
        if scale > 6 or integer_digits > 30:
            raise ValueError("credit limit supports at most 30 integer and 6 fractional digits")
        return amount

    @model_validator(mode="after")
    def account_sign_and_owner(self) -> ResourceAccount:
        source_roles = {"production_source", "environmental_source", "issuance_source"}
        sink_roles = {"consumption_sink", "destruction_sink"}
        if self.account_type == "holder" and self.balance < 0:
            raise ValueError("holder account balance cannot be negative")
        if self.account_type in source_roles and self.balance > 0:
            raise ValueError("source accounts may hold only zero or negative balances")
        if (
            self.account_type in source_roles
            and self.balance < 0
            and self.credit_limit is not None
            and self.balance < self.credit_limit.copy_negate()
        ):
            raise ValueError("source account balance exceeds its finite credit_limit")
        if self.account_type in sink_roles and self.balance < 0:
            raise ValueError("sink accounts may hold only zero or positive balances")
        if self.account_type == "holder" and self.credit_limit is not None:
            raise ValueError("holder accounts cannot have a system credit_limit")
        if self.account_type in sink_roles and self.credit_limit is not None:
            raise ValueError("sink accounts cannot have a system credit_limit")
        if self.account_type == "holder" and self.owner_id is None:
            raise ValueError("holder accounts require an owner_id")
        if self.account_type not in {"holder", "production_source", "environmental_source"} and self.owner_id is not None:
            raise ValueError("only holder, producer, and environmental source accounts may have an owner_id")
        return self


class WorldEntity(ContractModel):
    id: UUID
    kind: EntityKind
    name: str | None = Field(default=None, max_length=240)
    location_id: UUID | None = None
    status: str = Field(default="active", min_length=1, max_length=64)
    components: JsonObject = Field(default_factory=dict)

    @field_validator("components", mode="before")
    @classmethod
    def components_are_jsonable(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, dict):
            raise ValueError("components must be an object")
        if info.mode == "json":
            # JSON decoding already yields string keys and JSON-native values.
            return value
        return _jsonable_component_value(value)

    @field_validator("components")
    @classmethod
    def components_are_registered(cls, value: JsonObject, info: Any) -> JsonObject:
        entity_kind = info.data.get("kind")
        if not entity_kind:
            raise ValueError("entity kind is required before validating components")
        return validate_components(entity_kind, value)  # type: ignore[return-value]


class StoryArrival(ContractModel):
    """Author-only role data kept outside the world entity registry until admission."""

    trigger_id: UUID
    outcome: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,79}$")] | None = None
    actor: WorldEntity
    relations: tuple[RelationRecord, ...] = Field(default=(), max_length=24)
    goals: tuple[GoalRecord, ...] = Field(default=(), max_length=8)
    review_delay_ticks: Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)] | None = None

    @model_validator(mode="after")
    def validate_arrival_profile(self) -> StoryArrival:
        actor = self.actor
        if (actor.kind not in {"character", "person"} or actor.status != "active"
                or not actor.name or actor.location_id is None):
            raise ValueError("A story arrival must define an active named character and an entry place.")
        if actor.components.get("life", {}).get("population_origin") is not None:
            raise ValueError("A story arrival cannot create population cohort provenance.")
        if actor.components.get("health", {}).get("incapacitating_injury_id") is not None:
            raise ValueError("A story arrival cannot seed an unrecorded combat injury.")
        for relation in self.relations:
            if actor.id not in {relation.from_id, relation.to_id} or relation.evidence_refs or relation.obligations:
                raise ValueError("Arrival relationships must involve the arriving actor and may only seed dimensions.")
        if self.review_delay_ticks is not None and not self.goals:
            raise ValueError("An arrival review requires authored actor goals.")
        if any(goal.owner_id != actor.id or goal.status != "active" or goal.evidence_refs or goal.progress
               for goal in self.goals):
            raise ValueError("An arrival goal must be new, active, and owned by the arriving actor.")
        return self


class WorldState(ContractModel):
    """A typed state envelope retaining the v1 world projection shape."""

    current_tick: int = Field(default=0, ge=-(2**63), le=2**63 - 1)
    entities: dict[UUID, WorldEntity] = Field(default_factory=dict)
    resources: dict[UUID, ResourceAccount] = Field(default_factory=dict)
    relations: list[RelationRecord] = Field(default_factory=list)
    knowledge: list[KnowledgeRecord] = Field(default_factory=list)
    goals: list[GoalRecord] = Field(default_factory=list)
    scheduled_events: list[ScheduledEventRecord] = Field(default_factory=list)
    story_reactions: list[StoryReaction] = Field(default_factory=list, max_length=128, exclude_if=lambda records: not records)
    story_followups: list[StoryFollowup] = Field(default_factory=list, max_length=128, exclude_if=lambda records: not records)
    story_milestones: list[StoryMilestone] = Field(default_factory=list, exclude_if=lambda records: not records)
    story_arrivals: list[StoryArrival] = Field(default_factory=list, max_length=256, exclude_if=lambda records: not records)
    challenges: list[ChallengeDefinition] = Field(default_factory=list, max_length=256, exclude_if=lambda records: not records)
    challenge_attempts: list[ChallengeAttempt] = Field(default_factory=list, exclude_if=lambda records: not records)
    contest_definitions: list[ContestDefinition] = Field(default_factory=list, max_length=128, exclude_if=lambda records: not records)
    contests: list[ContestRecord] = Field(default_factory=list, max_length=100_000, exclude_if=lambda records: not records)
    investigation_cases: list[InvestigationCaseFile] = Field(default_factory=list)
    public_accusation_corrections: list[PublicAccusationCorrection] = Field(
        default_factory=list,
        max_length=100_000,
        exclude_if=lambda records: not records,
    )
    accusation_repair_offers: list[AccusationRepairOffer] = Field(
        default_factory=list,
        max_length=100_000,
        exclude_if=lambda records: not records,
    )
    world_book_entries: list[WorldBookEntry] = Field(
        default_factory=list,
        max_length=512,
        exclude_if=lambda entries: not entries,
    )
    relationship_promises: list[RelationshipPromise] = Field(default_factory=list)
    relationship_invitations: list[RelationshipInvitation] = Field(default_factory=list, exclude_if=lambda records: not records)
    private_gifts: list[PrivateGiftOffer] = Field(default_factory=list, exclude_if=lambda records: not records)
    combat_truce_offers: list[CombatTruceOffer] = Field(default_factory=list, exclude_if=lambda records: not records)
    relationship_reflections: list[MeetingReflection] = Field(default_factory=list, exclude_if=lambda records: not records)
    relationship_partnerships: list[RelationshipPartnership] = Field(default_factory=list, exclude_if=lambda records: not records)
    travel_journeys: list[TravelJourney] = Field(default_factory=list, exclude_if=lambda records: not records)
    waypoint_opportunity_decisions: list[WaypointOpportunityDecision] = Field(
        default_factory=list,
        exclude_if=lambda records: not records,
    )
    npc_meeting_attendance_decisions: list[NpcMeetingAttendanceDecision] = Field(
        default_factory=list,
        exclude_if=lambda records: not records,
    )
    institutional_credit_contracts: list[InstitutionalCreditContract] = Field(
        default_factory=list,
        exclude_if=lambda records: not records,
    )
    leadership_ballots: list[LeadershipBallot] = Field(default_factory=list, exclude_if=lambda records: not records)
    leadership_vacancy_reviews: dict[UUID, LeadershipVacancyReviewRecord] = Field(
        default_factory=dict,
        max_length=100_000,
        exclude_if=lambda reviews: not reviews,
    )
    safe_conduct_treaties: list[SafeConductTreaty] = Field(default_factory=list, exclude_if=lambda records: not records)
    kinship_links: list[KinshipLink] = Field(default_factory=list, max_length=MAX_KINSHIP_LINKS, exclude_if=lambda links: not links)
    work_postings: list[WorkPosting] = Field(default_factory=list, exclude_if=lambda postings: not postings)
    work_assignments: list[WorkAssignment] = Field(default_factory=list, exclude_if=lambda assignments: not assignments)
    npc_attendance: list[NpcAttendance] = Field(default_factory=list, exclude_if=lambda records: not records)
    npc_plans: list[NpcPlan] = Field(default_factory=list, exclude_if=lambda records: not records)
    promise_trust: JsonObject | None = Field(default=None, exclude_if=lambda value: value is None)
    player_id: UUID | None = None
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_state_invariants(self, info: ValidationInfo) -> WorldState:
        if info.mode == "json":
            # Pydantic has already validated every nested model while parsing
            # the JSON input. Repeating those validations only adds a full
            # serialization and parse pass over the state.
            validated_calendar = self.calendar
        else:
            # Python inputs may contain nested model instances forged with
            # ``model_copy(update=...)``, which skips validation. Keep this
            # defensive reparse for those paths before applying cross-record
            # constraints.
            for value in self.entities.values():
                raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
                WorldEntity.model_validate(raw)
            for value in self.resources.values():
                raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
                ResourceAccount.model_validate(raw)
            for record_type, records in (
                (RelationRecord, self.relations),
                (KnowledgeRecord, self.knowledge),
                (GoalRecord, self.goals),
                (ScheduledEventRecord, self.scheduled_events),
                (InvestigationCaseFile, self.investigation_cases),
                (PublicAccusationCorrection, self.public_accusation_corrections),
                (AccusationRepairOffer, self.accusation_repair_offers),
                (RelationshipPromise, self.relationship_promises),
                (NpcMeetingAttendanceDecision, self.npc_meeting_attendance_decisions),
                (InstitutionalCreditContract, self.institutional_credit_contracts),
                (WorldBookEntry, self.world_book_entries),
                (StoryReaction, self.story_reactions),
                (StoryFollowup, self.story_followups),
                (ChallengeDefinition, self.challenges),
                (ChallengeAttempt, self.challenge_attempts),
                (ContestDefinition, self.contest_definitions),
                (ContestRecord, self.contests),
            ):
                for value in records:
                    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
                    record_type.model_validate(raw)
            for value in self.leadership_vacancy_reviews.values():
                raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
                LeadershipVacancyReviewRecord.model_validate(raw)
            calendar_raw = self.calendar.model_dump(mode="json") if isinstance(self.calendar, BaseModel) else self.calendar
            validated_calendar = CalendarConfig.model_validate(calendar_raw)
        if not -(2**63) <= self.current_tick <= 2**63 - 1:
            raise ValueError("current_tick must fit a signed 64-bit integer")
        world_book_entry_ids = [entry.entry_id for entry in self.world_book_entries]
        if len(world_book_entry_ids) != len(set(world_book_entry_ids)):
            raise ValueError("world book entry IDs must be unique within a world")

        historical_evidence_ids: set[UUID] = set()
        investigation_case_ids: set[UUID] = set()
        for entity_id, entity in self.entities.items():
            if entity_id != entity.id:
                raise ValueError(f"entity key {entity_id} does not match entity id {entity.id}")
            if entity.location_id is not None:
                location = self.entities.get(entity.location_id)
                if location is None or location.kind not in {"location", "place"}:
                    raise ValueError(f"entity {entity_id} references a missing or non-location location")
                if location.status in {"dead", "retired"}:
                    raise ValueError(f"entity {entity_id} references a retired location")
                if entity_id == entity.location_id:
                    raise ValueError(f"entity {entity_id} cannot be located inside itself")
            raw_evidence = entity.components.get("historical_evidence")
            if raw_evidence is not None:
                try:
                    evidence = HistoricalEvidenceArtifact.model_validate(raw_evidence)
                except Exception as exc:
                    raise ValueError(f"Entity {entity_id} has invalid historical evidence.") from exc
                if evidence.evidence_id in historical_evidence_ids:
                    raise ValueError("historical evidence IDs must be unique within a world")
                if evidence.subject_id not in self.entities:
                    raise ValueError("historical evidence subject must reference an existing entity")
                historical_evidence_ids.add(evidence.evidence_id)
            raw_case = entity.components.get("investigation_case")
            if raw_case is not None:
                try:
                    case = InvestigationCaseDefinition.model_validate(raw_case)
                except Exception as exc:
                    raise ValueError(f"Entity {entity_id} has an invalid investigation case definition.") from exc
                if entity.kind != "item":
                    raise ValueError("investigation case definitions must be attached to items")
                if case.case_id in investigation_case_ids:
                    raise ValueError("investigation case IDs must be unique within a world")
                investigation_case_ids.add(case.case_id)
                for route in case.routes:
                    suspect = self.entities.get(route.suspect_id)
                    if suspect is None or suspect.kind not in {"character", "person"}:
                        raise ValueError("investigation case suspects must reference existing characters")
                    for requirement in route.requirements:
                        if requirement.subject_id not in self.entities:
                            raise ValueError("investigation case evidence subjects must reference existing entities")
        if self.player_id is not None:
            player = self.entities.get(self.player_id)
            if player is None or player.kind not in {"character", "person"}:
                raise ValueError("player_id must reference an existing character entity")
        for account_id, account in self.resources.items():
            if not isinstance(account_id, UUID):
                raise ValueError("resource account keys must be UUIDs")
            if account.owner_id is not None and account.owner_id not in self.entities:
                raise ValueError(f"resource account {account_id} references a missing owner entity")
        _validate_entity_components(self.entities, self.resources, self.current_tick, validated_calendar.day_seconds)
        _validate_reference_graph(self.entities)
        relation_ids: set[UUID] = set()
        for relation in self.relations:
            if relation.id in relation_ids:
                raise ValueError(f"relation id {relation.id} is duplicated")
            relation_ids.add(relation.id)
            if relation.from_id not in self.entities or relation.to_id not in self.entities:
                raise ValueError(f"relation {relation.id} references a missing entity")
            if relation.updated_tick > self.current_tick:
                raise ValueError(f"relation {relation.id} has a future updated_tick")
        knowledge_ids: set[UUID] = set()
        knowledge_by_id: dict[UUID, KnowledgeRecord] = {}
        relationship_disclosure_keys: set[tuple[UUID, int, UUID, UUID]] = set()
        for record in self.knowledge:
            if record.id in knowledge_ids:
                raise ValueError(f"knowledge id {record.id} is duplicated")
            knowledge_ids.add(record.id)
            knowledge_by_id[record.id] = record
            for reference in (record.owner_id, record.holder_id, record.subject_id):
                if reference is not None and reference not in self.entities:
                    raise ValueError(f"knowledge {record.id} references a missing entity")
            if record.received_tick > self.current_tick:
                raise ValueError(f"knowledge {record.id} has a future received_tick")
            receipt = record.relationship_disclosure
            if receipt is not None:
                key = (
                    receipt.source_partnership_id,
                    receipt.source_revision,
                    receipt.discloser_id,
                    receipt.recipient_id,
                )
                if key in relationship_disclosure_keys:
                    raise ValueError("a partner can disclose one partnership revision to the same recipient only once")
                relationship_disclosure_keys.add(key)
                if any(reference not in self.entities for reference in (
                    receipt.discloser_id, receipt.opposite_participant_id, receipt.recipient_id, receipt.location_id,
                )):
                    raise ValueError(f"relationship disclosure knowledge {record.id} references a missing entity")
                source = next((
                    item for item in self.relationship_partnerships
                    if item.partnership_id == receipt.source_partnership_id
                ), None)
                if source is None or receipt.opposite_participant_id not in {source.proposer_id, source.partner_id} or (
                    receipt.discloser_id not in {source.proposer_id, source.partner_id}
                ):
                    raise ValueError(f"relationship disclosure knowledge {record.id} references a missing source partnership")
                if self.entities[receipt.location_id].kind not in {"place", "location"}:
                    raise ValueError(f"relationship disclosure knowledge {record.id} references a non-location")
        if historical_evidence_ids & knowledge_ids:
            raise ValueError("historical evidence IDs must not collide with knowledge record IDs")
        case_file_ids: set[UUID] = set()
        owned_case_keys: set[tuple[UUID, UUID]] = set()
        case_files_by_id: dict[UUID, InvestigationCaseFile] = {}
        for case_file in self.investigation_cases:
            if case_file.id in case_file_ids:
                raise ValueError(f"investigation case file id {case_file.id} is duplicated")
            case_file_ids.add(case_file.id)
            case_files_by_id[case_file.id] = case_file
            key = (case_file.owner_id, case_file.case_id)
            if key in owned_case_keys:
                raise ValueError("an actor may have only one file for each authored investigation case")
            owned_case_keys.add(key)
            owner = self.entities.get(case_file.owner_id)
            anchor = self.entities.get(case_file.anchor_id)
            if owner is None or owner.kind not in {"character", "person"}:
                raise ValueError("investigation case file owner must reference an existing character")
            if anchor is None or anchor.kind != "item":
                raise ValueError("investigation case file anchor must reference an existing item")
            if case_file.case_id not in investigation_case_ids:
                raise ValueError("investigation case file must reference an authored case definition in this world")
            if max(case_file.opened_tick, case_file.updated_tick, case_file.closed_tick or 0) > self.current_tick:
                raise ValueError("investigation case file cannot reference a future tick")
            for evidence_id in case_file.evidence_refs:
                evidence_record = knowledge_by_id.get(evidence_id)
                if (
                    evidence_record is None
                    or evidence_record.owner_id != case_file.owner_id
                    or evidence_record.holder_id is not None
                    or evidence_record.assessment is None
                ):
                    raise ValueError("case file evidence must reference its owner's typed assessment records")
        correction_case_ids: set[UUID] = set()
        correction_event_ids: set[UUID] = set()
        correction_source_ids: set[UUID] = set()
        corrections_by_event_id: dict[UUID, PublicAccusationCorrection] = {}
        for correction in self.public_accusation_corrections:
            if correction.case_file_id in correction_case_ids:
                raise ValueError("a public accusation can be corrected only once")
            if correction.event_id in correction_event_ids:
                raise ValueError("public accusation correction event IDs must be unique")
            if correction.source_closure_event_id in correction_source_ids:
                raise ValueError("public accusation closure events can be corrected only once")
            case_file = case_files_by_id.get(correction.case_file_id)
            if (
                case_file is None
                or case_file.status != "closed"
                or case_file.outcome != "disconfirmed"
                or not case_file.public_accusation
                or case_file.suspect_id != correction.suspect_id
                or case_file.owner_id != correction.actor_id
                or case_file.revision != correction.expected_revision
                or case_file.closed_tick is None
                or correction.corrected_tick < case_file.closed_tick
                or correction.corrected_tick > self.current_tick
            ):
                raise ValueError("public accusation correction must match one closed, disconfirmed public case")
            actor = self.entities.get(correction.actor_id)
            suspect = self.entities.get(correction.suspect_id)
            location = self.entities.get(correction.location_id)
            if (
                actor is None or suspect is None
                or actor.kind not in {"character", "person"}
                or suspect.kind not in {"character", "person"}
                or location is None or location.kind not in {"place", "location"}
            ):
                raise ValueError("public accusation correction references unavailable participants or location")
            correction_case_ids.add(correction.case_file_id)
            correction_event_ids.add(correction.event_id)
            correction_source_ids.add(correction.source_closure_event_id)
            corrections_by_event_id[correction.event_id] = correction
        offer_ids: set[UUID] = set()
        offered_correction_ids: set[UUID] = set()
        for offer in self.accusation_repair_offers:
            if offer.offer_id in offer_ids:
                raise ValueError("accusation repair offer IDs must be unique")
            if offer.correction_event_id in offered_correction_ids:
                raise ValueError("a public correction can have only one repair offer")
            correction = corrections_by_event_id.get(offer.correction_event_id)
            case_file = case_files_by_id.get(offer.case_file_id)
            if (
                correction is None
                or case_file is None
                or correction.case_file_id != offer.case_file_id
                or correction.expected_revision != offer.case_revision
                or correction.source_closure_event_id != offer.source_closure_event_id
                or correction.actor_id != offer.accuser_id
                or correction.suspect_id != offer.suspect_id
                or case_file.owner_id != offer.accuser_id
                or case_file.suspect_id != offer.suspect_id
                or case_file.status != "closed"
                or case_file.outcome != "disconfirmed"
                or not case_file.public_accusation
                or case_file.revision != offer.case_revision
                or correction.corrected_tick > offer.offered_tick
                or offer.offered_tick > self.current_tick
                or offer.updated_tick > self.current_tick
            ):
                raise ValueError("accusation repair offer must match one recorded public correction")
            if offer.accuser_id not in self.entities or offer.suspect_id not in self.entities:
                raise ValueError("accusation repair offer references a missing participant")
            offer_ids.add(offer.offer_id)
            offered_correction_ids.add(offer.correction_event_id)
        goal_ids = {goal.id for goal in self.goals}
        seen_goal_ids: set[UUID] = set()
        for goal in self.goals:
            if goal.id in seen_goal_ids:
                raise ValueError(f"goal id {goal.id} is duplicated")
            seen_goal_ids.add(goal.id)
            if goal.owner_id not in self.entities:
                raise ValueError(f"goal {goal.id} references a missing owner entity")
            if goal.created_tick > self.current_tick or goal.updated_tick > self.current_tick:
                raise ValueError(f"goal {goal.id} has a future tick")
            for condition in goal.conditions:
                for reference in (condition.subject_id, condition.target_id):
                    if reference is not None and reference not in self.entities and reference not in goal_ids:
                        raise ValueError(f"goal {goal.id} condition references a missing target")
        scheduled_ids = [event.id for event in self.scheduled_events]
        if len(scheduled_ids) != len(set(scheduled_ids)):
            raise ValueError("scheduled event ids must be unique")
        scheduled_id_set = set(scheduled_ids)
        for event in self.scheduled_events:
            missing_dependencies = set(event.dependencies) - scheduled_id_set
            if missing_dependencies:
                raise ValueError(f"scheduled event {event.id} references missing dependencies")
        validate_story_state(self)
        validate_challenge_state(self)
        validate_contest_state(self)
        validate_shipment_state(self)
        validate_promise_state(self)
        if self.relationship_invitations:
            seen_invitation_ids: set[UUID] = set()
            live_pairs: set[frozenset[UUID]] = set()
            for raw_invitation in self.relationship_invitations:
                invitation = RelationshipInvitation.model_validate(raw_invitation.model_dump(mode="python"))
                pair = frozenset({invitation.proposer_id, invitation.invitee_id})
                if invitation.invitation_id in seen_invitation_ids or invitation.updated_tick > self.current_tick:
                    raise ValueError("Relationship invitations require distinct IDs and committed times.")
                if any(record.attended_tick > self.current_tick for record in invitation.meeting_attendance):
                    raise ValueError("Relationship meeting attendance cannot be recorded in the future.")
                if invitation.meeting is not None and invitation.meeting.venue_id is not None:
                    venue = self.entities.get(invitation.meeting.venue_id)
                    if venue is None or venue.kind not in {"place", "location"}:
                        raise ValueError("A timed invitation venue must be a registered place.")
                if any(actor not in self.entities or self.entities[actor].kind not in {"character", "person"} for actor in pair):
                    raise ValueError("A relationship invitation must refer to known characters.")
                if relationship_invitation_blocks_pair_at_tick(invitation, self.current_tick):
                    if pair in live_pairs:
                        raise ValueError("One relationship invitation may be open per pair.")
                    live_pairs.add(pair)
                seen_invitation_ids.add(invitation.invitation_id)
        validate_npc_meeting_attendance_decisions_state(self)
        validate_meeting_commitment_schedules(self)
        validate_institutional_credit_state(self)
        if self.private_gifts:
            gift_ids: set[UUID] = set()
            for gift in self.private_gifts:
                if (gift.gift_id in gift_ids or gift.offered_tick > self.current_tick
                        or (gift.resolved_tick is not None and gift.resolved_tick > self.current_tick)):
                    raise ValueError("Private gifts require distinct IDs and committed times.")
                if any(self.entities.get(actor_id) is None or self.entities[actor_id].kind not in {"character", "person"}
                       for actor_id in (gift.giver_id, gift.recipient_id)):
                    raise ValueError("A private gift must refer to registered characters.")
                place = self.entities.get(gift.place_id)
                if place is None or place.kind not in {"place", "location"}:
                    raise ValueError("A private gift must record its registered meeting place.")
                gift_ids.add(gift.gift_id)
        if self.combat_truce_offers:
            offer_ids: set[UUID] = set()
            for offer in self.combat_truce_offers:
                if (offer.offer_id in offer_ids or offer.offered_tick > self.current_tick
                        or offer.responded_tick is not None and offer.responded_tick > self.current_tick):
                    raise ValueError("Combat truce records require distinct IDs and committed times.")
                if any(actor not in self.entities or self.entities[actor].kind not in {"character", "person"}
                       for actor in (offer.proposer_id, offer.recipient_id)):
                    raise ValueError("Combat truce offers must reference known participants.")
                offer_ids.add(offer.offer_id)
        if self.relationship_reflections:
            invitations = {invitation.invitation_id: invitation for invitation in self.relationship_invitations}
            reflected: set[tuple[UUID, UUID]] = set()
            for reflection in self.relationship_reflections:
                key = reflection.invitation_id, reflection.actor_id
                invitation = invitations.get(reflection.invitation_id)
                if (
                    key in reflected
                    or invitation is None
                    or invitation.meeting is None
                    or invitation.status not in {"accepted", "ended"}
                    or len(invitation.meeting_attendance) != 2
                    or reflection.actor_id not in {invitation.proposer_id, invitation.invitee_id}
                    or not invitation.meeting.end_tick <= reflection.recorded_tick <= self.current_tick
                    or (invitation.status == "ended" and reflection.recorded_tick > invitation.updated_tick)
                ):
                    raise ValueError("Meeting reflections require a completed shared meeting and a unique participant.")
                reflected.add(key)
        if self.relationship_partnerships:
            partnership_ids: set[UUID] = set()
            open_partnership_pairs: set[frozenset[UUID]] = set()
            for partnership in self.relationship_partnerships:
                pair = frozenset({partnership.proposer_id, partnership.partner_id})
                if (
                    partnership.partnership_id in partnership_ids
                    or partnership.updated_tick > self.current_tick
                    or any(actor not in self.entities or self.entities[actor].kind not in {"character", "person"} for actor in pair)
                ):
                    raise ValueError("Partnerships require distinct IDs, known participants and committed times.")
                if partnership.status in {"pending", "active"}:
                    if pair in open_partnership_pairs:
                        raise ValueError("A pair may have only one open partnership.")
                    open_partnership_pairs.add(pair)
                partnership_ids.add(partnership.partnership_id)
        journey_ids: set[UUID] = set()
        travellers: set[UUID] = set()
        travel_schedules = {item.id: item for item in self.scheduled_events if item.handler_id == TRAVEL_RESOLUTION_HANDLER_ID}
        for journey in self.travel_journeys:
            traveler = self.entities.get(journey.actor_id)
            schedule = travel_schedules.pop(travel_resolution_schedule_id(journey.journey_id), None)
            if (
                journey.journey_id in journey_ids
                or traveler is None or traveler.kind not in {"character", "person"}
                or any(self.entities.get(place) is None or self.entities[place].kind not in {"place", "location"}
                       for place in (journey.from_place_id, journey.to_place_id))
                or journey.departed_tick > self.current_tick
                or (journey.resolved_tick is not None and journey.resolved_tick > self.current_tick)
            ):
                raise ValueError("Travel journeys require distinct IDs, known entities and committed times.")
            if journey.status == "in_transit":
                if journey.actor_id in travellers or traveler.location_id is not None:
                    raise ValueError("A traveller in transit has one active journey and no local presence.")
                travellers.add(journey.actor_id)
            if (
                schedule is None or schedule.handler_version != TRAVEL_RESOLUTION_HANDLER_VERSION or schedule.phase != "resolution"
                or schedule.payload != {"journey_id": str(journey.journey_id)}
                or schedule.due_tick != (journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick)
                or schedule.status != ("queued" if journey.status == "in_transit" else "completed")
                or (journey.status == "in_transit" and schedule.due_tick < self.current_tick)
            ):
                raise ValueError("A travel journey requires its exact registered resolution occurrence.")
            journey_ids.add(journey.journey_id)
        if travel_schedules:
            raise ValueError("A travel resolution occurrence requires a registered journey.")
        decisions_by_journey: set[UUID] = set()
        journeys_by_id = {journey.journey_id: journey for journey in self.travel_journeys}
        for decision in self.waypoint_opportunity_decisions:
            journey = journeys_by_id.get(decision.journey_id)
            if (
                decision.journey_id in decisions_by_journey
                or journey is None
                or journey.actor_id != decision.actor_id
                or journey.status != "arrived"
                or journey.resolved_tick != decision.at_tick
                or decision.itinerary_started_tick > journey.departed_tick
                or decision.at_tick > self.current_tick
            ):
                raise ValueError("Waypoint decisions require one matching arrived journey and committed times.")
            decisions_by_journey.add(decision.journey_id)
        seen_ballot_ids: set[UUID] = set()
        open_ballot_organizations: set[UUID] = set()
        for ballot in self.leadership_ballots:
            if ballot.ballot_id in seen_ballot_ids or ballot.updated_tick > self.current_tick:
                raise ValueError("Leadership ballots require unique IDs and committed times.")
            organization = self.entities.get(ballot.organization_id)
            if organization is None or organization.kind not in {"organization", "polity", "faction", "company", "government"}:
                raise ValueError("Leadership ballots require a known organization.")
            for actor_id in {ballot.procedural_actor_id, ballot.nominee_id, *ballot.eligible_voter_ids}:
                actor = self.entities.get(actor_id)
                if actor is None or actor.kind not in {"character", "person"}:
                    raise ValueError("Leadership ballots require known characters.")
            if ballot.status == "open":
                if ballot.organization_id in open_ballot_organizations:
                    raise ValueError("An organization may have one open leadership ballot.")
                open_ballot_organizations.add(ballot.organization_id)
            seen_ballot_ids.add(ballot.ballot_id)
        year_ticks = self.calendar.year_days * self.calendar.day_seconds
        ballots_by_id = {ballot.ballot_id: ballot for ballot in self.leadership_ballots}
        for organization_id, review in self.leadership_vacancy_reviews.items():
            if (
                organization_id != review.organization_id
                or review.reviewed_tick > self.current_tick
                or review.period_index != review.reviewed_tick // year_ticks
            ):
                raise ValueError("Leadership vacancy reviews require matching organization keys and committed calendar periods.")
            organization = self.entities.get(organization_id)
            if organization is None or organization.kind not in {"organization", "polity", "faction", "company", "government"}:
                raise ValueError("Leadership vacancy reviews require a known organization.")
            if review.ballot_id is not None:
                ballot = ballots_by_id.get(review.ballot_id)
                if (
                    review.ballot_id != uuid5(review.decision_request_id, "ballot")
                    or ballot is None
                    or ballot.organization_id != organization_id
                    or ballot.incumbent_id is not None
                    or ballot.vacancy_proposer_id != review.proposer_id
                    or ballot.opened_tick != review.reviewed_tick
                    or ballot.governance_revision != review.expected_governance_revision
                ):
                    raise ValueError("Leadership vacancy review ballot references must match their recorded opening.")
        treaty_ids: set[UUID] = set()
        live_safe_conduct_routes: set[tuple[UUID, UUID, UUID, UUID]] = set()
        for treaty in self.safe_conduct_treaties:
            if treaty.treaty_id in treaty_ids or treaty.updated_tick > self.current_tick:
                raise ValueError("Safe conduct treaties require unique IDs and committed times.")
            for organization_id in (treaty.proposer_organization_id, treaty.recipient_organization_id):
                organization = self.entities.get(organization_id)
                if organization is None or organization.kind not in {"organization", "polity", "faction", "company", "government"}:
                    raise ValueError("Safe conduct treaties require known organizations.")
            for actor_id in (treaty.proposer_actor_id, treaty.recipient_actor_id):
                actor = self.entities.get(actor_id)
                if actor is None or actor.kind not in {"character", "person"}:
                    raise ValueError("Safe conduct treaties require known representatives.")
            deadline = treaty.counter_expires_at_tick if treaty.status == "countered" else treaty.expires_at_tick
            if treaty.status in {"pending", "countered", "active"} and deadline > self.current_tick:
                route_key = (treaty.proposer_organization_id, treaty.recipient_organization_id, treaty.from_place_id, treaty.route_id)
                if route_key in live_safe_conduct_routes:
                    raise ValueError("A directed route may have one current treaty between the same organizations.")
                live_safe_conduct_routes.add(route_key)
            treaty_ids.add(treaty.treaty_id)
        validate_promise_trust_state(self)
        if self.kinship_links:
            parties = {entity.id for entity in self.entities.values() if entity.kind in {"character", "person"}}
            validate_kinship_graph(self.kinship_links, known_party_ids=parties)
            if any(link.created_tick > self.current_tick or (link.ended_tick is not None and link.ended_tick > self.current_tick)
                   for link in self.kinship_links):
                raise ValueError("Kinship facts cannot take effect after the current world tick.")
        if self.work_postings or self.work_assignments:
            self._validate_work_records()
        if self.npc_attendance:
            assignments_by_id = {row.assignment_id: row for row in self.work_assignments}
            attendance_ids: list[UUID] = []
            for raw in self.npc_attendance:
                row = NpcAttendance.model_validate(raw.model_dump(mode="python"))
                assignment = assignments_by_id.get(row.assignment_id)
                if (assignment is None or (assignment.actor_id, assignment.location_id) != (row.actor_id, row.workplace_id)
                    or row.actor_id == self.player_id or row.starts_at_tick > self.current_tick):
                    raise ValueError("NPC attendance must refer to its assignment, worker and workplace at a started tick.")
                attendance_ids.append(row.assignment_id)
            if attendance_ids != sorted(set(attendance_ids), key=str):
                raise ValueError("NPC attendance must be sorted by unique assignment IDs.")
        if self.npc_plans:
            plan_ids: list[UUID] = []
            goals_by_id = {goal.id: goal for goal in self.goals}
            for raw in self.npc_plans:
                plan = NpcPlan.model_validate(raw.model_dump(mode="python"))
                actor = self.entities.get(plan.actor_id)
                if actor is None or actor.kind not in {"character", "person"}:
                    raise ValueError("NPC plans must refer to existing characters.")
                if plan.updated_tick > self.current_tick:
                    raise ValueError("NPC plan revisions cannot come from a future world tick.")
                if any(goal_id not in goals_by_id or goals_by_id[goal_id].owner_id != plan.actor_id
                       for goal_id in plan.goal_ids):
                    raise ValueError("NPC plan goals must exist and belong to the plan actor.")
                plan_ids.append(plan.plan_id)
            if plan_ids != sorted(set(plan_ids), key=str):
                raise ValueError("NPC plans must be sorted by unique plan IDs.")
        if "work_shift_terms" in self.metadata:
            registered_work_shift_terms(self)
        validate_market_pricing_config(self)
        return self

    def _validate_work_records(self) -> None:
        postings = {}
        for raw in self.work_postings:
            posting = WorkPosting.model_validate(raw.model_dump(mode="python"))
            location = self.entities.get(posting.location_id)
            if posting.posting_id in postings or posting.employer_id not in self.entities:
                raise ValueError("Work posting must have a unique ID and a known employer.")
            if location is None or location.kind not in {"place", "location"}:
                raise ValueError("Work posting location must reference a known place.")
            postings[posting.posting_id] = posting

        assignment_ids = set()
        active_actors = set()
        filled_slots: dict[UUID, int] = {}
        for raw in self.work_assignments:
            assignment = WorkAssignment.model_validate(raw.model_dump(mode="python"))
            actor = self.entities.get(assignment.actor_id)
            posting = postings.get(assignment.posting_id)
            if assignment.assignment_id in assignment_ids or actor is None or actor.kind not in {"character", "person"}:
                raise ValueError("Work assignment must have a unique ID and a known character.")
            if posting is None or (posting.employer_id, posting.location_id) != (assignment.employer_id, assignment.location_id):
                raise ValueError("Work assignment must reference its registered employer and workplace.")
            if assignment.started_tick > self.current_tick or (assignment.ended_tick is not None and assignment.ended_tick > self.current_tick):
                raise ValueError("An assignment cannot record work changes beyond the current tick.")
            assignment_ids.add(assignment.assignment_id)
            if assignment.ended_tick is None:
                count = filled_slots.get(posting.posting_id, 0) + 1
                if assignment.actor_id in active_actors or count > posting.slots:
                    raise ValueError("Active work assignments must respect workforce and posting capacity.")
                active_actors.add(assignment.actor_id)
                filled_slots[posting.posting_id] = count


class NewWorld(ContractModel):
    world_id: UUID = Field(default_factory=uuid4)
    branch_id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=240)
    ruleset_version: str = Field(default="1.0.0", min_length=1, max_length=100)
    pack_version: str = Field(default="1.0.0", min_length=1, max_length=100)
    state: WorldState = Field(default_factory=WorldState)


class WorldRecord(ContractModel):
    world_id: UUID
    tenant_id: UUID
    owner_id: UUID
    title: str
    active_branch_id: UUID
    ruleset_version: str
    pack_version: str
    created_at: datetime


class BranchRecord(ContractModel):
    branch_id: UUID
    world_id: UUID
    name: str
    parent_branch_id: UUID | None
    fork_seq: int = Field(ge=0)
    head_seq: int = Field(ge=0)
    state_version: int = Field(ge=0)
    current_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime


class WorldCreated(ContractModel):
    world: WorldRecord
    branch: BranchRecord


class WorldView(ContractModel):
    world: WorldRecord
    branch: BranchRecord
    state: WorldState
    last_seq: int = Field(ge=0)
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


MAX_CHARACTER_CARD_SOURCE_BYTES = 256 * 1024
MAX_CHARACTER_CARD_SOURCE_BASE64_BYTES = ((MAX_CHARACTER_CARD_SOURCE_BYTES + 2) // 3) * 4


class CharacterCardSource(ContractModel):
    """Opaque source text retained for author-only export and backup."""

    # The raw source must survive validation byte-for-byte. In particular,
    # ContractModel's normal whitespace stripping would corrupt this field.
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
        validate_assignment=True,
        allow_inf_nan=False,
    )

    source_format: Literal["json", "png"]
    card_version: Literal["1", "2.0", "3.0"]
    original_json_base64: StrictStr = Field(
        min_length=4, max_length=MAX_CHARACTER_CARD_SOURCE_BASE64_BYTES,
    )
    sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def source_is_exact_bounded_utf8(self) -> CharacterCardSource:
        try:
            encoded = self.original_json_base64.encode("ascii", errors="strict")
        except UnicodeEncodeError as error:
            raise ValueError("character-card source base64 must contain only ASCII characters") from error
        if not encoded or len(encoded) > MAX_CHARACTER_CARD_SOURCE_BASE64_BYTES:
            raise ValueError("character-card source base64 exceeds the bounded encoded size")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("character-card source base64 is malformed") from error
        if base64.b64encode(decoded) != encoded:
            raise ValueError("character-card source base64 must use canonical encoding")
        if not decoded or len(decoded) > MAX_CHARACTER_CARD_SOURCE_BYTES:
            raise ValueError("character-card source must contain 1 to 256 KiB of UTF-8 text")
        try:
            decoded.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValueError("character-card source must be valid UTF-8 text") from error
        expected = hashlib.sha256(decoded).hexdigest()
        if self.sha256 != expected:
            raise ValueError("character-card source SHA-256 does not match its exact decoded bytes")
        return self


class EntityCreatedEvent(ContractModel):
    type: Literal["core.entity_created.v1"] = "core.entity_created.v1"
    event_id: UUID = Field(default_factory=uuid4)
    entity: WorldEntity
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})
    character_card_source: CharacterCardSource | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def imported_card_source_is_author_private(self) -> EntityCreatedEvent:
        if self.character_card_source is not None and (
            self.entity.kind != "character" or self.visibility != {"policy": "author_only"}
        ):
            raise ValueError(
                "character-card source can attach only to an author-only character creation event",
            )
        return self


class EntityFieldChangedEvent(ContractModel):
    type: Literal["core.entity_field_changed.v1"] = "core.entity_field_changed.v1"
    event_id: UUID = Field(default_factory=uuid4)
    entity_id: UUID
    field_name: Literal["name", "status"]
    value: str | None
    expected_population_revision: int | None = Field(default=None, ge=1)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


class ResourceTransferredEvent(ContractModel):
    type: Literal["core.resource_transferred.v1"] = "core.resource_transferred.v1"
    event_id: UUID = Field(default_factory=uuid4)
    from_account_id: UUID
    to_account_id: UUID
    resource_id: ResourceId
    quantity: Decimal = Field(gt=Decimal("0"))
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})

    @field_validator("quantity", mode="before")
    @classmethod
    def parse_exact_quantity(cls, value: object) -> Decimal:
        if isinstance(value, float) or not isinstance(value, (str, int, Decimal)):
            raise ValueError("resource quantities must use decimal strings or integers, not binary floats")
        try:
            quantity = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("resource quantity is not a valid decimal") from exc
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("resource quantity must be finite and greater than zero")
        scale, integer_digits = _decimal_shape(quantity)
        if scale > 6:
            raise ValueError("resource quantity supports at most 6 fractional digits")
        if integer_digits > 30:
            raise ValueError("resource quantity supports at most 30 integer digits")
        return quantity


class TimeAdvancedEvent(ContractModel):
    type: Literal["core.time_advanced.v1"] = "core.time_advanced.v1"
    event_id: UUID = Field(default_factory=uuid4)
    to_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})


from .environmental_source_events import EnvironmentalSourceAccountOpenedEvent
from .gameplay_events import (
    AuthorComponentRevisedEvent,
    BackgroundNeedsAdvancedEvent,
    BackgroundRoutineAdvancedEvent,
    BackgroundDeprivationResolvedEvent,
    CombatExchangeResolvedEvent,
    CombatRetreatResolvedEvent,
    ComponentReplacedEvent,
    DamageAppliedEvent,
    EntityDiscoveredEvent,
    GoalCreatedEvent,
    GoalUpdatedEvent,
    InvestigationCaseOpenedEvent,
    InvestigationCaseEvidenceAttachedEvent,
    InvestigationCaseClosedEvent,
    PublicAccusationCorrectionRecordedEvent,
    AccusationRepairOfferRecordedEvent,
    AccusationRepairResolvedEvent,
    ShipmentDispatchedEvent,
    ShipmentArrivalDeliveredEvent,
    ShipmentArrivalDeferredEvent,
    ShipmentReceiptAcceptedEvent,
    ShipmentSpoilageResolvedEvent,
    HistoricalEvidenceDestroyedEvent,
    HealthRestoredEvent,
    InjuryTreatedEvent,
    LifeEndedEvent,
    PlayerCharacterContinuedEvent,
    CohortMemberPromotedEvent,
    PopulationYearAdvancedEvent,
    OrganizationMembershipReviewedEvent,
    GovernanceSuccessionEvent,
    GovernanceLeadershipHandedOverEvent,
    OrganizationMemberResignedEvent,
    ProductionQuotaRenewedEvent,
    ScheduledEventCompletedEvent,
    KnowledgeAcquiredEvent,
    MovementCompletedEvent,
    MarketTradeExecutedEvent,
    RelationDimensionChangedEvent,
    RelationEstablishedEvent,
    ResourceConsumedEvent,
    ResourceAccountOpenedEvent,
    ResourceProducedEvent,
    RenewableResourceHarvestedEvent,
    ImprovisedCheckResolvedEvent,
)
from .cultivation_breakthrough_events import CultivationBreakthroughResolvedEvent

type DomainEvent = Annotated[
    EntityCreatedEvent
    | EntityFieldChangedEvent
    | ResourceTransferredEvent
    | TimeAdvancedEvent
    | MovementCompletedEvent
    | MarketTradeExecutedEvent
    | EntityDiscoveredEvent
    | ImprovisedCheckResolvedEvent
    | CultivationBreakthroughResolvedEvent
    | CombatExchangeResolvedEvent
    | CombatRetreatResolvedEvent
    | DamageAppliedEvent
    | HealthRestoredEvent
    | InjuryTreatedEvent
    | LifeEndedEvent
    | PlayerCharacterContinuedEvent
    | CohortMemberPromotedEvent
    | PopulationYearAdvancedEvent
    | OrganizationMembershipReviewedEvent
    | BackgroundNeedsAdvancedEvent
    | BackgroundRoutineAdvancedEvent
    | BackgroundDeprivationResolvedEvent
    | GovernanceSuccessionEvent
    | GovernanceLeadershipHandedOverEvent
    | LeadershipBallotChangedEvent
    | LeadershipVacancyReviewRecordedEvent
    | SafeConductChangedEvent
    | OrganizationMemberResignedEvent
    | ProductionQuotaRenewedEvent
    | RenewableResourceHarvestedEvent
    | ScheduledEventCompletedEvent
    | StoryMilestoneResolvedEvent
    | ChallengeResolvedEvent
    | ContestChangedEvent
    | ResourceAccountOpenedEvent
    | EnvironmentalSourceAccountOpenedEvent
    | NpcPlanCreatedEvent
    | NpcPlanRevisedEvent
    | NpcPlanReviewResolvedEvent
    | NpcPlanReviewScheduledEvent
    | ComponentReplacedEvent
    | AuthorComponentRevisedEvent
    | KnowledgeAcquiredEvent
    | RelationEstablishedEvent
    | RelationDimensionChangedEvent
    | GoalCreatedEvent
    | GoalUpdatedEvent
    | InvestigationCaseOpenedEvent
    | InvestigationCaseEvidenceAttachedEvent
    | InvestigationCaseClosedEvent
    | PublicAccusationCorrectionRecordedEvent
    | AccusationRepairOfferRecordedEvent
    | AccusationRepairResolvedEvent
    | CombatTruceChangedEvent
    | ShipmentDispatchedEvent
    | ShipmentArrivalDeliveredEvent
    | ShipmentArrivalDeferredEvent
    | ShipmentReceiptAcceptedEvent
    | ShipmentSpoilageResolvedEvent
    | HistoricalEvidenceDestroyedEvent
    | RelationshipPromiseRecordedEvent
    | PromiseItemReturnedEvent
    | PromiseReleasedEvent
    | PromiseDeadlineBreachedEvent
    | PromiseItemReturnedWithTrustEvent
    | PromiseDeadlineBreachedWithTrustEvent
    | PromiseItemReturnedLateEvent
    | KinshipLinkRecordedEvent
    | KinshipLinkEndedEvent
    | OccupationAppointedEvent
    | OccupationResignedEvent
    | OccupationDeathVacanciesResolvedEvent
    | OccupationLegacyVacanciesResolvedEvent
    | RelationInvitationChangedEvent
    | PrivateGiftChangedEvent
    | MeetingReflectionRecordedEvent
    | RelationshipPartnershipChangedEvent
    | RelationshipDisclosureRecordedEvent
    | ShipmentReliefDistributedEvent
    | TravelJourneyChangedEvent
    | WaypointOpportunityDecisionRecordedEvent
    | NpcMeetingAttendanceDecisionRecordedEvent
    | InstitutionalCreditChangedEvent
    | InstitutionalCreditExtensionChangedEvent
    | InstitutionalTreasuryMobilizedEvent
    | NpcAttendanceStartedEvent
    | NpcAttendanceCancelledEvent
    | NpcAttendanceSettledEvent
    | WorkShiftPaidEvent
    | MarketPriceReviewResolvedEvent
    | ResourceConsumedEvent
    | ResourceProducedEvent
    | WorldBookImportedEvent,
    Field(discriminator="type"),
]


class CommandEnvelope(ContractModel):
    command_id: UUID = Field(default_factory=uuid4)
    actor_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_state_version: int = Field(ge=0)
    command_type: str = Field(min_length=1, max_length=160)
    payload: JsonObject = Field(default_factory=dict)
    causation_ids: list[UUID] = Field(default_factory=list)
    correlation_id: UUID | None = None

    @field_validator("idempotency_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("idempotency key cannot be blank")
        return value

    def payload_hash(self) -> str:
        request = {
            "actor_id": str(self.actor_id),
            "expected_state_version": self.expected_state_version,
            "command_type": self.command_type,
            "payload": self.payload,
            "causation_ids": [str(item) for item in self.causation_ids],
        }
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EventRecord(ContractModel):
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    sequence: int = Field(ge=1)
    tick: int = Field(ge=-(2**63), le=2**63 - 1)
    type: str
    command_id: UUID
    actor_id: UUID
    causation_ids: list[UUID] = Field(default_factory=list)
    correlation_id: UUID | None = None
    ruleset_version: str
    payload: JsonObject
    visibility: JsonObject = Field(default_factory=lambda: {"policy": "author_only"})
    state_version_after: int = Field(ge=1)
    commit_boundary: bool = False
    committed_at: datetime


class EventPage(ContractModel):
    branch_id: UUID
    events: list[EventRecord]
    next_cursor: int = Field(ge=0)
    has_more: bool


class CommitResult(ContractModel):
    command_id: UUID
    branch_id: UUID
    duplicate: bool = False
    state_version: int = Field(ge=0)
    first_seq: int | None = Field(default=None, ge=1)
    last_seq: int | None = Field(default=None, ge=1)
    events: list[EventRecord] = Field(default_factory=list)
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class SnapshotRecord(ContractModel):
    snapshot_id: UUID
    world_id: UUID
    branch_id: UUID
    last_seq: int = Field(ge=0)
    state_version: int = Field(ge=0)
    current_tick: int = Field(ge=-(2**63), le=2**63 - 1)
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: WorldState
    created_at: datetime


class NewJob(ContractModel):
    job_id: UUID = Field(default_factory=uuid4)
    world_id: UUID
    branch_id: UUID
    job_type: str = Field(min_length=1, max_length=160)
    idempotency_key: str = Field(min_length=1, max_length=200)
    payload: JsonObject = Field(default_factory=dict)


class JobRecord(ContractModel):
    job_id: UUID
    tenant_id: UUID
    world_id: UUID
    branch_id: UUID
    command_id: UUID | None = None
    job_type: str
    status: JobStatus
    payload: JsonObject
    attempt_count: int = Field(ge=0)
    fencing_token: int = Field(ge=0)
    committed_through_tick: int | None = Field(default=None, ge=-(2**63), le=2**63 - 1)
    result: JsonObject | None = None
    created_at: datetime
    updated_at: datetime


class JobLease(ContractModel):
    job_id: UUID
    world_id: UUID
    branch_id: UUID
    worker_id: str = Field(min_length=1, max_length=200)
    fencing_token: int = Field(ge=1)
    expires_at: datetime


class OutboxRecord(ContractModel):
    outbox_id: UUID
    event_id: UUID
    world_id: UUID
    branch_id: UUID
    topic: str
    payload: JsonObject
    attempts: int = Field(ge=0)
    created_at: datetime
    published_at: datetime | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _decimal_shape(value: Decimal) -> tuple[int, int]:
    """Return scale and integer digits without context-sensitive normalization."""
    digits = list(value.as_tuple().digits)
    exponent = value.as_tuple().exponent
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    scale = max(0, -exponent)
    integer_digits = max(1, len(digits) + exponent)
    return scale, integer_digits


def _jsonable_component_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("component object keys must be strings")
        return {key: _jsonable_component_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable_component_value(item) for item in value]
    return value


def _validate_reference_graph(entities: dict[UUID, WorldEntity]) -> None:
    """Validate optional location and containment references form an acyclic graph."""
    parents: dict[UUID, set[UUID]] = {}
    for entity_id, entity in entities.items():
        if entity.location_id is not None:
            parents.setdefault(entity_id, set()).add(entity.location_id)
        parent_id = entity.components.get("parent_id")
        place_component = _component_as(entity, "place", PlaceComponent)
        place_parent = str(place_component.parent_id) if place_component and place_component.parent_id else None
        if parent_id is not None and place_parent is not None and parent_id != place_parent:
            raise ValueError(f"entity {entity_id} component parent_id values disagree")
        parent_id = parent_id or place_parent
        if parent_id is None:
            continue
        if not isinstance(parent_id, str):
            raise ValueError(f"entity {entity_id} parent_id must be a UUID string")
        try:
            parent = UUID(parent_id)
        except ValueError as exc:
            raise ValueError(f"entity {entity_id} parent_id is not a UUID") from exc
        if parent == entity_id:
            raise ValueError(f"entity {entity_id} cannot contain itself")
        if parent not in entities:
            raise ValueError(f"entity {entity_id} references a missing parent entity")
        parents.setdefault(entity_id, set()).add(parent)

    visiting: set[UUID] = set()
    visited: set[UUID] = set()

    def visit(node: UUID) -> None:
        if node in visiting:
            raise ValueError("entity location or containment graph contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for parent in parents.get(node, set()):
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    for origin in entities:
        visit(origin)


def _component_as(entity: WorldEntity, name: str, model_type: type[Any]) -> Any | None:
    value = entity.components.get(name)
    if value is None:
        return None
    try:
        return model_type.model_validate(value)
    except Exception as exc:
        raise ValueError(f"entity {entity.id} has an invalid {name} component: {exc}") from exc


def _validate_entity_components(
    entities: dict[UUID, WorldEntity],
    resources: dict[UUID, ResourceAccount],
    current_tick: int,
    day_seconds: int,
) -> None:
    """Validate references across registered components after the entity map is complete."""
    place_kinds = {"place", "location"}
    population_member_counts: dict[UUID, int] = {}
    validated_populations: dict[UUID, PopulationComponent | None] = {}
    renewable_source_owners: dict[UUID, UUID] = {}
    renewable_recipient_accounts: dict[UUID, tuple[UUID, str]] = {}

    def require_character(reference: UUID, description: str) -> None:
        target = entities.get(reference)
        if target is None or target.kind not in {"character", "person"}:
            raise ValueError(f"{description} must reference an existing character")

    def require_place(reference: UUID, description: str) -> None:
        target = entities.get(reference)
        if target is None or target.kind not in place_kinds:
            raise ValueError(f"{description} must reference an existing place")

    def population_for(entity: WorldEntity) -> PopulationComponent | None:
        if entity.id not in validated_populations:
            validated_populations[entity.id] = _component_as(entity, "population", PopulationComponent)
        return validated_populations[entity.id]

    for entity_id, entity in entities.items():
        components = entity.components
        combat_encounter = _component_as(entity, "combat_encounter", CombatEncounterComponent)
        if combat_encounter is not None:
            require_character(combat_encounter.opponent_id, f"combat encounter {entity_id} opponent_id")
            require_place(combat_encounter.location_id, f"combat encounter {entity_id} location_id")
        routine = _component_as(entity, "daily_routine", DailyRoutineComponent)
        if routine is not None:
            for window in routine.windows:
                if window.start_second + window.duration_seconds > day_seconds:
                    raise ValueError(f"daily routine window {window.window_id} must fit within one calendar day")
                require_place(window.place_id, f"daily routine window {window.window_id} place_id")
                if entities[window.place_id].status in {"dead", "retired"}:
                    raise ValueError(f"daily routine window {window.window_id} cannot reference a retired place")
        mobility = _component_as(entity, "routine_mobility", RoutineMobilityComponent)
        if mobility is not None:
            windows = {window.window_id: window for window in routine.windows} if routine is not None else {}
            if not mobility.cadences.keys() <= windows.keys():
                raise ValueError("routine mobility cadence must reference an existing daily window")
            for window_id, alternatives in mobility.alternatives.items():
                if window_id not in windows:
                    raise ValueError(f"routine mobility references unknown daily window {window_id}")
                for place_id in alternatives:
                    require_place(place_id, f"routine mobility alternative for {window_id}")
                    if place_id == windows[window_id].place_id:
                        raise ValueError(f"routine mobility alternative for {window_id} must differ from its main place")

        life = _component_as(entity, "life", LifeComponent)
        if life is not None and life.birth_tick > current_tick:
            raise ValueError(f"entity {entity_id} has a future birth_tick")
        if life is not None and life.population_origin is not None:
            if life.population_group_id != life.population_origin.population_group_id:
                raise ValueError(f"entity {entity_id} population origin does not match its life cohort")
        if life is not None and life.population_group_id is not None:
            population_group = entities.get(life.population_group_id)
            if population_group is None or population_group.kind not in {"group", "population", "population_group", "cohort"}:
                raise ValueError(f"entity {entity_id} references a missing population group")
            population = population_for(population_group)
            if population is None or population.species_id != life.species_id:
                raise ValueError(f"entity {entity_id} species does not match its population group")
            if population.unmodeled_headcount is None:
                raise ValueError(f"entity {entity_id} belongs to a group without a typed unmodeled population pool")
            if entity.status not in {"dead", "retired"}:
                population_member_counts[life.population_group_id] = (
                    population_member_counts.get(life.population_group_id, 0) + 1
                )

        health = _component_as(entity, "health", HealthComponent)
        if health is not None:
            if any(item.started_tick > current_tick for item in health.injuries):
                raise ValueError(f"entity {entity_id} has an injury from the future")
            if health.current == 0 and entity.status not in {"dead", "retired"}:
                raise ValueError(f"entity {entity_id} has zero health but is not terminal")
            if entity.status == "dead" and health.current != 0:
                raise ValueError(f"dead entity {entity_id} must have zero health")
            if health.incapacitating_injury_id is not None and entity.status != "incapacitated":
                raise ValueError(f"entity {entity_id} must be incapacitated while it has an incapacitating injury")

        needs = _component_as(entity, "needs", NeedsComponent)
        if needs is not None:
            need_values = [getattr(needs, name) for name in (
                "food", "water", "rest", "shelter", "safety", "health", "social", "belonging"
            )]
            need_values.extend(needs.custom.values())
            if any(need and need.last_satisfied_tick is not None and need.last_satisfied_tick > current_tick for need in need_values):
                raise ValueError(f"entity {entity_id} has a need updated in the future")

        place = _component_as(entity, "place", PlaceComponent)
        if place is not None:
            if place.region_id is not None:
                require_place(place.region_id, f"place {entity_id} region_id")
            if place.parent_id is not None:
                require_place(place.parent_id, f"place {entity_id} parent_id")
            for route in place.routes:
                if route.from_place_id != entity_id:
                    raise ValueError(f"route {route.route_id} must start at its containing place")
                require_place(route.to_place_id, f"route {route.route_id} destination")
            if place.renewable_harvests and place.place_type != "region":
                raise ValueError(f"place {entity_id} renewable harvest policies must belong to a region")
            for policy in place.renewable_harvests:
                source = resources.get(policy.source_account_id)
                if (
                    source is None
                    or source.account_type != "environmental_source"
                    or source.owner_id != entity_id
                    or source.resource_id != policy.resource_id
                    or source.credit_limit is None
                    or source.credit_limit != policy.issued_total
                    or source.balance.copy_negate() != policy.issued_total
                ):
                    raise ValueError(f"region {entity_id} renewable policy does not match its finite environmental source")
                prior_owner = renewable_source_owners.setdefault(policy.source_account_id, entity_id)
                if prior_owner != entity_id:
                    raise ValueError(f"environmental source {policy.source_account_id} cannot be shared by regions")
                for recipient in policy.recipients:
                    target = entities.get(recipient.entity_id)
                    account = resources.get(recipient.account_id)
                    if (
                        target is None
                        or target.kind not in {"character", "person"}
                        or account is None
                        or account.account_type != "holder"
                        or account.owner_id != recipient.entity_id
                        or account.resource_id != policy.resource_id
                    ):
                        raise ValueError(
                            f"region {entity_id} renewable recipient must reference a matching character-owned holder account"
                        )
                    if target.status not in {"dead", "retired"}:
                        try:
                            inventory = InventoryComponent.model_validate(target.components.get("inventory"))
                        except Exception as exc:
                            raise ValueError(
                                f"renewable recipient {recipient.entity_id} must have a typed inventory component"
                            ) from exc
                        if not any(
                            lot.account_id == recipient.account_id and lot.resource_id == policy.resource_id
                            for lot in inventory.lots
                        ) and account.balance != 0:
                            raise ValueError(
                                f"renewable recipient {recipient.entity_id} holder balance requires matching inventory lots"
                            )
                    previous_recipient = renewable_recipient_accounts.setdefault(
                        recipient.account_id, (recipient.entity_id, policy.resource_id)
                    )
                    if previous_recipient != (recipient.entity_id, policy.resource_id):
                        raise ValueError("a renewable recipient holder account cannot serve multiple entities or resources")

        governance = _component_as(entity, "governance", GovernanceComponent)
        if governance is not None:
            for policy in governance.membership_recruitment:
                region = entities.get(policy.region_id)
                if (
                    entity.location_id != policy.region_id
                    or region is None
                    or region.kind not in place_kinds
                ):
                    raise ValueError(
                        f"organization {entity_id} membership policy must reference its registered regional location"
                    )
            for member_id in governance.members:
                if member_id not in entities:
                    raise ValueError(f"organization {entity_id} has a missing member entity")
            if governance.leader_id is not None:
                require_character(governance.leader_id, f"organization {entity_id} leader_id")
            for office in governance.offices:
                if office.holder_id is not None:
                    require_character(office.holder_id, f"organization {entity_id} office holder")

        production = _component_as(entity, "production", ProductionComponent)
        if production is not None:
            for quota in production.quotas:
                account = resources.get(quota.source_account_id)
                if (
                    account is None
                    or account.account_type != "production_source"
                    or account.owner_id != entity_id
                    or account.resource_id != quota.resource_id
                    or account.credit_limit is None
                ):
                    raise ValueError(f"producer {entity_id} quota does not match a finite owned source account")
                with localcontext() as context:
                    context.prec = 80
                    expected_limit = quota.base_credit_limit + quota.issued_total
                if account.credit_limit != expected_limit:
                    raise ValueError(f"producer {entity_id} quota allowance diverges from its source account")

        treasury = _component_as(entity, "treasury", TreasuryComponent)
        if treasury is not None:
            for resource_id, account_id in treasury.accounts.items():
                account = resources.get(account_id)
                if account is None or account.resource_id != resource_id or account.owner_id != entity_id:
                    raise ValueError(f"organization {entity_id} treasury account does not match its resource or owner")

        market = _component_as(entity, "market", MarketComponent)
        if market is not None:
            for offer in market.offers:
                if offer.seller_id not in entities:
                    raise ValueError(f"market {entity_id} seller_id must reference an existing entity")
                account = resources.get(offer.stock_account_id)
                if account is None or account.resource_id != offer.resource_id or account.owner_id != offer.seller_id:
                    raise ValueError(f"market offer {offer.listing_id} stock account does not match its seller/resource")

        inventory = _component_as(entity, "inventory", InventoryComponent)
        if inventory is not None:
            totals: dict[UUID, Decimal] = {}
            with localcontext() as context:
                # Inventory components allow up to 100,000 lots; 50 digits
                # preserve each bounded amount through the full aggregation.
                context.prec = 50
                for lot in inventory.lots:
                    account = resources.get(lot.account_id)
                    if account is None or account.resource_id != lot.resource_id or account.owner_id != entity_id:
                        raise ValueError(f"inventory lot {lot.lot_id} account does not match its holder/resource")
                    totals[lot.account_id] = totals.get(lot.account_id, Decimal("0")) + lot.quantity
            for account_id, quantity in totals.items():
                if resources[account_id].balance < quantity:
                    raise ValueError(f"inventory lots exceed account balance for {account_id}")

    for entity_id, entity in entities.items():
        population = population_for(entity)
        if population is not None and population.unmodeled_headcount is not None:
            total = population.unmodeled_headcount + population_member_counts.get(entity_id, 0)
            if population.headcount != total:
                raise ValueError(
                    f"population group {entity_id} total headcount must equal its unmodeled pool plus linked living members"
                )

    for item in entities.values():
        knowledge_locations = item.components.get("known_places")
        if isinstance(knowledge_locations, list):
            for place_id in knowledge_locations:
                try:
                    parsed_id = UUID(str(place_id))
                except ValueError as exc:
                    raise ValueError(f"entity {item.id} known_places contains an invalid UUID") from exc
                require_place(parsed_id, f"entity {item.id} known_places")
