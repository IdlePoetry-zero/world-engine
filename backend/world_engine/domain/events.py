"""Event parsing helpers for the closed, versioned core event registry."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid5

from pydantic import TypeAdapter, ValidationError

from .adjudication import (
    HISTORICAL_EVIDENCE_OBSERVATION_SUMMARY,
    historical_evidence_digest,
    source_text_digest,
)
from .combat_truce import COMBAT_TRUCE_CHANGED_EVENT_TYPE, CombatTruceChangedEvent
from .components import HistoricalEvidenceArtifact
from .investigation import entity_visible_to_actor
from .investigation_cases import (
    case_anchor_is_local_and_available,
    case_closure_outcome,
    case_definition_digest,
    case_definition_from_anchor,
    matched_case_routes,
    validate_case_assessment_access,
)
from .errors import InvalidEvent
from .environmental_source_events import EnvironmentalSourceAccountOpenedEvent
from .npc_plan_events import NpcPlanCreatedEvent, NpcPlanRevisedEvent
from .npc_plan_review_events import NpcPlanReviewResolvedEvent, NpcPlanReviewScheduledEvent
from .npc_meeting_attendance import (
    NPC_MEETING_ATTENDANCE_DECISION_RECORDED_EVENT_TYPE,
    NpcMeetingAttendanceDecisionRecordedEvent,
)
from .institutional_credit_events import INSTITUTIONAL_CREDIT_CHANGED_EVENT_TYPE, InstitutionalCreditChangedEvent
from .institutional_credit_extension_events import CREDIT_EXTENSION_EVENT_TYPE, InstitutionalCreditExtensionChangedEvent
from .institutional_treasury_reserve_events import InstitutionalTreasuryMobilizedEvent
from .market_pricing_events import MarketPriceReviewResolvedEvent
from .family_events import KinshipLinkEndedEvent, KinshipLinkRecordedEvent
from .occupation_events import OccupationAppointedEvent, OccupationResignedEvent
from .occupation_death_vacancies import OccupationDeathVacanciesResolvedEvent
from .occupation_legacy_vacancies import OccupationLegacyVacanciesResolvedEvent
from .npc_attendance_events import (
    NPC_ATTENDANCE_EVENTS,
    NPC_ATTENDANCE_SETTLED_EVENT_TYPE,
    NpcAttendanceCancelledEvent,
    NpcAttendanceSettledEvent,
    NpcAttendanceStartedEvent,
)
from .relationship_invitation_events import RelationInvitationChangedEvent
from .private_gifts import PrivateGiftChangedEvent
from .relationship_reflection_events import MeetingReflectionRecordedEvent
from .relationship_partnership_events import RelationshipPartnershipChangedEvent
from .relationship_disclosure_events import RelationshipDisclosureRecordedEvent
from .shipment_relief_events import ShipmentReliefDistributedEvent
from .travel_journey_events import TravelJourneyChangedEvent
from .waypoint_opportunity_events import (
    WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE,
    WaypointOpportunityDecisionRecordedEvent,
)
from .leadership_ballot_events import LEADERSHIP_BALLOT_CHANGED_EVENT_TYPE, LeadershipBallotChangedEvent
from .autonomous_vacancy_events import (
    LEADERSHIP_VACANCY_REVIEW_RECORDED_EVENT_TYPE,
    LeadershipVacancyReviewRecordedEvent,
)
from .safe_conduct_events import SAFE_CONDUCT_CHANGED_EVENT_TYPE, SafeConductChangedEvent
from .work_shift_events import WorkShiftPaidEvent
from .promise_trust_events import PromiseDeadlineBreachedWithTrustEvent, PromiseItemReturnedWithTrustEvent
from .promise_late_return_event import PromiseItemReturnedLateEvent
from .promise_events import (
    PromiseDeadlineBreachedEvent, PromiseItemReturnedEvent, PromiseReleasedEvent, RelationshipPromiseRecordedEvent,
)
from .models import (
    BackgroundDeprivationResolvedEvent,
    BackgroundNeedsAdvancedEvent,
    BackgroundRoutineAdvancedEvent,
    CombatExchangeResolvedEvent,
    CombatRetreatResolvedEvent,
    AuthorComponentRevisedEvent,
    ComponentReplacedEvent,
    DamageAppliedEvent,
    EntityDiscoveredEvent,
    DomainEvent,
    WorldState,
    EntityCreatedEvent,
    EntityFieldChangedEvent,
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
    CohortMemberPromotedEvent,
    PopulationYearAdvancedEvent,
    OrganizationMembershipReviewedEvent,
    GovernanceSuccessionEvent,
    GovernanceLeadershipHandedOverEvent,
    OrganizationMemberResignedEvent,
    HealthRestoredEvent,
    InjuryTreatedEvent,
    LifeEndedEvent,
    PlayerCharacterContinuedEvent,
    KnowledgeAcquiredEvent,
    RelationDimensionChangedEvent,
    RelationEstablishedEvent,
    ResourceAccountOpenedEvent,
    ResourceConsumedEvent,
    ResourceProducedEvent,
    RenewableResourceHarvestedEvent,
    ProductionQuotaRenewedEvent,
    ScheduledEventCompletedEvent,
    MovementCompletedEvent,
    MarketTradeExecutedEvent,
    ResourceTransferredEvent,
    TimeAdvancedEvent,
    ImprovisedCheckResolvedEvent,
    CultivationBreakthroughResolvedEvent,
)
from .world_books import WorldBookImportedEvent
from .story_nodes import STORY_NODE_RESOLVED, StoryMilestoneResolvedEvent
from .challenges import CHALLENGE_RESOLVED, ChallengeResolvedEvent
from .evidence_destruction import validate_destroyable_historical_item

EVENT_TYPES: dict[str, type] = {
    "core.entity_created.v1": EntityCreatedEvent,
    "core.world_book_imported.v1": WorldBookImportedEvent,
    STORY_NODE_RESOLVED: StoryMilestoneResolvedEvent,
    CHALLENGE_RESOLVED: ChallengeResolvedEvent,
    "core.entity_field_changed.v1": EntityFieldChangedEvent,
    "core.resource_transferred.v1": ResourceTransferredEvent,
    "core.time_advanced.v1": TimeAdvancedEvent,
    "core.component_replaced.v1": ComponentReplacedEvent,
    "core.author_component_revised.v1": AuthorComponentRevisedEvent,
    "core.kinship_link_recorded.v1": KinshipLinkRecordedEvent,
    "core.kinship_link_ended.v1": KinshipLinkEndedEvent,
    "core.occupation_appointed.v1": OccupationAppointedEvent,
    "core.occupation_resigned.v1": OccupationResignedEvent,
    "core.occupation_death_vacancies_resolved.v1": OccupationDeathVacanciesResolvedEvent,
    "core.occupation_legacy_vacancies_resolved.v1": OccupationLegacyVacanciesResolvedEvent,
    "core.npc_attendance_started.v1": NpcAttendanceStartedEvent,
    "core.npc_plan_created.v1": NpcPlanCreatedEvent,
    "core.npc_plan_revised.v1": NpcPlanRevisedEvent,
    "core.npc_plan_review_resolved.v1": NpcPlanReviewResolvedEvent,
    "core.npc_plan_review_scheduled.v1": NpcPlanReviewScheduledEvent,
    NPC_MEETING_ATTENDANCE_DECISION_RECORDED_EVENT_TYPE: NpcMeetingAttendanceDecisionRecordedEvent,
    INSTITUTIONAL_CREDIT_CHANGED_EVENT_TYPE: InstitutionalCreditChangedEvent,
    CREDIT_EXTENSION_EVENT_TYPE: InstitutionalCreditExtensionChangedEvent,
    "core.institutional_treasury_mobilized.v1": InstitutionalTreasuryMobilizedEvent,
    "core.npc_attendance_cancelled.v1": NpcAttendanceCancelledEvent,
    NPC_ATTENDANCE_SETTLED_EVENT_TYPE: NpcAttendanceSettledEvent,
    "core.relationship_invitation_changed.v1": RelationInvitationChangedEvent,
    "core.private_gift_changed.v1": PrivateGiftChangedEvent,
    "core.meeting_reflection_recorded.v1": MeetingReflectionRecordedEvent,
    "core.relationship_partnership_changed.v1": RelationshipPartnershipChangedEvent,
    "core.relationship_disclosure_recorded.v1": RelationshipDisclosureRecordedEvent,
    "core.shipment_relief_distributed.v1": ShipmentReliefDistributedEvent,
    "core.travel_journey_changed.v1": TravelJourneyChangedEvent,
    WAYPOINT_OPPORTUNITY_DECISION_RECORDED_EVENT_TYPE: WaypointOpportunityDecisionRecordedEvent,
    "core.work_shift_paid.v1": WorkShiftPaidEvent,
    "core.movement_completed.v1": MovementCompletedEvent,
    "core.market_trade_executed.v1": MarketTradeExecutedEvent,
    "core.entity_discovered.v1": EntityDiscoveredEvent,
    "core.improvised_check_resolved.v1": ImprovisedCheckResolvedEvent,
    "core.cultivation_breakthrough_resolved.v1": CultivationBreakthroughResolvedEvent,
    "core.combat_exchange_resolved.v1": CombatExchangeResolvedEvent,
    "core.combat_retreat_resolved.v1": CombatRetreatResolvedEvent,
    COMBAT_TRUCE_CHANGED_EVENT_TYPE: CombatTruceChangedEvent,
    "core.damage_applied.v1": DamageAppliedEvent,
    "core.health_restored.v1": HealthRestoredEvent,
    "core.injury_treated.v1": InjuryTreatedEvent,
    "core.life_ended.v1": LifeEndedEvent,
    "core.player_character_continued.v1": PlayerCharacterContinuedEvent,
    "core.cohort_member_promoted.v1": CohortMemberPromotedEvent,
    "core.population_year_advanced.v1": PopulationYearAdvancedEvent,
    "core.organization_membership_reviewed.v1": OrganizationMembershipReviewedEvent,
    "core.background_needs_advanced.v1": BackgroundNeedsAdvancedEvent,
    "core.background_routine_advanced.v1": BackgroundRoutineAdvancedEvent,
    "core.background_deprivation_resolved.v1": BackgroundDeprivationResolvedEvent,
    "core.governance_succession.v1": GovernanceSuccessionEvent,
    "core.governance_leadership_handed_over.v1": GovernanceLeadershipHandedOverEvent,
    LEADERSHIP_BALLOT_CHANGED_EVENT_TYPE: LeadershipBallotChangedEvent,
    LEADERSHIP_VACANCY_REVIEW_RECORDED_EVENT_TYPE: LeadershipVacancyReviewRecordedEvent,
    SAFE_CONDUCT_CHANGED_EVENT_TYPE: SafeConductChangedEvent,
    "core.organization_member_resigned.v1": OrganizationMemberResignedEvent,
    "core.production_quota_renewed.v1": ProductionQuotaRenewedEvent,
    "core.renewable_resource_harvested.v1": RenewableResourceHarvestedEvent,
    "core.scheduled_event_completed.v1": ScheduledEventCompletedEvent,
    "core.resource_account_opened.v1": ResourceAccountOpenedEvent,
    "core.environmental_source_account_opened.v1": EnvironmentalSourceAccountOpenedEvent,
    "core.knowledge_acquired.v1": KnowledgeAcquiredEvent,
    "core.relation_established.v1": RelationEstablishedEvent,
    "core.relation_dimension_changed.v1": RelationDimensionChangedEvent,
    "core.goal_created.v1": GoalCreatedEvent,
    "core.goal_updated.v1": GoalUpdatedEvent,
    "core.investigation_case_opened.v1": InvestigationCaseOpenedEvent,
    "core.investigation_case_evidence_attached.v1": InvestigationCaseEvidenceAttachedEvent,
    "core.investigation_case_closed.v1": InvestigationCaseClosedEvent,
    "core.investigation_public_accusation_corrected.v1": PublicAccusationCorrectionRecordedEvent,
    "core.accusation_repair_offer_recorded.v1": AccusationRepairOfferRecordedEvent,
    "core.accusation_repair_resolved.v1": AccusationRepairResolvedEvent,
    "core.shipment_dispatched.v1": ShipmentDispatchedEvent,
    "core.shipment_arrival_delivered.v1": ShipmentArrivalDeliveredEvent,
    "core.shipment_arrival_deferred.v1": ShipmentArrivalDeferredEvent,
    "core.shipment_receipt_accepted.v1": ShipmentReceiptAcceptedEvent,
    "core.shipment_spoilage_resolved.v1": ShipmentSpoilageResolvedEvent,
    "core.historical_evidence_destroyed.v1": HistoricalEvidenceDestroyedEvent,
    "core.relationship_promise_recorded.v1": RelationshipPromiseRecordedEvent,
    "core.promise_item_returned.v1": PromiseItemReturnedEvent,
    "core.promise_released.v1": PromiseReleasedEvent,
    "core.promise_breached.v1": PromiseDeadlineBreachedEvent,
    "core.promise_item_returned.v2": PromiseItemReturnedWithTrustEvent,
    "core.promise_breached.v2": PromiseDeadlineBreachedWithTrustEvent,
    "core.promise_item_returned_late.v1": PromiseItemReturnedLateEvent,
    "core.market_price_review_resolved.v1": MarketPriceReviewResolvedEvent,
    "core.resource_consumed.v1": ResourceConsumedEvent,
    "core.resource_produced.v1": ResourceProducedEvent,
}
INVESTIGATION_CASE_COMMAND_TYPES = frozenset({
    "action.open_investigation_case",
    "action.attach_case_evidence",
    "action.close_investigation_case",
})
INVESTIGATION_CASE_EVENT_TYPES = frozenset({
    "core.investigation_case_opened.v1",
    "core.investigation_case_evidence_attached.v1",
    "core.investigation_case_closed.v1",
})
PUBLIC_ACCUSATION_CORRECTION_COMMAND_TYPES = frozenset({
    "action.correct_public_accusation",
})
PUBLIC_ACCUSATION_CORRECTION_EVENT_TYPES = frozenset({
    "core.investigation_public_accusation_corrected.v1",
})
ACCUSATION_REPAIR_OFFER_COMMAND_TYPES = frozenset({"action.offer_accusation_repair"})
ACCUSATION_REPAIR_RESOLUTION_COMMAND_TYPES = frozenset({
    "action.resolve_accusation_repair",
    "action.request_accusation_repair_response",
})
ACCUSATION_REPAIR_COMMAND_TYPES = (
    ACCUSATION_REPAIR_OFFER_COMMAND_TYPES | ACCUSATION_REPAIR_RESOLUTION_COMMAND_TYPES
)
ACCUSATION_REPAIR_EVENT_TYPES = frozenset({
    "core.accusation_repair_offer_recorded.v1",
    "core.accusation_repair_resolved.v1",
})
HISTORICAL_EVIDENCE_DESTRUCTION_COMMAND_TYPES = frozenset({
    "action.destroy_historical_evidence",
})
HISTORICAL_EVIDENCE_DESTRUCTION_EVENT_TYPES = frozenset({
    "core.historical_evidence_destroyed.v1",
})
_EVENT_ADAPTER = TypeAdapter(DomainEvent)


def requires_investigation_case_validation(command_type: str | None, events: Sequence[DomainEvent]) -> bool:
    """Cheaply identify command batches requiring the state-aware case validator."""
    return command_type in INVESTIGATION_CASE_COMMAND_TYPES or any(
        getattr(event, "type", None) in INVESTIGATION_CASE_EVENT_TYPES for event in events
    )


def requires_public_accusation_correction_validation(
    command_type: str | None,
    events: Sequence[DomainEvent],
) -> bool:
    """Identify commands and event batches that need correction-specific state checks."""
    return command_type in PUBLIC_ACCUSATION_CORRECTION_COMMAND_TYPES or any(
        getattr(event, "type", None) in PUBLIC_ACCUSATION_CORRECTION_EVENT_TYPES for event in events
    )


def requires_accusation_repair_validation(command_type: str | None, events: Sequence[DomainEvent]) -> bool:
    """Identify accusation repair commands and events that need a batch check."""
    return command_type in ACCUSATION_REPAIR_COMMAND_TYPES or any(
        getattr(event, "type", None) in ACCUSATION_REPAIR_EVENT_TYPES for event in events
    )


def requires_historical_evidence_destruction_validation(
    command_type: str | None, events: Sequence[DomainEvent],
) -> bool:
    """Cheaply identify command batches that need source-destruction validation."""
    return command_type in HISTORICAL_EVIDENCE_DESTRUCTION_COMMAND_TYPES or any(
        getattr(event, "type", None) in HISTORICAL_EVIDENCE_DESTRUCTION_EVENT_TYPES for event in events
    )


def parse_event(value: DomainEvent | Mapping[str, object]) -> DomainEvent:
    """Return a registered typed event or reject unknown/invalid input."""
    if isinstance(value, tuple(EVENT_TYPES.values())):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise InvalidEvent("Events must be typed core events or JSON objects.")
    event_type = value.get("type")
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise InvalidEvent(f"Unregistered event type: {event_type!r}.")
    try:
        return cast(DomainEvent, _EVENT_ADAPTER.validate_python(dict(value)))
    except ValidationError as exc:
        raise InvalidEvent(f"Event {event_type!r} failed schema validation: {exc}") from exc


def event_payload(event: DomainEvent) -> dict[str, object]:
    """Get event-specific fields, excluding envelope type/id/visibility."""
    payload = event.model_dump(mode="json", exclude={"type", "event_id", "visibility"})
    if isinstance(event, ImprovisedCheckResolvedEvent) and event.historical_evidence_id is None:
        # Keep event payloads byte-compatible for checks written before authored
        # historical evidence could be selected.
        payload.pop("historical_evidence_id", None)
        payload.pop("historical_evidence_hash", None)
    return payload


def validate_npc_attendance_scope(
    events: Sequence[DomainEvent], *, world_id: UUID, branch_id: UUID, command_id: UUID,
    actor_id: UUID, ruleset_version: str,
) -> None:
    for event in events:
        if isinstance(event, NPC_ATTENDANCE_EVENTS) and (
            event.world_id, event.branch_id, event.command_id, event.actor_id, event.ruleset_version,
        ) != (world_id, branch_id, command_id, actor_id, ruleset_version):
            raise InvalidEvent("NPC attendance must match the world, branch, actor and ruleset of its command.")


def validate_background_clock_batch(state_tick: int, actor_id: UUID, events: Sequence[DomainEvent]) -> None:
    """Bind the passive needs batch to the command's exact time advance and actor."""
    for event in events:
        if isinstance(event, BackgroundDeprivationResolvedEvent) and event.actor_id != actor_id:
            raise InvalidEvent("Background deprivation must use the command's acting character.")
    background = [event for event in events
                  if isinstance(event, (BackgroundNeedsAdvancedEvent, BackgroundRoutineAdvancedEvent))]
    if not background:
        return
    travel_delay = isinstance(events[0], TravelJourneyChangedEvent) and events[0].operation == "delay"
    clock_index = int(travel_delay)
    if len(events) < clock_index + 2:
        raise InvalidEvent("Background needs must follow their acting character's exact clock advance.")
    clock = events[clock_index]
    if (
        len(background) != 1 or events[clock_index + 1] is not background[0]
        or not isinstance(clock, TimeAdvancedEvent)
        or background[0].from_tick != state_tick
        or background[0].to_tick != clock.to_tick
        or background[0].actor_id != actor_id
    ):
        raise InvalidEvent("Background needs must follow their acting character's exact clock advance.")


