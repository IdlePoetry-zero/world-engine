"""Pure, deterministic rules for explicit player actions.

This layer has no repository, model-provider, or process-global randomness access.
It returns a complete event batch for the application to commit atomically.
All elapsed ticks are seconds, as defined by CalendarConfig and the world contract.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from hashlib import sha256
from typing import Iterable
from uuid import UUID, uuid5

from world_engine.domain.leadership_ballot_events import LeadershipBallotChangedEvent, leadership_ballot_event_id
from world_engine.domain.leadership_ballots import (
    LeadershipBallotError, cast_leadership_ballot_vote, close_leadership_ballot, open_leadership_ballot,
    open_vacant_leadership_ballot,
)
from world_engine.domain.safe_conduct import (
    SAFE_CONDUCT_TAG, propose_safe_conduct, registered_safe_conduct_route, safe_conduct_allows_route,
    transition_safe_conduct,
)
from world_engine.domain.safe_conduct_events import SafeConductChangedEvent, safe_conduct_event_id

from world_engine.domain.market_pricing import PRICE_REVIEW_HANDLER_ID, MarketPriceInputError
from world_engine.domain.institutional_credit import InstitutionalCreditError
from world_engine.domain.institutional_credit_state import CREDIT_MATURITY_HANDLER, plan_institutional_credit_maturity_event
from world_engine.domain.promise_trust_settlement import build_trusted_promise_settlement
from world_engine.domain.story_nodes import STORY_NODE_HANDLER, settle_story_milestone
from world_engine.domain.shipment_relief import plan_shipment_relief
from world_engine.domain.shipment_relief_events import (
    ShipmentReliefDistributedEvent,
    shipment_relief_event_id,
    shipment_relief_visibility,
)
from world_engine.domain.travel_journey_events import (
    TravelJourneyChangedEvent,
    travel_event_id,
    travel_journey_visibility,
)
from world_engine.domain.travel_journeys import (
    TravelHazardTiming, active_travel_journey, delay_travel_journey, plan_travel_departure,
    plan_travel_resolution,
)
from world_engine.domain.cultivation_breakthrough import (
    CULTIVATION_BREAKTHROUGH_ROLL_LABEL,
    CultivationBreakthroughError,
    registered_cultivation_breakthrough_policy,
    plan_cultivation_breakthrough,
)
from world_engine.domain.cultivation_breakthrough_events import breakthrough_effect_events
from world_engine.domain.ruleset_versions import (
    CULTIVATION_DISABLED_CODE,
    CULTIVATION_DISABLED_MESSAGE,
)
from world_engine.domain.relationship_invitation_events import (
    RelationInvitationChangedEvent,
    invitation_event_id,
    participant_visibility,
)
from world_engine.domain.relationship_reflection_events import (
    MeetingReflectionRecordedEvent,
    meeting_reflection_event_id,
    meeting_reflection_visibility,
)
from world_engine.domain.relationship_invitations import (
    InvitationMeetingUnavailable,
    accept_relationship_invitation,
    attend_relationship_meeting,
    decline_relationship_invitation,
    end_relationship_invitation,
    propose_relationship_invitation,
    withdraw_relationship_invitation,
)
from world_engine.domain.relationship_reflections import plan_meeting_reflection
from world_engine.domain.relationship_partnership_events import (
    RelationshipPartnershipChangedEvent,
    partnership_event_id,
    partnership_visibility,
)
from world_engine.domain.relationship_disclosure_events import plan_relationship_disclosure
from world_engine.domain.relationship_partnerships import plan_relationship_partnership
from world_engine.domain.occupation_events import OccupationAppointedEvent, OccupationResignedEvent, occupation_event_id
from world_engine.domain.occupation_settlement import plan_occupation_change
from world_engine.domain.work_shift import (
    WorkShiftError, plan_paid_work_shift, registered_work_shift_terms, select_wage_holder_account,
)
from world_engine.domain.work_shift_events import WorkShiftPaidEvent, work_shift_paid_event_id
from world_engine.domain.components import (
    AttributesComponent,
    CapabilitiesComponent,
    CombatEncounterComponent,
    DailyRoutineComponent,
    GoalRecord,
    HealthComponent,
    GovernanceComponent,
    InventoryComponent,
    InventoryLot,
    KnowledgeRecord,
    LifeComponent,
    MarketComponent,
    NeedValue,
    NeedsComponent,
    HistoricalEvidenceArtifact,
    PlaceComponent,
    PlaceRoute,
    ProductionRecipe,
    PopulationComponent,
    ProductionComponent,
    RenewableHarvestPolicy,
    ProgressionComponent,
    RelationRecord,
    ResourceName,
    ScheduledEventRecord,
    SkillsComponent,
    TreasuryComponent,
    # The component is registered on shipment entities by the domain layer.
    is_leadership_office,
)
from world_engine.domain.adjudication import (
    IMPROVISED_EXAMINE_DURATION,
    IMPROVISED_EXAMINE_POLICY,
    IMPROVISED_EXAMINE_ROLL_LABEL,
    HISTORICAL_EVIDENCE_OBSERVATION_SUMMARY,
    improvised_examine_chance,
    historical_evidence_digest,
    source_text_digest,
)
from world_engine.domain.population_seats import plan_population_seat_promotions
from world_engine.domain.organization_recruitment import (
    build_membership_recruitment_review,
    plan_membership_recruitment,
)
from world_engine.domain.investigation import (
    ASK_NPC_CLAIM_POLICY,
    FALLIBLE_CLAIM_KINDS,
    WEIGH_EVIDENCE_DURATION,
    WEIGH_EVIDENCE_POLICY,
    EvidenceAssessment,
    assessment_text,
    compare_evidence_sources,
    entity_visible_to_actor,
    validate_disclosure_record,
    validate_assessment_record,
)
from world_engine.domain.investigation_cases import (
    InvestigationCaseFile,
    case_anchor_is_local_and_available,
    case_closure_outcome,
    case_definition_digest,
    case_definition_from_anchor,
    matched_case_routes,
    validate_case_assessment_access,
)
from world_engine.domain.evidence_destruction import validate_destroyable_historical_item
from world_engine.domain.shipment import (
    ShipmentAccountFlow,
    ShipmentCargoManifest,
    ShipmentComponent,
    ShipmentLotManifest,
    ShipmentLotReceipt,
    ShipmentSpoiledLot,
    active_shipments_on_route,
    plan_dispatch_lots,
    shipment_destination_available,
)
from world_engine.domain.errors import DomainError
from world_engine.domain.accusation_repair import plan_accusation_repair_offer, plan_accusation_repair_resolution
from world_engine.domain.public_accusation_corrections import plan_public_accusation_correction
from world_engine.domain.gameplay_events import (
    AuthorComponentRevisedEvent,
    BackgroundAccountExpectation,
    BackgroundConsumptionLot,
    BackgroundDeprivationResolvedEvent,
    BackgroundDeprivationSubject,
    BackgroundNeedRevisionExpectation,
    BackgroundNeedsAdvancedEvent,
    BackgroundRoutineAdvancedEvent,
    BackgroundRoutineAgentExpectation,
    BackgroundRoutineNeedExpectation,
    ComponentReplacedEvent,
    ConsumedResourceFlow,
    CombatExchangeResolvedEvent,
    CombatRetreatResolvedEvent,
    DamageAppliedEvent,
    EntityDiscoveredEvent,
    GoalCreatedEvent,
    GoalUpdatedEvent,
    GovernanceLeadershipHandedOverEvent,
    ImprovisedCheckResolvedEvent,
    InvestigationCaseOpenedEvent,
    InvestigationCaseEvidenceAttachedEvent,
    InvestigationCaseClosedEvent,
    HistoricalEvidenceDestroyedEvent,
    ShipmentDispatchedEvent,
    ShipmentArrivalDeliveredEvent,
    ShipmentArrivalDeferredEvent,
    ShipmentReceiptAcceptedEvent,
    ShipmentSpoilageResolvedEvent,
    HealthRestoredEvent,
    InjuryTreatedEvent,
    LifeEndedEvent,
    KnowledgeAcquiredEvent,
    MarketTradeExecutedEvent,
    MovementCompletedEvent,
    OrganizationMemberResignedEvent,
    ProductionQuotaRenewedEvent,
    RenewableHarvestExpectation,
    RenewableHarvestRecipientExpectation,
    RenewableResourceHarvestedEvent,
    PopulationRevisionExpectation,
    PopulationYearAdvancedEvent,
    OrganizationMembershipReviewedEvent,
    ScheduledEventCompletedEvent,
    ProducedResourceFlow,
    ProvenanceReference,
    RelationDimensionChangedEvent,
    RelationEstablishedEvent,
    ResourceAccountOpenedEvent,
    ResourceConsumedEvent,
    ResourceConversion,
    ResourceProducedEvent,
)
from world_engine.domain.models import (
    DomainEvent,
    EntityFieldChangedEvent,
    ResourceAccount,
    ResourceTransferredEvent,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)
from world_engine.domain.inventory import allocate_fifo_lots
from world_engine.domain.combat import (
    COMBAT_ATTACK_POLICY,
    COMBAT_ATTACK_ROLL_LABEL,
    COMBAT_EXCHANGE_DURATION,
    active_combat_opponent,
    combat_attack_chance,
    combat_damage,
    combat_death_protection_enabled,
)
from world_engine.domain.combat_truce import CombatTruceChangedEvent, plan_combat_truce
from world_engine.domain.private_gifts import plan_private_gift_offer, plan_private_gift_resolution
from world_engine.domain.treatment import (
    INJURY_TREATMENT_DURATION,
    INJURY_TREATMENT_HEALING,
    INJURY_TREATMENT_MIN_CARE,
    INJURY_TREATMENT_POLICY,
    care_skill,
    is_medicine_resource,
    settled_treatment_place,
)
from world_engine.domain.background_routine import advance_background_routine
from world_engine.domain.npc_plans import NPC_EXPLORATION_TICKS, NPC_TRADE_TICKS, NpcTimedExecution, occupied_npc_ids
from world_engine.domain.needs import next_deprivation_damage_tick, resource_matches_need
from world_engine.domain.deprivation import resolve_due_deprivation
from world_engine.domain.reducers import apply_event

from .contracts import (
    ACTION_INTENT_ADAPTER,
    ActionIntent,
    AttemptCultivationBreakthroughIntent,
    AttendRelationshipMeetingIntent,
    AskNpcClaimIntent,
    OpenInvestigationCaseIntent,
    AttachCaseEvidenceIntent,
    CloseInvestigationCaseIntent,
    CorrectPublicAccusationIntent,
    OfferAccusationRepairIntent,
    ResolveAccusationRepairIntent,
    DestroyHistoricalEvidenceIntent,
    DistributeShipmentReliefIntent,
    DispatchShipmentIntent,
    ReceiveShipmentIntent,
    RecordReturnPromiseIntent,
    FulfillReturnPromiseIntent,
    LateReturnPromiseIntent,
    ReleaseReturnPromiseIntent,
    OfferPrivateGiftIntent,
    ResolvePrivateGiftIntent,
    TakeJobIntent,
    ResignJobIntent,
    WorkShiftIntent,
    BackgroundAgentChoice,
    AttackIntent,
    ConsumeIntent,
    DiscloseRelationshipPartnershipIntent,
    ExploreIntent,
    HandoverLeadershipIntent,
    OpenLeadershipBallotIntent,
    OpenVacantLeadershipBallotIntent,
    CastLeadershipBallotVoteIntent,
    CloseLeadershipBallotIntent,
    ProposeSafeConductIntent,
    OfferCombatTruceIntent,
    ResolveCombatTruceIntent,
    ResolveSafeConductIntent,
    ImprovisedCheckIntent,
    LeaveOrganizationIntent,
    MoveIntent,
    RetreatIntent,
    ProduceIntent,
    RestIntent,
    ProposeRelationshipInvitationIntent,
    ProposeRelationshipPartnershipIntent,
    RecordMeetingReflectionIntent,
    ResolveRelationshipInvitationIntent,
    ResolveRelationshipPartnershipIntent,
    UpdateRelationshipPartnershipProposalIntent,
    RuleContext,
    RuleInterruption,
    RuleOutcome,
    RuleResolution,
    RuleRoll,
    SpeakIntent,
    TrainIntent,
    TradeIntent,
    OfferInstitutionalCreditIntent,
    ResolveInstitutionalCreditIntent,
    MobilizeTreasuryReserveIntent,
    ProposeCreditExtensionIntent,
    ResolveCreditExtensionIntent,
    TreatInjuryIntent,
    WaitIntent,
    WeighEvidenceIntent,
    parse_action,
)
from .promise_actions import (
    build_record_return_promise_event, build_fulfill_return_promise_event,
    build_late_return_promise_event,
    build_release_return_promise_event, build_scheduled_promise_breach_event,
)
from .market_pricing import resolve_market_price_review
from .speech import SpeechActionError, build_private_speech_events
from .versions import LEGACY_RULESET_VERSION, cultivation_disabled_by_world_rules


RULES_VERSION = LEGACY_RULESET_VERSION
_DAY_SECONDS = 86_400
_HOUR_SECONDS = 3_600
_CONSUMPTION_SINK_TYPES = {"consumption_sink"}
_CAPTURE_VALIDATED_PROJECTION: ContextVar[bool] = ContextVar(
    "world_engine_capture_validated_projection", default=False,
)


class RuleInvariantError(RuntimeError):
    """Raised when an internal rule produces a batch rejected by domain reducers."""


class RuleEngine:
    """Evaluate one typed semantic intent without mutating or persisting state."""

    _capture_itinerary_care_projection = False

    @contextmanager
    def _capture_validated_projection(self):
        """Retain the validated final state for one trusted in-process caller."""

        token = _CAPTURE_VALIDATED_PROJECTION.set(True)
        try:
            yield
        finally:
            _CAPTURE_VALIDATED_PROJECTION.reset(token)

    def resolve_scheduled(
        self,
        state: WorldState,
        scheduled: ScheduledEventRecord,
        context: RuleContext,
    ) -> RuleResolution:
        """Resolve one registered due-time handler as a replayable event batch."""
        current = next((item for item in state.scheduled_events if item.id == scheduled.id), None)
        if current != scheduled or scheduled.status != "queued":
            return self._rejected(context, "scheduled_event_stale", "The scheduled event is no longer queued in this state.")
        if scheduled.due_tick != state.current_tick:
            return self._rejected(context, "scheduled_event_not_due", "Scheduled handlers resolve at their exact world-time boundary.")
        by_id = {item.id: item for item in state.scheduled_events}
        if any(by_id[dependency].status != "completed" for dependency in scheduled.dependencies):
            return self._rejected(context, "scheduled_dependencies_pending", "The scheduled event has unfinished dependencies.")
        if scheduled.handler_id == STORY_NODE_HANDLER:
            event = settle_story_milestone(state, scheduled, self._event_id(context, f"story.milestone:{scheduled.id}"))
            return self._resolution(
                state, context, status="succeeded", code="story_milestone_resolved",
                message="A story milestone reached its scheduled boundary.", events=[event],
            )
        if scheduled.handler_id == "population.year_advance":
            return self._resolve_population_year(state, scheduled, context)
        if scheduled.handler_id == "environmental.harvest":
            return self._resolve_renewable_harvest(state, scheduled, context)
        if scheduled.handler_id == "shipment.arrival":
            return self._resolve_shipment_arrival(state, scheduled, context)
        if scheduled.handler_id == "shipment.spoilage":
            return self._resolve_shipment_spoilage(state, scheduled, context)
        if scheduled.handler_id == CREDIT_MATURITY_HANDLER:
            try:
                event = plan_institutional_credit_maturity_event(
                    state, scheduled, world_id=context.world_id, branch_id=context.branch_id,
                    command_id=context.command_id, actor_id=context.actor_id, ruleset_version=context.ruleset_version,
                )
            except (DomainError, InstitutionalCreditError):
                return self._rejected(context, "credit_maturity_stale", "The exact credit maturity cannot be settled.")
            return self._resolution(
                state, context, status="succeeded", code="institutional_credit_overdue",
                message="An outstanding institutional credit reached its overdue boundary.", events=[event],
            )
        if scheduled.handler_id == PRICE_REVIEW_HANDLER_ID:
            try:
                event = resolve_market_price_review(
                    state, scheduled, world_id=context.world_id, branch_id=context.branch_id,
                    command_id=context.command_id, ruleset_version=context.ruleset_version,
                )
            except MarketPriceInputError:
                return self._rejected(context, "market_price_review_invalid", "The exact market price review cannot be resolved.")
            return self._resolution(state, context, status="succeeded", code="market_prices_reviewed",
                                    message="Market quotes were reviewed against current stock.", events=[event])
        if scheduled.handler_id == "relationship_promise.deadline":
            try:
                event = build_trusted_promise_settlement(state, build_scheduled_promise_breach_event(state, context, scheduled))
            except DomainError:
                return self._rejected(context, "promise_deadline_invalid", "The exact promise deadline cannot be resolved.")
            return self._resolution(state, context, status="succeeded", code="promise_breached",
                                    message="The unfulfilled promise reached its deadline.", events=[event])
        if scheduled.handler_id != "production.quota_renewal" or scheduled.handler_version != "1.0.0":
            return self._rejected(context, "scheduled_handler_unregistered", "No versioned rule handler is registered for this schedule.")
        if set(scheduled.payload) != {"producer_id", "quota_id"}:
            return self._rejected(context, "scheduled_payload_invalid", "Quota renewal payload must contain only producer_id and quota_id.")
        try:
            producer_id = UUID(str(scheduled.payload["producer_id"]))
            quota_id = str(scheduled.payload["quota_id"])
        except (TypeError, ValueError):
            return self._rejected(context, "scheduled_payload_invalid", "Quota renewal schedule identifiers are invalid.")
        producer = state.entities.get(producer_id)
        if producer is None:
            return self._rejected(context, "quota_producer_missing", "The scheduled quota producer does not exist.")
        try:
            production = ProductionComponent.model_validate(producer.components.get("production"))
        except Exception:
            return self._rejected(context, "quota_policy_missing", "The scheduled producer has no valid quota policy.")
        quota = next((item for item in production.quotas if item.quota_id == quota_id), None)
        if quota is None:
            return self._rejected(context, "quota_policy_missing", "The scheduled quota policy is not registered.")
        if scheduled.repeat_ticks is not None and scheduled.repeat_ticks != quota.period_ticks:
            return self._rejected(context, "quota_schedule_mismatch", "Recurring quota schedule interval must match its typed policy.")
        completion = ScheduledEventCompletedEvent(
            event_id=self._event_id(context, f"scheduled.completed:{scheduled.id}"),
            scheduled_event_id=scheduled.id,
            expected_revision=scheduled.revision,
        )
        if quota.issued_total >= quota.maximum_renewal:
            return self._resolution(
                state,
                context,
                status="succeeded",
                code="production_quota_exhausted",
                message="The finite renewal allowance is exhausted; the due schedule occurrence was recorded without issuing quota.",
                events=[completion],
            )
        period_index = state.current_tick // quota.period_ticks
        if period_index != quota.last_issued_period + 1:
            return self._rejected(context, "quota_period_sequence", "Quota periods must be renewed once, in sequence.")
        renewal = ProductionQuotaRenewedEvent(
            event_id=self._event_id(context, f"quota.renewed:{scheduled.id}"),
            producer_id=producer_id,
            quota_id=quota.quota_id,
            expected_production_revision=production.revision,
            period_index=period_index,
            scheduled_event_id=scheduled.id,
            provenance=[ProvenanceReference(kind="rule", reference_id="production.quota_renewal.v1")],
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="production_quota_renewed",
            message="One finite quota slice was issued from the producer's registered policy.",
            events=[renewal, completion],
        )

    def resolve_background_deprivation(
        self,
        state: WorldState,
        context: RuleContext,
        *,
        include_actor: bool = False,
    ) -> RuleResolution:
        """Apply all exact, already-due nonacting NPC deprivation consequences."""
        subjects: list[BackgroundDeprivationSubject] = []
        for entity in sorted(state.entities.values(), key=lambda item: str(item.id)):
            if (
                not include_actor and entity.id == context.actor_id
                or entity.kind not in {"character", "person"}
                or entity.status in {"dead", "retired"}
                or entity.components.get("needs") is None
            ):
                continue
            try:
                needs = self._needs(entity)
                if needs is None:
                    continue
            except Exception as exc:
                raise RuleInvariantError("An active character has an invalid registered needs component.") from exc
            boundary = next_deprivation_damage_tick(
                needs,
                from_tick=state.current_tick,
                day_seconds=state.calendar.day_seconds,
            )
            if boundary is None or boundary > state.current_tick:
                continue
            consequence = resolve_due_deprivation(
                needs,
                at_tick=state.current_tick,
                day_seconds=state.calendar.day_seconds,
            )
            if consequence.damage <= 0:
                continue
            health = self._health(entity)
            terminal = health is not None and consequence.damage >= health.current
            population_revision = None
            if terminal:
                population_revision = self._population_revision(state, entity)
            subjects.append(
                BackgroundDeprivationSubject(
                    entity_id=entity.id,
                    boundary_tick=boundary,
                    expected_needs_revision=needs.revision,
                    expected_health_revision=health.revision if health is not None else None,
                    expected_population_revision=population_revision,
                    expected_damage=consequence.damage,
                    terminal_status="dead" if terminal else "active",
                )
            )
        if not subjects:
            return self._rejected(
                context,
                "background_deprivation_not_due",
                "No nonacting character has an unresolved deprivation boundary at this tick.",
            )
        event = BackgroundDeprivationResolvedEvent(
            event_id=self._event_id(context, f"background.deprivation:{state.current_tick}"),
            actor_id=context.actor_id,
            include_actor=include_actor,
            resolved_tick=state.current_tick,
            subjects=subjects,
            provenance=[ProvenanceReference(kind="rule", reference_id="needs.deprivation.v1")],
        )
        dead_ids = [str(subject.entity_id) for subject in subjects if subject.terminal_status == "dead"]
        return self._resolution(
            state,
            context,
            status="partial" if dead_ids else "succeeded",
            code="background_deprivation_death" if dead_ids else "background_deprivation_resolved",
            message=(
                "Deprivation consequences ended one or more nonacting characters."
                if dead_ids else "Accumulated nonacting character deprivation consequences were applied."
            ),
            details={
                "subject_ids": [str(subject.entity_id) for subject in subjects],
                "dead_subject_ids": dead_ids,
            },
            events=[event],
        )

    def _resolve_population_year(
        self,
        state: WorldState,
        scheduled: ScheduledEventRecord,
        context: RuleContext,
    ) -> RuleResolution:
        """Advance all registered anonymous age cohorts in one replayable batch."""
        year_seconds = state.calendar.day_seconds * state.calendar.year_days
        if (
            scheduled.handler_version != "1.0.0"
            or scheduled.payload
            or scheduled.repeat_ticks != year_seconds
            or scheduled.due_tick <= 0
            or scheduled.due_tick % year_seconds != 0
            or scheduled.completed_runs + 1 != state.current_tick // year_seconds
        ):
            return self._rejected(
                context,
                "population_schedule_mismatch",
                "Population-year schedule must be an empty-payload finite recurrence at each world-year boundary.",
            )
        age_groups: list[tuple[WorldEntity, PopulationComponent]] = []
        for entity in state.entities.values():
            if entity.kind not in {"group", "population", "population_group", "cohort"}:
                continue
            raw = entity.components.get("population")
            if not isinstance(raw, dict) or not raw.get("age_cohorts"):
                continue
            try:
                population = PopulationComponent.model_validate(raw)
            except Exception:
                return self._rejected(
                    context,
                    "population_component_invalid",
                    "A scheduled population group has an invalid registered age-cohort component.",
                )
            age_groups.append((entity, population))
        age_groups.sort(key=lambda pair: str(pair[0].id))
        if not age_groups:
            return self._rejected(
                context,
                "population_cohorts_missing",
                "No age-structured population cohorts are registered for this yearly schedule.",
            )
        year_index = state.current_tick // year_seconds
        progression = PopulationYearAdvancedEvent(
            event_id=self._event_id(context, f"population.year_advanced:{year_index}"),
            year_index=year_index,
            scheduled_event_id=scheduled.id,
            expected_populations=[
                PopulationRevisionExpectation(cohort_id=entity.id, expected_revision=population.revision)
                for entity, population in age_groups
            ],
            provenance=[ProvenanceReference(kind="rule", reference_id="population.year_advance.v1")],
        )
        events: list[DomainEvent] = [progression]
        membership_summary: dict[str, object] | None = None
        if any(
            isinstance(entity.components.get("governance"), dict)
            and bool(entity.components["governance"].get("membership_recruitment"))
            for entity in state.entities.values()
        ):
            try:
                recruitment_input = build_membership_recruitment_review(
                    state,
                    census_population_revision_increment=1,
                )
                recruitment_plan = plan_membership_recruitment(recruitment_input)
            except Exception as exc:
                return self._rejected(
                    context,
                    "membership_review_input_invalid",
                    f"The annual membership review could not validate its registered policy and resident sources: {exc}",
                )
            if recruitment_plan.expected_organizations:
                membership_review = OrganizationMembershipReviewedEvent(
                    event_id=self._event_id(context, f"organization.membership_reviewed:{year_index}"),
                    year_index=year_index,
                    scheduled_event_id=scheduled.id,
                    census_event_id=progression.event_id,
                    review=recruitment_plan,
                    provenance=[
                        ProvenanceReference(kind="rule", reference_id="organization.membership_recruitment.v1")
                    ],
                )
                events.append(membership_review)
                membership_summary = {
                    "organization_count": len(recruitment_plan.expected_organizations),
                    "enrollment_count": len(recruitment_plan.enrollments),
                    "original_key_people_seeded": recruitment_plan.original_key_people.original_key_people_seeded,
                    "original_key_people_alive": recruitment_plan.original_key_people.original_key_people_alive,
                }
        completion = ScheduledEventCompletedEvent(
            event_id=self._event_id(context, f"scheduled.completed:{scheduled.id}"),
            scheduled_event_id=scheduled.id,
            expected_revision=scheduled.revision,
        )
        events.append(completion)
        details: dict[str, object] = {"year_index": year_index, "cohort_count": len(age_groups)}
        if membership_summary is not None:
            details["membership_review"] = membership_summary
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="population_year_advanced",
            message=(
                "Age-structured populations advanced and opted-in organizations reviewed eligible resident membership."
                if membership_summary is not None
                else "All registered anonymous age cohorts advanced one deterministic world-year."
            ),
            details=details,
            events=events,
        )

    def _resolve_renewable_harvest(
        self,
        state: WorldState,
        scheduled: ScheduledEventRecord,
        context: RuleContext,
    ) -> RuleResolution:
        """Build one atomic 30-day finite ration distribution for named people."""
        period_ticks = state.calendar.day_seconds * 30
        period_index = state.current_tick // period_ticks
        if (
            scheduled.handler_version != "1.0.0"
            or scheduled.payload
            or scheduled.repeat_ticks != period_ticks
            or scheduled.due_tick <= 0
            or scheduled.due_tick % period_ticks != 0
            or scheduled.completed_runs + 1 != period_index
        ):
            return self._rejected(
                context,
                "harvest_schedule_mismatch",
                "Environmental harvest must be an empty-payload finite recurrence every 30 world days.",
            )

        seat_plan = plan_population_seat_promotions(state, period_index)
        harvest_state = seat_plan.state
        policies: list[tuple[WorldEntity, PlaceComponent, RenewableHarvestPolicy]] = []
        for region in harvest_state.entities.values():
            raw_place = region.components.get("place")
            if not isinstance(raw_place, dict):
                continue
            try:
                place = PlaceComponent.model_validate(raw_place)
            except Exception:
                return self._rejected(
                    context,
                    "harvest_policy_invalid",
                    "A region has an invalid registered place component.",
                )
            for policy in place.renewable_harvests:
                if policy.period_ticks != period_ticks:
                    return self._rejected(
                        context,
                        "harvest_period_mismatch",
                        "Each environmental policy must use a 30 world day period.",
                    )
                if policy.issued_total < policy.maximum_total:
                    if policy.last_issued_period != period_index - 1:
                        return self._rejected(
                            context,
                            "harvest_period_sequence",
                            "Finite harvest policy must be advanced once for every due period.",
                        )
                    policies.append((region, place, policy))
        policies.sort(key=lambda item: (str(item[0].id), item[2].policy_id))

        expectations: list[RenewableHarvestExpectation] = []
        for region, place, policy in policies:
            source = harvest_state.resources.get(policy.source_account_id)
            if (
                source is None
                or source.account_type != "environmental_source"
                or source.owner_id != region.id
                or source.resource_id != policy.resource_id
                or source.credit_limit is None
                or source.credit_limit != policy.issued_total
                or source.balance.copy_negate() != policy.issued_total
            ):
                return self._rejected(
                    context,
                    "harvest_source_invalid",
                    "The finite environmental source no longer matches its registered region policy.",
                )
            active = [
                recipient for recipient in policy.recipients
                if (target := harvest_state.entities.get(recipient.entity_id)) is not None
                and target.kind in {"character", "person"}
                and target.status not in {"dead", "retired"}
            ]
            active.sort(key=lambda item: str(item.entity_id))
            remaining = policy.maximum_total - policy.issued_total
            recipient_expectations: list[RenewableHarvestRecipientExpectation] = []
            for recipient in active:
                quantity = min(recipient.per_period, remaining)
                remaining -= quantity
                if quantity <= 0:
                    continue
                target = harvest_state.entities[recipient.entity_id]
                account = harvest_state.resources.get(recipient.account_id)
                if (
                    account is None
                    or account.account_type != "holder"
                    or account.owner_id != target.id
                    or account.resource_id != policy.resource_id
                ):
                    return self._rejected(
                        context,
                        "harvest_recipient_account_invalid",
                        "A named recipient is missing its matching owned holder account.",
                    )
                try:
                    inventory = InventoryComponent.model_validate(target.components.get("inventory"))
                except Exception:
                    return self._rejected(
                        context,
                        "harvest_recipient_inventory_invalid",
                        "A named recipient is missing a valid registered inventory.",
                    )
                recipient_expectations.append(
                    RenewableHarvestRecipientExpectation(
                        entity_id=target.id,
                        account_id=recipient.account_id,
                        expected_balance=account.balance,
                        expected_inventory_revision=inventory.revision,
                        quantity=quantity,
                    )
                )
            expectations.append(
                RenewableHarvestExpectation(
                    region_id=region.id,
                    policy_id=policy.policy_id,
                    resource_id=policy.resource_id,
                    source_account_id=policy.source_account_id,
                    expected_place_revision=place.revision,
                    expected_policy_revision=policy.revision,
                    source_expected_balance=source.balance,
                    source_expected_credit_limit=source.credit_limit,
                    recipients=recipient_expectations,
                )
            )

        events: list[DomainEvent] = []
        if expectations:
            event = RenewableResourceHarvestedEvent(
                event_id=self._event_id(context, f"environmental.harvest:{scheduled.id}:{period_index}"),
                period_index=period_index,
                scheduled_event_id=scheduled.id,
                items=expectations,
                promotions=list(seat_plan.promotions),
                provenance=[ProvenanceReference(kind="rule", reference_id="environmental.harvest.v1")],
            )
            events.append(event)
        completion = ScheduledEventCompletedEvent(
            event_id=self._event_id(context, f"scheduled.completed:{scheduled.id}"),
            scheduled_event_id=scheduled.id,
            expected_revision=scheduled.revision,
        )
        events.append(completion)
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="environmental_harvest_distributed" if expectations else "environmental_harvest_exhausted",
            message=(
                "One finite 30-day ration was distributed to each active named recipient."
                if expectations else "All registered finite harvest policies are exhausted."
            ),
            details={
                "period_index": period_index,
                "policy_count": len(expectations),
                "promoted_member_ids": [str(item.character.id) for item in seat_plan.promotions],
            },
            events=events,
        )

    def evaluate(
        self,
        state: WorldState,
        intent: ActionIntent | dict[str, object],
        context: RuleContext,
    ) -> RuleResolution:
        try:
            action = parse_action(intent)
        except ValueError:
            return self._rejected(
                context,
                "invalid_action",
                "The action is malformed or does not match a registered action.",
            )

        actor = state.entities.get(context.actor_id)
        if actor is None or actor.kind not in {"character", "person"}:
            return self._rejected(context, "actor_missing", "The requested actor is not a character in this world.")
        if actor.status in {"dead", "retired"}:
            return self._rejected(context, "actor_terminal", "A dead or retired character cannot act.")
        if actor.status == "incapacitated":
            return self._rejected(context, "actor_incapacitated", "An incapacitated character cannot act.")
        if actor.id in occupied_npc_ids(state, state.current_tick):
            return self._rejected(context, "actor_busy", "The character already has an action in progress.")

        if isinstance(action, MoveIntent):
            return self._move(state, actor, action, context)
        if isinstance(action, RetreatIntent):
            return self._retreat(state, actor, action, context)
        if isinstance(action, ExploreIntent):
            return self._explore(state, actor, action, context)
        if isinstance(action, SpeakIntent):
            return self._speak(state, actor, action, context)
        if isinstance(action, ImprovisedCheckIntent):
            return self._improvised_check(state, actor, action, context)
        if isinstance(action, WeighEvidenceIntent):
            return self._weigh_evidence(state, actor, action, context)
        if isinstance(action, AskNpcClaimIntent):
            return self._ask_npc_claim(state, actor, action, context)
        if isinstance(action, OpenInvestigationCaseIntent):
            return self._open_investigation_case(state, actor, action, context)
        if isinstance(action, AttachCaseEvidenceIntent):
            return self._attach_case_evidence(state, actor, action, context)
        if isinstance(action, CloseInvestigationCaseIntent):
            return self._close_investigation_case(state, actor, action, context)
        if isinstance(action, CorrectPublicAccusationIntent):
            return self._correct_public_accusation(state, actor, action, context)
        if isinstance(action, (OfferAccusationRepairIntent, ResolveAccusationRepairIntent)):
            return self._accusation_repair(state, action, context)
        if isinstance(action, DestroyHistoricalEvidenceIntent):
            return self._destroy_historical_evidence(state, actor, action, context)
        if isinstance(action, DispatchShipmentIntent):
            return self._dispatch_shipment(state, actor, action, context)
        if isinstance(action, ReceiveShipmentIntent):
            return self._receive_shipment(state, actor, action, context)
        if isinstance(action, DistributeShipmentReliefIntent):
            return self._distribute_shipment_relief(state, actor, action, context)
        if isinstance(action, (RecordReturnPromiseIntent, FulfillReturnPromiseIntent, LateReturnPromiseIntent, ReleaseReturnPromiseIntent)):
            try:
                if isinstance(action, RecordReturnPromiseIntent):
                    event = build_record_return_promise_event(
                        state, context, promisee_id=action.promisee_id, resource_id=action.resource_id,
                        quantity=action.quantity, deadline_tick=action.deadline_tick, private_text=action.private_text,
                    )
                elif isinstance(action, FulfillReturnPromiseIntent):
                    event = build_trusted_promise_settlement(
                        state, build_fulfill_return_promise_event(state, context, promise_id=action.promise_id),
                    )
                elif isinstance(action, LateReturnPromiseIntent):
                    event = build_late_return_promise_event(state, context, promise_id=action.promise_id)
                else:
                    event = build_release_return_promise_event(state, context, promise_id=action.promise_id)
            except DomainError:
                return self._rejected(context, "promise_action_unavailable", "The promise action cannot be completed in the current state.")
            return self._resolution(state, context, status="succeeded", code=action.kind,
                                    message="A private promise action was recorded.", events=[event])
        if isinstance(action, (OfferPrivateGiftIntent, ResolvePrivateGiftIntent)):
            return self._private_gift_change(state, actor, action, context)
        if isinstance(action, (
            ProposeRelationshipInvitationIntent,
            ResolveRelationshipInvitationIntent,
            AttendRelationshipMeetingIntent,
        )):
            return self._relationship_invitation_change(state, actor, action, context)
        if isinstance(action, (
            ProposeRelationshipPartnershipIntent,
            ResolveRelationshipPartnershipIntent,
            UpdateRelationshipPartnershipProposalIntent,
        )):
            return self._relationship_partnership_change(state, actor, action, context)
        if isinstance(action, DiscloseRelationshipPartnershipIntent):
            return self._disclose_relationship_partnership(state, actor, action, context)
        if isinstance(action, RecordMeetingReflectionIntent):
            return self._record_meeting_reflection(state, actor, action, context)
        if isinstance(action, (TakeJobIntent, ResignJobIntent)):
            return self._occupation_change(state, action, context)
        if isinstance(action, WorkShiftIntent):
            return self._work_shift(state, actor, action, context)
        if isinstance(action, (ConsumeIntent, RestIntent, WaitIntent)):
            return self._stationary_action(state, actor, action, context)
        if isinstance(action, TradeIntent):
            return self._trade(state, actor, action, context)
        if isinstance(action, MobilizeTreasuryReserveIntent):
            from .institutional_credit_rules import plan_institutional_reserve_action

            try:
                event = plan_institutional_reserve_action(state, action, context)
            except InstitutionalCreditError as error:
                return self._rejected(context, f"treasury_reserve_{error.code}", "The reserve could not be mobilized.")
            except DomainError:
                return self._rejected(context, "treasury_reserve_unavailable", "The reserve could not be mobilized.")
            return self._resolution(
                state, context, status="succeeded", code="treasury_reserve_mobilized",
                message="The institution moved its own reserves to its treasury.", events=[event],
            )
        if isinstance(action, (ProposeCreditExtensionIntent, ResolveCreditExtensionIntent)):
            from .institutional_credit_rules import plan_institutional_credit_extension

            try:
                event = plan_institutional_credit_extension(state, action, context)
            except InstitutionalCreditError as error:
                return self._rejected(context, f"credit_extension_{error.code}", "The extension could not settle.")
            except DomainError:
                return self._rejected(context, "credit_extension_unavailable", "The extension could not settle.")
            return self._resolution(
                state, context, status="succeeded", code=f"credit_extension_{event.operation}",
                message="The institutional credit extension was recorded.", events=[event],
            )
        if isinstance(action, (OfferInstitutionalCreditIntent, ResolveInstitutionalCreditIntent)):
            from .institutional_credit_rules import plan_institutional_credit_action

            try:
                event = plan_institutional_credit_action(state, action, context)
            except InstitutionalCreditError as error:
                return self._rejected(context, f"institutional_credit_{error.code}", "The credit action could not settle.")
            except DomainError:
                return self._rejected(context, "institutional_credit_unavailable", "The credit action could not settle.")
            return self._resolution(
                state, context, status="succeeded", code=f"institutional_credit_{event.operation}",
                message="The institutional credit action was recorded.", events=[event],
            )
        if isinstance(action, ProduceIntent):
            return self._produce(state, actor, action, context)
        if isinstance(action, LeaveOrganizationIntent):
            return self._leave_organization(state, actor, action, context)
        if isinstance(action, HandoverLeadershipIntent):
            return self._handover_leadership(state, actor, action, context)
        if isinstance(action, (OpenLeadershipBallotIntent, OpenVacantLeadershipBallotIntent,
                               CastLeadershipBallotVoteIntent, CloseLeadershipBallotIntent)):
            return self._leadership_ballot_change(state, action, context)
        if isinstance(action, (ProposeSafeConductIntent, ResolveSafeConductIntent)):
            return self._safe_conduct_change(state, action, context)
        if isinstance(action, (OfferCombatTruceIntent, ResolveCombatTruceIntent)):
            return self._combat_truce_change(state, action, context)
        if isinstance(action, AttackIntent):
            return self._attack(state, actor, action, context)
        if isinstance(action, TreatInjuryIntent):
            return self._treat_injury(state, actor, action, context)
        if isinstance(action, TrainIntent):
            return self._train(state, actor, action, context)
        if isinstance(action, AttemptCultivationBreakthroughIntent):
            return self._attempt_cultivation_breakthrough(state, actor, action, context)
        return self._rejected(context, "unsupported_action", "The action is not registered.")

    def _stationary_action(
        self, state: WorldState, actor: WorldEntity, action: ConsumeIntent | RestIntent | WaitIntent,
        context: RuleContext,
    ) -> RuleResolution:
        delay = None
        journey = None if context.travelling_window or actor.location_id is not None else active_travel_journey(state, actor.id)
        if journey is not None:
            duration = action.duration_ticks
            start, end = state.current_tick, state.current_tick + duration
            due = journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick
            if due == start:
                return self._rejected(context, "scheduled_event_due", "The journey must settle before this action can advance time.")
            try:
                delayed = delay_travel_journey(journey, start, end)
            except DomainError:
                return self._rejected(context, "travel_delay_unavailable", "The journey cannot be delayed at this clock boundary.")
            command = context.simulation_command_id or context.command_id
            delay = TravelJourneyChangedEvent(
                event_id=travel_event_id(command, context.ruleset_version, "delay", journey.journey_id),
                world_id=context.world_id, branch_id=context.branch_id, command_id=command,
                actor_id=actor.id, ruleset_version=context.ruleset_version, operation="delay",
                journey=delayed, visibility=travel_journey_visibility(actor.id),
                stationary_from_tick=start, stationary_until_tick=end,
            )
            state = apply_event(state, delay)
        if isinstance(action, ConsumeIntent):
            result = self._consume(state, actor, action, context)
        elif isinstance(action, RestIntent):
            result = self._rest(state, actor, action, context)
        else:
            result = self._wait(state, actor, action, context)
        if delay is not None and result.elapsed_ticks:
            return result.model_copy(update={"events": [delay, *result.events]})
        return result

    def _relationship_invitation_change(
        self,
        state: WorldState,
        actor: WorldEntity,
        action: ProposeRelationshipInvitationIntent | ResolveRelationshipInvitationIntent | AttendRelationshipMeetingIntent,
        context: RuleContext,
    ) -> RuleResolution:
        if isinstance(action, ProposeRelationshipInvitationIntent):
            target = state.entities.get(action.target_id)
            if target is None or not entity_visible_to_actor(state, actor.id, target.id):
                return self._rejected(
                    context,
                    "relationship_invitation_target_not_visible",
                    "The other character must be visible to you here.",
                )
            invitation_id = uuid5(context.command_id, "relationship-invitation")
            operation = "propose"
            expected_revision = 0
            try:
                resulting = propose_relationship_invitation(
                    state,
                    state.relationship_invitations,
                    invitation_id=invitation_id,
                    proposer_id=actor.id,
                    invitee_id=action.target_id,
                    meeting=action.meeting,
                )
            except InvitationMeetingUnavailable:
                return self._rejected(
                    context,
                    "relationship_invitation_meeting_unavailable",
                    "That proposed meeting is no longer available, so this invitation cannot proceed with that time.",
                )
            except DomainError:
                return self._rejected(
                    context,
                    "relationship_invitation_unavailable",
                    "A new invitation is not available for these characters in the current scene.",
                )
        elif isinstance(action, AttendRelationshipMeetingIntent):
            current = next(
                (item for item in state.relationship_invitations if item.invitation_id == action.invitation_id),
                None,
            )
            if current is None:
                return self._rejected(
                    context,
                    "relationship_meeting_attendance_unavailable",
                    "The scheduled relationship meeting is unavailable for attendance.",
                )
            operation = "attend"
            expected_revision = current.revision
            try:
                resulting = attend_relationship_meeting(
                    state,
                    state.relationship_invitations,
                    invitation_id=action.invitation_id,
                    actor_id=actor.id,
                    expected_revision=expected_revision,
                    check_work_calendar=True,
                )
            except DomainError:
                return self._rejected(
                    context,
                    "relationship_meeting_attendance_unavailable",
                    "The scheduled relationship meeting is unavailable for attendance.",
                )
        else:
            current = next(
                (item for item in state.relationship_invitations if item.invitation_id == action.invitation_id),
                None,
            )
            if current is None:
                return self._rejected(
                    context,
                    "relationship_invitation_unavailable",
                    "That invitation is no longer available.",
                )
            transitions = {
                "accept": accept_relationship_invitation,
                "decline": decline_relationship_invitation,
                "withdraw": withdraw_relationship_invitation,
                "end": end_relationship_invitation,
            }
            operation = action.operation
            expected_revision = current.revision
            try:
                resulting = transitions[operation](
                    state,
                    state.relationship_invitations,
                    invitation_id=action.invitation_id,
                    actor_id=actor.id,
                    expected_revision=expected_revision,
                )
            except InvitationMeetingUnavailable:
                return self._rejected(
                    context,
                    "relationship_invitation_meeting_unavailable",
                    "That proposed meeting is no longer available, so this invitation cannot proceed with that time.",
                )
            except DomainError:
                return self._rejected(
                    context,
                    "relationship_invitation_unavailable",
                    "That invitation cannot be changed by this character in the current scene.",
                )

        event = RelationInvitationChangedEvent(
            event_id=invitation_event_id(
                context.command_id, context.ruleset_version, operation, resulting.invitation_id,
            ),
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            actor_id=actor.id,
            ruleset_version=context.ruleset_version,
            operation=operation,
            expected_revision=expected_revision,
            resulting_invitation=resulting,
            visibility=participant_visibility(resulting.proposer_id, resulting.invitee_id),
            work_calendar_checked=True if operation == "attend" else None,
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code=f"relationship_invitation_{operation}",
            message=(
                "Attendance at a private relationship meeting was recorded."
                if operation == "attend"
                else "A private relationship invitation was updated."
            ),
            details={"invitation_id": str(resulting.invitation_id), "status": resulting.status},
            events=[event],
        )

    def _record_meeting_reflection(
        self,
        state: WorldState,
        actor: WorldEntity,
        action: RecordMeetingReflectionIntent,
        context: RuleContext,
    ) -> RuleResolution:
        try:
            reflection = plan_meeting_reflection(
                state,
                invitation_id=action.invitation_id,
                actor_id=actor.id,
                outlook=action.outlook,
            )
        except DomainError:
            return self._rejected(
                context,
                "meeting_reflection_unavailable",
                "A private meeting reflection cannot be recorded in the current state.",
            )

        event = MeetingReflectionRecordedEvent(
            event_id=meeting_reflection_event_id(
                context.command_id, context.ruleset_version, action.invitation_id,
            ),
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            actor_id=actor.id,
            ruleset_version=context.ruleset_version,
            reflection=reflection,
            visibility=meeting_reflection_visibility(actor.id),
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="meeting_reflection_recorded",
            message="Your private meeting reflection was recorded.",
            events=[event],
        )

    def _relationship_partnership_change(
        self,
        state: WorldState,
        actor: WorldEntity,
        action: (
            ProposeRelationshipPartnershipIntent
            | ResolveRelationshipPartnershipIntent
            | UpdateRelationshipPartnershipProposalIntent
        ),
        context: RuleContext,
    ) -> RuleResolution:
        if isinstance(action, ProposeRelationshipPartnershipIntent):
            partner = state.entities.get(action.partner_id)
            if partner is None or not entity_visible_to_actor(state, actor.id, partner.id):
                return self._rejected(
                    context,
                    "relationship_partnership_target_not_visible",
                    "The other character must be visible to you here.",
                )
            partnership_id = uuid5(context.command_id, "relationship-partnership")
            operation = "propose"
            expected_revision = 0
            partner_id = action.partner_id
        else:
            partnership_id = action.partnership_id
            operation = action.operation
            expected_revision = action.expected_revision
            partner_id = None

        try:
            resulting = plan_relationship_partnership(
                state,
                operation=operation,
                partnership_id=partnership_id,
                actor_id=actor.id,
                partner_id=partner_id,
                expected_revision=expected_revision,
                proposal_terms=(
                    action.proposal_terms
                    if isinstance(action, (ProposeRelationshipPartnershipIntent, UpdateRelationshipPartnershipProposalIntent))
                    else ()
                ),
            )
        except DomainError:
            return self._rejected(
                context,
                "relationship_partnership_unavailable",
                "That relationship partnership cannot be changed by this character in the current state.",
            )

        event = RelationshipPartnershipChangedEvent(
            event_id=partnership_event_id(
                context.command_id, context.ruleset_version, operation, resulting.partnership_id,
            ),
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            actor_id=actor.id,
            ruleset_version=context.ruleset_version,
            operation=operation,
            expected_revision=expected_revision,
            resulting_partnership=resulting,
            visibility=partnership_visibility(resulting.proposer_id, resulting.partner_id),
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code=f"relationship_partnership_{operation}",
            message=(
                "A private relationship partnership proposal was recorded."
                if operation == "propose"
                else "A private relationship partnership was updated."
            ),
            details={
                "partnership_id": str(resulting.partnership_id),
                "status": resulting.status,
            },
            events=[event],
        )

    def _disclose_relationship_partnership(
        self,
        state: WorldState,
        actor: WorldEntity,
        action: DiscloseRelationshipPartnershipIntent,
        context: RuleContext,
    ) -> RuleResolution:
        try:
            event = plan_relationship_disclosure(
                state,
                world_id=context.world_id,
                branch_id=context.branch_id,
                command_id=context.command_id,
                ruleset_version=context.ruleset_version,
                actor_id=actor.id,
                partnership_id=action.partnership_id,
                expected_revision=action.expected_revision,
                recipient_id=action.recipient_id,
            )
        except DomainError:
            return self._rejected(
                context,
                "relationship_disclosure_unavailable",
                "That statement cannot be disclosed in the current state.",
            )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="relationship_disclosure_recorded",
            message="Your statement was shared privately with the selected character.",
            events=[event],
        )

    def _occupation_change(
        self, state: WorldState, action: TakeJobIntent | ResignJobIntent, context: RuleContext,
    ) -> RuleResolution:
        resign = isinstance(action, ResignJobIntent)
        if resign:
            subject_id = action.assignment_id
            row = next((assignment for assignment in state.work_assignments if assignment.assignment_id == subject_id), None)
        else:
            subject_id = action.posting_id
            row = next((posting for posting in state.work_postings if posting.posting_id == subject_id), None)
        if row is None:
            return self._rejected(context, "occupation_unavailable", "The requested occupation is unavailable.")
        event_id = occupation_event_id(
            context.command_id, context.ruleset_version, "resigned" if resign else "appointed", context.actor_id, subject_id,
        )
        try:
            assignment = plan_occupation_change(
                state, actor_id=context.actor_id, event_id=event_id, subject_id=subject_id,
                expected_revision=row.revision, resign=resign,
            )
        except DomainError:
            return self._rejected(context, "occupation_unavailable", "The requested occupation is unavailable.")
        fields = dict(event_id=event_id, world_id=context.world_id, branch_id=context.branch_id,
                      command_id=context.command_id, ruleset_version=context.ruleset_version, actor_id=context.actor_id,
                      effective_tick=state.current_tick, resulting_assignment=assignment)
        if resign:
            event = OccupationResignedEvent(**fields, assignment_id=subject_id, expected_assignment_revision=row.revision)
        else:
            event = OccupationAppointedEvent(**fields, posting_id=subject_id, expected_posting_revision=row.revision)
        return self._resolution(state, context, status="succeeded", code=action.kind,
                                message="The occupation change was recorded.", events=[event])

    def _work_shift(
        self, state: WorldState, actor: WorldEntity, action: WorkShiftIntent, context: RuleContext,
    ) -> RuleResolution:
        assignment = next((item for item in state.work_assignments if item.assignment_id == action.assignment_id
                           and item.actor_id == actor.id and item.ended_tick is None), None)
        if assignment is None or assignment.location_id != actor.location_id:
            return self._rejected(context, "work_shift_unavailable", "The worker must hold this job and be at its workplace.")
        terms = registered_work_shift_terms(state).get(assignment.posting_id)
        posting = next((item for item in state.work_postings if item.posting_id == assignment.posting_id), None)
        if terms is None or posting is None:
            return self._rejected(context, "work_shift_unavailable", "This job has no registered paid shift.")
        worker_account_id = select_wage_holder_account(state, actor.id, terms.resource_id)
        if worker_account_id is None:
            return self._rejected(context, "work_shift_unavailable", "The worker has no registered wage account.")
        duration = terms.duration_seconds
        issue = self._duration_issue(state, actor, duration)
        if issue is not None:
            return self._rejected(context, *issue)
        needs_event, deprivation = self._time_need_effects(state, actor, duration, context, rest_delta=1)
        effects = [event for event in (needs_event, deprivation) if event is not None]
        timeline = self._resolution(state, context, status="succeeded", code="work_shift",
                                    message="The registered work shift was completed.",
                                    elapsed_ticks=duration, events=effects)
        if not timeline.events:
            return timeline
        completed = state
        for event in timeline.events:
            completed = apply_event(completed, event)
        try:
            payment = plan_paid_work_shift(
                completed, terms=terms, actor_id=actor.id, assignment_id=assignment.assignment_id,
                worker_account_id=worker_account_id, shift_started_tick=state.current_tick,
                expected_posting_revision=posting.revision, expected_assignment_revision=assignment.revision,
            )
        except WorkShiftError as exc:
            return self._rejected(context, exc.code, str(exc))
        wage = WorkShiftPaidEvent(
            event_id=work_shift_paid_event_id(context.command_id, context.ruleset_version, assignment.assignment_id),
            world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
            actor_id=context.actor_id, ruleset_version=context.ruleset_version, plan=payment,
        )
        return timeline.model_copy(update={"events": [*timeline.events, wage]})

    def evaluate_world_action(
        self,
        state: WorldState,
        intent: ActionIntent | dict[str, object],
        context: RuleContext,
        choices: Iterable[BackgroundAgentChoice | dict[str, object]],
        *,
        include_actor: bool = False,
    ) -> RuleResolution:
        """Evaluate the player/NPC action and trusted background choices atomically."""
        try:
            background_context = self._context_with_background_choices(
                context, choices, include_actor=include_actor
            )
        except Exception as exc:
            return self._rejected(context, "background_choices_invalid", str(exc))
        return self.evaluate(state, intent, background_context)

    def resolve_background_window(
        self,
        state: WorldState,
        *,
        from_tick: int,
        to_tick: int,
        context: RuleContext,
        choices: Iterable[BackgroundAgentChoice | dict[str, object]],
        include_actor: bool = False,
    ) -> RuleResolution:
        """Advance an idle/simulation window and apply all background choices together."""
        if from_tick != state.current_tick or to_tick <= from_tick:
            return self._rejected(
                context,
                "background_window_clock_mismatch",
                "Background windows must begin at the current tick and advance world time.",
            )
        actor = state.entities.get(context.actor_id)
        if not include_actor and (actor is None or actor.kind not in {"character", "person"}):
            return self._rejected(context, "actor_missing", "The background window needs an existing acting character.")
        try:
            background_context = self._context_with_background_choices(
                context, choices, include_actor=include_actor
            )
        except Exception as exc:
            return self._rejected(context, "background_choices_invalid", str(exc))
        issue = self._duration_issue(
            state,
            actor if not include_actor else None,
            to_tick - from_tick,
            include_actor_background=include_actor,
            check_background_boundary=not include_actor,
        )
        if issue is not None:
            return self._rejected(background_context, *issue)
        return self._resolution(
            state,
            background_context,
            status="succeeded",
            code="background_window_advanced",
            message="The background window advanced with registered self-care choices.",
            details={"from_tick": from_tick, "to_tick": to_tick},
            elapsed_ticks=to_tick - from_tick,
        )

    @staticmethod
    def _context_with_background_choices(
        context: RuleContext,
        choices: Iterable[BackgroundAgentChoice | dict[str, object]],
        *,
        include_actor: bool | None = None,
    ) -> RuleContext:
        include_actor = context.include_actor_in_background if include_actor is None else include_actor
        typed_choices: list[BackgroundAgentChoice] = []
        for choice in choices:
            if isinstance(choice, BackgroundAgentChoice):
                typed_choices.append(choice)
                continue
            raw = choice.model_dump(mode="python") if hasattr(choice, "model_dump") else choice
            if not isinstance(raw, dict):
                raise ValueError("background choice must be a typed policy result or object")
            typed_choices.append(BackgroundAgentChoice.model_validate({
                "entity_id": raw.get("entity_id", raw.get("actor_id")),
                "action": raw.get("action", raw.get("activity")),
                "resource_id": raw.get("resource_id"),
                "quantity": raw.get("quantity"),
                "account_id": raw.get("account_id"),
                "reason_code": raw.get("reason_code"),
            }))
        return RuleContext(
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            actor_id=context.actor_id,
            ruleset_version=context.ruleset_version,
            include_actor_in_background=include_actor,
            background_routine_mode=include_actor,
            background_choices=typed_choices,
            simulation_command_id=context.simulation_command_id,
            travelling_window=context.travelling_window,
        )

    def next_background_boundary(
        self,
        state: WorldState,
        *,
        actor_id: UUID | None = None,
        include_actor: bool = False,
    ) -> int | None:
        """Return the earliest exact NPC deprivation-damage boundary.

        The planned actor is excluded because their action-specific need effects
        are resolved in the same rules batch. A boundary at or before the current
        tick is returned as-is so simulation can pause for its registered outcome.
        """
        boundaries: list[int] = []
        for entity in state.entities.values():
            if (
                not include_actor and entity.id == actor_id
                or entity.kind not in {"character", "person"}
                or entity.status in {"dead", "retired"}
                or entity.components.get("needs") is None
            ):
                continue
            try:
                needs = self._needs(entity)
            except Exception as exc:
                raise RuleInvariantError("An active character has an invalid registered needs component.") from exc
            if needs is None:
                continue
            boundary = next_deprivation_damage_tick(
                needs,
                from_tick=state.current_tick,
                day_seconds=state.calendar.day_seconds,
            )
            if boundary is not None:
                boundaries.append(boundary)
        return min(boundaries) if boundaries else None

    def evaluate_travel_departure(
        self,
        state: WorldState,
        route_id: UUID,
        context: RuleContext,
        *,
        itinerary_journey_id: UUID | None = None,
    ) -> RuleResolution:
        """Record durable departure without advancing through the route duration.

        Ordinary callers retain command-scoped journey and risk-roll identity.
        The durable itinerary worker may supply its fixed per-leg journey ID so
        retries after unrelated commits or speed changes keep the same route
        risk sample. Event identity and scope remain bound to ``context``.
        """
        if itinerary_journey_id is not None and not isinstance(itinerary_journey_id, UUID):
            return self._rejected(
                context,
                "travel_itinerary_identity_invalid",
                "The itinerary journey identity must be a UUID.",
            )
        actor = state.entities.get(context.actor_id)
        if actor is None or actor.kind not in {"character", "person"}:
            return self._rejected(context, "actor_missing", "The requested actor is not a character in this world.")
        if actor.status in {"dead", "retired"}:
            return self._rejected(context, "actor_terminal", "A dead or retired character cannot travel.")
        if actor.status == "incapacitated":
            return self._rejected(context, "actor_incapacitated", "An incapacitated character cannot begin traveling.")
        if actor.id in occupied_npc_ids(state, state.current_tick):
            return self._rejected(context, "actor_busy", "The character already has an action in progress.")
        if active_combat_opponent(state, actor) is not None:
            return self._rejected(
                context,
                "combat_encounter_active",
                "An active co-located threat requires an explicit retreat action.",
            )
        if actor.location_id is None:
            return self._rejected(context, "actor_has_no_location", "The character has no current location.")
        origin = state.entities.get(actor.location_id)
        if origin is None or origin.kind not in {"place", "location"}:
            return self._rejected(context, "origin_not_traversable", "The current place has no valid route map.")
        try:
            place = PlaceComponent.model_validate(origin.components.get("place"))
        except Exception:
            return self._rejected(context, "origin_not_traversable", "The current place has no valid route map.")
        route = next((item for item in place.routes if item.route_id == route_id), None)
        if route is None:
            return self._rejected(context, "route_unavailable", "There is no registered route from the current place.")
        destination = state.entities.get(route.to_place_id)
        access_issue = route_access_reason(state, actor, route, destination)
        if access_issue is not None:
            return self._rejected(context, *access_issue)

        rolls: list[RuleRoll] = []
        hazard = False
        hazard_timing: TravelHazardTiming | None = None
        threshold = int((route.risk * Decimal("100")).to_integral_value(rounding=ROUND_CEILING))
        if threshold > 0:
            roll_context = (
                context
                if itinerary_journey_id is None
                else context.model_copy(update={"command_id": itinerary_journey_id})
            )
            roll = self._roll(roll_context, "movement.route_risk", 100)
            hazard = roll.value <= threshold
            rolls.append(roll.model_copy(update={"outcome": "hazard" if hazard else "safe"}))
        if hazard and itinerary_journey_id is not None:
            hazard_timing = "journey_seed_v1"

        journey_id = itinerary_journey_id or uuid5(context.command_id, "travel-journey")
        try:
            journey = plan_travel_departure(
                state,
                journey_id=journey_id,
                actor_id=actor.id,
                route_id=route.route_id,
                hazard=hazard,
                hazard_timing=hazard_timing,
            )
        except DomainError:
            return self._rejected(
                context,
                "travel_departure_unavailable",
                "The selected route cannot be started in the current state.",
            )

        event = TravelJourneyChangedEvent(
            event_id=travel_event_id(context.command_id, context.ruleset_version, "depart", journey.journey_id),
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            actor_id=actor.id,
            ruleset_version=context.ruleset_version,
            operation="depart",
            journey=journey,
            hazard_timing=hazard_timing,
            visibility=travel_journey_visibility(actor.id),
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="travel_departed",
            message="The character began traveling on the registered route.",
            details={"journey_id": str(journey.journey_id)},
            events=[event],
            rolls=rolls,
        )

    def evaluate_travel_resolution(
        self,
        state: WorldState,
        journey_id: UUID,
        context: RuleContext,
    ) -> RuleResolution:
        """Arrive or interrupt one durable journey at its declared resolution tick."""
        matches = [item for item in state.travel_journeys if item.journey_id == journey_id]
        if len(matches) != 1:
            return self._rejected(
                context,
                "travel_journey_unavailable",
                "The requested travel journey is unavailable in this state.",
            )
        journey = matches[0]
        if journey.status != "in_transit":
            return self._rejected(
                context,
                "travel_journey_resolved",
                "The requested travel journey has already been resolved.",
            )

        due_tick = journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick
        if state.current_tick != due_tick:
            return self._rejected(
                context,
                "travel_journey_not_due",
                "The travel journey resolves only at its registered boundary.",
            )
        actor = state.entities.get(journey.actor_id)
        if actor is None:
            return self._rejected(
                context,
                "travel_journey_unavailable",
                "The requested travel journey is unavailable in this state.",
            )
        operation = "interrupt" if journey.hazard_tick is not None or actor.status in {"dead", "retired"} else "arrive"
        try:
            resolved = plan_travel_resolution(
                state,
                journey_id=journey.journey_id,
                actor_id=journey.actor_id,
                operation=operation,
            )
        except DomainError:
            return self._rejected(
                context,
                "travel_journey_unavailable",
                "The requested travel journey cannot be resolved in this state.",
            )

        event = TravelJourneyChangedEvent(
            event_id=travel_event_id(
                context.command_id,
                context.ruleset_version,
                operation,
                resolved.journey_id,
            ),
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            actor_id=resolved.actor_id,
            ruleset_version=context.ruleset_version,
            operation=operation,
            journey=resolved,
            visibility=travel_journey_visibility(resolved.actor_id),
        )
        if operation == "arrive":
            return self._resolution(
                state,
                context,
                status="succeeded",
                code="travel_arrived",
                message="The character arrived at the registered destination.",
                details={"journey_id": str(resolved.journey_id)},
                events=[event],
            )

        hazard_interruption = (
            [RuleInterruption(
                code="route_hazard",
                message="A route hazard interrupted the journey before arrival.",
                at_tick=state.current_tick,
            )]
            if journey.hazard_tick is not None
            else []
        )
        resolution_events: list[DomainEvent] = [event]
        if journey.hazard_tick is not None and actor.status == "active":
            health = self._health(actor)
            if health is not None and health.current > 0:
                resolution_events.append(DamageAppliedEvent(
                    event_id=self._event_id(context, "movement.hazard_damage"),
                    target_id=actor.id,
                    damage=min(1_000_000, max(1, health.maximum // 20)),
                    injury_type="travel.route_hazard",
                    description="Injury sustained during a route hazard.",
                    expected_health_revision=health.revision,
                    expected_population_revision=self._population_revision(state, actor),
                ))
        return self._resolution(
            state,
            context,
            status="partial",
            code="route_hazard" if journey.hazard_tick is not None else "travel_interrupted",
            message="The journey was interrupted before arrival.",
            details={"journey_id": str(resolved.journey_id)},
            events=resolution_events,
            interruptions=hazard_interruption,
        )

    def _retreat(
        self, state: WorldState, actor: WorldEntity, intent: RetreatIntent, context: RuleContext
    ) -> RuleResolution:
        encounter = active_combat_opponent(state, actor)
        if encounter is None:
            return self._rejected(
                context,
                "combat_encounter_unavailable",
                "A retreat requires an active, co-located registered threat.",
            )
        return self._move(
            state,
            actor,
            MoveIntent(destination_id=intent.destination_id, route_id=intent.route_id),
            context,
            retreat_pair=encounter,
        )

    def _move(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: MoveIntent,
        context: RuleContext,
        *,
        retreat_pair: tuple[CombatEncounterComponent, WorldEntity] | None = None,
    ) -> RuleResolution:
        if retreat_pair is None and active_combat_opponent(state, actor) is not None:
            return self._rejected(
                context,
                "combat_encounter_active",
                "An active co-located threat requires an explicit retreat action.",
            )
        if actor.location_id is None:
            return self._rejected(context, "actor_has_no_location", "The character has no current location.")
        origin = state.entities.get(actor.location_id)
        destination = state.entities.get(intent.destination_id)
        if origin is None or destination is None or destination.kind not in {"place", "location"}:
            return self._rejected(context, "destination_unavailable", "The destination is not a registered place.")
        if destination.components.get("hidden") is True and destination.id not in _observed_entities(state, actor.id):
            return self._rejected(context, "destination_unseen", "The character has not discovered that destination.")
        try:
            place = PlaceComponent.model_validate(origin.components.get("place"))
        except Exception:
            return self._rejected(context, "origin_not_traversable", "The current place has no valid route map.")
        candidates = [
            route
            for route in place.routes
            if route.to_place_id == intent.destination_id
            and (intent.route_id is None or route.route_id == intent.route_id)
        ]
        if not candidates:
            return self._rejected(context, "route_unavailable", "There is no registered route to that destination.")
        route = sorted(candidates, key=lambda item: (item.travel_ticks, str(item.route_id)))[0]
        access_issue = route_access_reason(state, actor, route, destination)
        if access_issue is not None:
            return self._rejected(context, *access_issue)
        duration_issue = self._duration_issue(state, actor, route.travel_ticks)
        if duration_issue is not None:
            return self._rejected(context, *duration_issue)

        rolls: list[RuleRoll] = []
        interruptions: list[RuleInterruption] = []
        followup: list[DomainEvent] = []
        threshold = int((route.risk * Decimal("100")).to_integral_value(rounding=ROUND_CEILING))
        if threshold > 0:
            roll = self._roll(context, "movement.route_risk", 100)
            roll = roll.model_copy(update={"outcome": "hazard" if roll.value <= threshold else "safe"})
            rolls.append(roll)
            if roll.value <= threshold:
                hazard_duration = max(1, route.travel_ticks // 2)
                interruptions.append(
                    RuleInterruption(
                        code="route_hazard",
                        message="A route hazard forces the character to stop before reaching the destination.",
                        at_tick=state.current_tick + hazard_duration,
                    )
                )
                health = self._health(actor)
                needs_event, deprivation = self._time_need_effects(
                    state, actor, hazard_duration, context, rest_delta=0
                )
                followup = []
                if needs_event is not None:
                    followup.append(needs_event)
                hazard_damage = next((event for event in followup if isinstance(event, DamageAppliedEvent)), None)
                if health is not None and health.current > 0:
                    followup.append(
                        DamageAppliedEvent(
                            event_id=self._event_id(context, "movement.hazard_damage"),
                            target_id=actor.id,
                            damage=max(1, health.maximum // 20),
                            injury_type="travel.route_hazard",
                            description="Injury sustained during a route hazard.",
                            expected_health_revision=health.revision,
                            expected_population_revision=self._population_revision(state, actor),
                        )
                    )
                    hazard_damage = followup[-1]
                if deprivation is not None and not self._damage_is_lethal(actor, hazard_damage.damage if hazard_damage else 0):
                    if hazard_damage is not None:
                        deprivation = deprivation.model_copy(
                            update={"expected_health_revision": deprivation.expected_health_revision + 1}
                        )
                    followup.append(deprivation)
                life_event = self._lifespan_event(state, actor, hazard_duration, context, followup)
                if life_event is not None:
                    followup.append(life_event)
                return self._resolution(
                    state,
                    context,
                    status="partial",
                    code="retreat_interrupted" if retreat_pair is not None else "route_hazard",
                    message=(
                        "A route hazard interrupted the retreat; the threat remains close."
                        if retreat_pair is not None
                        else "Travel was interrupted by a route hazard."
                    ),
                    details={"from_place_id": str(actor.location_id), "to_place_id": str(intent.destination_id)},
                    elapsed_ticks=hazard_duration,
                    events=followup,
                    rolls=rolls,
                    interruptions=interruptions,
                )

        if retreat_pair is not None:
            encounter, opponent = retreat_pair
            try:
                opponent_encounter = CombatEncounterComponent.model_validate(
                    opponent.components.get("combat_encounter")
                )
            except Exception:
                return self._rejected(
                    context,
                    "combat_encounter_unavailable",
                    "The registered encounter is no longer available.",
                )
            followup.append(
                CombatRetreatResolvedEvent(
                    event_id=self._event_id(context, "combat.retreat"),
                    world_id=context.world_id,
                    branch_id=context.branch_id,
                    command_id=context.command_id,
                    ruleset_version=context.ruleset_version,
                    encounter_id=encounter.encounter_id,
                    actor_id=actor.id,
                    opponent_id=opponent.id,
                    route_id=route.route_id,
                    from_place_id=actor.location_id,
                    to_place_id=intent.destination_id,
                    expected_actor_encounter_revision=encounter.revision,
                    expected_opponent_encounter_revision=opponent_encounter.revision,
                    started_tick=state.current_tick,
                    arrived_tick=state.current_tick + route.travel_ticks,
                    visibility={
                        "policy": "subject_ids",
                        "subject_ids": sorted((str(actor.id), str(opponent.id))),
                    },
                )
            )
        movement = MovementCompletedEvent(
            event_id=self._event_id(context, "movement.completed"),
            actor_id=actor.id,
            route_id=route.route_id,
            from_place_id=actor.location_id,
            to_place_id=intent.destination_id,
            started_tick=state.current_tick,
            arrived_tick=state.current_tick + route.travel_ticks,
        )
        followup.append(movement)
        needs_event, deprivation = self._time_need_effects(state, actor, route.travel_ticks, context, rest_delta=0)
        if needs_event is not None:
            followup.append(needs_event)
        if deprivation is not None:
            followup.append(deprivation)
        life_event = self._lifespan_event(state, actor, route.travel_ticks, context, followup)
        if life_event is not None:
            followup.append(life_event)
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="retreated" if retreat_pair is not None else "arrived",
            message=(
                "The character escaped the active encounter over the registered route."
                if retreat_pair is not None
                else "The character arrived by the registered route."
            ),
            details={"from_place_id": str(actor.location_id), "to_place_id": str(intent.destination_id)},
            elapsed_ticks=route.travel_ticks,
            events=followup,
            rolls=rolls,
        )

    def _explore(
        self, state: WorldState, actor: WorldEntity, intent: ExploreIntent, context: RuleContext
    ) -> RuleResolution:
        issue = self._exploration_site_issue(state, actor, intent)
        if issue is not None:
            return self._rejected(context, *issue)
        duration = NPC_EXPLORATION_TICKS
        issue = self._duration_issue(state, actor, duration)
        if issue is not None:
            return self._rejected(context, *issue)
        outcome, events, rolls = self._exploration_search(
            state, actor, intent, context, observed_tick=state.current_tick + duration,
        )
        needs_event, deprivation = self._time_need_effects(state, actor, duration, context)
        events.extend(item for item in (needs_event, deprivation) if item is not None)
        life_event = self._lifespan_event(state, actor, duration, context, events)
        if life_event is not None:
            events.append(life_event)
        return self._resolution(
            state, context,
            status="partial" if deprivation is not None or life_event is not None else outcome.status,
            code="lifespan_expired" if life_event is not None else "survival_pressure" if deprivation else outcome.code,
            message=outcome.message, details=outcome.details, elapsed_ticks=duration, events=events, rolls=rolls,
        )

    @staticmethod
    def _exploration_site_issue(
        state: WorldState, actor: WorldEntity, intent: ExploreIntent,
    ) -> tuple[str, str] | None:
        if actor.location_id != intent.place_id:
            return "not_at_place", "The character must be present to explore this place."
        place = state.entities.get(intent.place_id)
        observed_target_ids = _observed_entities(state, actor.id)
        if place is None or place.kind not in {"place", "location"} or (
            place.components.get("hidden") is True and place.id not in observed_target_ids
        ):
            return "place_unavailable", "The place is not visible to this character."
        return None

    def _exploration_search(
        self, state: WorldState, actor: WorldEntity, intent: ExploreIntent, context: RuleContext, *, observed_tick: int,
    ) -> tuple[RuleOutcome, list[DomainEvent], list[RuleRoll]]:
        """Evaluate the search at its observation boundary without settling elapsed time."""
        place = state.entities[intent.place_id]
        observed_target_ids = _observed_entities(state, actor.id)
        hidden_targets = [
            entity
            for entity in state.entities.values()
            if entity.status == "active"
            and entity.components.get("hidden") is True
            and entity.id not in observed_target_ids
            and self._is_local_to(entity, intent.place_id)
            and (intent.focus == "general" or self._focus_matches(entity, intent.focus))
        ]
        if not hidden_targets:
            return RuleOutcome(
                status="succeeded",
                code="nothing_hidden_found",
                message="The character examined the area but found no hidden target in that focus.",
                details={"place_id": str(intent.place_id), "focus": intent.focus},
            ), [], []
        target = sorted(hidden_targets, key=lambda item: str(item.id))[0]
        skill = self._skill_level(actor, "skill.investigation") or self._skill_level(actor, "skill.local_knowledge")
        difficulty = 50
        chance = max(5, min(95, 30 + int(skill // Decimal("5")) - difficulty // 5))
        roll = self._roll(context, "explore.hidden_target", 100)
        if roll.value > chance:
            roll = roll.model_copy(update={"outcome": "missed"})
            return RuleOutcome(
                status="failed",
                code="search_failed",
                message="The character searched carefully but did not find the hidden target.",
                details={"place_id": str(intent.place_id), "focus": intent.focus},
            ), [], [roll]
        roll = roll.model_copy(update={"outcome": "found"})
        discovery = EntityDiscoveredEvent(
            event_id=self._event_id(context, "explore.discovered"),
            discoverer_id=actor.id,
            entity_id=target.id,
            place_id=intent.place_id,
            difficulty=difficulty,
            roll=roll.value,
            chance_percent=chance,
            reveal_scope="discoverer",
        )
        description = target.components.get("public_description")
        claim = f"你发现了{target.name or '一条新线索'}。"
        if isinstance(description, str) and description.strip():
            claim += description.strip()
        knowledge = KnowledgeAcquiredEvent(
            event_id=self._event_id(context, "explore.knowledge"),
            record={
                "id": str(self._event_id(context, "explore.knowledge_record")),
                "owner_id": str(actor.id),
                "subject_id": str(target.id),
                "claim": claim,
                "summary": f"Exploration at {place.name or 'the current place'}.",
                "confidence": "0.95",
                "source_event_id": str(discovery.event_id),
                "received_tick": observed_tick,
                "fact": {
                    "predicate": "entity_location",
                    "value": str(intent.place_id),
                    "effective_tick": observed_tick,
                },
                "kind": "observation",
                "visibility": {"policy": "private", "subject_ids": [str(actor.id)]},
            },
        )
        return RuleOutcome(
            status="succeeded",
            code="discovered",
            message="The character found something hidden and recorded the observation.",
            details={"entity_id": str(target.id), "place_id": str(intent.place_id), "claim": claim},
        ), [discovery, knowledge], [roll]

    def _improvised_check(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: ImprovisedCheckIntent,
        context: RuleContext,
    ) -> RuleResolution:
        """Resolve one fixed, low-risk check that records only authored truth."""
        location_id = actor.location_id
        location = state.entities.get(location_id) if location_id is not None else None
        if location is None or location.kind not in {"place", "location"} or location.status != "active":
            return self._rejected(context, "improvised_check_location_missing", "The actor must be at an active registered place.")

        target = state.entities.get(intent.target_id)
        if (
            target is None
            or target.kind not in {"place", "location", "item"}
            or target.status != "active"
            or not (target.id == location_id or self._is_local_to(target, location_id))
        ):
            return self._rejected(context, "improvised_check_target_unavailable", "The examination target must be an active local place or item.")
        if target.components.get("hidden") is True and target.id not in _observed_entities(state, actor.id):
            return self._rejected(context, "improvised_check_target_unseen", "The examination target is not visible to this character.")
        description = target.components.get("public_description")
        if not isinstance(description, str) or not description.strip():
            return self._rejected(context, "improvised_check_no_authored_detail", "This check only examines an existing authored public description.")
        raw_historical_evidence = target.components.get("historical_evidence")
        try:
            historical_evidence = (
                HistoricalEvidenceArtifact.model_validate(raw_historical_evidence)
                if raw_historical_evidence is not None else None
            )
        except Exception:
            return self._rejected(context, "historical_evidence_invalid", "The registered historical source is invalid.")
        if historical_evidence is not None:
            historical_subject = state.entities.get(historical_evidence.subject_id)
            if (
                historical_subject is None
                or historical_evidence.fact.effective_tick > state.current_tick
                or not entity_visible_to_actor(state, actor.id, historical_subject.id)
            ):
                return self._rejected(context, "historical_evidence_unavailable", "The historical source is not available for this examination.")
            already_observed = any(
                actor.id in {record.owner_id, record.holder_id}
                and record.kind == "observation"
                and historical_evidence.evidence_id in record.source_refs
                for record in state.knowledge
            )
        else:
            already_observed = any(
                actor.id in {record.owner_id, record.holder_id}
                and record.subject_id == target.id
                and record.kind == "observation"
                for record in state.knowledge
            )
        if already_observed:
            return self._rejected(context, "improvised_check_already_observed", "This character has already recorded an observation of that target.")

        try:
            skills = SkillsComponent.model_validate(actor.components.get("skills", {}))
        except Exception:
            return self._rejected(context, "improvised_check_skills_invalid", "The character's registered skills are invalid.")
        skill = skills.skills.get(intent.skill_id)
        if skill is None:
            return self._rejected(context, "improvised_check_skill_unavailable", "The character does not have that registered examination competency.")

        duration = IMPROVISED_EXAMINE_DURATION
        issue = self._duration_issue(state, actor, duration)
        if issue is not None:
            return self._rejected(context, *issue)
        roll = self._roll(context, IMPROVISED_EXAMINE_ROLL_LABEL, 100)
        chance = improvised_examine_chance(skill.level)
        passed = roll.value <= chance
        audit_id = self._event_id(context, "improvised_check.audit")
        observation_id = self._event_id(context, "improvised_check.observation_record") if passed else None
        end_tick = state.current_tick + duration
        audit = ImprovisedCheckResolvedEvent(
            event_id=audit_id,
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            ruleset_version=context.ruleset_version,
            actor_id=actor.id,
            target_id=target.id,
            location_id=location_id,
            skill_id=intent.skill_id,
            expected_skills_revision=skills.revision,
            skill_level=skill.level,
            policy_version=IMPROVISED_EXAMINE_POLICY,
            chance_percent=chance,
            roll=roll.value,
            roll_digest=roll.digest,
            passed=passed,
            source_description_hash=source_text_digest(description),
            historical_evidence_id=(
                historical_evidence.evidence_id
                if passed and historical_evidence is not None else None
            ),
            historical_evidence_hash=(
                historical_evidence_digest(historical_evidence)
                if passed and historical_evidence is not None else None
            ),
            started_tick=state.current_tick,
            resolved_tick=end_tick,
            observation_record_id=observation_id,
        )
        events: list[DomainEvent] = [audit]
        if passed and observation_id is not None:
            claim = historical_evidence.claim if historical_evidence is not None else description
            fact = (
                historical_evidence.fact.model_dump(mode="json")
                if historical_evidence is not None
                else (
                    {
                        "predicate": "entity_location",
                        "value": str(location_id),
                        "effective_tick": end_tick,
                    }
                    if target.id != location_id and target.location_id == location_id
                    else {
                        "predicate": "entity_status",
                        "value": target.status,
                        "effective_tick": end_tick,
                    }
                )
            )
            events.append(
                KnowledgeAcquiredEvent(
                    event_id=self._event_id(context, "improvised_check.observation"),
                    record={
                        "id": str(observation_id),
                        "owner_id": str(actor.id),
                        "subject_id": str(
                            historical_evidence.subject_id if historical_evidence is not None else target.id
                        ),
                        "kind": "observation",
                        "claim": claim,
                        "summary": (
                            HISTORICAL_EVIDENCE_OBSERVATION_SUMMARY
                            if historical_evidence is not None
                            else "A focused examination confirmed an existing authored description."
                        ),
                        "confidence": "1",
                        "source_event_id": str(audit.event_id),
                        "source_refs": (
                            [str(historical_evidence.evidence_id)]
                            if historical_evidence is not None else []
                        ),
                        "provenance": (
                            [item.model_dump(mode="json") for item in historical_evidence.provenance]
                            if historical_evidence is not None else []
                        ),
                        "received_tick": end_tick,
                        "fact": fact,
                        "visibility": {"policy": "private"},
                    },
                )
            )
        needs_event, deprivation = self._time_need_effects(state, actor, duration, context)
        followup: list[DomainEvent] = [*events]
        if needs_event is not None:
            followup.append(needs_event)
        if deprivation is not None:
            followup.append(deprivation)
        life_event = self._lifespan_event(state, actor, duration, context, followup)
        if life_event is not None:
            followup.append(life_event)

        status = "succeeded" if passed else "failed"
        code = "improvised_check_succeeded" if passed else "improvised_check_failed"
        message = (
            "The examination recorded the target's existing authored description."
            if passed else "The examination did not produce a confirmed observation."
        )
        if life_event is not None:
            status, code, message = "partial", "lifespan_expired", "The examination resolved as a character reached the end of their lifespan."
        elif deprivation is not None:
            status, code, message = "partial", "survival_pressure", "The examination resolved, but hunger or thirst caused an injury."
        return self._resolution(
            state,
            context,
            status=status,
            code=code,
            message=(
                "The examination recorded one authored historical source at its stated effective time."
                if passed and historical_evidence is not None else message
            ),
            details={
                "target_id": str(target.id), "skill_id": intent.skill_id, "passed": passed,
                **({"historical_evidence_id": str(historical_evidence.evidence_id)}
                   if historical_evidence is not None else {}),
            },
            elapsed_ticks=duration,
            events=followup,
            rolls=[roll.model_copy(update={"outcome": "success" if passed else "miss"})],
        )

    def _weigh_evidence(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: WeighEvidenceIntent,
        context: RuleContext,
    ) -> RuleResolution:
        duration = WEIGH_EVIDENCE_DURATION
        issue = self._duration_issue(state, actor, duration)
        if issue is not None:
            return self._rejected(context, *issue)
        try:
            finding = compare_evidence_sources(
                intent.observation_id,
                intent.claim_id,
                state.knowledge,
                actor_id=actor.id,
                at_tick=state.current_tick,
            )
        except ValueError:
            return self._rejected(
                context,
                "evidence_unavailable",
                "Choose an available observation and a distinct, independently sourced claim about the same subject and time.",
            )

        observation = next(record for record in state.knowledge if record.id == intent.observation_id)
        claim_source = next(record for record in state.knowledge if record.id == intent.claim_id)
        assert observation.fact is not None
        end_tick = state.current_tick + duration
        claim_text, message = assessment_text(observation.fact, finding)
        assessment = EvidenceAssessment(
            finding=finding,
            observation_id=observation.id,
            claim_id=claim_source.id,
            predicate=observation.fact.predicate,
            effective_tick=observation.fact.effective_tick,
        )
        assessment_record = KnowledgeRecord(
            id=self._event_id(context, "weigh_evidence.record"),
            owner_id=actor.id,
            subject_id=observation.subject_id,
            claim=claim_text,
            summary=message,
            confidence=Decimal("1"),
            source_refs=[observation.id, claim_source.id],
            received_tick=end_tick,
            kind="belief",
            assessment=assessment,
            provenance=[ProvenanceReference(kind="rule", reference_id=WEIGH_EVIDENCE_POLICY)],
            visibility={"policy": "private"},
        )
        try:
            validate_assessment_record(
                assessment_record,
                [*state.knowledge, assessment_record],
                actor_id=actor.id,
                at_tick=end_tick,
            )
        except ValueError:
            return self._rejected(context, "evidence_unavailable", "The selected sources cannot be compared safely.")

        knowledge_event = KnowledgeAcquiredEvent(
            event_id=self._event_id(context, "weigh_evidence.acquired"),
            record=assessment_record,
        )
        needs_event, deprivation = self._time_need_effects(state, actor, duration, context)
        events: list[DomainEvent] = [knowledge_event]
        if needs_event is not None:
            events.append(needs_event)
        if deprivation is not None:
            events.append(deprivation)
        life_event = self._lifespan_event(state, actor, duration, context, events)
        if life_event is not None:
            events.append(life_event)

        status = "partial" if deprivation is not None or life_event is not None else "succeeded"
        code = "evidence_corroborated" if finding == "corroborated" else "evidence_conflict"
        if life_event is not None:
            status, code = "partial", "lifespan_expired"
            message = "证据复核期间角色寿终；复核记录已保存。"
        elif deprivation is not None:
            status = "partial"
            message += " 复核期间还发生了生存压力，请检查角色状态。"
        return self._resolution(
            state,
            context,
            status=status,
            code=code,
            message=message,
            details={
                "finding": finding,
                "predicate": observation.fact.predicate,
                "effective_tick": observation.fact.effective_tick,
                "observation_id": str(observation.id),
                "claim_id": str(claim_source.id),
                "assessment_id": str(assessment_record.id),
            },
            elapsed_ticks=duration,
            events=events,
        )

    def _speak(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: SpeakIntent,
        context: RuleContext,
    ) -> RuleResolution:
        try:
            events = build_private_speech_events(state, actor, intent, context)
        except SpeechActionError as exc:
            return self._rejected(context, exc.code, exc.message)
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="private_speech_recorded",
            message="The private utterance was recorded as a memory for its named audience.",
            details={"recipient_count": len(intent.target_ids)},
            events=events,
        )

    def _ask_npc_claim(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: AskNpcClaimIntent,
        context: RuleContext,
    ) -> RuleResolution:
        """Disclose one eligible typed claim only after an explicit ask action."""
        location_id = actor.location_id
        location = state.entities.get(location_id) if location_id is not None else None
        speaker = state.entities.get(intent.target_id)
        subject = state.entities.get(intent.subject_id)
        if (
            actor.status != "active"
            or location is None
            or location.kind not in {"place", "location"}
            or location.status != "active"
            or speaker is None
            or speaker.kind not in {"character", "person"}
            or speaker.status != "active"
            or speaker.id == actor.id
            or speaker.location_id != location_id
            or speaker.components.get("hidden") is True and speaker.id not in _observed_entities(state, actor.id)
            or subject is None
            or not entity_visible_to_actor(state, actor.id, subject.id)
        ):
            return self._rejected(
                context,
                "npc_claim_speaker_unavailable",
                "The selected person is not available to answer here.",
            )

        matching = [
            record for record in state.knowledge
            if record.holder_id == speaker.id
            and record.disclosure_policy == "if_asked"
            and record.disclosure is None
            and record.assessment is None
            and record.kind in FALLIBLE_CLAIM_KINDS
            and record.subject_id == intent.subject_id
            and record.fact is not None
            and record.fact.predicate in {"entity_status", "entity_location"}
            and (intent.predicate is None or record.fact.predicate == intent.predicate)
            and (intent.effective_tick is None or record.fact.effective_tick == intent.effective_tick)
            and record.received_tick <= state.current_tick
            and (record.valid_from_tick is None or record.valid_from_tick <= state.current_tick)
            and record.visibility.policy == "private"
            and not record.visibility.subject_ids
        ]
        # Do not distinguish an absent claim from a hidden or ambiguous one.
        if len(matching) != 1:
            return self._rejected(
                context,
                "npc_claim_unavailable",
                "That person cannot provide one eligible claim for the requested subject and scope.",
            )

        source = matching[0]
        disclosed = KnowledgeRecord(
            id=self._event_id(context, "ask_npc_claim.disclosed_record"),
            owner_id=actor.id,
            subject_id=source.subject_id,
            claim=source.claim,
            summary=source.summary,
            confidence=source.confidence,
            source_refs=[source.id],
            source_event_id=source.source_event_id,
            provenance=source.provenance,
            received_tick=state.current_tick,
            valid_from_tick=source.valid_from_tick,
            kind=source.kind,
            fact=source.fact,
            disclosure={
                "source_record_id": str(source.id),
                "speaker_id": str(speaker.id),
                "recipient_id": str(actor.id),
                "location_id": str(location_id),
                "disclosed_tick": state.current_tick,
                "policy_version": ASK_NPC_CLAIM_POLICY,
            },
            visibility={"policy": "private"},
        )
        try:
            validate_disclosure_record(disclosed, state)
        except ValueError:
            return self._rejected(
                context,
                "npc_claim_unavailable",
                "That person cannot provide one eligible claim for the requested subject and scope.",
            )
        event = KnowledgeAcquiredEvent(
            event_id=self._event_id(context, "ask_npc_claim.acquired"),
            record=disclosed,
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="npc_claim_disclosed",
            message="The person shared one authorized claim without changing the claim's factual effective tick.",
            details={
                "disclosed_record_id": str(disclosed.id),
                "speaker_id": str(speaker.id),
                "source_record_id": str(source.id),
                "subject_id": str(source.subject_id),
                "predicate": source.fact.predicate,
                "effective_tick": source.fact.effective_tick,
            },
            events=[event],
        )

    def _open_investigation_case(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: OpenInvestigationCaseIntent,
        context: RuleContext,
    ) -> RuleResolution:
        if not case_anchor_is_local_and_available(state, actor.id, intent.anchor_id):
            return self._rejected(
                context,
                "investigation_case_anchor_unavailable",
                "The case anchor is not available at the character's current location.",
            )
        anchor = state.entities[intent.anchor_id]
        try:
            definition = case_definition_from_anchor(anchor)
            definition_hash = case_definition_digest(definition)
        except Exception:
            return self._rejected(
                context,
                "investigation_case_definition_invalid",
                "The authored case definition is unavailable.",
            )
        if any(item.owner_id == actor.id and item.case_id == definition.case_id for item in state.investigation_cases):
            return self._rejected(
                context,
                "investigation_case_already_opened",
                "This character already has a file for that case.",
            )
        case_file = InvestigationCaseFile(
            id=self._event_id(context, "investigation_case.file"),
            case_id=definition.case_id,
            anchor_id=anchor.id,
            owner_id=actor.id,
            definition_hash=definition_hash,
            title=definition.title,
            brief=definition.brief,
            opened_tick=state.current_tick,
            updated_tick=state.current_tick,
        )
        event = InvestigationCaseOpenedEvent(
            event_id=self._event_id(context, "investigation_case.opened"),
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            ruleset_version=context.ruleset_version,
            actor_id=actor.id,
            case_file=case_file,
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="investigation_case_opened",
            message="已建立这宗调查的私人档案。",
            details={"case_file_id": str(case_file.id)},
            events=[event],
        )

    def _attach_case_evidence(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: AttachCaseEvidenceIntent,
        context: RuleContext,
    ) -> RuleResolution:
        case_file = next((item for item in state.investigation_cases if item.id == intent.case_file_id), None)
        if case_file is None or case_file.owner_id != actor.id or case_file.status != "open":
            return self._rejected(context, "investigation_case_file_unavailable", "That open case file is unavailable.")
        anchor = state.entities.get(case_file.anchor_id)
        try:
            definition = case_definition_from_anchor(anchor) if anchor is not None else None
        except Exception:
            definition = None
        if definition is None or definition.case_id != case_file.case_id or case_definition_digest(definition) != case_file.definition_hash:
            return self._rejected(
                context,
                "investigation_case_definition_changed",
                "The authored case rules changed after this file was opened.",
            )
        record = next((item for item in state.knowledge if item.id == intent.assessment_id), None)
        if (
            record is None or record.owner_id != actor.id or record.holder_id is not None
            or record.assessment is None or record.visibility.policy != "private"
            or intent.assessment_id in case_file.evidence_refs
        ):
            return self._rejected(
                context,
                "investigation_case_assessment_unavailable",
                "Choose an available private evidence assessment that is not already in this file.",
            )
        try:
            validate_case_assessment_access(
                record, state.knowledge, actor_id=actor.id, at_tick=state.current_tick,
            )
        except ValueError:
            return self._rejected(
                context,
                "investigation_case_assessment_invalid",
                "That evidence assessment no longer has a valid source chain.",
            )
        event = InvestigationCaseEvidenceAttachedEvent(
            event_id=self._event_id(context, "investigation_case.evidence_attached"),
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            ruleset_version=context.ruleset_version,
            actor_id=actor.id,
            case_file_id=case_file.id,
            expected_revision=case_file.revision,
            assessment_id=record.id,
            updated_tick=state.current_tick,
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="case_evidence_attached",
            message="已将这条证据复核记录加入私人档案。",
            details={"case_file_id": str(case_file.id), "assessment_id": str(record.id)},
            events=[event],
        )

    def _accusation_repair(
        self, state: WorldState, action: OfferAccusationRepairIntent | ResolveAccusationRepairIntent,
        context: RuleContext,
    ) -> RuleResolution:
        provenance = dict(world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
                          ruleset_version=context.ruleset_version, actor_id=context.actor_id)
        try:
            if isinstance(action, OfferAccusationRepairIntent):
                offer = plan_accusation_repair_offer(
                    state, **provenance, correction_event_id=action.correction_event_id, statement=action.statement,
                )
                events: list[DomainEvent] = [offer]
            else:
                response, relation = plan_accusation_repair_resolution(
                    state, **provenance, offer_id=action.offer_id, expected_revision=action.expected_revision,
                    operation=action.operation, requested_by=context.accusation_repair_requested_by,
                    trust_proof=context.accusation_repair_trust_proof,
                )
                events = [response] if relation is None else [response, relation]
        except DomainError:
            return self._rejected(context, "accusation_repair_unavailable", "This repair action is unavailable here.")
        return self._resolution(state, context, status="succeeded", code="accusation_repair_recorded",
                                message="The repair action was recorded.", events=events)

    def _correct_public_accusation(
        self, state: WorldState, actor: WorldEntity, intent: CorrectPublicAccusationIntent, context: RuleContext,
    ) -> RuleResolution:
        try:
            event = plan_public_accusation_correction(
                state, world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
                ruleset_version=context.ruleset_version, actor_id=actor.id, case_file_id=intent.case_file_id,
                expected_revision=intent.expected_revision, source_closure_event_id=intent.source_closure_event_id,
            )
        except DomainError:
            return self._rejected(context, "public_accusation_correction_unavailable", "That accusation cannot be corrected here.")
        return self._resolution(
            state, context, status="succeeded", code="public_accusation_corrected",
            message="You publicly corrected the earlier accusation.", events=[event],
        )

    def _close_investigation_case(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: CloseInvestigationCaseIntent,
        context: RuleContext,
    ) -> RuleResolution:
        case_file = next((item for item in state.investigation_cases if item.id == intent.case_file_id), None)
        if case_file is None or case_file.owner_id != actor.id or case_file.status != "open":
            return self._rejected(context, "investigation_case_file_unavailable", "That open case file is unavailable.")
        anchor = state.entities.get(case_file.anchor_id)
        try:
            definition = case_definition_from_anchor(anchor) if anchor is not None else None
        except Exception:
            definition = None
        if definition is None or definition.case_id != case_file.case_id or case_definition_digest(definition) != case_file.definition_hash:
            return self._rejected(
                context,
                "investigation_case_definition_changed",
                "The authored case rules changed after this file was opened.",
            )
        suspect = state.entities.get(intent.suspect_id) if intent.suspect_id is not None else None
        if intent.suspect_id is not None and (
            suspect is None or suspect.kind not in {"character", "person"}
            or suspect.id == actor.id or not entity_visible_to_actor(state, actor.id, suspect.id)
        ):
            return self._rejected(
                context,
                "investigation_case_suspect_unavailable",
                "The selected person is not currently known to this character.",
            )
        routes = matched_case_routes(
            definition, case_file, state.knowledge, actor_id=actor.id, at_tick=state.current_tick,
        )
        outcome = case_closure_outcome(intent.suspect_id, routes)
        if intent.public_accusation:
            if (
                suspect is None or suspect.status != "active" or actor.location_id is None
                or suspect.location_id != actor.location_id
                or not any(route.suspect_id == suspect.id for route in routes)
            ):
                return self._rejected(
                    context,
                    "public_case_accusation_unavailable",
                    "A public accusation requires a present person and an applicable filed evidence route.",
                )
        event = InvestigationCaseClosedEvent(
            event_id=self._event_id(context, "investigation_case.closed"),
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            ruleset_version=context.ruleset_version,
            actor_id=actor.id,
            case_file_id=case_file.id,
            expected_revision=case_file.revision,
            suspect_id=intent.suspect_id,
            public_accusation=intent.public_accusation,
            outcome=outcome,
            closed_tick=state.current_tick,
        )
        events: list[DomainEvent] = [event]
        relation_effect_applied = False
        if outcome == "disconfirmed" and intent.public_accusation and suspect is not None and definition.wrong_suspect_effect is not None:
            relation = next((
                item for item in state.relations
                if item.from_id == suspect.id and item.to_id == actor.id
            ), None)
            if sum(item.from_id == suspect.id and item.to_id == actor.id for item in state.relations) > 1:
                return self._rejected(context, "case_relation_ambiguous", "The directed relationship for this consequence is ambiguous.")
            effect = definition.wrong_suspect_effect
            current = relation.dimensions.get(effect.dimension) if relation is not None else None
            previous_value = current if current is not None else Decimal("0")
            value = min(Decimal("100"), max(Decimal("-100"), previous_value + Decimal(effect.delta)))
            if value != previous_value:
                participant_ids = sorted({str(actor.id), str(suspect.id)})
                relation_event_id = self._event_id(context, "investigation_case.wrong_suspect_relation.event")
                relation_visibility = {"policy": "subject_ids", "subject_ids": participant_ids}
                if relation is None:
                    events.append(RelationEstablishedEvent(
                        event_id=relation_event_id,
                        relation=RelationRecord(
                            id=self._event_id(context, "investigation_case.wrong_suspect_relation"),
                            from_id=suspect.id,
                            to_id=actor.id,
                            dimensions={effect.dimension: value},
                            evidence_refs=[event.event_id],
                            updated_tick=state.current_tick,
                            visibility=relation_visibility,
                        ),
                        visibility=relation_visibility,
                    ))
                else:
                    events.append(RelationDimensionChangedEvent(
                        event_id=relation_event_id,
                        relation_id=relation.id,
                        dimension=effect.dimension,
                        expected_previous=current,
                        value=value,
                        evidence_refs=[event.event_id],
                        visibility=relation_visibility,
                    ))
                relation_effect_applied = True
        messages = {
            "supported": "档案中的独立证据路线支持你的判断。",
            "disconfirmed": "档案中的证据路线排除了你选定的嫌疑人。",
            "contested": "档案中的证据路线对该嫌疑人相互冲突，结论仍有争议。",
            "unresolved": "档案已结案，但现有记录不足以支持或排除这一判断。",
        }
        message = messages[outcome]
        if intent.public_accusation:
            message = "你已当面公开提出这一指认。" + message
            if relation_effect_applied:
                message += " 对方对你的看法也按案件规则发生了变化。"
        return self._resolution(
            state,
            context,
            status="succeeded",
            code=f"case_{outcome}",
            message=message,
            details={
                "case_file_id": str(case_file.id),
                "outcome": outcome,
                "suspect_id": str(intent.suspect_id) if intent.suspect_id is not None else None,
                "public_accusation": intent.public_accusation,
                "relation_effect_applied": relation_effect_applied,
            },
            events=events,
        )

    def _destroy_historical_evidence(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: DestroyHistoricalEvidenceIntent,
        context: RuleContext,
    ) -> RuleResolution:
        try:
            target, artifact, location_id = validate_destroyable_historical_item(
                state, actor_id=actor.id, target_id=intent.target_id,
            )
        except ValueError:
            return self._rejected(
                context,
                "historical_evidence_unavailable",
                "这件材料当前不可销毁。",
            )
        event = HistoricalEvidenceDestroyedEvent(
            event_id=self._event_id(context, "historical_evidence.destroyed"),
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            ruleset_version=context.ruleset_version,
            actor_id=actor.id,
            target_id=target.id,
            location_id=location_id,
            historical_evidence_id=artifact.evidence_id,
            historical_evidence_hash=historical_evidence_digest(artifact),
            destroyed_tick=state.current_tick,
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="historical_evidence_destroyed",
            message="已销毁现场的历史材料；已经取得的抄录和个人记录不受影响。",
            details={"target_id": str(target.id)},
            events=[event],
        )

    def _dispatch_shipment(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: DispatchShipmentIntent,
        context: RuleContext,
    ) -> RuleResolution:
        if intent.source_owner_id != actor.id:
            return self._rejected(
                context, "shipment_source_not_owned", "The sender must own the source stock account."
            )
        if actor.location_id is None:
            return self._rejected(context, "shipment_origin_missing", "The sender has no physical origin place.")
        origin = state.entities.get(actor.location_id)
        try:
            origin_place = PlaceComponent.model_validate(origin.components.get("place")) if origin else None
        except Exception:
            origin_place = None
        if origin is None or origin_place is None:
            return self._rejected(context, "shipment_origin_invalid", "The sender's location has no registered route map.")
        route = next((item for item in origin_place.routes if item.route_id == intent.route_id), None)
        if route is None or route.from_place_id != origin.id or not route.is_open:
            return self._rejected(context, "shipment_route_unavailable", "The shipment route is not open at the sender's origin.")
        destination_place = state.entities.get(route.to_place_id)
        if destination_place is None or destination_place.status != "active":
            return self._rejected(context, "shipment_destination_unavailable", "The registered destination is unavailable.")
        access_issue = route_access_reason(state, actor, route, destination_place)
        if access_issue is not None:
            return self._rejected(context, *access_issue)
        # Delivery/environment/decision phases own a due tick. Do not let a
        # zero-duration dispatch run ahead of those same-tick events.
        if any(
            item.status == "queued" and item.due_tick <= state.current_tick
            for item in state.scheduled_events
        ):
            return self._rejected(
                context, "scheduled_event_due", "A due world schedule must resolve before another shipment dispatch."
            )
        if not shipment_destination_available(state, intent.destination_owner_id, route.to_place_id):
            return self._rejected(
                context,
                "shipment_receiver_not_present",
                "The named receiver must be active at the registered destination when the shipment is dispatched.",
            )
        source_account = state.resources.get(intent.source_account_id)
        destination_account = state.resources.get(intent.destination_account_id)
        if (
            source_account is None
            or source_account.account_type != "holder"
            or source_account.owner_id != actor.id
            or source_account.resource_id != intent.resource_id
        ):
            return self._rejected(context, "shipment_source_account_invalid", "Source stock is not held by the local sender.")
        if (
            destination_account is None
            or destination_account.account_type != "holder"
            or destination_account.owner_id != intent.destination_owner_id
            or destination_account.resource_id != intent.resource_id
            or intent.destination_account_id == intent.source_account_id
        ):
            return self._rejected(
                context, "shipment_destination_account_invalid", "The destination account does not belong to the named receiver."
            )
        if source_account.balance < intent.quantity:
            return self._rejected(context, "shipment_stock_unavailable", "The sender does not hold enough stock.")
        try:
            source_inventory = InventoryComponent.model_validate(actor.components.get("inventory"))
            InventoryComponent.model_validate(state.entities[intent.destination_owner_id].components.get("inventory"))
        except Exception:
            return self._rejected(
                context, "shipment_inventory_unavailable", "Source and receiver must have typed inventories."
            )
        arrival_tick = state.current_tick + route.travel_ticks
        if arrival_tick > 2**63 - 1:
            return self._rejected(context, "shipment_tick_overflow", "The route arrival exceeds the world clock range.")
        if route.capacity is not None and active_shipments_on_route(state, route.route_id) >= route.capacity:
            return self._rejected(
                context,
                "shipment_route_capacity_full",
                "The registered route has no free in-transit shipment slot.",
            )
        try:
            allocations = plan_dispatch_lots(
                source_inventory,
                source_account_id=intent.source_account_id,
                resource_id=intent.resource_id,
                quantity=intent.quantity,
                arrival_tick=arrival_tick,
            )
        except ValueError:
            return self._rejected(
                context,
                "shipment_stock_expires_before_arrival",
                "Unexpired FIFO lots do not cover the shipment through its exact arrival tick.",
            )

        dispatch_event_id = self._event_id(context, "shipment.dispatched")
        shipment_id = self._event_id(context, "shipment.entity")
        transit_account_id = self._event_id(context, f"shipment.transit-account:{intent.resource_id}")
        arrival_schedule_id = self._event_id(context, "shipment.arrival-schedule")
        lot_transfers = [
            ShipmentLotManifest(
                source_lot_id=allocation.lot_id,
                transit_lot_id=self._event_id(context, f"shipment.transit-lot:{allocation.lot_id}"),
                quantity=allocation.quantity,
            )
            for allocation in allocations
        ]
        source_lots_by_id = {lot.lot_id: lot for lot in source_inventory.lots}
        spoilage_due_tick = min(
            (
                source_lots_by_id[allocation.lot_id].expiry_tick
                for allocation in allocations
                if source_lots_by_id[allocation.lot_id].expiry_tick is not None
            ),
            default=None,
        )
        spoilage_schedule_id = (
            uuid5(dispatch_event_id, f"shipment.spoilage-schedule:{shipment_id}:{spoilage_due_tick}")
            if spoilage_due_tick is not None else None
        )
        visible_to = sorted({str(actor.id), str(intent.destination_owner_id)})
        event = ShipmentDispatchedEvent(
            event_id=dispatch_event_id,
            shipment_id=shipment_id,
            sender_id=actor.id,
            source_owner_id=intent.source_owner_id,
            destination_owner_id=intent.destination_owner_id,
            origin_place_id=origin.id,
            destination_place_id=route.to_place_id,
            route_id=route.route_id,
            resource_id=intent.resource_id,
            quantity=intent.quantity,
            source_account_id=intent.source_account_id,
            transit_account_id=transit_account_id,
            destination_account_id=intent.destination_account_id,
            arrival_schedule_id=arrival_schedule_id,
            departure_tick=state.current_tick,
            arrival_tick=arrival_tick,
            lifecycle_version="expiry_v2",
            spoilage_schedule_id=spoilage_schedule_id,
            spoilage_due_tick=spoilage_due_tick,
            expected_source_inventory_revision=source_inventory.revision,
            expected_source_account_balance=source_account.balance,
            lot_transfers=lot_transfers,
            visibility={"policy": "subject_ids", "subject_ids": visible_to},
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="shipment_dispatched",
            message="The FIFO cargo has left the sender's stock and is in shipment custody until its scheduled arrival.",
            details={"shipment_id": str(shipment_id), "arrival_tick": arrival_tick},
            events=[event],
        )

    def _resolve_shipment_arrival(
        self,
        state: WorldState,
        scheduled: ScheduledEventRecord,
        context: RuleContext,
    ) -> RuleResolution:
        if scheduled.handler_version != "1.0.0" or set(scheduled.payload) != {"shipment_id"}:
            return self._rejected(context, "shipment_schedule_invalid", "The shipment arrival schedule is not valid version 1.0.0.")
        try:
            shipment_id = UUID(str(scheduled.payload["shipment_id"]))
        except (TypeError, ValueError):
            return self._rejected(context, "shipment_schedule_invalid", "The shipment arrival schedule has an invalid identifier.")
        shipment_entity = state.entities.get(shipment_id)
        if shipment_entity is None or shipment_entity.kind != "shipment":
            return self._rejected(context, "shipment_missing", "The scheduled shipment does not exist.")
        try:
            shipment = ShipmentComponent.model_validate(shipment_entity.components.get("shipment"))
        except Exception:
            return self._rejected(context, "shipment_invalid", "The scheduled shipment state is invalid.")
        if (
            shipment.status != "in_transit"
            or shipment.arrival_schedule_id != scheduled.id
            or shipment.arrival_tick != state.current_tick
            or scheduled.phase != "delivery"
            or scheduled.max_runs != 1
        ):
            return self._rejected(context, "shipment_arrival_stale", "The shipment is not awaiting this exact arrival boundary.")
        visible_to = sorted({str(shipment.sender_id), str(shipment.destination_owner_id)})
        visibility = {"policy": "subject_ids", "subject_ids": visible_to}
        if not shipment_destination_available(state, shipment.destination_owner_id, shipment.destination_place_id):
            event = ShipmentArrivalDeferredEvent(
                event_id=self._event_id(context, f"shipment.arrival-deferred:{shipment_entity.id}:{scheduled.id}"),
                shipment_id=shipment_entity.id,
                expected_shipment_revision=shipment.revision,
                scheduled_event_id=scheduled.id,
                expected_schedule_revision=scheduled.revision,
                arrival_tick=state.current_tick,
                visibility=visibility,
            )
            return self._resolution(
                state,
                context,
                status="partial",
                code="shipment_receipt_pending",
                message="The shipment reached its destination, but its named receiver is unavailable; cargo remains in shipment custody.",
                events=[event],
            )

        if len(shipment.cargo) != 1:
            return self._rejected(context, "shipment_manifest_unsupported", "This resolver version accepts one resource per shipment manifest.")
        cargo = shipment.cargo[0]
        spoilage_schedule = next(
            (item for item in state.scheduled_events if item.id == shipment.spoilage_schedule_id),
            None,
        )
        receiver = state.entities[shipment.destination_owner_id]
        try:
            inventory = InventoryComponent.model_validate(receiver.components.get("inventory"))
        except Exception:
            return self._rejected(context, "shipment_receiver_inventory_invalid", "The shipment receiver has no valid inventory.")
        account = state.resources.get(cargo.destination_account_id)
        if account is None or not _fits_resource_balance(account.balance + cargo.quantity):
            return self._rejected(context, "shipment_receiver_account_limit", "The receiver account cannot hold the arriving quantity.")
        transit_inventory = InventoryComponent.model_validate(shipment_entity.components.get("inventory"))
        lot_by_id = {lot.lot_id: lot for lot in transit_inventory.lots}
        remaining_lots = shipment.remaining_lot_quantities()
        receipts: list[ShipmentLotReceipt] = []
        for manifest_lot in cargo.lots:
            if manifest_lot.transit_lot_id not in remaining_lots:
                continue
            lot = lot_by_id.get(manifest_lot.transit_lot_id)
            if (
                lot is None
                or lot.quantity != remaining_lots[manifest_lot.transit_lot_id]
                or lot.expiry_tick is not None and lot.expiry_tick <= state.current_tick
            ):
                return self._rejected(
                    context,
                    "shipment_spoilage_requires_resolution",
                    "An expired in-transit lot requires the registered spoilage handler before delivery.",
                )
            receipts.append(
                ShipmentLotReceipt(
                    transit_lot_id=lot.lot_id,
                    destination_lot_id=self._event_id(context, f"shipment.receipt-lot:{shipment_entity.id}:{lot.lot_id}"),
                    quantity=lot.quantity,
                )
            )
        event = ShipmentArrivalDeliveredEvent(
            event_id=self._event_id(context, f"shipment.arrival:{shipment_entity.id}:{scheduled.id}"),
            shipment_id=shipment_entity.id,
            expected_shipment_revision=shipment.revision,
            scheduled_event_id=scheduled.id,
            expected_schedule_revision=scheduled.revision,
            expected_spoilage_schedule_revision=(
                spoilage_schedule.revision if spoilage_schedule is not None else None
            ),
            arrival_tick=state.current_tick,
            expected_transit_inventory_revision=transit_inventory.revision,
            expected_destination_inventory_revision=inventory.revision,
            expected_destination_account_balance=account.balance,
            flows=[ShipmentAccountFlow(
                resource_id=cargo.resource_id,
                transit_account_id=cargo.transit_account_id,
                destination_account_id=cargo.destination_account_id,
                quantity=shipment.remaining_quantity,
            )],
            receipts=receipts,
            visibility=visibility,
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="shipment_arrived",
            message="The shipment arrived and the named receiver accepted the exact FIFO manifest.",
            details={"shipment_id": str(shipment_entity.id), "arrival_tick": state.current_tick},
            events=[event],
        )

    def _receive_shipment(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: ReceiveShipmentIntent,
        context: RuleContext,
    ) -> RuleResolution:
        shipment_entity = state.entities.get(intent.shipment_id)
        if shipment_entity is None or shipment_entity.kind != "shipment":
            return self._rejected(context, "shipment_missing", "The shipment is unavailable.")
        try:
            shipment = ShipmentComponent.model_validate(shipment_entity.components.get("shipment"))
        except Exception:
            return self._rejected(context, "shipment_invalid", "The shipment state is invalid.")
        if (
            shipment.status != "awaiting_receipt"
            or shipment.destination_owner_id != actor.id
            or not shipment_destination_available(state, actor.id, shipment.destination_place_id)
        ):
            return self._rejected(
                context,
                "shipment_receipt_unavailable",
                "Only the designated active receiver at the destination can accept this deferred shipment.",
            )
        if len(shipment.cargo) != 1:
            return self._rejected(context, "shipment_manifest_unsupported", "This resolver version accepts one resource per shipment manifest.")
        cargo = shipment.cargo[0]
        if any(item.status == "queued" and item.due_tick <= state.current_tick for item in state.scheduled_events):
            return self._rejected(
                context, "scheduled_event_due", "A due world schedule must resolve before shipment receipt."
            )
        spoilage_schedule = next(
            (item for item in state.scheduled_events if item.id == shipment.spoilage_schedule_id),
            None,
        )
        try:
            inventory = InventoryComponent.model_validate(actor.components.get("inventory"))
            transit_inventory = InventoryComponent.model_validate(shipment_entity.components.get("inventory"))
        except Exception:
            return self._rejected(context, "shipment_inventory_invalid", "The shipment or receiver inventory is invalid.")
        account = state.resources.get(cargo.destination_account_id)
        if account is None or not _fits_resource_balance(account.balance + cargo.quantity):
            return self._rejected(context, "shipment_receiver_account_limit", "The receiver account cannot hold the shipment quantity.")
        lot_by_id = {lot.lot_id: lot for lot in transit_inventory.lots}
        remaining_lots = shipment.remaining_lot_quantities()
        receipts: list[ShipmentLotReceipt] = []
        for manifest_lot in cargo.lots:
            if manifest_lot.transit_lot_id not in remaining_lots:
                continue
            lot = lot_by_id.get(manifest_lot.transit_lot_id)
            if lot is None or lot.quantity != remaining_lots[manifest_lot.transit_lot_id]:
                return self._rejected(context, "shipment_manifest_invalid", "Shipment custody no longer matches its FIFO manifest.")
            if lot.expiry_tick is not None and lot.expiry_tick <= state.current_tick:
                return self._rejected(
                    context,
                    "shipment_spoilage_requires_resolution",
                    "An expired in-transit lot requires the registered spoilage handler before receipt.",
                )
            receipts.append(
                ShipmentLotReceipt(
                    transit_lot_id=lot.lot_id,
                    destination_lot_id=self._event_id(context, f"shipment.receipt-lot:{shipment_entity.id}:{lot.lot_id}"),
                    quantity=lot.quantity,
                )
            )
        visible_to = sorted({str(shipment.sender_id), str(actor.id)})
        event = ShipmentReceiptAcceptedEvent(
            event_id=self._event_id(context, f"shipment.received:{shipment_entity.id}"),
            shipment_id=shipment_entity.id,
            expected_shipment_revision=shipment.revision,
            receiver_id=actor.id,
            received_tick=state.current_tick,
            expected_spoilage_schedule_revision=(
                spoilage_schedule.revision if spoilage_schedule is not None else None
            ),
            expected_transit_inventory_revision=transit_inventory.revision,
            expected_destination_inventory_revision=inventory.revision,
            expected_destination_account_balance=account.balance,
            flows=[ShipmentAccountFlow(
                resource_id=cargo.resource_id,
                transit_account_id=cargo.transit_account_id,
                destination_account_id=cargo.destination_account_id,
                quantity=shipment.remaining_quantity,
            )],
            receipts=receipts,
            visibility={"policy": "subject_ids", "subject_ids": visible_to},
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="shipment_received",
            message="The designated receiver accepted the deferred shipment from shipment custody.",
            details={"shipment_id": str(shipment_entity.id)},
            events=[event],
        )

    def _distribute_shipment_relief(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: DistributeShipmentReliefIntent,
        context: RuleContext,
    ) -> RuleResolution:
        try:
            plan = plan_shipment_relief(
                state,
                actor_id=actor.id,
                organization_id=intent.organization_id,
                shipment_id=intent.shipment_id,
                recipient_id=intent.recipient_id,
                quantity=intent.quantity,
            )
        except DomainError:
            return self._rejected(
                context,
                "shipment_relief_unavailable",
                "This shipment relief distribution is unavailable in the current state.",
            )

        event = ShipmentReliefDistributedEvent(
            event_id=shipment_relief_event_id(
                context.command_id,
                context.ruleset_version,
                plan.shipment_id,
                plan.recipient_id,
            ),
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            actor_id=actor.id,
            ruleset_version=context.ruleset_version,
            plan=plan,
            visibility=shipment_relief_visibility(actor.id, plan.recipient_id),
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="shipment_relief_distributed",
            message="Shipment relief was distributed according to the registered plan.",
            events=[event],
        )

    def _resolve_shipment_spoilage(
        self,
        state: WorldState,
        scheduled: ScheduledEventRecord,
        context: RuleContext,
    ) -> RuleResolution:
        if scheduled.handler_version != "1.0.0" or set(scheduled.payload) != {"shipment_id"}:
            return self._rejected(context, "shipment_spoilage_schedule_invalid", "The shipment spoilage schedule is not valid version 1.0.0.")
        try:
            shipment_id = UUID(str(scheduled.payload["shipment_id"]))
        except (TypeError, ValueError):
            return self._rejected(context, "shipment_spoilage_schedule_invalid", "The shipment spoilage schedule has an invalid identifier.")
        shipment_entity = state.entities.get(shipment_id)
        if shipment_entity is None or shipment_entity.kind != "shipment":
            return self._rejected(context, "shipment_missing", "The scheduled shipment does not exist.")
        try:
            shipment = ShipmentComponent.model_validate(shipment_entity.components.get("shipment"))
            transit_inventory = InventoryComponent.model_validate(shipment_entity.components.get("inventory"))
        except Exception:
            return self._rejected(context, "shipment_invalid", "The shipment state is invalid.")
        if (
            shipment.lifecycle_version != "expiry_v2"
            or shipment.status not in {"in_transit", "awaiting_receipt"}
            or shipment.spoilage_schedule_id != scheduled.id
            or scheduled.phase != "delivery"
            or scheduled.due_tick != state.current_tick
            or scheduled.max_runs != 1
            or scheduled.repeat_ticks is not None
        ):
            return self._rejected(context, "shipment_spoilage_stale", "The shipment is not at this exact expiry boundary.")
        if shipment.status == "in_transit":
            return self._rejected(context, "shipment_arrival_pending", "The shipment arrival must resolve before cargo expiry.")
        if len(shipment.cargo) != 1:
            return self._rejected(context, "shipment_manifest_unsupported", "This expiry handler accepts one resource per shipment manifest.")
        cargo = shipment.cargo[0]
        transit_account = state.resources.get(cargo.transit_account_id)
        if transit_account is None:
            return self._rejected(context, "shipment_transit_account_missing", "Shipment custody account is unavailable.")
        current_due = min(
            (lot.expiry_tick for lot in transit_inventory.lots if lot.expiry_tick is not None),
            default=None,
        )
        if current_due != state.current_tick:
            return self._rejected(context, "shipment_spoilage_boundary_mismatch", "The schedule must match the earliest remaining cargo expiry.")
        expired_lots = sorted(
            (lot for lot in transit_inventory.lots if lot.expiry_tick is not None and lot.expiry_tick <= state.current_tick),
            key=lambda lot: str(lot.lot_id),
        )
        if not expired_lots:
            return self._rejected(context, "shipment_spoilage_nothing_due", "No transit lot has reached its expiry boundary.")

        roles = state.metadata.get("resource_roles", {})
        configured = roles.get("destruction_sink_account_ids", {}) if isinstance(roles, dict) else {}
        has_configured_sink = isinstance(configured, dict) and cargo.resource_id in configured
        if has_configured_sink:
            try:
                sink_id = UUID(str(configured[cargo.resource_id]))
            except (TypeError, ValueError):
                return self._rejected(context, "shipment_destruction_sink_invalid", "The configured destruction sink ID is invalid.")
            if sink_id not in state.resources:
                return self._rejected(context, "shipment_destruction_sink_missing", "The configured destruction sink account is missing.")
        else:
            sink_id = uuid5(context.world_id, f"resource-destruction-sink:{cargo.resource_id}")
        sink = state.resources.get(sink_id)
        create_sink = sink is None
        if sink is not None and (
            sink.account_type != "destruction_sink"
            or sink.owner_id is not None
            or sink.resource_id != cargo.resource_id
        ):
            return self._rejected(context, "shipment_destruction_sink_collision", "The stable destruction sink ID is occupied by an incompatible account.")
        if create_sink and has_configured_sink:
            return self._rejected(context, "shipment_destruction_sink_missing", "A configured destruction sink cannot be synthesized during expiry.")
        expired_ids = {lot.lot_id for lot in expired_lots}
        remaining_inventory = [lot for lot in transit_inventory.lots if lot.lot_id not in expired_ids]
        next_due = min(
            (lot.expiry_tick for lot in remaining_inventory if lot.expiry_tick is not None),
            default=None,
        )
        event_id = self._event_id(context, f"shipment.spoilage:{shipment_entity.id}:{scheduled.id}")
        spoiled = [
            ShipmentSpoiledLot(
                transit_lot_id=lot.lot_id,
                quantity=lot.quantity,
                spoiled_tick=state.current_tick,
                event_id=event_id,
                destruction_sink_account_id=sink_id,
            )
            for lot in expired_lots
        ]
        next_schedule_id = (
            self._event_id(context, f"shipment.spoilage-schedule:{shipment_entity.id}:{next_due}")
            if next_due is not None else None
        )
        event = ShipmentSpoilageResolvedEvent(
            event_id=event_id,
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            ruleset_version=context.ruleset_version,
            shipment_id=shipment_entity.id,
            expected_shipment_revision=shipment.revision,
            scheduled_event_id=scheduled.id,
            expected_schedule_revision=scheduled.revision,
            spoiled_tick=state.current_tick,
            expected_transit_inventory_revision=transit_inventory.revision,
            expected_transit_account_balance=transit_account.balance,
            resource_id=cargo.resource_id,
            transit_account_id=cargo.transit_account_id,
            destruction_sink_account_id=sink_id,
            expected_destruction_sink_balance=sink.balance if sink is not None else None,
            create_destruction_sink=create_sink,
            spoiled_lots=spoiled,
            next_spoilage_schedule_id=next_schedule_id,
            next_spoilage_due_tick=next_due,
            visibility={
                "policy": "subject_ids",
                "subject_ids": sorted({str(shipment.sender_id), str(shipment.destination_owner_id)}),
            },
        )
        remaining_quantity = shipment.remaining_quantity - sum(
            (lot.quantity for lot in expired_lots), Decimal("0")
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="shipment_spoiled" if remaining_quantity == 0 else "shipment_partial_spoilage",
            message=(
                "All remaining shipment cargo expired in custody."
                if remaining_quantity == 0
                else "Expired shipment lots were recorded; unexpired lots remain in shipment custody."
            ),
            details={
                "shipment_id": str(shipment_entity.id),
                "spoiled_quantity": str(sum((lot.quantity for lot in expired_lots), Decimal("0"))),
                "remaining_quantity": str(remaining_quantity),
                "spoiled_tick": state.current_tick,
            },
            events=[event],
        )

    def _consume(
        self, state: WorldState, actor: WorldEntity, intent: ConsumeIntent, context: RuleContext
    ) -> RuleResolution:
        duration = intent.duration_ticks
        issue = self._duration_issue(state, actor, duration)
        if issue is not None:
            return self._rejected(context, *issue)
        suitable = (
            is_medicine_resource(intent.resource_id) if intent.need == "health"
            else resource_matches_need(state.metadata, intent.resource_id, intent.need)
        )
        if not suitable:
            return self._rejected(context, "resource_not_suitable", f"{intent.resource_id} cannot restore {intent.need}.")
        source = self._holder_account(state, actor.id, intent.resource_id)
        sink = self._system_account(state, intent.resource_id, _CONSUMPTION_SINK_TYPES)
        if source is None or sink is None:
            return self._rejected(context, "resource_account_missing", "A matching holder or consumption sink account is missing.")
        if source.balance < intent.quantity:
            return self._rejected(context, "insufficient_resource", "The character does not hold enough of that resource.")
        try:
            inventory_event, _ = self._inventory_reduction(
                state,
                actor,
                [(self._account_id(state, source), intent.resource_id, intent.quantity)],
                context,
                "consume.inventory",
            )
        except ValueError as exc:
            return self._rejected(context, "inventory_insufficient", str(exc))
        consumed = ResourceConsumedEvent(
            event_id=self._event_id(context, "consume.resource"),
            consumer_id=actor.id,
            purpose=f"need:{intent.need}",
            flows=[
                ConsumedResourceFlow(
                    holder_account_id=self._account_id(state, source),
                    sink_account_id=self._account_id(state, sink),
                    resource_id=intent.resource_id,
                    quantity=intent.quantity,
                )
            ],
        )
        effects: list[DomainEvent] = [item for item in (inventory_event,) if item is not None]
        effects.append(consumed)
        restoration: dict[str, int] = {}
        if intent.need in {"food", "water"}:
            needs = self._needs(actor)
            need_value = getattr(needs, intent.need) if needs is not None else None
            if needs is None or need_value is None:
                return self._rejected(context, "need_missing", f"The character has no tracked {intent.need} need.")
            points = int((intent.quantity * Decimal("10")).to_integral_value(rounding=ROUND_FLOOR))
            if points <= 0:
                return self._rejected(context, "quantity_too_small", "The quantity is too small to restore a need.")
            if need_value.value >= 100:
                return self._rejected(context, "need_satisfied", f"The character's {intent.need} need is already satisfied.")
            restoration[intent.need] = points
        else:
            health = self._health(actor)
            if health is None or health.current >= health.maximum:
                return self._rejected(context, "healing_unavailable", "The character has no health deficit to treat.")
            points = int((intent.quantity * Decimal("10")).to_integral_value(rounding=ROUND_FLOOR))
            if points <= 0:
                return self._rejected(context, "quantity_too_small", "The quantity is too small to restore health.")
            points = min(points, health.maximum - health.current)

        needs_event, deprivation = self._time_need_effects(
            state, actor, duration, context, rest_delta=0, restore=restoration
        )
        if needs_event is not None:
            effects.append(needs_event)
        if intent.need == "health":
            health = self._health(actor)
            healing = HealthRestoredEvent(
                event_id=self._event_id(context, "consume.healing"),
                target_id=actor.id,
                amount=points,
                method="core.medicine",
                expected_health_revision=health.revision,
                source_event_ids=[consumed.event_id],
            )
            effects.append(healing)
            if deprivation is not None:
                deprivation = deprivation.model_copy(
                    update={"expected_health_revision": deprivation.expected_health_revision + 1}
                )
        if deprivation is not None:
            effects.append(deprivation)
        life_event = self._lifespan_event(state, actor, duration, context, effects)
        if life_event is not None:
            effects.append(life_event)
        killed = deprivation is not None and self._damage_is_lethal(actor, deprivation.damage)
        if life_event is not None:
            status, code, message = "partial", "lifespan_expired", "The character reached the end of their lifespan while consuming the resource."
        elif killed:
            status, code, message = "failed", "died_of_deprivation", "The character died from hunger or thirst during the action."
        elif deprivation is not None:
            status, code, message = "partial", "survival_pressure", "The resource was consumed, but hunger or thirst caused an injury."
        else:
            status, code, message = "succeeded", "consumed", f"The character consumed {intent.resource_id} to address {intent.need}."
        return self._resolution(
            state,
            context,
            status=status,
            code=code,
            message=message,
            details={"need": intent.need, "resource_id": intent.resource_id, "quantity": format(intent.quantity, "f")},
            elapsed_ticks=duration,
            events=effects,
        )

    def _treat_injury(
        self, state: WorldState, actor: WorldEntity, intent: TreatInjuryIntent, context: RuleContext
    ) -> RuleResolution:
        target = state.entities.get(intent.target_id)
        if actor.status != "active":
            return self._rejected(context, "healer_unavailable", "Injury treatment requires an active healer.")
        if (
            target is None or target.kind not in {"character", "person"}
            or target.status not in {"active", "incapacitated"}
        ):
            return self._rejected(
                context, "treatment_target_unavailable", "Injury treatment requires an active or incapacitated patient.",
            )
        if actor.location_id is None or target.location_id != actor.location_id:
            return self._rejected(context, "treatment_target_out_of_reach", "The healer and patient must be at the same place.")
        if target.id != actor.id and target.components.get("hidden") is True and target.id not in _observed_entities(state, actor.id):
            return self._rejected(context, "target_unseen", "The treatment target is not visible to the healer.")
        if settled_treatment_place(state, actor.location_id) is None:
            return self._rejected(
                context,
                "treatment_location_unavailable",
                "Injury treatment requires an active registered settlement or building.",
            )
        skill = care_skill(actor)
        if skill is None:
            return self._rejected(context, "care_skill_unavailable", "The healer has no registered care skill.")
        skill_revision, skill_level = skill
        if skill_level < INJURY_TREATMENT_MIN_CARE:
            return self._rejected(context, "care_skill_insufficient", "Injury treatment requires skill.care level 10 or higher.")
        if not is_medicine_resource(intent.medicine_resource_id):
            return self._rejected(context, "resource_not_suitable", "The selected resource is not a registered medicine.")
        try:
            health = self._health(target)
        except Exception:
            health = None
        if health is None:
            return self._rejected(context, "health_unavailable", "The treatment target has no registered health component.")
        recovering_patient = target.status == "incapacitated"
        if recovering_patient and health.incapacitating_injury_id != intent.injury_id:
            return self._rejected(context, "injury_unavailable", "Treat the injury that incapacitated the patient first.")
        injury_index = next(
            (index for index, injury in enumerate(health.injuries) if injury.injury_id == intent.injury_id),
            None,
        )
        if injury_index is None or health.injuries[injury_index].treated:
            return self._rejected(context, "injury_unavailable", "Choose one known, existing injury that has not been treated.")
        duration = INJURY_TREATMENT_DURATION
        issue = self._duration_issue(state, actor, duration)
        if issue is not None:
            return self._rejected(context, *issue)
        source = self._holder_account(state, actor.id, intent.medicine_resource_id)
        sink = self._system_account(state, intent.medicine_resource_id, _CONSUMPTION_SINK_TYPES)
        if source is None or sink is None or source.balance < 1:
            return self._rejected(context, "medicine_unavailable", "The healer needs one unit of held medicine.")
        try:
            inventory_event, _ = self._inventory_reduction(
                state,
                actor,
                [(self._account_id(state, source), intent.medicine_resource_id, Decimal("1"))],
                context,
                "injury_treatment.inventory",
            )
        except ValueError as exc:
            return self._rejected(context, "medicine_unavailable", str(exc))
        consumed = ResourceConsumedEvent(
            event_id=self._event_id(context, "injury_treatment.supply"),
            consumer_id=actor.id,
            purpose="injury_treatment",
            flows=[ConsumedResourceFlow(
                holder_account_id=self._account_id(state, source),
                sink_account_id=self._account_id(state, sink),
                resource_id=intent.medicine_resource_id,
                quantity=Decimal("1"),
            )],
        )
        healing_amount = min(INJURY_TREATMENT_HEALING, health.maximum - health.current)
        treatment = InjuryTreatedEvent(
            event_id=self._event_id(context, "injury_treatment.applied"),
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            ruleset_version=context.ruleset_version,
            policy_version=INJURY_TREATMENT_POLICY,
            healer_id=actor.id,
            target_id=target.id,
            injury_id=intent.injury_id,
            location_id=actor.location_id,
            expected_health_revision=health.revision,
            expected_skills_revision=skill_revision,
            skill_level=skill_level,
            medicine_resource_id=intent.medicine_resource_id,
            consumed_event_id=consumed.event_id,
            started_tick=state.current_tick,
            resolved_tick=state.current_tick + duration,
            healing_amount=healing_amount,
            provenance=[ProvenanceReference(kind="rule", reference_id=INJURY_TREATMENT_POLICY)],
            visibility={"policy": "subject_ids", "subject_ids": sorted({str(actor.id), str(target.id)})},
        )
        needs_event, deprivation = self._time_need_effects(state, actor, duration, context)
        events: list[DomainEvent] = [event for event in (inventory_event, consumed, needs_event) if event is not None]
        events.append(treatment)
        healing: HealthRestoredEvent | None = None
        if healing_amount > 0:
            healing = HealthRestoredEvent(
                event_id=self._event_id(context, "injury_treatment.heal"),
                target_id=target.id,
                amount=healing_amount,
                method="core.injury_treatment",
                expected_health_revision=health.revision + 1,
                source_event_ids=[consumed.event_id, treatment.event_id],
            )
            events.append(healing)
        if deprivation is not None:
            if deprivation.target_id == target.id:
                deprivation = deprivation.model_copy(update={
                    "expected_health_revision": deprivation.expected_health_revision + 1 + int(healing_amount > 0),
                })
            events.append(deprivation)
        life_event = self._lifespan_event(state, actor, duration, context, events)
        if life_event is not None:
            events.append(life_event)

        actor_health = self._health(actor)
        effective_actor_health = (actor_health.current if actor_health is not None else 0)
        if target.id == actor.id:
            effective_actor_health += healing_amount
        died = deprivation is not None and deprivation.target_id == actor.id and deprivation.damage >= effective_actor_health
        if life_event is not None:
            status, code, message = "partial", "lifespan_expired", "Treatment completed as the healer reached the end of their lifespan."
        elif died:
            status, code, message = "failed", "died_of_deprivation", "The healer died from hunger or thirst during treatment."
        elif deprivation is not None:
            status, code, message = "partial", "survival_pressure", "Treatment completed, but hunger or thirst caused an injury."
        elif recovering_patient:
            status, code, message = "succeeded", "injury_treated", "Treatment of the disabling injury ended the patient's incapacitation."
        else:
            status, code, message = "succeeded", "injury_treated", (
                "The selected injury was treated and up to five health was restored."
                if healing_amount > 0
                else "The selected injury was treated at full health."
            )
        if recovering_patient and status != "succeeded":
            message += " Treatment of the disabling injury ended the patient's incapacitation."
        return self._resolution(
            state,
            context,
            status=status,
            code=code,
            message=message,
            details={
                "target_id": str(target.id),
                "injury_id": str(intent.injury_id),
                "resource_id": intent.medicine_resource_id,
                "healing_amount": healing_amount,
                "duration_seconds": duration,
                **({"patient_recovered": True} if recovering_patient else {}),
            },
            elapsed_ticks=duration,
            events=events,
        )

    def _trade(self, state: WorldState, actor: WorldEntity, intent: TradeIntent, context: RuleContext) -> RuleResolution:
        duration = NPC_TRADE_TICKS
        issue = self._duration_issue(state, actor, duration)
        if issue is not None:
            return self._rejected(context, *issue)
        settlement = self._trade_settlement(state, actor, intent, context)
        if settlement.outcome.status == "rejected":
            return settlement
        needs_event, deprivation = self._time_need_effects(state, actor, duration, context)
        events: list[DomainEvent] = [item for item in (needs_event,) if item is not None]
        events.extend(settlement.events)
        if deprivation is not None:
            events.append(deprivation)
        life_event = self._lifespan_event(state, actor, duration, context, events)
        status, code, message = settlement.outcome.status, settlement.outcome.code, settlement.outcome.message
        if life_event is not None:
            events.append(life_event)
            status, code, message = "partial", "lifespan_expired", "The trade settled as the character reached the end of their lifespan."
        elif deprivation is not None:
            status, code, message = "partial", "survival_pressure", "The trade settled, but hunger or thirst caused an injury."
        return self._resolution(
            state, context, status=status, code=code, message=message,
            details=settlement.outcome.details, elapsed_ticks=duration, events=events,
        )

    def _trade_settlement(
        self, state: WorldState, actor: WorldEntity, intent: TradeIntent, context: RuleContext,
    ) -> RuleResolution:
        """Build the atomic exchange against current stock, cash and the current listing."""
        market_entity = state.entities.get(actor.location_id) if actor.location_id is not None else None
        if market_entity is None or market_entity.status != "active":
            return self._rejected(context, "market_unavailable", "The character must be at an active market.")
        if market_entity.components.get("hidden") is True and market_entity.id not in _observed_entities(state, actor.id):
            return self._rejected(context, "market_unavailable", "The character must be at an active market.")
        try:
            market = MarketComponent.model_validate(market_entity.components.get("market"))
        except Exception:
            return self._rejected(context, "market_unavailable", "There is no valid registered market here.")
        ceiling = intent.price_ceiling
        if ceiling is not None and ceiling.currency_resource_id != market.currency_resource_id:
            return self._rejected(
                context,
                "price_ceiling_currency_mismatch",
                "The market currency no longer matches the purchase price ceiling.",
            )
        offer = next((candidate for candidate in market.offers if candidate.listing_id == intent.listing_id), None)
        if offer is None or not offer.active:
            return self._rejected(context, "listing_unavailable", "That market listing is not active.")
        seller = state.entities.get(offer.seller_id)
        if (seller is not None and seller.id != actor.id and seller.components.get("hidden") is True
                and seller.id not in _observed_entities(state, actor.id)):
            return self._rejected(context, "listing_unavailable", "That market listing is not active.")
        if intent.quantity < offer.minimum_quantity:
            return self._rejected(context, "below_minimum_quantity", "The requested quantity is below the listing minimum.")
        if offer.resource_id == market.currency_resource_id:
            return self._rejected(context, "invalid_market_offer", "A listing cannot sell the market's own currency for itself.")
        if seller is None or seller.status != "active" or seller.location_id != market_entity.id:
            return self._rejected(context, "seller_unavailable", "The listed seller is not present at this market.")
        try:
            stock = state.resources[offer.stock_account_id]
            seller_inventory = InventoryComponent.model_validate(seller.components.get("inventory"))
        except Exception:
            return self._rejected(context, "listing_unavailable", "The listing has no valid seller stock account or inventory.")
        if stock.account_type != "holder" or stock.owner_id != seller.id or stock.resource_id != offer.resource_id:
            return self._rejected(context, "listing_unavailable", "The listing stock account does not belong to its seller.")
        buyer_money = self._holder_account(state, actor.id, market.currency_resource_id)
        seller_money = self._holder_account(state, seller.id, market.currency_resource_id)
        buyer_goods = self._holder_account(state, actor.id, offer.resource_id)
        with localcontext() as decimal_context:
            decimal_context.prec = 80
            payment = intent.quantity * offer.unit_price_minor
        if payment != payment.to_integral_value():
            return self._rejected(context, "fractional_minor_unit", "The requested quantity does not resolve to an integer minor-unit payment.")
        if ceiling is not None and payment > ceiling.max_total_minor:
            return self._rejected(
                context,
                "price_ceiling_exceeded",
                "The current whole-transaction payment exceeds the purchase price ceiling.",
            )
        if buyer_money is None or buyer_money.balance < payment:
            return self._rejected(context, "insufficient_currency", "The character does not hold enough market currency.")
        seller_money_balance = seller_money.balance if seller_money is not None else Decimal("0")
        buyer_goods_balance = buyer_goods.balance if buyer_goods is not None else Decimal("0")
        if not _fits_resource_balance(seller_money_balance + payment):
            return self._rejected(context, "currency_balance_limit", "The seller's currency account cannot hold the sale proceeds.")
        if not _fits_resource_balance(buyer_goods_balance + intent.quantity):
            return self._rejected(context, "resource_balance_limit", "The buyer's resource account cannot hold this quantity.")
        try:
            _, source_lot_ids = self._inventory_reduction(
                state,
                seller,
                [(offer.stock_account_id, offer.resource_id, intent.quantity)],
                context,
                "trade.inventory.plan",
            )
        except ValueError as exc:
            return self._rejected(context, "insufficient_stock", str(exc))
        account_events: list[DomainEvent] = []

        def ensure_holder(owner: WorldEntity, resource_id: str, current: ResourceAccount | None, label: str) -> UUID:
            if current is not None:
                return self._account_id(state, current)
            new_id = self._event_id(context, f"trade.account.{label}.{owner.id}.{resource_id}")
            account_events.append(
                ResourceAccountOpenedEvent(
                    event_id=self._event_id(context, f"trade.account_opened.{label}"),
                    account_id=new_id,
                    owner_id=owner.id,
                    resource_id=resource_id,
                )
            )
            return new_id

        buyer_goods_id = ensure_holder(actor, offer.resource_id, buyer_goods, "buyer_goods")
        buyer_money_id = ensure_holder(actor, market.currency_resource_id, buyer_money, "buyer_currency")
        seller_money_id = ensure_holder(seller, market.currency_resource_id, seller_money, "seller_currency")
        buyer_inventory_raw = actor.components.get("inventory")
        try:
            buyer_inventory = InventoryComponent.model_validate(buyer_inventory_raw) if buyer_inventory_raw is not None else None
        except Exception:
            return self._rejected(context, "invalid_inventory", "The buyer inventory is invalid.")
        trade = MarketTradeExecutedEvent(
            event_id=self._event_id(context, "trade.settled"),
            buyer_id=actor.id,
            seller_id=seller.id,
            market_id=market_entity.id,
            listing_id=offer.listing_id,
            resource_id=offer.resource_id,
            currency_resource_id=market.currency_resource_id,
            quantity=intent.quantity,
            unit_price_minor=offer.unit_price_minor,
            stock_account_id=offer.stock_account_id,
            buyer_resource_account_id=buyer_goods_id,
            buyer_currency_account_id=buyer_money_id,
            seller_currency_account_id=seller_money_id,
            source_lot_ids=source_lot_ids,
            buyer_lot_id=self._event_id(context, "trade.buyer_lot"),
            expected_seller_inventory_revision=seller_inventory.revision,
            expected_buyer_inventory_revision=buyer_inventory.revision if buyer_inventory else None,
        )
        # This is a proposal. The caller validates the final ordered batch at
        # the instant when its accounts and inventory will actually change.
        return RuleResolution(
            ruleset_version=context.ruleset_version, events=[*account_events, trade],
            outcome=RuleOutcome(
                status="succeeded", code="trade_completed", message="The market trade settled against the registered offer.",
                details={"listing_id": str(offer.listing_id), "resource_id": offer.resource_id,
                         "quantity": format(intent.quantity, "f"), "payment_minor": format(payment, "f")},
            ),
        )

    def _produce(self, state: WorldState, actor: WorldEntity, intent: ProduceIntent, context: RuleContext) -> RuleResolution:
        settlement = self._production_settlement(state, actor, intent, context, check_advance=True)
        if settlement.outcome.status == "rejected":
            return settlement
        duration = int(settlement.outcome.details["duration_seconds"])
        needs_event, deprivation = self._time_need_effects(state, actor, duration, context)
        events: list[DomainEvent] = []
        for event in settlement.events:
            if isinstance(event, ResourceProducedEvent) and needs_event is not None:
                events.append(needs_event)
            events.append(event)
        if deprivation is not None:
            events.append(deprivation)
        life_event = self._lifespan_event(state, actor, duration, context, events)
        if life_event is not None:
            events.append(life_event)
        status, code, message = settlement.outcome.status, settlement.outcome.code, settlement.outcome.message
        if life_event is not None:
            status, code, message = "partial", "lifespan_expired", "Production completed as the character reached the end of their lifespan."
        elif deprivation is not None:
            status, code, message = "partial", "survival_pressure", "Production completed, but hunger or thirst caused an injury."
        return self._resolution(
            state, context, status=status, code=code, message=message, details=settlement.outcome.details,
            elapsed_ticks=duration, events=events,
        )

    def _production_settlement(
        self, state: WorldState, actor: WorldEntity, intent: ProduceIntent, context: RuleContext, *, check_advance: bool = False,
    ) -> RuleResolution:
        """Build the registered, atomic inventory conversion without advancing time."""
        producer = state.entities.get(intent.producer_id)
        if producer is None or producer.status != "active":
            return self._rejected(context, "producer_unavailable", "The registered producer is not active.")
        colocated = producer.id == actor.id or actor.location_id == producer.id or (
            producer.location_id is not None and producer.location_id == actor.location_id
        )
        if not colocated:
            return self._rejected(context, "producer_unavailable", "The character must be present at the producer.")
        try:
            production = ProductionComponent.model_validate(producer.components.get("production"))
        except Exception:
            return self._rejected(context, "recipe_unavailable", "The producer has no valid registered production recipes.")
        recipe = next((entry for entry in production.recipes if entry.recipe_id == intent.recipe_id), None)
        if recipe is None:
            return self._rejected(context, "recipe_unavailable", "That recipe is not registered for this producer.")
        if recipe.required_skill is not None and self._skill_level(actor, recipe.required_skill) < recipe.required_skill_level:
            return self._rejected(context, "skill_requirement_unmet", "The character lacks the required skill level for this recipe.")
        with localcontext() as decimal_context:
            decimal_context.prec = 50
            input_requirements = [
                (flow.resource_id, flow.quantity * intent.batch_count) for flow in recipe.inputs
            ]
            output_requirements = [
                (flow.resource_id, flow.quantity * intent.batch_count) for flow in recipe.outputs
            ]
        duration = recipe.duration_ticks * intent.batch_count
        if check_advance:
            issue = self._duration_issue(state, actor, duration)
            if issue is not None:
                return self._rejected(context, *issue)

        input_flows: list[ConsumedResourceFlow] = []
        inventory_requirements: list[tuple[UUID, str, Decimal]] = []
        for resource_id, quantity in input_requirements:
            holder = self._holder_account(state, producer.id, resource_id)
            sink = self._system_account(state, resource_id, _CONSUMPTION_SINK_TYPES)
            if holder is None or sink is None:
                return self._rejected(context, "production_account_missing", f"No holder or consumption sink exists for {resource_id}.")
            if holder.balance < quantity:
                return self._rejected(context, "insufficient_input", f"The producer lacks enough {resource_id} for this recipe.")
            inventory_requirements.append((self._account_id(state, holder), resource_id, quantity))
            input_flows.append(
                ConsumedResourceFlow(
                    holder_account_id=self._account_id(state, holder),
                    sink_account_id=self._account_id(state, sink),
                    resource_id=resource_id,
                    quantity=quantity,
                )
            )

        output_flows: list[ProducedResourceFlow] = []
        account_events: list[DomainEvent] = []
        for resource_id, quantity in output_requirements:
            source = next(
                (
                    account for account in state.resources.values()
                    if account.account_type == "production_source"
                    and account.owner_id == producer.id
                    and account.resource_id == resource_id
                ),
                None,
            )
            if source is None or source.credit_limit is None:
                return self._rejected(context, "production_quota_missing", f"No finite producer quota exists for {resource_id}.")
            with localcontext() as decimal_context:
                decimal_context.prec = 50
                if source.balance - quantity < source.credit_limit.copy_negate():
                    return self._rejected(context, "production_quota_exhausted", f"The finite production quota for {resource_id} is exhausted.")
            holder = self._holder_account(state, producer.id, resource_id)
            current_output_balance = holder.balance if holder is not None else Decimal("0")
            if not _fits_resource_balance(current_output_balance + quantity):
                return self._rejected(context, "resource_balance_limit", f"The producer cannot hold more {resource_id}.")
            if holder is None:
                holder_id = self._event_id(context, f"produce.account.{producer.id}.{resource_id}")
                account_events.append(
                    ResourceAccountOpenedEvent(
                        event_id=self._event_id(context, f"produce.account_opened.{resource_id}"),
                        account_id=holder_id,
                        owner_id=producer.id,
                        resource_id=resource_id,
                    )
                )
            else:
                holder_id = self._account_id(state, holder)
            output_flows.append(
                ProducedResourceFlow(
                    source_account_id=self._account_id(state, source),
                    holder_account_id=holder_id,
                    resource_id=resource_id,
                    quantity=quantity,
                )
            )

        try:
            inventory_event, _ = self._inventory_reduction(
                state, producer, inventory_requirements, context, "produce.inventory_inputs"
            )
            if inventory_event is None:
                return self._rejected(context, "invalid_recipe", "A production recipe must consume registered inventory lots.")
            reduced_inventory = InventoryComponent.model_validate(inventory_event.replacement)
        except ValueError as exc:
            return self._rejected(context, "insufficient_input_lots", str(exc))

        conversions: list[ResourceConversion] = []
        steps = max(len(input_requirements), len(output_requirements))
        for index in range(steps):
            input_resource, input_quantity = input_requirements[index] if index < len(input_requirements) else (output_requirements[index][0], Decimal("0"))
            output_resource, output_quantity = output_requirements[index] if index < len(output_requirements) else (input_resource, Decimal("0"))
            conversions.append(
                ResourceConversion(
                    step_id=f"recipe.step.{index}",
                    input_resource_id=input_resource,
                    input_quantity=input_quantity,
                    output_resource_id=output_resource,
                    output_quantity=output_quantity,
                )
            )
        try:
            produced = ResourceProducedEvent(
                event_id=self._event_id(context, "produce.completed"),
                producer_id=producer.id,
                recipe_id=recipe.recipe_id,
                batch_count=intent.batch_count,
                consumed_inputs=input_flows,
                produced_outputs=output_flows,
                conversions=conversions,
                provenance=[
                    ProvenanceReference(
                        kind="rule",
                        reference_id=f"{context.ruleset_version}:production:{recipe.recipe_id}",
                        notes="Validated registered producer recipe and finite source quota.",
                    )
                ],
            )
        except Exception as exc:
            return self._rejected(context, "invalid_recipe_batch", f"The selected batch exceeds a registered quantity bound: {exc}")

        output_lots = [
            InventoryLot(
                lot_id=self._event_id(context, f"produce.output_lot.{index}"),
                account_id=flow.holder_account_id,
                resource_id=flow.resource_id,
                quantity=flow.quantity,
                origin_event_id=produced.event_id,
            )
            for index, flow in enumerate(output_flows)
        ]
        output_inventory = reduced_inventory.model_copy(
            update={"revision": reduced_inventory.revision + 1, "lots": [*reduced_inventory.lots, *output_lots]}
        )
        inventory_output_event = ComponentReplacedEvent(
            event_id=self._event_id(context, "produce.inventory_outputs"),
            entity_id=producer.id,
            component_name="inventory",
            expected_revision=reduced_inventory.revision,
            replacement=output_inventory.model_dump(mode="json"),
        )
        details: dict[str, object] = {
            "producer_id": str(producer.id), "recipe_id": recipe.recipe_id,
            "batch_count": intent.batch_count, "duration_seconds": duration,
        }
        if not check_advance:
            details["recipe_terms_hash"] = sha256(recipe.model_dump_json().encode("utf-8")).hexdigest()
        return RuleResolution(
            ruleset_version=context.ruleset_version,
            outcome=RuleOutcome(
                status="succeeded", code="production_completed",
                message="The registered recipe consumed inputs and produced its listed outputs.", details=details,
            ),
            events=[*account_events, inventory_event, produced, inventory_output_event],
        )

    def _leave_organization(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: LeaveOrganizationIntent,
        context: RuleContext,
    ) -> RuleResolution:
        if actor.status != "active":
            return self._rejected(context, "actor_terminal", "An inactive character cannot act.")
        organization = state.entities.get(intent.organization_id)
        organization_kinds = {"organization", "polity", "faction", "company", "government"}
        if (
            organization is None
            or organization.kind not in organization_kinds
            or organization.status != "active"
        ):
            return self._rejected(
                context,
                "membership_unavailable",
                "The requested organization membership cannot be changed from this state.",
            )
        try:
            governance = GovernanceComponent.model_validate(organization.components.get("governance"))
        except Exception:
            return self._rejected(
                context,
                "membership_unavailable",
                "The requested organization membership cannot be changed from this state.",
            )
        if actor.id not in governance.members:
            return self._rejected(
                context,
                "membership_unavailable",
                "The requested organization membership cannot be changed from this state.",
            )
        if governance.leader_id == actor.id:
            return self._rejected(
                context,
                "leader_handover_required",
                "The active leader must complete handover before leaving the organization.",
            )

        cleared_office_ids = sorted(
            office.office_id for office in governance.offices if office.holder_id == actor.id
        )
        event = OrganizationMemberResignedEvent(
            event_id=self._event_id(context, "organization.member_resigned"),
            organization_id=organization.id,
            actor_id=actor.id,
            expected_governance_revision=governance.revision,
            cleared_office_ids=cleared_office_ids,
            provenance=[ProvenanceReference(
                kind="rule", reference_id="organization.membership_resignation.v1",
            )],
            visibility={"policy": "public"},
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="organization_member_resigned",
            message="You left the organization; the offices you held are now vacant.",
            events=[event],
        )

    def _safe_conduct_change(
        self, state: WorldState, intent: ProposeSafeConductIntent | ResolveSafeConductIntent, context: RuleContext,
    ) -> RuleResolution:
        try:
            if isinstance(intent, ProposeSafeConductIntent):
                route = registered_safe_conduct_route(state, intent.from_place_id, intent.route_id)
                if route is None:
                    raise ValueError("The requested route has no registered safe conduct terms.")
                operation = "propose"
                expected_revision = 0
                treaty = propose_safe_conduct(
                    state, treaty_id=uuid5(context.command_id, "safe-conduct-treaty"),
                    proposer_organization_id=intent.proposer_organization_id,
                    recipient_organization_id=intent.recipient_organization_id, proposer_actor_id=context.actor_id,
                    route=route, expires_at_tick=intent.expires_at_tick, existing_treaties=state.safe_conduct_treaties,
                )
            else:
                prior = next((item for item in state.safe_conduct_treaties if item.treaty_id == intent.treaty_id), None)
                if prior is None:
                    raise ValueError("The treaty is missing from this branch.")
                operation = intent.operation
                expected_revision = intent.expected_revision
                treaty = transition_safe_conduct(
                    state, prior, actor_id=context.actor_id, operation=operation, expected_revision=expected_revision,
                    **({"counter_expires_at_tick": intent.counter_expires_at_tick} if operation == "counter" else {}),
                )
        except ValueError:
            return self._rejected(context, "safe_conduct_unavailable", "This treaty action is unavailable in the current state.")

        event = SafeConductChangedEvent(
            event_id=safe_conduct_event_id(context.command_id, context.ruleset_version, operation, treaty.treaty_id),
            world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
            actor_id=context.actor_id, ruleset_version=context.ruleset_version, operation=operation,
            expected_revision=expected_revision, resulting_treaty=treaty,
        )
        return self._resolution(
            state, context, status="succeeded", code=intent.kind,
            message="The safe conduct treaty action was recorded.", events=[event],
        )

    def _combat_truce_change(
        self,
        state: WorldState,
        intent: OfferCombatTruceIntent | ResolveCombatTruceIntent,
        context: RuleContext,
    ) -> RuleResolution:
        if isinstance(intent, OfferCombatTruceIntent):
            actor = state.entities.get(context.actor_id)
            pair = active_combat_opponent(state, actor) if actor is not None else None
            if pair is None:
                return self._rejected(
                    context,
                    "combat_truce_unavailable",
                    "A truce can only be offered during your current active combat encounter.",
                )
            encounter, _opponent = pair
            if intent.expected_last_event_id != encounter.last_event_id:
                return self._rejected(
                    context,
                    "combat_truce_encounter_changed",
                    "The combat exchange changed before this truce offer could be recorded.",
                )
            operation = "offer"
            offer_id = None
        else:
            operation = intent.operation
            offer_id = intent.offer_id

        event: CombatTruceChangedEvent
        try:
            event = plan_combat_truce(
                state,
                operation=operation,
                actor_id=context.actor_id,
                world_id=context.world_id,
                branch_id=context.branch_id,
                command_id=context.command_id,
                ruleset_version=context.ruleset_version,
                existing_offers=state.combat_truce_offers,
                offer_id=offer_id,
            )
        except DomainError:
            return self._rejected(
                context,
                "combat_truce_unavailable",
                "This truce offer or response is unavailable in the current encounter.",
            )

        code, message = {
            "offer": (
                "combat_truce_offered",
                "A voluntary truce offer was recorded for the other combatant.",
            ),
            "accept": (
                "combat_truce_accepted",
                "You accepted the voluntary truce offer.",
            ),
            "decline": (
                "combat_truce_declined",
                "You declined the voluntary truce offer.",
            ),
        }[operation]
        return self._resolution(
            state,
            context,
            status="succeeded",
            code=code,
            message=message,
            events=[event],
        )

    def _private_gift_change(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: OfferPrivateGiftIntent | ResolvePrivateGiftIntent,
        context: RuleContext,
    ) -> RuleResolution:
        """Plan one private gift transition and expose only its safe receipt fields."""
        operation = "offer" if isinstance(intent, OfferPrivateGiftIntent) else intent.operation
        try:
            if isinstance(intent, OfferPrivateGiftIntent):
                event = plan_private_gift_offer(
                    state,
                    world_id=context.world_id,
                    branch_id=context.branch_id,
                    command_id=context.command_id,
                    actor_id=actor.id,
                    ruleset_version=context.ruleset_version,
                    recipient_id=intent.recipient_id,
                    resource_id=intent.resource_id,
                    quantity=intent.quantity,
                    expires_at_tick=intent.expires_at_tick,
                )
            else:
                event = plan_private_gift_resolution(
                    state,
                    world_id=context.world_id,
                    branch_id=context.branch_id,
                    command_id=context.command_id,
                    actor_id=actor.id,
                    ruleset_version=context.ruleset_version,
                    gift_id=intent.gift_id,
                    expected_revision=intent.expected_revision,
                    operation=intent.operation,
                )
        except DomainError:
            return self._rejected(
                context,
                "private_gift_unavailable",
                "This private gift offer cannot be changed by this character in the current state.",
            )

        offer = event.offer
        code, message = {
            "offer": (
                "private_gift_offered",
                "A private gift offer was recorded for the other character.",
            ),
            "accept": (
                "private_gift_accepted",
                "You accepted the private gift offer.",
            ),
            "decline": (
                "private_gift_declined",
                "You declined the private gift offer.",
            ),
            "withdraw": (
                "private_gift_withdrawn",
                "You withdrew the private gift offer.",
            ),
        }[operation]
        return self._resolution(
            state,
            context,
            status="succeeded",
            code=code,
            message=message,
            details={
                "gift_id": str(offer.gift_id),
                "status": offer.status,
                "resource_id": offer.resource_id,
                "quantity": format(offer.quantity, "f"),
            },
            events=[event],
        )

    def _leadership_ballot_change(
        self, state: WorldState,
        intent: OpenLeadershipBallotIntent | OpenVacantLeadershipBallotIntent
        | CastLeadershipBallotVoteIntent | CloseLeadershipBallotIntent,
        context: RuleContext,
    ) -> RuleResolution:
        try:
            if isinstance(intent, (OpenLeadershipBallotIntent, OpenVacantLeadershipBallotIntent)):
                operation = "open"
                expected_revision = 0
                open_ballot = open_leadership_ballot
                if isinstance(intent, OpenVacantLeadershipBallotIntent):
                    operation = "open_vacant"
                    open_ballot = open_vacant_leadership_ballot
                ballot = open_ballot(
                    state, ballot_id=uuid5(context.command_id, "leadership-ballot"),
                    organization_id=intent.organization_id, nominee_id=intent.nominee_id,
                    actor_id=context.actor_id, existing_ballots=state.leadership_ballots,
                )
            else:
                prior = next((item for item in state.leadership_ballots if item.ballot_id == intent.ballot_id), None)
                if prior is None:
                    raise LeadershipBallotError("No ballot with that ID on this branch.")
                expected_revision = intent.expected_ballot_revision
                if isinstance(intent, CastLeadershipBallotVoteIntent):
                    operation = "cast"
                    ballot = cast_leadership_ballot_vote(
                        state, prior, voter_id=context.actor_id, choice=intent.choice,
                        expected_ballot_revision=expected_revision,
                    )
                else:
                    operation = "close"
                    ballot = close_leadership_ballot(
                        state, prior, actor_id=context.actor_id, expected_ballot_revision=expected_revision,
                    )
        except LeadershipBallotError:
            return self._rejected(context, "leadership_ballot_unavailable", "The nomination, vote or tally is unavailable in this state.")

        event = LeadershipBallotChangedEvent(
            event_id=leadership_ballot_event_id(context.command_id, context.ruleset_version, operation, ballot.ballot_id),
            world_id=context.world_id, branch_id=context.branch_id, command_id=context.command_id,
            actor_id=context.actor_id, ruleset_version=context.ruleset_version, operation=operation,
            expected_revision=expected_revision, resulting_ballot=ballot,
        )
        return self._resolution(
            state, context, status="succeeded", code=intent.kind,
            message="The leadership ballot action was recorded.", events=[event],
        )

    def _handover_leadership(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: HandoverLeadershipIntent,
        context: RuleContext,
    ) -> RuleResolution:
        if actor.status != "active":
            return self._rejected(context, "actor_terminal", "An inactive character cannot act.")
        organization = state.entities.get(intent.organization_id)
        organization_kinds = {"organization", "polity", "faction", "company", "government"}
        if (
            organization is None
            or organization.kind not in organization_kinds
            or organization.status != "active"
        ):
            return self._rejected(
                context,
                "leadership_handover_unavailable",
                "Leadership handover cannot be completed from this state.",
            )
        try:
            governance = GovernanceComponent.model_validate(organization.components.get("governance"))
        except Exception:
            return self._rejected(
                context,
                "leadership_handover_unavailable",
                "Leadership handover cannot be completed from this state.",
            )
        if governance.leader_id != actor.id or actor.id not in governance.members:
            return self._rejected(
                context,
                "leadership_handover_incumbent_required",
                "Only the current organization leader may initiate a handover.",
            )
        if governance.succession_policy != "appointment":
            return self._rejected(
                context,
                "leadership_handover_requires_appointment",
                "This organization requires its registered succession process.",
            )
        successor = state.entities.get(intent.successor_id)
        if (
            successor is None
            or successor.kind not in {"character", "person"}
            or successor.status != "active"
            or successor.id not in governance.members
            or successor.id == actor.id
        ):
            return self._rejected(
                context,
                "leadership_handover_target_unavailable",
                "Choose a distinct active member eligible for this handover.",
            )

        transferred_office_ids = sorted(
            office.office_id for office in governance.offices
            if office.holder_id == actor.id and is_leadership_office(office)
        )
        event = GovernanceLeadershipHandedOverEvent(
            event_id=self._event_id(context, "organization.leadership_handed_over"),
            organization_id=organization.id,
            predecessor_id=actor.id,
            successor_id=successor.id,
            expected_governance_revision=governance.revision,
            transferred_office_ids=transferred_office_ids,
            provenance=[ProvenanceReference(
                kind="rule", reference_id="organization.leadership_handover.v1",
            )],
            visibility={"policy": "public"},
        )
        return self._resolution(
            state,
            context,
            status="succeeded",
            code="leadership_handed_over",
            message="Leadership was handed over to the selected active member.",
            events=[event],
        )

    def _attack(self, state: WorldState, actor: WorldEntity, intent: AttackIntent, context: RuleContext) -> RuleResolution:
        duration = COMBAT_EXCHANGE_DURATION
        issue = self._duration_issue(state, actor, duration)
        if issue is not None:
            return self._rejected(context, *issue)
        target = state.entities.get(intent.target_id)
        if target is None or target.kind not in {"character", "person"} or target.status != "active":
            return self._rejected(context, "target_unavailable", "The target is not an active character.")
        if target.id == actor.id or actor.location_id is None or target.location_id != actor.location_id:
            return self._rejected(context, "target_out_of_reach", "The character and target must be different and at the same location.")
        actor_encounter = active_combat_opponent(state, actor)
        if actor_encounter is not None and actor_encounter[1].id != target.id:
            return self._rejected(
                context,
                "combat_encounter_active",
                "This character is already engaged with a different co-located threat.",
            )
        target_encounter = active_combat_opponent(state, target)
        if target_encounter is not None and target_encounter[1].id != actor.id:
            return self._rejected(
                context,
                "target_engaged",
                "The target is already engaged with another co-located character.",
            )
        if target.components.get("hidden") is True and target.id not in _observed_entities(state, actor.id):
            return self._rejected(context, "target_unseen", "The target has not been discovered.")
        if "world.combat" in self._capabilities(actor).restrictions:
            return self._rejected(context, "combat_restricted", "This character is not permitted to attack.")
        target_health = self._health(target)
        if target_health is None:
            return self._rejected(context, "target_has_no_health", "The target has no registered health component.")
        inventory_event: ComponentReplacedEvent | None = None
        consumed: ResourceConsumedEvent | None = None
        if intent.weapon_resource_id is not None:
            weapon = self._holder_account(state, actor.id, intent.weapon_resource_id)
            sink = self._system_account(state, intent.weapon_resource_id, _CONSUMPTION_SINK_TYPES)
            if weapon is None or sink is None or weapon.balance < 1:
                return self._rejected(context, "weapon_unavailable", "The character lacks one unit of the required weapon resource.")
            try:
                inventory_event, _ = self._inventory_reduction(
                    state, actor, [(self._account_id(state, weapon), intent.weapon_resource_id, Decimal("1"))],
                    context, "attack.weapon_inventory",
                )
            except ValueError as exc:
                return self._rejected(context, "weapon_unavailable", str(exc))
            consumed = ResourceConsumedEvent(
                event_id=self._event_id(context, "attack.weapon_cost"),
                consumer_id=actor.id,
                purpose="combat.weapon_resource",
                flows=[
                    ConsumedResourceFlow(
                        holder_account_id=self._account_id(state, weapon),
                        sink_account_id=self._account_id(state, sink),
                        resource_id=intent.weapon_resource_id,
                        quantity=Decimal("1"),
                    )
                ],
            )
        chance = combat_attack_chance(actor, target)
        roll = self._roll(context, COMBAT_ATTACK_ROLL_LABEL, 100)
        hit = roll.value <= chance
        roll = roll.model_copy(update={"outcome": "hit" if hit else "miss"})
        damage_amount = combat_damage(actor) if hit else 0
        encounter_resolved = hit and damage_amount >= target_health.current
        target_incapacitated = encounter_resolved and combat_death_protection_enabled(state, target)
        incapacitation_message = "The hit incapacitated the target and ended the encounter."
        existing_actor_encounter = (
            CombatEncounterComponent.model_validate(actor.components["combat_encounter"])
            if actor.components.get("combat_encounter") is not None
            else None
        )
        existing_target_encounter = (
            CombatEncounterComponent.model_validate(target.components["combat_encounter"])
            if target.components.get("combat_encounter") is not None
            else None
        )
        if actor_encounter is not None:
            encounter_id = actor_encounter[0].encounter_id
            exchange_number = actor_encounter[0].exchange_count + 1
        else:
            encounter_id = uuid5(context.command_id, f"{context.ruleset_version}:combat.encounter")
            exchange_number = 1
        exchange_event = CombatExchangeResolvedEvent(
            event_id=self._event_id(context, "combat.exchange"),
            world_id=context.world_id,
            branch_id=context.branch_id,
            command_id=context.command_id,
            ruleset_version=context.ruleset_version,
            policy_version=COMBAT_ATTACK_POLICY,
            encounter_id=encounter_id,
            exchange_number=exchange_number,
            actor_id=actor.id,
            target_id=target.id,
            location_id=actor.location_id,
            expected_actor_encounter_revision=(existing_actor_encounter.revision if existing_actor_encounter else None),
            expected_target_encounter_revision=(existing_target_encounter.revision if existing_target_encounter else None),
            started_tick=state.current_tick,
            resolved_tick=state.current_tick + duration,
            chance_percent=chance,
            roll=roll.value,
            roll_digest=roll.digest,
            outcome="hit" if hit else "miss",
            weapon_resource_id=intent.weapon_resource_id,
            encounter_status="resolved" if encounter_resolved else "active",
            damage_event_id=(self._event_id(context, "combat.damage") if hit else None),
            damage=damage_amount,
            expected_target_health_revision=(target_health.revision if hit else None),
            visibility={"policy": "subject_ids", "subject_ids": sorted((str(actor.id), str(target.id)))},
        )
        needs_event, deprivation = self._time_need_effects(state, actor, duration, context)
        events: list[DomainEvent] = [item for item in (inventory_event, needs_event, consumed) if item is not None]
        damage = None
        if hit:
            damage = DamageAppliedEvent(
                event_id=self._event_id(context, "combat.damage"),
                target_id=target.id,
                source_id=actor.id,
                damage=damage_amount,
                injury_type="combat.weapon" if intent.weapon_resource_id else "core.injury.blunt",
                description="Injury sustained in a registered combat action.",
                expected_health_revision=target_health.revision,
                expected_population_revision=self._population_revision(state, target),
                source_event_ids=[exchange_event.event_id],
            )
        events.append(exchange_event)
        if damage is not None:
            events.append(damage)
        if deprivation is not None:
            events.append(deprivation)
        life_event = self._lifespan_event(state, actor, duration, context, events)
        if life_event is not None:
            events.append(life_event)
        if life_event is not None:
            status, code, message = "partial", "lifespan_expired", "The attack resolved as the character reached the end of their lifespan."
        elif deprivation is not None and self._damage_is_lethal(actor, deprivation.damage):
            status, code, message = "failed", "died_of_deprivation", "The character died from hunger or thirst during the attack."
        elif deprivation is not None:
            status, code, message = "partial", "survival_pressure", "The attack resolved, but hunger or thirst caused an injury."
        elif target_incapacitated:
            status, code, message = "succeeded", "combat_target_incapacitated", incapacitation_message
        elif hit:
            status, code, message = "succeeded", "attack_hit", "The attack hit and caused a registered injury."
        else:
            status, code, message = "failed", "attack_missed", "The attack missed."
        if target_incapacitated and status != "succeeded":
            message = f"{message} {incapacitation_message}"
        return self._resolution(
            state, context, status=status, code=code, message=message,
            details={"target_id": str(target.id), "hit_chance_percent": chance,
                     "damage": damage.damage if damage else 0,
                     "encounter_id": str(encounter_id), "exchange_number": exchange_number,
                     **({"target_incapacitated": True} if target_incapacitated else {})},
            elapsed_ticks=duration, events=events, rolls=[roll],
        )

    def _train(self, state: WorldState, actor: WorldEntity, intent: TrainIntent, context: RuleContext) -> RuleResolution:
        duration = intent.duration_ticks
        issue = self._duration_issue(state, actor, duration)
        if issue is not None:
            return self._rejected(context, *issue)
        progress = self._training_progress(actor, intent, context, trained_at_tick=state.current_tick + duration)
        if progress.outcome.status == "rejected":
            return progress
        needs_event, deprivation = self._time_need_effects(state, actor, duration, context)
        events = [item for item in (needs_event, *progress.events, deprivation) if item is not None]
        life_event = self._lifespan_event(state, actor, duration, context, events)
        if life_event is not None:
            events.append(life_event)
        status, code, message = progress.outcome.status, progress.outcome.code, progress.outcome.message
        if life_event is not None:
            status, code, message = "partial", "lifespan_expired", "Training completed as the character reached the end of their lifespan."
        elif deprivation is not None:
            status, code, message = "partial", "survival_pressure", "Training completed, but hunger or thirst caused an injury."
        return self._resolution(
            state, context, status=status, code=code, message=message, details=progress.outcome.details,
            elapsed_ticks=duration, events=events,
        )

    def _training_progress(
        self, actor: WorldEntity, intent: TrainIntent, context: RuleContext, *, trained_at_tick: int,
    ) -> RuleResolution:
        """Compute the registered training gains without advancing time or needs."""
        duration = intent.duration_ticks
        skills_raw = actor.components.get("skills")
        try:
            skills = SkillsComponent.model_validate(skills_raw) if skills_raw is not None else SkillsComponent()
        except Exception:
            return self._rejected(context, "skills_unavailable", "The character's registered skills are invalid.")
        current = skills.skills.get(intent.skill_id)
        if current is None:
            return self._rejected(context, "skill_not_registered", "The selected skill is not registered for this character.")
        experience_gained = max(1, duration // 300)
        with localcontext() as decimal_context:
            decimal_context.prec = 50
            level = min(Decimal("100"), current.level + Decimal(experience_gained) / Decimal("100"))
        trained = current.model_copy(
            update={
                "level": level,
                "experience": current.experience + experience_gained,
                "trained_ticks": current.trained_ticks + duration,
                "last_practiced_tick": trained_at_tick,
                "revision": current.revision + 1,
            }
        )
        next_skills = skills.model_copy(
            update={"revision": skills.revision + 1,
                    "skills": {**skills.skills, intent.skill_id: trained}}
        )
        skills_event = ComponentReplacedEvent(
            event_id=self._event_id(context, "training.skill"),
            entity_id=actor.id,
            component_name="skills",
            expected_revision=skills.revision,
            replacement=next_skills.model_dump(mode="json"),
        )
        progression_raw = actor.components.get("progression")
        try:
            progression = ProgressionComponent.model_validate(progression_raw) if progression_raw is not None else ProgressionComponent()
        except Exception:
            return self._rejected(context, "progression_unavailable", "The character's registered progression is invalid.")
        total_experience = progression.experience + experience_gained
        next_level = progression.level
        threshold = progression.next_threshold or next_level * 1000
        milestones = list(progression.milestones)
        while total_experience >= threshold:
            next_level += 1
            milestones.append(f"level.{next_level}")
            threshold = next_level * 1000
        next_progression = progression.model_copy(
            update={"experience": total_experience, "level": next_level,
                    "next_threshold": threshold, "milestones": milestones,
                    "revision": progression.revision + 1 if progression_raw is not None else 1}
        )
        progression_event = ComponentReplacedEvent(
            event_id=self._event_id(context, "training.progression"),
            entity_id=actor.id,
            component_name="progression",
            expected_revision=progression.revision if progression_raw is not None else None,
            replacement=next_progression.model_dump(mode="json"),
        )
        return RuleResolution(
            ruleset_version=context.ruleset_version,
            outcome=RuleOutcome(
                status="succeeded", code="training_completed",
                message="Training advanced the selected registered skill and character experience.",
                details={"skill_id": intent.skill_id, "experience_gained": experience_gained,
                         "skill_level": format(level, "f"), "duration_seconds": duration},
            ),
            events=[skills_event, progression_event],
        )

    def _attempt_cultivation_breakthrough(
        self,
        state: WorldState,
        actor: WorldEntity,
        intent: AttemptCultivationBreakthroughIntent,
        context: RuleContext,
    ) -> RuleResolution:
        """Resolve one timed attempt using only its server-registered policy."""
        if cultivation_disabled_by_world_rules(context.ruleset_version, state.metadata):
            return self._rejected(context, CULTIVATION_DISABLED_CODE, CULTIVATION_DISABLED_MESSAGE)

        policy = registered_cultivation_breakthrough_policy(state, intent.policy_id)
        if policy is None:
            return self._rejected(
                context,
                "cultivation_breakthrough_policy_unavailable",
                "That cultivation breakthrough policy is not registered for this world.",
            )

        if active_combat_opponent(state, actor) is not None:
            return self._rejected(
                context,
                "cultivation_breakthrough_combat_blocked",
                "A character cannot attempt a cultivation breakthrough during active combat.",
            )
        if any(
            journey.actor_id == actor.id and journey.status == "in_transit"
            for journey in state.travel_journeys
        ):
            return self._rejected(
                context,
                "cultivation_breakthrough_travel_blocked",
                "A character cannot attempt a cultivation breakthrough while traveling.",
            )

        duration = policy.duration_ticks
        issue = self._duration_issue(state, actor, duration)
        if issue is not None:
            return self._rejected(context, *issue)
        try:
            plan = plan_cultivation_breakthrough(
                state,
                actor_id=actor.id,
                policy=policy,
                world_id=context.world_id,
                branch_id=context.branch_id,
                command_id=context.command_id,
                ruleset_version=context.ruleset_version,
            )
            # The domain event builder binds the plan's exact FIFO allocations,
            # ledger CAS expectations, result and consequences into replayable
            # events. It intentionally never plans or publishes partial costs.
            events = breakthrough_effect_events(state, plan)
        except CultivationBreakthroughError as exc:
            return self._rejected(context, exc.code, str(exc))
        except ValueError:
            return self._rejected(
                context,
                "cultivation_breakthrough_unavailable",
                "The registered cultivation breakthrough cannot be resolved from the current state.",
            )

        health = self._health(actor)
        breakthrough_damage = sum(
            event.damage
            for event in events
            if isinstance(event, DamageAppliedEvent) and event.target_id == actor.id
        )
        breakthrough_damage_is_terminal = (
            health is not None and breakthrough_damage >= health.current
        )

        needs_event, deprivation = self._time_need_effects(state, actor, duration, context)
        # A terminal breakthrough injury ends the character at the completion
        # tick. Avoid emitting any later actor component or damage event against
        # that newly terminal entity.
        if not breakthrough_damage_is_terminal:
            if needs_event is not None:
                events.append(needs_event)
            if deprivation is not None:
                # The breakthrough's failure injury is applied first and bumps
                # health once, so advance the deprivation CAS accordingly.
                prior_actor_damage_count = sum(
                    1
                    for event in events
                    if isinstance(event, DamageAppliedEvent) and event.target_id == actor.id
                )
                if prior_actor_damage_count:
                    deprivation = deprivation.model_copy(update={
                        "expected_health_revision": (
                            deprivation.expected_health_revision + prior_actor_damage_count
                        ),
                    })
                events.append(deprivation)

        life_event = self._lifespan_event(state, actor, duration, context, events)
        if life_event is not None:
            events.append(life_event)

        if plan.outcome == "succeeded":
            status, code, message = (
                "succeeded",
                "cultivation_breakthrough_succeeded",
                "The cultivation breakthrough succeeded and advanced the character's realm.",
            )
            if deprivation is not None and not breakthrough_damage_is_terminal:
                status, code, message = (
                    "partial",
                    "cultivation_breakthrough_survival_pressure",
                    "The cultivation breakthrough succeeded, but hunger or thirst caused an injury.",
                )
        else:
            status, code, message = (
                "failed",
                "cultivation_breakthrough_failed",
                "The cultivation breakthrough failed; the registered qi cost and failure injury were resolved.",
            )

        return self._resolution(
            state,
            context,
            status=status,
            code=code,
            message=message,
            details={
                "policy_id": plan.policy_id,
                "policy_version": plan.policy_version,
                "source_realm": plan.source_realm,
                "target_realm": plan.target_realm,
                "breakthrough_outcome": plan.outcome,
                "roll": plan.roll,
                "chance_percent": plan.chance_percent,
                "qi_cost": format(plan.qi_cost, "f"),
                "duration_seconds": duration,
            },
            elapsed_ticks=duration,
            events=events,
            rolls=[RuleRoll(
                label=CULTIVATION_BREAKTHROUGH_ROLL_LABEL,
                value=plan.roll,
                maximum=100,
                outcome=plan.outcome,
                digest=plan.roll_digest,
            )],
        )

    def _rest(self, state: WorldState, actor: WorldEntity, intent: RestIntent, context: RuleContext) -> RuleResolution:
        issue = self._duration_issue(state, actor, intent.duration_ticks)
        if issue is not None:
            return self._rejected(context, *issue)
        component_event, deprivation = self._time_need_effects(
            state, actor, intent.duration_ticks, context, rest_delta=-1
        )
        events: list[DomainEvent] = [item for item in (component_event,) if item is not None]
        health = self._health(actor)
        health_restored = False
        if health is not None and health.current < health.maximum:
            amount = min(1_000_000, health.maximum - health.current, intent.duration_ticks // (2 * _HOUR_SECONDS))
            if amount > 0:
                events.append(
                    HealthRestoredEvent(
                        event_id=self._event_id(context, "rest.health"),
                        target_id=actor.id,
                        amount=amount,
                        method="core.rest",
                        expected_health_revision=health.revision,
                    )
                )
                health_restored = True
        if deprivation is not None:
            if health_restored:
                deprivation = deprivation.model_copy(
                    update={"expected_health_revision": deprivation.expected_health_revision + 1}
                )
            events.append(deprivation)
        life_event = self._lifespan_event(state, actor, intent.duration_ticks, context, events)
        if life_event is not None:
            events.append(life_event)
        status, code, message = "succeeded", "rested", "The character rested and recovered some rest need."
        if life_event is not None:
            status, code, message = "partial", "lifespan_expired", "The character reached the end of their lifespan while resting."
        elif deprivation is not None:
            status, code, message = "partial", "survival_pressure", "The character rested while hunger or thirst caused injury."
        return self._resolution(
            state, context, status=status, code=code, message=message,
            details={"duration_seconds": intent.duration_ticks}, elapsed_ticks=intent.duration_ticks, events=events,
        )

    def _wait(self, state: WorldState, actor: WorldEntity, intent: WaitIntent, context: RuleContext) -> RuleResolution:
        issue = self._duration_issue(state, actor, intent.duration_ticks)
        if issue is not None:
            return self._rejected(context, *issue)
        component_event, deprivation = self._time_need_effects(
            state, actor, intent.duration_ticks, context, rest_delta=0
        )
        events: list[DomainEvent] = [item for item in (component_event, deprivation) if item is not None]
        life_event = self._lifespan_event(state, actor, intent.duration_ticks, context, events)
        if life_event is not None:
            events.append(life_event)
        killed = deprivation is not None and self._damage_is_lethal(actor, deprivation.damage)
        if life_event is not None:
            status, code, message = "partial", "lifespan_expired", "The character reached the end of their lifespan while waiting."
        elif killed:
            status, code, message = "failed", "died_of_deprivation", "The character died from hunger or thirst while waiting."
        elif deprivation is not None:
            status, code, message = "partial", "survival_pressure", "Waiting caused an injury from hunger or thirst."
        else:
            status, code, message = "succeeded", "waited", "The character waited; hunger, thirst and fatigue advanced with time."
        return self._resolution(
            state, context, status=status, code=code, message=message,
            details={"duration_seconds": intent.duration_ticks}, elapsed_ticks=intent.duration_ticks, events=events,
        )

    def _time_need_effects(
        self,
        state: WorldState,
        actor: WorldEntity,
        duration: int,
        context: RuleContext,
        *,
        rest_delta: int = 0,
        restore: dict[str, int] | None = None,
    ) -> tuple[ComponentReplacedEvent | None, DamageAppliedEvent | None]:
        """Apply fixed per-world-day need rates and retain sub-point progress."""
        needs = self._needs(actor)
        if needs is None or duration <= 0:
            return None, None
        day_seconds = state.calendar.day_seconds
        end_tick = state.current_tick + duration
        rates = {"food": 18, "water": 30, "rest": -36 if rest_delta < 0 else 24}
        updated: dict[str, NeedValue | None] = {}
        total_deprivation_damage = 0
        with localcontext() as decimal_context:
            decimal_context.prec = 50
            for name, per_day in rates.items():
                value = getattr(needs, name)
                if value is None:
                    continue
                numerator = per_day * duration + value.decay_remainder
                change = _trunc_div(numerator, day_seconds)
                remainder = numerator - change * day_seconds
                amount = max(0, min(100, value.value - change))
                restoration = (restore or {}).get(name, 0)
                if restoration:
                    amount = min(100, amount + restoration)
                    remainder = 0
                penalty_tick = value.last_critical_damage_tick
                if name in {"food", "water"} and amount < value.critical_below:
                    penalty_tick = penalty_tick if penalty_tick is not None else state.current_tick
                    critical_seconds = end_tick - penalty_tick
                    if critical_seconds >= day_seconds:
                        days = critical_seconds // day_seconds
                        total_deprivation_damage += days
                        penalty_tick += days * day_seconds
                else:
                    penalty_tick = None
                updated[name] = value.model_copy(
                    update={
                        "value": amount,
                        "decay_remainder": remainder,
                        "last_satisfied_tick": end_tick if restoration else value.last_satisfied_tick,
                        "last_critical_damage_tick": penalty_tick,
                    }
                )

        needs_updates = {name: value for name, value in updated.items() if value is not None}
        changed = any(getattr(needs, name) != value for name, value in needs_updates.items())
        component_event = None
        if changed:
            replacement = needs.model_copy(update={**needs_updates, "revision": needs.revision + 1})
            component_event = ComponentReplacedEvent(
                event_id=self._event_id(context, "needs.time"),
                entity_id=actor.id,
                component_name="needs",
                expected_revision=needs.revision,
                replacement=replacement.model_dump(mode="json"),
            )
        health = self._health(actor)
        damage = None
        if total_deprivation_damage and health is not None and health.current > 0:
            damage = DamageAppliedEvent(
                event_id=self._event_id(context, "needs.deprivation_damage"),
                target_id=actor.id,
                damage=min(1_000_000, total_deprivation_damage),
                injury_type="core.injury.deprivation",
                description="Repeated critical hunger or thirst caused injury.",
                expected_health_revision=health.revision,
                expected_population_revision=self._population_revision(state, actor),
            )
        return component_event, damage

    def _duration_issue(
        self,
        state: WorldState,
        actor: WorldEntity | None,
        duration: int,
        *,
        include_actor_background: bool = False,
        check_background_boundary: bool = True,
    ) -> tuple[str, str] | None:
        if duration < 0 or state.current_tick + duration > 2**63 - 1:
            return "tick_overflow", "The action duration exceeds the world clock range."
        if duration == 0:
            return None
        end_tick = state.current_tick + duration
        if any(
            item.status == "queued" and item.due_tick <= state.current_tick
            for item in state.scheduled_events
        ):
            return "scheduled_event_due", "A scheduled world event must resolve before this action can advance time."
        boundaries: list[tuple[int, str, str]] = [
            (item.due_tick, "scheduled_boundary_required", "The action must stop at the next scheduled world event boundary.")
            for item in state.scheduled_events
            if item.status == "queued" and state.current_tick < item.due_tick < end_tick
        ]
        for plan in state.npc_plans:
            active = plan.active_execution
            if isinstance(active, NpcTimedExecution):
                if active.due_tick <= state.current_tick:
                    return "npc_action_due", "An NPC action is due at the current world time."
                if active.due_tick < end_tick:
                    boundaries.append((active.due_tick, "npc_action_boundary", "An NPC action must settle before advancing past its end."))
        for subject in state.entities.values():
            if subject.kind not in {"character", "person"} or subject.status in {"dead", "retired"}:
                continue
            life = self._life(subject)
            if life is None or life.is_immortal or life.lifespan_ticks is None:
                continue
            expiry = life.birth_tick + life.lifespan_ticks
            if expiry <= state.current_tick:
                return "lifespan_expiry_pending", "A character's lifespan expiry must resolve before another action."
            if state.current_tick < expiry < end_tick:
                boundaries.append((expiry, "lifespan_boundary_required", "The action must stop at the next character lifespan boundary."))
        background_boundary = (
            self.next_background_boundary(
                state,
                actor_id=actor.id if actor is not None else None,
                include_actor=include_actor_background,
            )
            if check_background_boundary
            else None
        )
        if background_boundary is not None:
            if background_boundary <= state.current_tick:
                return "background_deprivation_pending", "A nonacting character reached a deprivation boundary; resolve it before advancing time."
            if background_boundary < end_tick:
                boundaries.append((
                    background_boundary,
                    "background_boundary_required",
                    "The action must stop at the next nonacting character deprivation boundary.",
                ))
        if boundaries:
            _tick, code, message = min(boundaries, key=lambda item: (item[0], item[1]))
            return code, message
        return None

    def _inventory_reduction(
        self,
        state: WorldState,
        owner: WorldEntity,
        requirements: list[tuple[UUID, str, Decimal]],
        context: RuleContext,
        label: str,
    ) -> tuple[ComponentReplacedEvent | None, list[UUID]]:
        if not requirements:
            return None, []
        raw_inventory = owner.components.get("inventory")
        if raw_inventory is None:
            raise ValueError("The holder has no registered inventory lots for that resource.")
        try:
            inventory = InventoryComponent.model_validate(raw_inventory)
        except Exception as exc:
            raise ValueError("The holder's inventory component is invalid.") from exc
        needed: dict[tuple[UUID, str], Decimal] = {}
        with localcontext() as decimal_context:
            decimal_context.prec = 50
            for account_id, resource_id, quantity in requirements:
                key = (account_id, resource_id)
                needed[key] = needed.get(key, Decimal("0")) + quantity
            remaining = dict(needed)
            lots: list[InventoryLot] = []
            selected_ids: list[UUID] = []
            for lot in inventory.lots:
                key = (lot.account_id, lot.resource_id)
                amount = remaining.get(key, Decimal("0"))
                if amount <= 0 or lot.expiry_tick is not None and lot.expiry_tick <= state.current_tick:
                    lots.append(lot)
                    continue
                taken = min(amount, lot.quantity)
                selected_ids.append(lot.lot_id)
                remaining[key] = amount - taken
                if taken < lot.quantity:
                    lots.append(
                        lot.model_copy(update={"quantity": lot.quantity - taken, "revision": lot.revision + 1})
                    )
        shortages = {key: amount for key, amount in remaining.items() if amount > 0}
        if shortages:
            _, resource = next(iter(shortages))
            raise ValueError(f"Unexpired inventory lots do not cover the required {resource} quantity.")
        replacement = inventory.model_copy(update={"revision": inventory.revision + 1, "lots": lots})
        event = ComponentReplacedEvent(
            event_id=self._event_id(context, label),
            entity_id=owner.id,
            component_name="inventory",
            expected_revision=inventory.revision,
            replacement=replacement.model_dump(mode="json"),
        )
        return event, selected_ids

    def _lifespan_event(
        self,
        state: WorldState,
        actor: WorldEntity,
        duration: int,
        context: RuleContext,
        events: list[DomainEvent],
    ) -> LifeEndedEvent | None:
        life = self._life(actor)
        if life is None or life.is_immortal or life.lifespan_ticks is None:
            return None
        expiry = life.birth_tick + life.lifespan_ticks
        if expiry != state.current_tick + duration:
            return None
        health = self._health(actor)
        if health is not None:
            remaining = health.current
            for event in events:
                if isinstance(event, HealthRestoredEvent) and event.target_id == actor.id:
                    remaining += event.amount
                elif isinstance(event, DamageAppliedEvent) and event.target_id == actor.id:
                    remaining -= event.damage
            if remaining <= 0:
                return None
        return LifeEndedEvent(
            event_id=self._event_id(context, "life.expired"),
            entity_id=actor.id,
            expected_life_revision=life.revision,
            expected_population_revision=self._population_revision(state, actor),
        )

    @staticmethod
    def _damage_is_lethal(actor: WorldEntity, amount: int) -> bool:
        health = RuleEngine._health(actor)
        return health is not None and amount >= health.current

    def _resolution(
        self,
        state: WorldState,
        context: RuleContext,
        *,
        status: str,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
        elapsed_ticks: int = 0,
        events: Iterable[DomainEvent] = (),
        rolls: Iterable[RuleRoll] = (),
        interruptions: Iterable[RuleInterruption] = (),
    ) -> RuleResolution:
        batch = list(events)
        if any(isinstance(event, TimeAdvancedEvent) for event in batch):
            raise RuleInvariantError("TimeAdvancedEvent is owned by RuleEngine._resolution.")
        result_events: list[DomainEvent] = []
        routine_event: BackgroundRoutineAdvancedEvent | None = None
        routine_boundary_tick: int | None = None
        if elapsed_ticks:
            if state.current_tick + elapsed_ticks > 2**63 - 1:
                return self._rejected(context, "tick_overflow", "The action duration exceeds the world clock range.")
            end_tick = state.current_tick + elapsed_ticks
            if context.background_routine_mode:
                routine_event, deprivation_boundary = self._background_routine_event(
                    state, elapsed_ticks, context
                )
                routine_boundary_tick = deprivation_boundary
                if deprivation_boundary is not None and deprivation_boundary < end_tick:
                    return self._rejected(
                        context,
                        "background_deprivation_pending",
                        "Routine care stock is exhausted before the next deprivation boundary; stop there and resolve it.",
                        details={"boundary_tick": deprivation_boundary},
                    )
            time_event = TimeAdvancedEvent(
                event_id=self._event_id(context, "time.advanced"),
                to_tick=end_tick,
            )
            try:
                projected = apply_event(state, time_event)
            except Exception as exc:
                raise RuleInvariantError(f"Rule-generated time event failed validation: {exc}") from exc
            result_events.append(time_event)
            background_event = (
                routine_event
                if context.background_routine_mode
                else self._background_needs_event(state, elapsed_ticks, context)
            )
            if background_event is not None:
                try:
                    projected = apply_event(projected, background_event)
                except Exception as exc:
                    raise RuleInvariantError(f"Rule-generated background-needs batch failed validation: {exc}") from exc
                result_events.append(background_event)
        else:
            end_tick = state.current_tick
            projected = state
        normalized_batch: list[DomainEvent] = []
        for event in batch:
            target_id: UUID | None = None
            terminal_transition = isinstance(event, LifeEndedEvent)
            if isinstance(event, LifeEndedEvent):
                target_id = event.entity_id
            elif isinstance(event, DamageAppliedEvent):
                target_id = event.target_id
                target = projected.entities.get(target_id)
                health = self._health(target) if target is not None else None
                terminal_transition = health is not None and event.damage >= health.current
            elif isinstance(event, EntityFieldChangedEvent) and event.field_name == "status" and event.value in {"dead", "retired"}:
                target_id = event.entity_id
                terminal_transition = True
            if terminal_transition and target_id is not None:
                target = projected.entities.get(target_id)
                if target is not None:
                    population_revision = self._population_revision(projected, target)
                    if population_revision is not None:
                        event = event.model_copy(update={"expected_population_revision": population_revision})
            try:
                projected = apply_event(projected, event)
            except Exception as exc:
                raise RuleInvariantError(f"Rule-generated event batch failed validation at {event.type}: {exc}") from exc
            normalized_batch.append(event)
        result_events.extend(normalized_batch)
        if elapsed_ticks:
            due_lives, projected = self._lifespans_at(projected, end_tick, context)
            result_events.extend(due_lives)
        batch = result_events
        outcome_details = dict(details or {})
        if elapsed_ticks and routine_boundary_tick == end_tick:
            outcome_details["boundary_tick"] = routine_boundary_tick
        if elapsed_ticks:
            expired_ids = [str(event.entity_id) for event in batch if isinstance(event, LifeEndedEvent)]
            if expired_ids:
                outcome_details["expired_entity_ids"] = expired_ids
                if status not in {"rejected", "failed"} and code != "lifespan_expired":
                    status = "partial"
                    code = "lifespan_expired"
                    message = "The action resolved as one or more characters reached the end of their lifespan."
        outcome = RuleOutcome(status=status, code=code, message=message, details=outcome_details)
        result = RuleResolution(
            events=batch,
            outcome=outcome,
            elapsed_ticks=elapsed_ticks,
            interruptions=list(interruptions),
            rolls=list(rolls),
            ruleset_version=context.ruleset_version,
        )
        # Retain this state only when an exact trusted in-process caller asks
        # for its validated projection. PrivateAttr keeps it out of contracts.
        if (getattr(self, "_capture_itinerary_care_projection", False)
            or _CAPTURE_VALIDATED_PROJECTION.get()):
            result._validated_projection = projected
        return result

    def _background_needs_event(
        self,
        state: WorldState,
        duration_ticks: int,
        context: RuleContext,
    ) -> BackgroundNeedsAdvancedEvent | None:
        expected_agents: list[BackgroundNeedRevisionExpectation] = []
        expected_accounts: dict[UUID, BackgroundAccountExpectation] = {}
        valid_agents = {
            entity.id: entity
            for entity in state.entities.values()
            if (context.include_actor_in_background or entity.id != context.actor_id)
            and entity.kind in {"character", "person"}
            and entity.status not in {"dead", "retired"}
            and entity.components.get("needs") is not None
        }
        choices = {choice.entity_id: choice for choice in context.background_choices}
        if set(choices) - set(valid_agents):
            raise RuleInvariantError("Background self-care choice targets a nonactive or protected character.")
        for entity in sorted(valid_agents.values(), key=lambda item: str(item.id)):
            try:
                needs = self._needs(entity)
            except Exception as exc:
                raise RuleInvariantError("An active character has an invalid registered needs component.") from exc
            if needs is None:
                continue
            choice = choices.get(entity.id, BackgroundAgentChoice(entity_id=entity.id, action="continue"))
            fields: dict[str, object] = {
                "entity_id": entity.id,
                "expected_revision": needs.revision,
                "action": choice.action,
            }
            if choice.action in {"eat", "drink"}:
                need_name = "food" if choice.action == "eat" else "water"
                if choice.resource_id is None or choice.quantity is None:
                    raise RuleInvariantError("Background consumption choice is missing resource details.")
                need_value = getattr(needs, need_name)
                if not resource_matches_need(state.metadata, choice.resource_id, need_name) or need_value is None or need_value.value >= 100:
                    raise RuleInvariantError("Background consumption choice does not match an active food/water need.")
                with localcontext() as decimal_context:
                    decimal_context.prec = 80
                    points = int((choice.quantity * Decimal("10")).to_integral_value(rounding=ROUND_FLOOR))
                if points <= 0 or points > 100 - need_value.value:
                    raise RuleInvariantError("Background consumption quantity must restore need without waste.")
                try:
                    inventory = InventoryComponent.model_validate(entity.components.get("inventory"))
                    allocations = allocate_fifo_lots(
                        inventory,
                        resource_id=choice.resource_id,
                        quantity=choice.quantity,
                        at_tick=state.current_tick,
                        account_id=choice.account_id,
                    )
                except Exception as exc:
                    raise RuleInvariantError("Background consumption is not covered by valid FIFO inventory lots.") from exc
                sink_candidates = sorted(
                    (
                        (account_id, account)
                        for account_id, account in state.resources.items()
                        if account.resource_id == choice.resource_id and account.account_type == "consumption_sink"
                    ),
                    key=lambda item: str(item[0]),
                )
                if not sink_candidates:
                    raise RuleInvariantError("Background consumption has no registered sink for its resource.")
                sink_id, _sink = sink_candidates[0]
                account_ids: set[UUID] = {sink_id}
                lots: list[BackgroundConsumptionLot] = []
                for allocation in allocations:
                    account = state.resources.get(allocation.account_id)
                    if (
                        account is None
                        or account.account_type != "holder"
                        or account.owner_id != entity.id
                        or account.resource_id != choice.resource_id
                    ):
                        raise RuleInvariantError("Background FIFO lot is not backed by the agent's holder account.")
                    if account.balance < allocation.quantity:
                        raise RuleInvariantError("Background holder account cannot cover its FIFO lot quantity.")
                    account_ids.add(allocation.account_id)
                    lots.append(BackgroundConsumptionLot(
                        lot_id=allocation.lot_id,
                        account_id=allocation.account_id,
                        quantity=allocation.quantity,
                    ))
                for account_id in account_ids:
                    expected_accounts[account_id] = BackgroundAccountExpectation(
                        account_id=account_id,
                        expected_balance=state.resources[account_id].balance,
                    )
                fields.update({
                    "resource_id": choice.resource_id,
                    "consumed_quantity": choice.quantity,
                    "consumption_account_id": choice.account_id,
                    "expected_inventory_revision": inventory.revision,
                    "consumption_sink_account_id": sink_id,
                    "consumption_lots": lots,
                })
            expected_agents.append(BackgroundNeedRevisionExpectation(**fields))
        if not expected_agents:
            return None
        expected_agents.sort(key=lambda item: str(item.entity_id))
        return BackgroundNeedsAdvancedEvent(
            event_id=self._event_id(context, f"background.needs:{state.current_tick}:{duration_ticks}"),
            actor_id=context.actor_id,
            include_actor=context.include_actor_in_background,
            from_tick=state.current_tick,
            to_tick=state.current_tick + duration_ticks,
            expected_agents=expected_agents,
            expected_accounts=sorted(expected_accounts.values(), key=lambda item: str(item.account_id)),
            provenance=[ProvenanceReference(kind="rule", reference_id="needs.passive.v1")],
        )

    def _background_routine_event(
        self,
        state: WorldState,
        duration_ticks: int,
        context: RuleContext,
    ) -> tuple[BackgroundRoutineAdvancedEvent | None, int | None]:
        """Plan one all-agent routine batch and report any unsafe damage boundary."""
        end_tick = state.current_tick + duration_ticks
        agents: list[BackgroundRoutineAgentExpectation] = []
        expected_accounts: dict[UUID, BackgroundAccountExpectation] = {}
        boundary_at_end: int | None = None
        planning_end_tick = end_tick
        earliest_interior_boundary: int | None = None
        candidates = [
            entity for entity in state.entities.values()
            if (context.include_actor_in_background or entity.id != context.actor_id)
            and entity.kind in {"character", "person"}
            and entity.status not in {"dead", "retired"}
            and entity.components.get("needs") is not None
        ]
        candidates.sort(key=lambda entity: str(entity.id))
        occupied = occupied_npc_ids(state, state.current_tick)
        for entity in candidates:
            try:
                needs = NeedsComponent.model_validate(entity.components.get("needs"))
                raw_inventory = entity.components.get("inventory")
                inventory = InventoryComponent.model_validate(raw_inventory) if raw_inventory is not None else None
                raw_routine = entity.components.get("daily_routine")
                routine = DailyRoutineComponent.model_validate(raw_routine) if raw_routine is not None else None
            except Exception as exc:
                raise RuleInvariantError("An active character has an invalid needs, inventory or daily_routine component.") from exc
            advanced = advance_background_routine(
                needs,
                inventory=inventory,
                resources=state.resources,
                metadata=state.metadata,
                routine=None if entity.id in occupied else routine,
                location_id=entity.location_id,
                from_tick=state.current_tick,
                to_tick=planning_end_tick,
                day_seconds=state.calendar.day_seconds,
            )
            if (
                advanced.deprivation_boundary is not None
                and advanced.deprivation_boundary < planning_end_tick
            ):
                earliest_interior_boundary = advanced.deprivation_boundary
                planning_end_tick = advanced.deprivation_boundary
            if earliest_interior_boundary is not None:
                # The event will be rejected at this unsafe tick. Continue only
                # to find an earlier boundary; later manifests are discarded.
                continue
            if advanced.deprivation_boundary == end_tick:
                boundary_at_end = end_tick

            care = {item.need: item for item in advanced.care}

            def care_expectation(need: str) -> BackgroundRoutineNeedExpectation | None:
                item = care.get(need)
                if item is None:
                    return None
                lots = [
                    BackgroundConsumptionLot(
                        lot_id=lot.lot_id,
                        account_id=lot.account_id,
                        quantity=lot.quantity,
                    )
                    for lot in item.lots
                ]
                return BackgroundRoutineNeedExpectation(
                    resource_id=item.resource_id,
                    threshold=item.threshold,
                    action_count=item.action_count,
                    sink_account_id=item.sink_account_id,
                    consumption_lots=lots,
                )

            agents.append(
                BackgroundRoutineAgentExpectation(
                    entity_id=entity.id,
                    expected_needs_revision=needs.revision,
                    expected_inventory_revision=inventory.revision if inventory is not None else None,
                    expected_routine_revision=routine.revision if routine is not None else None,
                    expected_location_id=entity.location_id,
                    food=care_expectation("food"),
                    water=care_expectation("water"),
                )
            )
            for account_id, account in advanced.expected_accounts.items():
                expected_accounts.setdefault(
                    account_id,
                    BackgroundAccountExpectation(account_id=account_id, expected_balance=account.balance),
                )

        if earliest_interior_boundary is not None:
            return None, earliest_interior_boundary
        if not agents:
            return None, None
        event = BackgroundRoutineAdvancedEvent(
            event_id=self._event_id(context, f"background.routine:{state.current_tick}:{duration_ticks}"),
            actor_id=context.actor_id,
            include_actor=context.include_actor_in_background,
            from_tick=state.current_tick,
            to_tick=end_tick,
            expected_agents=agents,
            expected_accounts=sorted(expected_accounts.values(), key=lambda item: str(item.account_id)),
            provenance=[ProvenanceReference(kind="rule", reference_id="needs.background_routine.v1")],
        )
        return event, boundary_at_end

    def _lifespans_at(
        self,
        projected: WorldState,
        end_tick: int,
        context: RuleContext,
    ) -> tuple[list[LifeEndedEvent], WorldState]:
        results: list[LifeEndedEvent] = []
        for entity in sorted(projected.entities.values(), key=lambda item: str(item.id)):
            if entity.kind not in {"character", "person"} or entity.status in {"dead", "retired"}:
                continue
            life = self._life(entity)
            if life is None or life.is_immortal or life.lifespan_ticks is None:
                continue
            if life.birth_tick + life.lifespan_ticks != end_tick:
                continue
            event = LifeEndedEvent(
                event_id=self._event_id(context, f"life.expired:{entity.id}"),
                entity_id=entity.id,
                expected_life_revision=life.revision,
                expected_population_revision=self._population_revision(projected, entity),
            )
            projected = apply_event(projected, event)
            results.append(event)
        return results, projected

    def _rejected(
        self,
        context: RuleContext,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> RuleResolution:
        return self._resolution(
            WorldState(),
            context,
            status="rejected",
            code=code,
            message=message,
            details=details,
        )

    @staticmethod
    def _event_id(context: RuleContext, label: str) -> UUID:
        return uuid5(context.command_id, f"{context.ruleset_version}:{label}")

    @staticmethod
    def _roll(context: RuleContext, label: str, maximum: int) -> RuleRoll:
        material = ":".join(
            (str(context.world_id), str(context.branch_id), str(context.command_id), context.ruleset_version, label)
        ).encode("utf-8")
        digest = sha256(material).hexdigest()
        value = int(digest, 16) % maximum + 1
        return RuleRoll(label=label, value=value, maximum=maximum, outcome="drawn", digest=digest)

    @staticmethod
    def _capabilities(entity: WorldEntity) -> CapabilitiesComponent:
        return CapabilitiesComponent.model_validate(entity.components.get("capabilities", {}))

    @staticmethod
    def _health(entity: WorldEntity) -> HealthComponent | None:
        raw = entity.components.get("health")
        return HealthComponent.model_validate(raw) if raw is not None else None

    @staticmethod
    def _life(entity: WorldEntity) -> LifeComponent | None:
        raw = entity.components.get("life")
        return LifeComponent.model_validate(raw) if raw is not None else None

    @staticmethod
    def _population_revision(state: WorldState, entity: WorldEntity) -> int | None:
        life = RuleEngine._life(entity)
        if life is None or life.population_group_id is None:
            return None
        cohort = state.entities.get(life.population_group_id)
        if cohort is None:
            return None
        raw_population = cohort.components.get("population")
        if raw_population is None:
            return None
        return PopulationComponent.model_validate(raw_population).revision

    @staticmethod
    def _needs(entity: WorldEntity) -> NeedsComponent | None:
        raw = entity.components.get("needs")
        return NeedsComponent.model_validate(raw) if raw is not None else None

    @staticmethod
    def _skill_level(entity: WorldEntity, skill_id: str) -> Decimal:
        raw = entity.components.get("skills")
        if raw is None:
            return Decimal("0")
        try:
            skills = SkillsComponent.model_validate(raw)
        except Exception:
            return Decimal("0")
        value = skills.skills.get(skill_id)
        return value.level if value is not None else Decimal("0")

    @staticmethod
    def _attribute_int(entity: WorldEntity, attribute_id: str, default: int) -> int:
        raw = entity.components.get("attributes")
        if raw is None:
            return default
        try:
            attributes = AttributesComponent.model_validate(raw)
        except Exception:
            return default
        value = attributes.values.get(attribute_id, default)
        if isinstance(value, bool):
            return default
        try:
            number = Decimal(str(value))
        except Exception:
            return default
        if not number.is_finite():
            return default
        return max(-100, min(100, int(number)))

    @staticmethod
    def _is_local_to(entity: WorldEntity, place_id: UUID) -> bool:
        if entity.location_id == place_id:
            return True
        raw_parent = entity.components.get("parent_id")
        nested_place = entity.components.get("place")
        nested_parent = nested_place.get("parent_id") if isinstance(nested_place, dict) else None
        return str(raw_parent or nested_parent) == str(place_id)

    @staticmethod
    def _focus_matches(entity: WorldEntity, focus: str) -> bool:
        if focus == "people":
            return entity.kind in {"character", "person"}
        if focus == "routes":
            return entity.kind in {"place", "location"} and bool(
                entity.components.get("place", {}).get("routes", [])
                if isinstance(entity.components.get("place"), dict)
                else False
            )
        if focus == "supplies":
            return entity.kind == "item" or bool(
                entity.components.get("inventory", {}).get("lots", [])
                if isinstance(entity.components.get("inventory"), dict)
                else False
            )
        if focus == "history":
            place = entity.components.get("place")
            is_site = (
                entity.kind in {"place", "location"}
                and isinstance(place, dict)
                and place.get("place_type") == "site"
            )
            return (
                entity.kind in {"archive", "record", "monument"}
                or "historical_record" in entity.components
                or is_site
            )
        return True

    @staticmethod
    def _holder_account(state: WorldState, owner_id: UUID, resource_id: str) -> ResourceAccount | None:
        return next(
            (
                account for account in state.resources.values()
                if account.account_type == "holder" and account.owner_id == owner_id and account.resource_id == resource_id
            ),
            None,
        )

    @staticmethod
    def _system_account(state: WorldState, resource_id: str, roles: set[str]) -> ResourceAccount | None:
        return next(
            (
                account for account in state.resources.values()
                if account.account_type in roles and account.owner_id is None and account.resource_id == resource_id
            ),
            None,
        )

    @staticmethod
    def _account_id(state: WorldState, account: ResourceAccount) -> UUID:
        for account_id, candidate in state.resources.items():
            if candidate is account:
                return account_id
        for account_id, candidate in state.resources.items():
            if candidate == account:
                return account_id
        raise RuleInvariantError("A selected resource account is not part of the state.")


def _observed_entities(state: WorldState, actor_id: UUID) -> set[UUID]:
    return {
        record.subject_id for record in state.knowledge
        if record.subject_id is not None and actor_id in {record.holder_id, record.owner_id}
        and record.kind == "observation" and record.received_tick <= state.current_tick
        and (record.valid_from_tick is None or record.valid_from_tick <= state.current_tick)
    }


def route_access_reason(
    state: WorldState,
    actor: WorldEntity,
    route: PlaceRoute,
    destination: WorldEntity | None = None,
) -> tuple[str, str] | None:
    """Explain why a route is inaccessible; shared by actions and player scene projection."""
    if actor.location_id != route.from_place_id:
        return "route_origin_mismatch", "The character is not at this route's origin."
    if not route.is_open:
        return "route_closed", "The registered route is closed."
    destination = destination or state.entities.get(route.to_place_id)
    if destination is None or destination.kind not in {"place", "location"}:
        return "destination_unavailable", "The route destination is not a registered place."
    if destination.status in {"dead", "retired"}:
        return "destination_unavailable", "The route destination is retired."
    if destination.components.get("hidden") is True and destination.id not in _observed_entities(state, actor.id):
        return "destination_unseen", "The character has not discovered that destination."
    world_travel = state.metadata.get("travel")
    if world_travel is False or isinstance(world_travel, dict) and world_travel.get("enabled") is False:
        return "world_travel_disabled", "Travel is disabled by this world's policy."
    try:
        capabilities = RuleEngine._capabilities(actor)
    except Exception:
        return "travel_capability_required", "This character has no valid travel capabilities."
    grants = set(capabilities.grants)
    restrictions = set(capabilities.restrictions)
    if "world.travel" not in grants or "world.travel" in restrictions:
        return "travel_capability_required", "This character lacks permission to travel."
    if (SAFE_CONDUCT_TAG in route.access_tags and SAFE_CONDUCT_TAG not in grants
        and SAFE_CONDUCT_TAG not in restrictions
        and safe_conduct_allows_route(state, actor, route, state.safe_conduct_treaties)):
        grants.add(SAFE_CONDUCT_TAG)
    if set(route.access_tags) & restrictions or not set(route.access_tags).issubset(grants):
        return "route_access_denied", "The character lacks a required route capability."
    mode_map = {"travel.walk": "world.travel", "travel.ride": "travel.ride", "travel.boat": "travel.boat"}
    modes = {mode_map.get(mode, mode) for mode in route.travel_modes}
    if modes & restrictions or not modes.issubset(grants):
        return "travel_mode_unavailable", "The character cannot use this route's travel mode."
    try:
        place = PlaceComponent.model_validate(destination.components.get("place", {}))
    except Exception:
        place = None
    policy = place.access_policy if place is not None else "open"
    destination_required = {"restricted": "place.enter_restricted", "private": "place.enter_private"}.get(policy)
    if destination_required is not None and (
        destination_required in restrictions or destination_required not in grants
    ):
        return "destination_access_denied", "The destination requires an access capability."
    return None


def route_accessible(
    state: WorldState,
    actor: WorldEntity,
    route: PlaceRoute,
    destination: WorldEntity | None = None,
) -> bool:
    """Pure route predicate shared by rule resolution and action-scene projection."""
    return route_access_reason(state, actor, route, destination) is None


def _trunc_div(value: int, divisor: int) -> int:
    """Integer division rounded toward zero, preserving a signed exact remainder."""
    return value // divisor if value >= 0 else -((-value) // divisor)


def _fits_resource_balance(value: Decimal) -> bool:
    if not value.is_finite():
        return False
    digits = list(value.as_tuple().digits)
    exponent = value.as_tuple().exponent
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    scale = max(0, -exponent)
    integer_digits = max(1, len(digits) + exponent)
    return scale <= 6 and integer_digits <= 30