def validate_population_membership_batch(state: WorldState, events: Sequence[DomainEvent]) -> None:
    """Require opted-in membership review immediately after its annual census.

    Replay and commit callers invoke this once with the pre-batch state. Reducers
    then verify the typed plan against the post-census state and its provenance.
    """
    census = [event for event in events if isinstance(event, PopulationYearAdvancedEvent)]
    reviews = [event for event in events if isinstance(event, OrganizationMembershipReviewedEvent)]
    if len(census) > 1 or len(reviews) > 1:
        raise InvalidEvent("A command cannot repeat the annual population census or membership review.")
    if not census:
        if reviews:
            raise InvalidEvent("An organization membership review requires its annual population census.")
        return
    policy_enabled = any(
        isinstance(entity.components.get("governance"), dict)
        and bool(entity.components["governance"].get("membership_recruitment"))
        for entity in state.entities.values()
    )
    census_event = census[0]
    if not policy_enabled:
        if reviews:
            raise InvalidEvent("A world without recruitment policy cannot record an annual membership review.")
        return
    if len(reviews) != 1:
        raise InvalidEvent("A recruitment-enabled world requires one membership review after its annual census.")
    review = reviews[0]
    census_index = next(index for index, event in enumerate(events) if event is census_event)
    if (
        census_index + 1 >= len(events)
        or events[census_index + 1] is not review
        or review.census_event_id != census_event.event_id
        or review.scheduled_event_id != census_event.scheduled_event_id
        or review.year_index != census_event.year_index
        or census_index + 2 >= len(events)
        or not isinstance(events[census_index + 2], ScheduledEventCompletedEvent)
        or events[census_index + 2].scheduled_event_id != census_event.scheduled_event_id
    ):
        raise InvalidEvent("Membership review must immediately follow its matching census before schedule completion.")


def validate_adjudication_batch(
    state_tick: int, actor_id: UUID, events: Sequence[DomainEvent], *,
    world_id: UUID, branch_id: UUID, command_id: UUID, ruleset_version: str,
    state: WorldState | None = None,
) -> None:
    audits = [event for event in events if isinstance(event, ImprovisedCheckResolvedEvent)]
    if not audits:
        return
    audit = audits[0]
    clock = events[0]
    if (len(audits) != 1 or not isinstance(clock, TimeAdvancedEvent)
        or (audit.world_id, audit.branch_id, audit.command_id, audit.ruleset_version, audit.actor_id)
        != (world_id, branch_id, command_id, ruleset_version, actor_id)
        or audit.started_tick != state_tick or audit.resolved_tick != clock.to_tick):
        raise InvalidEvent("An improvised check must match its committed command and clock window.")
    observations = [event.record for event in events if isinstance(event, KnowledgeAcquiredEvent)
                    and event.record.source_event_id == audit.event_id]
    if not audit.passed and not observations:
        return
    if not audit.passed or len(observations) != 1:
        raise InvalidEvent("Only a successful improvised check can produce one observation.")
    record = observations[0]
    common_mismatch = (
        record.id != audit.observation_record_id
        or record.owner_id != actor_id
        or record.holder_id is not None
        or record.kind != "observation"
        or record.received_tick != clock.to_tick
        or record.valid_from_tick is not None
        or record.visibility.policy != "private"
        or record.visibility.subject_ids
        or record.assessment is not None
        or record.disclosure is not None
        or record.disclosure_policy != "never"
    )
    if audit.historical_evidence_id is None:
        if (
            common_mismatch
            or record.subject_id != audit.target_id
            or source_text_digest(record.claim) != audit.source_description_hash
        ):
            raise InvalidEvent("An improvised observation must reproduce the verified description for its actor.")
        return

    if state is None:
        raise InvalidEvent("Historical evidence inspection requires the command's pre-batch world state.")
    target = state.entities.get(audit.target_id)
    actor = state.entities.get(actor_id)
    place = state.entities.get(audit.location_id)
    if (
        state.current_tick != state_tick
        or actor is None
        or actor.kind not in {"character", "person"}
        or actor.status != "active"
        or actor.location_id != audit.location_id
        or target is None
        or target.kind != "item"
        or target.status != "active"
        or place is None
        or place.kind not in {"place", "location"}
        or place.status != "active"
    ):
        raise InvalidEvent("Historical evidence inspection participants are not active at the recorded place.")
    raw_target_place = target.components.get("place")
    nested_parent = raw_target_place.get("parent_id") if isinstance(raw_target_place, dict) else None
    target_parent = target.components.get("parent_id")
    if (
        target.location_id != audit.location_id
        and str(target_parent or nested_parent) != str(audit.location_id)
    ):
        raise InvalidEvent("Historical evidence item is not local to the examiner.")
    if not entity_visible_to_actor(state, actor_id, target.id):
        raise InvalidEvent("Historical evidence item is not visible to the examiner.")
    try:
        artifact = HistoricalEvidenceArtifact.model_validate(target.components.get("historical_evidence"))
    except Exception as exc:
        raise InvalidEvent("Historical evidence audit does not identify a registered artifact.") from exc
    subject = state.entities.get(artifact.subject_id)
    if (
        artifact.evidence_id != audit.historical_evidence_id
        or historical_evidence_digest(artifact) != audit.historical_evidence_hash
        or artifact.fact.effective_tick > state.current_tick
        or subject is None
        or not entity_visible_to_actor(state, actor_id, subject.id)
        or any(
            artifact.evidence_id in known.source_refs
            and known.kind == "observation"
            and actor_id in {known.owner_id, known.holder_id}
            for known in state.knowledge
        )
    ):
        raise InvalidEvent("Historical evidence source is stale, unavailable, or already inspected by this actor.")
    if (
        common_mismatch
        or record.subject_id != artifact.subject_id
        or record.claim != artifact.claim
        or record.summary != HISTORICAL_EVIDENCE_OBSERVATION_SUMMARY
        or record.confidence != 1
        or record.fact != artifact.fact
        or record.source_refs != [artifact.evidence_id]
        or record.provenance != artifact.provenance
    ):
        raise InvalidEvent("An improvised observation must reproduce the verified description for its actor.")


def validate_investigation_case_batch(
    state: WorldState,
    actor_id: UUID,
    events: Sequence[DomainEvent],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    command_type: str | None,
    command_payload: Mapping[str, object] | None,
) -> None:
    """Verify case opening, evidence filing, closure and any authored public consequence."""
    case_events = [event for event in events if isinstance(event, (
        InvestigationCaseOpenedEvent,
        InvestigationCaseEvidenceAttachedEvent,
        InvestigationCaseClosedEvent,
    ))]
    if not case_events:
        if events and command_type in INVESTIGATION_CASE_COMMAND_TYPES:
            raise InvalidEvent("A registered case command cannot commit a batch without a case event.")
        return
    if (command_type is None) != (command_payload is None):
        raise InvalidEvent("Case command metadata must provide both command_type and command_payload or neither.")
    if len(case_events) != 1:
        raise InvalidEvent("A command may perform exactly one investigation case operation.")
    event = case_events[0]
    if (
        event.world_id != world_id
        or event.branch_id != branch_id
        or event.command_id != command_id
        or event.ruleset_version != ruleset_version
        or event.actor_id != actor_id
    ):
        raise InvalidEvent("Investigation case event must match its committed command and actor.")
    relation_events = [item for item in events if isinstance(item, (RelationEstablishedEvent, RelationDimensionChangedEvent))]
    if any(item is not event and item not in relation_events for item in events):
        raise InvalidEvent("Investigation case commands cannot include unrelated world events.")

    if isinstance(event, InvestigationCaseOpenedEvent):
        if command_type is not None:
            if command_type != "action.open_investigation_case":
                raise InvalidEvent("Case opening must match its registered action command.")
            if command_payload != {"kind": "open_investigation_case", "anchor_id": str(event.case_file.anchor_id)}:
                raise InvalidEvent("Case opening must match its exact authored anchor proposal.")
        if relation_events:
            raise InvalidEvent("Opening a case file cannot change character relations.")
        if not case_anchor_is_local_and_available(state, actor_id, event.case_file.anchor_id):
            raise InvalidEvent("Investigation case anchor is not locally available to the actor.")
        anchor = state.entities[event.case_file.anchor_id]
        try:
            definition = case_definition_from_anchor(anchor)
        except Exception as exc:
            raise InvalidEvent("Case opening anchor has no valid authored investigation definition.") from exc
        case_file = event.case_file
        if (
            definition.case_id != case_file.case_id
            or case_definition_digest(definition) != case_file.definition_hash
            or case_file.title != definition.title
            or case_file.brief != definition.brief
            or case_file.owner_id != actor_id
            or case_file.anchor_id != anchor.id
            or case_file.opened_tick != state.current_tick
            or case_file.updated_tick != state.current_tick
            or case_file.revision != 1
            or case_file.status != "open"
            or case_file.evidence_refs
            or event.visibility != {"policy": "author_only"}
            or any(item.owner_id == actor_id and item.case_id == definition.case_id for item in state.investigation_cases)
        ):
            raise InvalidEvent("Opened case file does not match the available authored definition.")
        return

    case_file = next((item for item in state.investigation_cases if item.id == event.case_file_id), None)
    if (
        case_file is None
        or case_file.owner_id != actor_id
        or case_file.status != "open"
        or event.visibility != {"policy": "author_only"}
    ):
        raise InvalidEvent("Investigation case file is unavailable to this actor or already closed.")
    anchor = state.entities.get(case_file.anchor_id)
    if anchor is None or anchor.kind != "item":
        raise InvalidEvent("Investigation case anchor is unavailable.")
    try:
        definition = case_definition_from_anchor(anchor)
    except Exception as exc:
        raise InvalidEvent("Investigation case definition is unavailable.") from exc
    if definition.case_id != case_file.case_id or case_definition_digest(definition) != case_file.definition_hash:
        raise InvalidEvent("Investigation case definition changed after the file was opened.")

    if isinstance(event, InvestigationCaseEvidenceAttachedEvent):
        if command_type is not None:
            if command_type != "action.attach_case_evidence":
                raise InvalidEvent("Evidence filing must match its registered action command.")
            if command_payload != {
                "kind": "attach_case_evidence",
                "case_file_id": str(event.case_file_id),
                "assessment_id": str(event.assessment_id),
            }:
                raise InvalidEvent("Evidence filing must match its exact case and assessment IDs.")
        if relation_events or event.expected_revision != case_file.revision or event.updated_tick != state.current_tick:
            raise InvalidEvent("Evidence filing uses a stale case revision or tick.")
        if event.assessment_id in case_file.evidence_refs:
            raise InvalidEvent("This assessment is already attached to the case file.")
        record = next((item for item in state.knowledge if item.id == event.assessment_id), None)
        if (
            record is None or record.owner_id != actor_id or record.holder_id is not None
            or record.assessment is None or record.visibility.policy != "private"
        ):
            raise InvalidEvent("Only an actor-owned private evidence assessment can be filed.")
        try:
            validate_case_assessment_access(
                record, state.knowledge, actor_id=actor_id, at_tick=state.current_tick,
            )
        except ValueError as exc:
            raise InvalidEvent("Filed evidence assessment does not have a valid independent source chain.") from exc
        return

    if command_type is not None:
        if command_type != "action.close_investigation_case":
            raise InvalidEvent("Case closure must match its registered action command.")
        if command_payload != {
            "kind": "close_investigation_case",
            "case_file_id": str(event.case_file_id),
            "suspect_id": str(event.suspect_id) if event.suspect_id is not None else None,
            "public_accusation": event.public_accusation,
        }:
            raise InvalidEvent("Case closure must match its exact suspect and accusation proposal.")
    if relation_events and not event.public_accusation:
        raise InvalidEvent("A private case conclusion cannot change an NPC relation.")
    if event.expected_revision != case_file.revision or event.closed_tick != state.current_tick:
        raise InvalidEvent("Case closure uses a stale revision or tick.")
    if event.suspect_id is not None:
        suspect = state.entities.get(event.suspect_id)
        actor = state.entities.get(actor_id)
        if (
            suspect is None or suspect.kind not in {"character", "person"}
            or suspect.id == actor_id or actor is None
            or not entity_visible_to_actor(state, actor_id, suspect.id)
        ):
            raise InvalidEvent("Selected suspect is not known to the case-file owner.")
    routes = matched_case_routes(
        definition, case_file, state.knowledge, actor_id=actor_id, at_tick=state.current_tick,
    )
    outcome = case_closure_outcome(event.suspect_id, routes)
    if event.outcome != outcome:
        raise InvalidEvent("Case outcome does not match its actor-owned filed evidence and authored routes.")
    if event.public_accusation:
        actor = state.entities.get(actor_id)
        suspect = state.entities.get(event.suspect_id) if event.suspect_id is not None else None
        if (
            suspect is None or suspect.status != "active" or actor is None or actor.location_id is None
            or suspect.location_id != actor.location_id
            or not entity_visible_to_actor(state, actor_id, suspect.id)
            or not any(route.suspect_id == suspect.id for route in routes)
        ):
            raise InvalidEvent("A public accusation requires a present, visible suspect and an applicable authored route.")

    effect = definition.wrong_suspect_effect if outcome == "disconfirmed" and event.public_accusation else None
    _validate_case_relation_effect(
        state,
        actor_id=actor_id,
        suspect_id=event.suspect_id,
        closed_event=event,
        events=relation_events,
        effect=effect,
        command_id=command_id,
        ruleset_version=ruleset_version,
    )


def _validate_case_relation_effect(
    state: WorldState,
    *,
    actor_id: UUID,
    suspect_id: UUID | None,
    closed_event: InvestigationCaseClosedEvent,
    events: Sequence[DomainEvent],
    effect: object | None,
    command_id: UUID,
    ruleset_version: str,
) -> None:
    if effect is None:
        if events:
            raise InvalidEvent("No relation change is authorized for this case closure.")
        return
    if suspect_id is None:
        raise InvalidEvent("A wrong-suspect consequence requires a selected suspect.")
    dimension = getattr(effect, "dimension", None)
    delta = getattr(effect, "delta", None)
    if not isinstance(dimension, str) or not isinstance(delta, int) or isinstance(delta, bool) or delta == 0:
        raise InvalidEvent("Authored wrong-suspect relation effect is invalid.")
    target_ids = sorted({str(actor_id), str(suspect_id)})
    expected_event_id = uuid5(command_id, f"{ruleset_version}:investigation_case.wrong_suspect_relation.event")
    expected_relation_id = uuid5(command_id, f"{ruleset_version}:investigation_case.wrong_suspect_relation")
    directed = [item for item in state.relations if item.from_id == suspect_id and item.to_id == actor_id]
    if len(directed) > 1:
        raise InvalidEvent("Authored accusation effect cannot target duplicated directed relations.")
    if not directed:
        if len(events) != 1 or not isinstance(events[0], RelationEstablishedEvent):
            raise InvalidEvent("A missing directed relation requires one authored relation-establishment event.")
        event = events[0]
        relation = event.relation
        if (
            event.event_id != expected_event_id
            or event.visibility != {"policy": "subject_ids", "subject_ids": target_ids}
            or relation.id != expected_relation_id
            or relation.from_id != suspect_id or relation.to_id != actor_id
            or relation.dimensions != {dimension: Decimal(delta)}
            or relation.updated_tick != state.current_tick
            or relation.evidence_refs != [closed_event.event_id]
            or relation.visibility.policy != "subject_ids"
            or relation.visibility.subject_ids != [UUID(value) for value in target_ids]
        ):
            raise InvalidEvent("New accusation relation does not match its authored bounded consequence.")
        return

    relation = directed[0]
    previous = relation.dimensions.get(dimension)
    current = previous if previous is not None else Decimal("0")
    value = min(Decimal("100"), max(Decimal("-100"), current + Decimal(delta)))
    if value == current:
        if events:
            raise InvalidEvent("A bounded accusation relation already at its limit cannot be changed.")
        return
    if len(events) != 1 or not isinstance(events[0], RelationDimensionChangedEvent):
        raise InvalidEvent("An existing directed relation requires one authored dimension-change event.")
    event = events[0]
    if (
        event.event_id != expected_event_id
        or event.visibility != {"policy": "subject_ids", "subject_ids": target_ids}
        or event.relation_id != relation.id
        or event.dimension != dimension
        or event.expected_previous != previous
        or event.value != value
        or event.evidence_refs != [closed_event.event_id]
    ):
        raise InvalidEvent("Updated accusation relation does not match its authored bounded consequence.")


def validate_public_accusation_correction_batch(
    state: WorldState,
    actor_id: UUID,
    events: Sequence[DomainEvent],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    command_type: str | None,
    command_payload: Mapping[str, object] | None,
) -> None:
    """Validate one exact, zero-time correction of the actor's disconfirmed accusation."""
    corrections = [event for event in events if isinstance(event, PublicAccusationCorrectionRecordedEvent)]
    if not corrections:
        if events and command_type in PUBLIC_ACCUSATION_CORRECTION_COMMAND_TYPES:
            raise InvalidEvent("Public accusation correction is unavailable.")
        return
    if (command_type is None) != (command_payload is None):
        raise InvalidEvent("Public accusation correction is unavailable.")
    if len(events) != 1 or len(corrections) != 1:
        raise InvalidEvent("Public accusation correction is unavailable.")

    event = corrections[0]
    record = event.record
    if (
        event.world_id != world_id
        or event.branch_id != branch_id
        or event.command_id != command_id
        or event.ruleset_version != ruleset_version
        or event.actor_id != actor_id
        or record.actor_id != actor_id
        or record.event_id != event.event_id
        or record.corrected_tick != state.current_tick
        or record.source_closure_event_id == event.event_id
        or event.visibility != {"policy": "author_only"}
    ):
        raise InvalidEvent("Public accusation correction is unavailable.")
    if command_type is not None and (
        command_type != "action.correct_public_accusation"
        or command_payload != {
            "kind": "correct_public_accusation",
            "case_file_id": str(record.case_file_id),
            "expected_revision": record.expected_revision,
            "source_closure_event_id": str(record.source_closure_event_id),
        }
    ):
        raise InvalidEvent("Public accusation correction is unavailable.")

    case_file = next((item for item in state.investigation_cases if item.id == record.case_file_id), None)
    actor = state.entities.get(actor_id)
    suspect = state.entities.get(record.suspect_id)
    location = state.entities.get(record.location_id)
    if (
        case_file is None
        or case_file.owner_id != actor_id
        or case_file.status != "closed"
        or case_file.outcome != "disconfirmed"
        or not case_file.public_accusation
        or case_file.suspect_id != record.suspect_id
        or case_file.revision != record.expected_revision
        or case_file.closed_tick is None
        or case_file.closed_tick > record.corrected_tick
        or actor is None or actor.kind not in {"character", "person"} or actor.status != "active"
        or suspect is None or suspect.kind not in {"character", "person"} or suspect.status != "active"
        or actor.location_id is None
        or actor.location_id != suspect.location_id
        or actor.location_id != record.location_id
        or location is None or location.kind not in {"place", "location"} or location.status != "active"
        or not entity_visible_to_actor(state, actor_id, suspect.id)
        or any(item.case_file_id == record.case_file_id for item in state.public_accusation_corrections)
        or any(item.source_closure_event_id == record.source_closure_event_id
               for item in state.public_accusation_corrections)
    ):
        raise InvalidEvent("Public accusation correction is unavailable.")


def validate_accusation_repair_batch(
    state: WorldState,
    actor_id: UUID,
    events: Sequence[DomainEvent],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    command_type: str | None,
    command_payload: Mapping[str, object] | None,
    causation_ids: Sequence[UUID] | None = None,
) -> None:
    """Validate one repair offer or one target-owned response and its scoped trust effect."""
    offers = [event for event in events if isinstance(event, AccusationRepairOfferRecordedEvent)]
    responses = [event for event in events if isinstance(event, AccusationRepairResolvedEvent)]
    if not offers and not responses:
        if events and command_type in ACCUSATION_REPAIR_COMMAND_TYPES:
            raise InvalidEvent("Accusation repair is unavailable.")
        return
    if (command_type is None) != (command_payload is None):
        raise InvalidEvent("Accusation repair is unavailable.")
    if len(offers) + len(responses) != 1:
        raise InvalidEvent("Accusation repair is unavailable.")

    if offers:
        event = offers[0]
        record = event.record
        if (
            len(events) != 1
            or (event.world_id, event.branch_id, event.command_id, event.ruleset_version, event.actor_id)
            != (world_id, branch_id, command_id, ruleset_version, actor_id)
            or event.visibility != {"policy": "author_only"}
            or record.offer_id != event.event_id
            or record.offered_tick != state.current_tick
            or record.updated_tick != state.current_tick
            or record.revision != 1
            or record.status != "pending"
            or record.last_operation is not None
        ):
            raise InvalidEvent("Accusation repair is unavailable.")
        if command_type is not None and (
            command_type != "action.offer_accusation_repair"
            or command_payload != {
                "kind": "offer_accusation_repair",
                "correction_event_id": str(record.correction_event_id),
                "statement": record.statement,
            }
        ):
            raise InvalidEvent("Accusation repair is unavailable.")
        if causation_ids is not None and list(causation_ids) != [record.correction_event_id]:
            raise InvalidEvent("Accusation repair is unavailable.")

        correction = next((
            item for item in state.public_accusation_corrections
            if item.event_id == record.correction_event_id
        ), None)
        case_file = next((item for item in state.investigation_cases if item.id == record.case_file_id), None)
        actor = state.entities.get(record.accuser_id)
        suspect = state.entities.get(record.suspect_id)
        location = state.entities.get(actor.location_id) if actor is not None and actor.location_id is not None else None
        if (
            correction is None
            or correction.actor_id != record.accuser_id
            or correction.suspect_id != record.suspect_id
            or correction.case_file_id != record.case_file_id
            or correction.expected_revision != record.case_revision
            or correction.source_closure_event_id != record.source_closure_event_id
            or case_file is None
            or case_file.owner_id != record.accuser_id
            or case_file.status != "closed"
            or case_file.outcome != "disconfirmed"
            or not case_file.public_accusation
            or case_file.suspect_id != record.suspect_id
            or case_file.revision != record.case_revision
            or correction.corrected_tick > record.offered_tick
            or any(item.correction_event_id == record.correction_event_id for item in state.accusation_repair_offers)
            or actor is None or actor.kind not in {"character", "person"} or actor.status != "active"
            or suspect is None or suspect.kind not in {"character", "person"} or suspect.status != "active"
            or actor.location_id is None
            or actor.location_id != suspect.location_id
            or location is None or location.kind not in {"place", "location"} or location.status != "active"
            or not entity_visible_to_actor(state, record.accuser_id, record.suspect_id)
        ):
            raise InvalidEvent("Accusation repair is unavailable.")
        return

    event = responses[0]
    offer = event.resulting_offer
    hosted = event.requested_by is not None
    command_actor_id = event.requested_by if hosted else event.actor_id
    if (
        len(events) not in {1, 2}
        or (event.world_id, event.branch_id, event.command_id, event.ruleset_version, command_actor_id)
        != (world_id, branch_id, command_id, ruleset_version, actor_id)
        or event.actor_id != offer.suspect_id
        or event.visibility != {"policy": "author_only"}
        or offer.updated_tick != state.current_tick
    ):
        raise InvalidEvent("Accusation repair is unavailable.")
    relation_events = [item for item in events if isinstance(item, RelationDimensionChangedEvent)]
    if len(events) != 1 + len(relation_events) or len(relation_events) > 1:
        raise InvalidEvent("Accusation repair is unavailable.")
    expected_command = (
        "action.request_accusation_repair_response"
        if hosted else "action.resolve_accusation_repair"
    )
    expected_payload: dict[str, object] = {
        "kind": "request_accusation_repair_response" if hosted else "resolve_accusation_repair",
        "offer_id": str(offer.offer_id),
        "expected_revision": event.expected_revision,
    }
    if not hosted:
        expected_payload["operation"] = event.operation
    if command_type is not None and (
        command_type != expected_command or command_payload != expected_payload
    ):
        raise InvalidEvent("Accusation repair is unavailable.")

    current_offer = next((item for item in state.accusation_repair_offers if item.offer_id == offer.offer_id), None)
    if (
        current_offer is None
        or current_offer.status != "pending"
        or current_offer.revision != event.expected_revision
        or event.expected_revision < 1
        or offer.offer_id != current_offer.offer_id
        or offer.correction_event_id != current_offer.correction_event_id
        or offer.source_closure_event_id != current_offer.source_closure_event_id
        or offer.case_file_id != current_offer.case_file_id
        or offer.case_revision != current_offer.case_revision
        or offer.accuser_id != current_offer.accuser_id
        or offer.suspect_id != current_offer.suspect_id
        or offer.statement != current_offer.statement
        or offer.offered_tick != current_offer.offered_tick
        or offer.revision != current_offer.revision + 1
        or offer.status != {"accept": "accepted", "decline": "declined", "defer": "pending"}[event.operation]
        or offer.last_operation != event.operation
        or event.requested_by is not None and event.requested_by != current_offer.accuser_id
        or actor_id not in {current_offer.accuser_id, current_offer.suspect_id}
    ):
        raise InvalidEvent("Accusation repair is unavailable.")
    accuser = state.entities.get(current_offer.accuser_id)
    suspect = state.entities.get(current_offer.suspect_id)
    if (
        accuser is None or accuser.kind not in {"character", "person"} or accuser.status != "active"
        or suspect is None or suspect.kind not in {"character", "person"} or suspect.status != "active"
    ):
        raise InvalidEvent("Accusation repair is unavailable.")

    recovery = event.trust_recovery
    if event.operation != "accept" and (recovery is not None or relation_events):
        raise InvalidEvent("Only an accepted repair offer can change directed trust.")
    if recovery is not None:
        proof = recovery.proof
        if (
            proof.case_file_id != current_offer.case_file_id
            or proof.source_closure_event_id != current_offer.source_closure_event_id
            or proof.accuser_id != current_offer.accuser_id
            or proof.suspect_id != current_offer.suspect_id
        ):
            raise InvalidEvent("Accusation repair is unavailable.")

    applied_delta = recovery.applied_delta if recovery is not None else 0
    if not applied_delta:
        if relation_events:
            raise InvalidEvent("Accusation repair is unavailable.")
    else:
        if recovery is None or len(relation_events) != 1:
            raise InvalidEvent("Accusation repair is unavailable.")
        proof = recovery.proof
        directed = [
            item for item in state.relations
            if item.from_id == current_offer.suspect_id and item.to_id == current_offer.accuser_id
        ]
        if len(directed) != 1:
            raise InvalidEvent("Accusation repair is unavailable.")
        relation = directed[0]
        expected_previous = relation.dimensions.get("trust")
        current_value = expected_previous if expected_previous is not None else Decimal("0")
        maximum_from_loss = int((proof.baseline_trust - proof.post_effect_trust) / 3)
        maximum_to_baseline = max(0, int(proof.baseline_trust - current_value))
        expected_delta = min(maximum_from_loss, maximum_to_baseline)
        relation_event = relation_events[0]
        participant_ids = sorted((str(current_offer.accuser_id), str(current_offer.suspect_id)))
        expected_relation_event_id = uuid5(
            command_id,
            f"{ruleset_version}:accusation_repair.trust_recovery:{current_offer.offer_id}",
        )
        if (
            applied_delta != expected_delta
            or relation_event.event_id != expected_relation_event_id
            or relation_event.relation_id != relation.id
            or relation_event.dimension != "trust"
            or relation_event.expected_previous != expected_previous
            or relation_event.value != current_value + Decimal(applied_delta)
            or relation_event.evidence_refs != [event.event_id]
            or relation_event.visibility != {"policy": "subject_ids", "subject_ids": participant_ids}
        ):
            raise InvalidEvent("Accusation repair trust recovery is not the exact bounded directed effect.")

    expected_causation = [current_offer.offer_id]
    if applied_delta > 0 and recovery is not None:
        expected_causation.append(recovery.proof.source_relation_effect_event_id)
    if causation_ids is not None and list(causation_ids) != expected_causation:
        raise InvalidEvent("Accusation repair causation does not match its exact offer and trust source.")


def validate_historical_evidence_destruction_batch(
    state: WorldState,
    actor_id: UUID,
    events: Sequence[DomainEvent],
    *,
    world_id: UUID,
    branch_id: UUID,
    command_id: UUID,
    ruleset_version: str,
    command_type: str | None,
    command_payload: Mapping[str, object] | None,
) -> None:
    """Bind one physical-source destruction event to its current local artifact."""
    destruction_events = [
        event for event in events if isinstance(event, HistoricalEvidenceDestroyedEvent)
    ]
    if not destruction_events:
        if events and command_type in HISTORICAL_EVIDENCE_DESTRUCTION_COMMAND_TYPES:
            raise InvalidEvent("A registered evidence-destruction command cannot commit without its destruction event.")
        return
    if (command_type is None) != (command_payload is None):
        raise InvalidEvent("Evidence-destruction command metadata must be provided together or omitted together.")
    if len(events) != 1 or len(destruction_events) != 1:
        raise InvalidEvent("Evidence destruction must be a single-event atomic command.")
    event = destruction_events[0]
    if (
        event.world_id != world_id
        or event.branch_id != branch_id
        or event.command_id != command_id
        or event.ruleset_version != ruleset_version
        or event.actor_id != actor_id
        or event.visibility != {"policy": "author_only"}
    ):
        raise InvalidEvent("Evidence-destruction event does not match its committed command or private audience.")
    if command_type is not None and (
        command_type != "action.destroy_historical_evidence"
        or command_payload != {"kind": "destroy_historical_evidence", "target_id": str(event.target_id)}
    ):
        raise InvalidEvent("Evidence destruction must match its exact registered action proposal.")
    if event.destroyed_tick != state.current_tick:
        raise InvalidEvent("Evidence destruction must be recorded at the pre-batch world tick.")
    try:
        _item, _artifact, location_id = validate_destroyable_historical_item(
            state,
            actor_id=actor_id,
            target_id=event.target_id,
            expected_evidence_id=event.historical_evidence_id,
            expected_evidence_hash=event.historical_evidence_hash,
        )
    except ValueError as exc:
        raise InvalidEvent("Evidence-destruction source is no longer local, visible, active or unchanged.") from exc
    if event.location_id != location_id:
        raise InvalidEvent("Evidence-destruction event has a stale actor location.")
