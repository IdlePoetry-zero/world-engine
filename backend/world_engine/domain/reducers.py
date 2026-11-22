"""Deterministic reducers for the small, registered core event set."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, localcontext
from typing import Protocol, cast
from uuid import UUID, uuid5

from pydantic import TypeAdapter
from pydantic_core import to_jsonable_python

from .errors import InsufficientResources, InvalidEvent
from .npc_plan_events import NpcPlanCreatedEvent, NpcPlanRevisedEvent
from .npc_plans import apply_npc_plan_created, apply_npc_plan_revised, occupied_npc_ids
from .npc_plan_review_events import (
    NpcPlanReviewResolvedEvent, NpcPlanReviewScheduledEvent, apply_npc_plan_review_resolved, apply_npc_plan_review_scheduled,
)
from .environmental_source_events import (
    EnvironmentalSourceAccountOpenedEvent, apply_environmental_source_account_opened,
)
from .events import parse_event
from .family_events import KinshipLinkEndedEvent, KinshipLinkRecordedEvent
from .occupation_events import OccupationAppointedEvent, OccupationResignedEvent
from .occupation_death_vacancies import (
    OccupationDeathVacanciesResolvedEvent,
    OccupationDeathVacancyError,
    apply_occupation_death_vacancies,
)
from .occupation_legacy_vacancies import (
    OccupationLegacyVacanciesResolvedEvent,
    OccupationLegacyVacancyError,
    apply_legacy_occupation_vacancies,
)
from .occupation_settlement import apply_occupation_change
from .npc_attendance import npc_attendance_is_valid, plan_npc_attendance_start
from .npc_attendance_events import (
    NpcAttendanceCancelledEvent,
    NpcAttendanceSettledEvent,
    NpcAttendanceStartedEvent,
)
from .npc_meeting_attendance import (
    NpcMeetingAttendanceDecisionRecordedEvent,
    validate_npc_meeting_attendance_decision_transition,
)
from .institutional_credit import InstitutionalCreditError
from .institutional_credit_events import InstitutionalCreditChangedEvent
from .institutional_credit_state import apply_institutional_credit_event
from .institutional_credit_extension_events import InstitutionalCreditExtensionChangedEvent
from .institutional_credit_extension_state import apply_credit_extension_event
from .institutional_treasury_reserve import apply_reserve_mobilization
from .institutional_treasury_reserve_events import InstitutionalTreasuryMobilizedEvent
from .npc_work_shift import plan_npc_work_shift
from .relationship_invitation_events import RelationInvitationChangedEvent, invitation_event_id
from .private_gifts import PrivateGiftChangedEvent, apply_private_gift
from .relationship_reflection_events import MeetingReflectionRecordedEvent
from .relationship_reflections import plan_meeting_reflection
from .relationship_partnership_events import RelationshipPartnershipChangedEvent
from .relationship_partnerships import plan_relationship_partnership
from .relationship_disclosure_events import RelationshipDisclosureRecordedEvent, plan_relationship_disclosure
from .shipment_relief import plan_shipment_relief
from .shipment_relief_events import ShipmentReliefDistributedEvent
from .travel_journeys import (
    TRAVEL_RESOLUTION_HANDLER_ID, TRAVEL_RESOLUTION_HANDLER_VERSION,
    delay_travel_journey, plan_travel_departure, plan_travel_resolution, travel_resolution_schedule_id,
)
from .travel_journey_events import TravelJourneyChangedEvent
from .waypoint_opportunity_events import WaypointOpportunityDecisionRecordedEvent
from .world_books import WorldBookImportedEvent
from .relationship_invitation_state import apply_relationship_invitation
from .player_continuation import validate_player_character_continued_batch
from .leadership_ballot_events import LeadershipBallotChangedEvent
from .leadership_ballot_state import apply_leadership_ballot
from .autonomous_vacancy_events import (
    LeadershipVacancyReviewRecord,
    LeadershipVacancyReviewRecordedEvent,
    leadership_vacancy_review_ballot_id,
)
from .autonomous_vacancy import vacancy_opportunity
from .leadership_ballots import open_vacant_leadership_ballot
from .safe_conduct_events import SafeConductChangedEvent
from .safe_conduct_state import apply_safe_conduct
from .work_shift import WorkShiftError, registered_work_shift_terms, revalidate_paid_work_shift
from .work_shift_events import WorkShiftPaidEvent
from .market_pricing import PRICE_REVIEW_HANDLER_ID
from .market_pricing_events import MarketPriceReviewResolvedEvent
from .market_pricing_reducers import apply_market_price_review
from .promise_events import (
    PromiseDeadlineBreachedEvent, PromiseItemReturnedEvent, PromiseReleasedEvent, RelationshipPromiseRecordedEvent,
)
from .promise_reducers import apply_promise_event, promise_ledger_postings
from .promise_trust_events import PromiseDeadlineBreachedWithTrustEvent, PromiseItemReturnedWithTrustEvent
from .promise_late_return_event import PromiseItemReturnedLateEvent
from .promise_trust_settlement import apply_trusted_promise_settlement, configured_promise_trust_policy
from .gameplay_events import (
    AuthorComponentRevisedEvent,
    BackgroundDeprivationResolvedEvent,
    BackgroundConsumptionLot,
    BackgroundNeedsAdvancedEvent,
    BackgroundRoutineAdvancedEvent,
    BackgroundRoutineNeedExpectation,
    ComponentReplacedEvent,
    CombatExchangeResolvedEvent,
    CombatRetreatResolvedEvent,
    DamageAppliedEvent,
    CohortMemberPromotedEvent,
    PopulationYearAdvancedEvent,
    OrganizationMembershipReviewedEvent,
    GovernanceSuccessionEvent,
    GovernanceLeadershipHandedOverEvent,
    OrganizationMemberResignedEvent,
    EntityDiscoveredEvent,
    ImprovisedCheckResolvedEvent,
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
    KnowledgeAcquiredEvent,
    MarketTradeExecutedEvent,
    RelationDimensionChangedEvent,
    RelationEstablishedEvent,
    ResourceConsumedEvent,
    ResourceProducedEvent,
    ResourceAccountOpenedEvent,
    ProductionQuotaRenewedEvent,
    RenewableResourceHarvestedEvent,
    ScheduledEventCompletedEvent,
    ConsumedResourceFlow,
    MovementCompletedEvent,
    validate_author_replacement,
    validate_replacement,
)
from .cultivation_breakthrough_events import CultivationBreakthroughResolvedEvent
from .components import (
    COMPONENT_REGISTRY,
    CapabilitiesComponent,
    CombatEncounterComponent,
    HealthComponent,
    LifeComponent,
    InventoryComponent,
    InventoryLot,
    InjuryRecord,
    MarketComponent,
    PlaceComponent,
    PopulationComponent,
    ScheduledEventRecord,
    DailyRoutineComponent,
    PopulationAgeCohort,
    HistoricalEvidenceArtifact,
    NeedsComponent,
    ProgressionComponent,
    SkillsComponent,
    ProductionComponent,
    LifeComponent,
    GovernanceComponent,
    PopulationOrigin,
    ProvenanceReference,
    is_leadership_office,
    leadership_office_ids_held_by,
)
from .needs import advance_passive_needs, next_deprivation_damage_tick, resource_matches_need
from .deprivation import resolve_due_deprivation
from .adjudication import (
    historical_evidence_digest,
    improvised_examine_chance,
    source_text_digest,
)
from .inventory import FifoLotAllocation, allocate_fifo_lots, reduce_fifo_inventory
from .background_routine import advance_background_routine
from .combat_truce import CombatTruceChangedEvent, plan_combat_truce
from .combat import (
    COMBAT_ATTACK_POLICY,
    COMBAT_EXCHANGE_DURATION,
    active_combat_opponent,
    combat_attack_chance,
    combat_damage,
    combat_death_protection_enabled,
)
from .treatment import (
    INJURY_TREATMENT_DURATION,
    INJURY_TREATMENT_HEALING,
    INJURY_TREATMENT_MIN_CARE,
    INJURY_TREATMENT_POLICY,
    care_skill,
    is_medicine_resource,
    settled_treatment_place,
)
from .population_seats import plan_population_seat_promotions
from .organization_recruitment import (
    build_membership_recruitment_review,
    plan_membership_recruitment,
)
from .investigation import (
    entity_visible_to_actor,
    validate_assessment_record,
    validate_disclosure_record,
)
from .evidence_destruction import validate_destroyable_historical_item
from .shipment import (
    ShipmentCargoManifest,
    ShipmentComponent,
    ShipmentSpoiledLot,
    ShipmentDeliveredLot,
    active_shipments_on_route,
    plan_dispatch_lots,
    shipment_destination_available,
)
from .models import (
    DomainEvent,
    EntityCreatedEvent,
    EntityFieldChangedEvent,
    ResourceAccount,
    ResourceTransferredEvent,
    TimeAdvancedEvent,
    WorldEntity,
    WorldState,
)


@dataclass(frozen=True, slots=True)
class LedgerPosting:
    """One balanced projection row emitted by a resource transfer."""

    account_id: UUID
    resource_id: str
    delta: Decimal


def apply_event(state: WorldState, raw_event: DomainEvent | dict[str, object]) -> WorldState:
    event = parse_event(raw_event)
    if isinstance(event, WorldBookImportedEvent):
        if len(state.world_book_entries) + len(event.entries) > 512:
            raise InvalidEvent("A world cannot contain more than 512 world book entries.")
        existing_ids = {entry.entry_id for entry in state.world_book_entries}
        imported_ids = [entry.entry_id for entry in event.entries]
        if len(imported_ids) != len(set(imported_ids)) or existing_ids.intersection(imported_ids):
            raise InvalidEvent("World book import entries must have distinct, previously unused IDs.")
        return _validated_state(state, {"world_book_entries": [*state.world_book_entries, *event.entries]})
    if isinstance(event, (OccupationAppointedEvent, OccupationResignedEvent)):
        return _validated_state(state, {"work_assignments": apply_occupation_change(state, event)})
    if isinstance(event, OccupationDeathVacanciesResolvedEvent):
        if event.effective_tick != state.current_tick:
            raise InvalidEvent("Occupation death vacancies must close at the current world tick.")
        for row in event.closures:
            worker = state.entities.get(row.actor_id)
            if worker is None or worker.kind not in {"character", "person"} or worker.status != "dead":
                raise InvalidEvent("Employment vacancies require a newly dead assignment holder.")
        terminal_events = {row.actor_id: row.terminal_event_id for row in event.closures}
        try:
            assignments = apply_occupation_death_vacancies(
                state.work_assignments,
                event,
                terminal_transition_event_by_actor=terminal_events,
                current_tick=state.current_tick,
            )
        except OccupationDeathVacancyError as exc:
            raise InvalidEvent(str(exc)) from exc
        return _validated_state(state, {"work_assignments": assignments})
    if isinstance(event, OccupationLegacyVacanciesResolvedEvent):
        try:
            assignments = apply_legacy_occupation_vacancies(state, event)
        except OccupationLegacyVacancyError as exc:
            raise InvalidEvent(str(exc)) from exc
        return _validated_state(state, {"work_assignments": assignments})
    if isinstance(event, RelationInvitationChangedEvent):
        return _validated_state(state, {"relationship_invitations": apply_relationship_invitation(state, event)})
    if isinstance(event, InstitutionalCreditChangedEvent):
        try:
            patches = apply_institutional_credit_event(state, event)
        except InstitutionalCreditError as exc:
            raise InvalidEvent(str(exc)) from exc
        return _validated_state(state, patches)
    if isinstance(event, InstitutionalTreasuryMobilizedEvent):
        try:
            resources = apply_reserve_mobilization(state, event)
        except InstitutionalCreditError as exc:
            raise InvalidEvent(str(exc)) from exc
        return _validated_state(state, {"resources": resources})
    if isinstance(event, InstitutionalCreditExtensionChangedEvent):
        try:
            changes = apply_credit_extension_event(state, event)
        except InstitutionalCreditError as exc:
            raise InvalidEvent(str(exc)) from exc
        return _validated_state(state, changes)
    if isinstance(event, NpcMeetingAttendanceDecisionRecordedEvent):
        record = event.record
        validate_npc_meeting_attendance_decision_transition(state, record)
        if record.choice == "attend" and record.linked_attendance_event_id != invitation_event_id(
            event.command_id,
            event.ruleset_version,
            "attend",
            record.invitation_id,
        ):
            raise InvalidEvent("An attended NPC decision must link its command's canonical attendance event.")
        return _validated_state(state, {
            "npc_meeting_attendance_decisions": [*state.npc_meeting_attendance_decisions, record],
        })
    if isinstance(event, PrivateGiftChangedEvent):
        return _validated_state(state, apply_private_gift(state, event))
    if isinstance(event, MeetingReflectionRecordedEvent):
        reflection = plan_meeting_reflection(
            state, invitation_id=event.reflection.invitation_id,
            actor_id=event.actor_id, outlook=event.reflection.outlook,
        )
        if event.requested_by is not None:
            invitation = next(row for row in state.relationship_invitations if row.invitation_id == reflection.invitation_id)
            if {event.actor_id, event.requested_by} != {invitation.proposer_id, invitation.invitee_id}:
                raise InvalidEvent("An NPC meeting reflection can only be requested by the other participant.")
        if reflection != event.reflection:
            raise InvalidEvent("Stored meeting reflection does not match its authoritative transition.")
        return _validated_state(state, {"relationship_reflections": [*state.relationship_reflections, reflection]})
    if isinstance(event, RelationshipPartnershipChangedEvent):
        stored = event.resulting_partnership
        expected = plan_relationship_partnership(
            state, operation=event.operation, partnership_id=stored.partnership_id,
            actor_id=event.actor_id, partner_id=stored.partner_id if event.operation == "propose" else None,
            expected_revision=event.expected_revision,
            proposal_terms=stored.proposal_terms if event.operation in {"propose", "revise", "counter"} else (),
        )
        if expected != stored:
            raise InvalidEvent("Stored relationship partnership does not match its authoritative transition.")
        records = state.relationship_partnerships
        updated = [*records, stored] if event.operation == "propose" else [
            stored if row.partnership_id == stored.partnership_id else row for row in records
        ]
        return _validated_state(state, {"relationship_partnerships": updated})
    if isinstance(event, RelationshipDisclosureRecordedEvent):
        receipt = event.record.relationship_disclosure
        if receipt is None:
            raise InvalidEvent("A relationship disclosure event requires its typed receipt.")
        expected = plan_relationship_disclosure(
            state,
            world_id=event.world_id,
            branch_id=event.branch_id,
            command_id=event.command_id,
            ruleset_version=event.ruleset_version,
            actor_id=event.actor_id,
            partnership_id=receipt.source_partnership_id,
            expected_revision=receipt.source_revision,
            recipient_id=receipt.recipient_id,
        )
        if expected != event:
            raise InvalidEvent("Stored relationship disclosure does not match its authoritative transition.")
        if any(record.id == event.record.id for record in state.knowledge):
            raise InvalidEvent(f"Knowledge record {event.record.id} already exists.")
        return _validated_state(state, {"knowledge": [*state.knowledge, event.record]})
    if isinstance(event, ShipmentReliefDistributedEvent):
        return _apply_shipment_relief(state, event)
    if isinstance(event, TravelJourneyChangedEvent):
        journey = event.journey
        actor = state.entities.get(event.actor_id)
        if actor is None:
            raise InvalidEvent("A travel journey requires a known character.")
        if event.operation == "delay":
            previous = next((row for row in state.travel_journeys if row.journey_id == journey.journey_id), None)
            if (previous is None or previous.actor_id != actor.id or actor.status != "active"
                or actor.location_id is not None or event.stationary_from_tick != state.current_tick
                or event.stationary_until_tick is None):
                raise InvalidEvent("Travel delay does not match the active traveler and current clock.")
            planned = delay_travel_journey(previous, state.current_tick, event.stationary_until_tick)
            if planned != journey:
                raise InvalidEvent("Travel delay must preserve the journey and its remaining route.")
            schedule_id = travel_resolution_schedule_id(journey.journey_id)
            due = journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick
            return _validated_state(state, {
                "travel_journeys": [journey if row.journey_id == journey.journey_id else row for row in state.travel_journeys],
                "scheduled_events": [
                    row.model_copy(update={"due_tick": due, "revision": row.revision + 1})
                    if row.id == schedule_id else row for row in state.scheduled_events
                ],
            })
        if event.operation == "depart":
            planned = plan_travel_departure(
                state, journey_id=journey.journey_id, actor_id=event.actor_id,
                route_id=journey.route_id, hazard=journey.hazard_tick is not None,
                hazard_timing=event.hazard_timing,
            )
            records = [*state.travel_journeys, planned]
            location_id = None
            schedule = ScheduledEventRecord(
                id=travel_resolution_schedule_id(journey.journey_id),
                due_tick=journey.hazard_tick if journey.hazard_tick is not None else journey.arrival_tick,
                phase="resolution", handler_id=TRAVEL_RESOLUTION_HANDLER_ID,
                handler_version=TRAVEL_RESOLUTION_HANDLER_VERSION, priority=-1,
                payload={"journey_id": str(journey.journey_id)},
            )
            if any(item.id == schedule.id for item in state.scheduled_events):
                raise InvalidEvent("Travel resolution schedule already exists.")
            schedules = [*state.scheduled_events, schedule]
        else:
            planned = plan_travel_resolution(
                state, journey_id=journey.journey_id, actor_id=event.actor_id,
                operation=event.operation,
            )
            records = [
                planned if item.journey_id == journey.journey_id else item
                for item in state.travel_journeys
            ]
            location_id = (
                journey.to_place_id if event.operation == "arrive" else
                journey.from_place_id if actor.status not in {"dead", "retired"} else None
            )
            schedule_id = travel_resolution_schedule_id(journey.journey_id)
            schedule = next((item for item in state.scheduled_events if item.id == schedule_id), None)
            if (
                schedule is None or schedule.status != "queued"
                or schedule.handler_id != TRAVEL_RESOLUTION_HANDLER_ID
                or schedule.handler_version != TRAVEL_RESOLUTION_HANDLER_VERSION
                or schedule.phase != "resolution" or schedule.due_tick != state.current_tick
                or schedule.payload != {"journey_id": str(journey.journey_id)}
            ):
                raise InvalidEvent("Travel resolution must settle its exact queued occurrence.")
            schedules = [
                item.model_copy(update={
                    "status": "completed", "completed_runs": 1, "revision": item.revision + 1,
                }) if item.id == schedule_id else item
                for item in state.scheduled_events
            ]
        if planned != journey:
            raise InvalidEvent("Stored journey does not match its authoritative transition.")
        entities = {**state.entities, actor.id: actor.model_copy(update={"location_id": location_id})}
        return _validated_state(state, {"entities": entities, "travel_journeys": records, "scheduled_events": schedules})
    if isinstance(event, WaypointOpportunityDecisionRecordedEvent):
        record = event.record
        if record.at_tick != state.current_tick:
            raise InvalidEvent("A waypoint opportunity decision must be recorded at the exact current world tick.")
        if any(row.journey_id == record.journey_id for row in state.waypoint_opportunity_decisions):
            raise InvalidEvent("A journey can have only one waypoint opportunity decision.")
        journey = next((row for row in state.travel_journeys if row.journey_id == record.journey_id), None)
        if (
            journey is None
            or journey.status != "arrived"
            or journey.resolved_tick != record.at_tick
            or journey.actor_id != record.actor_id
            or record.itinerary_started_tick > journey.departed_tick
        ):
            raise InvalidEvent("A waypoint opportunity decision requires its actor's actual arrival at this tick.")
        actor = state.entities.get(record.actor_id)
        if (
            actor is None
            or actor.kind not in {"character", "person"}
            or actor.status != "active"
            or actor.location_id != journey.to_place_id
        ):
            raise InvalidEvent("A waypoint opportunity decision requires its active actor at the arrived destination.")
        destination = state.entities.get(journey.to_place_id)
        if destination is None or destination.kind not in {"place", "location"}:
            raise InvalidEvent("A waypoint opportunity decision requires the journey's registered destination.")
        try:
            place = PlaceComponent.model_validate(destination.components.get("place"))
        except (TypeError, ValueError) as exc:
            raise InvalidEvent("A waypoint opportunity decision requires a valid destination place component.") from exc
        opportunity = next(
            (item for item in place.waypoint_opportunities if item.opportunity_id == record.opportunity_id),
            None,
        )
        if opportunity is None or not opportunity.is_available(
            state.current_tick,
            state.current_tick - record.itinerary_started_tick,
        ):
            raise InvalidEvent("The selected waypoint opportunity is not registered and available at arrival.")
        return _validated_state(state, {
            "waypoint_opportunity_decisions": [*state.waypoint_opportunity_decisions, record],
        })
    if isinstance(event, LeadershipBallotChangedEvent):
        return _validated_state(state, apply_leadership_ballot(state, event))
    if isinstance(event, LeadershipVacancyReviewRecordedEvent):
        return _apply_leadership_vacancy_review(state, event)
    if isinstance(event, SafeConductChangedEvent):
        return _validated_state(state, {"safe_conduct_treaties": apply_safe_conduct(state, event)})
    if isinstance(event, NpcPlanCreatedEvent):
        return apply_npc_plan_created(state, event)
    if isinstance(event, NpcPlanRevisedEvent):
        return apply_npc_plan_revised(state, event)
    if isinstance(event, NpcPlanReviewScheduledEvent):
        return _validated_state(state, {"scheduled_events": apply_npc_plan_review_scheduled(state, event)})
    if isinstance(event, NpcPlanReviewResolvedEvent):
        return apply_npc_plan_review_resolved(state, event)
    if isinstance(event, NpcAttendanceStartedEvent):
        record = event.attendance
        if any(row.assignment_id == record.assignment_id for row in state.npc_attendance):
            raise InvalidEvent("The NPC assignment already has an open attendance interval.")
        if record != plan_npc_attendance_start(state, record.assignment_id):
            raise InvalidEvent("NPC attendance must start at its exact registered work time and workplace.")
        records = sorted([*state.npc_attendance, record], key=lambda row: str(row.assignment_id))
        return _validated_state(state, {"npc_attendance": records})
    if isinstance(event, NpcAttendanceCancelledEvent):
        record = event.attendance
        if event.effective_tick != state.current_tick or record not in state.npc_attendance:
            raise InvalidEvent("NPC attendance cancellation must match the open interval and current tick.")
        if npc_attendance_is_valid(state, record):
            raise InvalidEvent("NPC attendance can be interrupted only when its registered conditions no longer hold.")
        return _validated_state(state, {"npc_attendance": [row for row in state.npc_attendance if row != record]})
    if isinstance(event, NpcAttendanceSettledEvent):
        record = event.attendance
        if event.effective_tick != state.current_tick or record not in state.npc_attendance:
            raise InvalidEvent("NPC attendance settlement must match the recorded interval and current tick.")
        if state.current_tick < record.ends_at_tick:
            raise InvalidEvent("NPC attendance can be settled only at or after its scheduled end.")

        expected_payment = None
        if state.current_tick == record.ends_at_tick and npc_attendance_is_valid(state, record):
            expected_payment = plan_npc_work_shift(state, record.assignment_id)
        if event.payment != expected_payment:
            raise InvalidEvent("NPC attendance payment must exactly match its eligible completed work shift.")

        updates: dict[str, object] = {
            "npc_attendance": [row for row in state.npc_attendance if row != record],
        }
        if event.payment is not None:
            resources = dict(state.resources)
            for entry in event.payment.ledger_entries:
                resources[entry.account_id] = resources[entry.account_id].model_copy(
                    update={"balance": entry.balance_after},
                )
            updates["resources"] = resources
        return _validated_state(state, updates)
    if isinstance(event, WorkShiftPaidEvent):
        terms = registered_work_shift_terms(state).get(event.plan.terms.posting_id)
        if terms is None:
            raise InvalidEvent("Work-shift payment requires registered posting terms.")
        try:
            payment = revalidate_paid_work_shift(state, event.plan, terms=terms, actor_id=event.actor_id)
        except WorkShiftError as exc:
            raise InvalidEvent(str(exc)) from exc
        resources = dict(state.resources)
        for entry in payment.ledger_entries:
            resources[entry.account_id] = resources[entry.account_id].model_copy(update={"balance": entry.balance_after})
        return _validated_state(state, {"resources": resources})
    if isinstance(event, (KinshipLinkRecordedEvent, KinshipLinkEndedEvent)):
        if event.effective_tick != state.current_tick:
            raise InvalidEvent("Kinship changes must use the current world tick.")
        links = list(state.kinship_links)
        if isinstance(event, KinshipLinkRecordedEvent):
            links.append(event.link)
        else:
            index = next((i for i, link in enumerate(links) if link.link_id == event.link_id), None)
            if index is None:
                raise InvalidEvent("Kinship link does not exist.")
            link = links[index]
            if (link.ended_tick is not None or link.created_tick != event.expected_created_tick
                    or (event.expected_relationship is not None and event.expected_relationship != link.relationship)):
                raise InvalidEvent("Kinship link does not match the expected active record.")
            links[index] = link.model_copy(update={"ended_tick": event.effective_tick})
        return _validated_state(state, {"kinship_links": links})
    if isinstance(event, MarketPriceReviewResolvedEvent):
        return apply_market_price_review(state, event)
    if isinstance(event, (PromiseItemReturnedWithTrustEvent, PromiseDeadlineBreachedWithTrustEvent, PromiseItemReturnedLateEvent)):
        return apply_trusted_promise_settlement(state, event)
    if (isinstance(event, RelationshipPromiseRecordedEvent)
            and event.promise.trust_policy_id != configured_promise_trust_policy(state)):
        raise InvalidEvent("New promises must pin the installed world trust policy.")
    if isinstance(event, (PromiseItemReturnedEvent, PromiseDeadlineBreachedEvent)) and any(
        promise.id == event.promise_id and promise.trust_policy_id is not None
        for promise in state.relationship_promises
    ):
        raise InvalidEvent("A trust-enabled promise requires its atomic v2 settlement event.")
    if isinstance(event, (RelationshipPromiseRecordedEvent, PromiseItemReturnedEvent,
                          PromiseReleasedEvent, PromiseDeadlineBreachedEvent)):
        return cast(WorldState, apply_promise_event(state, event))

    if isinstance(event, EntityCreatedEvent):
        if event.entity.id in state.entities:
            raise InvalidEvent(f"Entity {event.entity.id} already exists.")
        raw_life = event.entity.components.get("life")
        if isinstance(raw_life, dict) and LifeComponent.model_validate(raw_life).population_origin is not None:
            raise InvalidEvent("Population origin can only be recorded by a registered cohort promotion event.")
        raw_health = event.entity.components.get("health")
        if isinstance(raw_health, dict) and raw_health.get("incapacitating_injury_id") is not None:
            raise InvalidEvent("An incapacitating injury must be recorded by a combat damage event.")
        if event.entity.location_id is not None:
            _require_location(state, event.entity.location_id)
        new_entities = {**state.entities, event.entity.id: event.entity}
        return _validated_state(state, {"entities": new_entities})

    if isinstance(event, EntityFieldChangedEvent):
        entity = state.entities.get(event.entity_id)
        if entity is None:
            raise InvalidEvent(f"Entity {event.entity_id} does not exist.")
        if entity.status in {"dead", "retired"}:
            raise InvalidEvent(f"Entity {event.entity_id} is {entity.status}; its state is terminal.")
        value: object = event.value
        if event.field_name == "name":
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 240):
                raise InvalidEvent("Entity name must be non-empty text of at most 240 characters.")
        elif event.field_name == "status":
            if value is None or not isinstance(value, str) or not value.strip() or len(value) > 64:
                raise InvalidEvent("Entity status must be non-empty text of at most 64 characters.")
            if entity.status == "dead" and value != "dead":
                raise InvalidEvent("A dead entity cannot be revived by a core field change.")
        updated_components = dict(entity.components)
        if event.field_name == "status" and value in {"dead", "retired"} and updated_components.get("health") is not None:
            try:
                health = HealthComponent.model_validate(updated_components["health"])
            except Exception as exc:
                raise InvalidEvent("A terminal status transition has an invalid health component.") from exc
            if (value == "dead" and health.current > 0) or health.incapacitating_injury_id is not None:
                updated_components["health"] = health.model_copy(
                    update={
                        "current": 0 if value == "dead" else health.current,
                        "incapacitating_injury_id": None,
                        "revision": health.revision + 1,
                    }
                ).model_dump(mode="json")
        updated_entity = entity.model_copy(
            update={
                event.field_name: value,
                "components": updated_components,
            }
        )
        new_entities = {**state.entities, event.entity_id: updated_entity}
        if event.field_name == "status" and value in {"dead", "retired"}:
            new_entities = _apply_population_departure(
                state,
                entity,
                updated_entity,
                expected_population_revision=event.expected_population_revision,
            )
        return _validated_state(state, {"entities": new_entities})

    if isinstance(event, MovementCompletedEvent):
        actor = state.entities.get(event.actor_id)
        if actor is None or actor.kind not in {"character", "person"}:
            raise InvalidEvent("Movement actor must be an existing character.")
        if actor.status in {"dead", "retired"}:
            raise InvalidEvent("A terminal character cannot complete movement.")
        if active_combat_opponent(state, actor) is not None:
            raise InvalidEvent("An active combatant must resolve a registered retreat before movement.")
        if actor.location_id != event.from_place_id:
            raise InvalidEvent("Movement origin does not match the character's current location.")
        if event.arrived_tick != state.current_tick or event.arrived_tick <= event.started_tick:
            raise InvalidEvent("Movement arrival must match the current tick and follow departure.")
        origin = state.entities.get(event.from_place_id)
        destination = state.entities.get(event.to_place_id)
        if origin is None or destination is None or origin.kind not in {"place", "location"} or destination.kind not in {"place", "location"}:
            raise InvalidEvent("Movement endpoints must be registered places.")
        try:
            place = PlaceComponent.model_validate(origin.components.get("place", {}))
        except Exception as exc:
            raise InvalidEvent("Movement origin has no valid registered place component.") from exc
        route = next((item for item in place.routes if item.route_id == event.route_id), None)
        if route is None or not route.is_open:
            raise InvalidEvent("Movement route is not registered and open at its origin.")
        if route.from_place_id != event.from_place_id or route.to_place_id != event.to_place_id:
            raise InvalidEvent("Movement route endpoints do not match the event.")
        if event.arrived_tick - event.started_tick != route.travel_ticks:
            raise InvalidEvent("Movement duration must match the registered route duration in seconds.")
        if destination.status in {"dead", "retired"}:
            raise InvalidEvent("Movement destination is retired.")
        capacity = PlaceComponent.model_validate(destination.components.get("place", {})).capacity
        occupants = sum(1 for entity in state.entities.values() if entity.location_id == event.to_place_id)
        if capacity is not None and occupants >= capacity:
            raise InvalidEvent("Movement destination has reached its registered capacity.")
        entities = {
            **state.entities,
            event.actor_id: actor.model_copy(update={"location_id": event.to_place_id}),
        }
        return _validated_state(state, {"entities": entities})

    if isinstance(event, EntityDiscoveredEvent):
        if event.roll > event.chance_percent:
            raise InvalidEvent("Discovery roll does not meet the recorded success threshold.")
        discoverer = state.entities.get(event.discoverer_id)
        target = state.entities.get(event.entity_id)
        if discoverer is None or discoverer.kind not in {"character", "person"}:
            raise InvalidEvent("Discovery actor must be an existing character.")
        if target is None:
            raise InvalidEvent("Discovery target does not exist.")
        if discoverer.status in {"dead", "retired"} or discoverer.location_id != event.place_id:
            raise InvalidEvent("Discovery actor must be active and present at the stated place.")
        target_parent = target.components.get("parent_id")
        target_place = target.components.get("place")
        nested_parent = target_place.get("parent_id") if isinstance(target_place, dict) else None
        if target.location_id != event.place_id and str(target_parent or nested_parent) != str(event.place_id):
            raise InvalidEvent("Discovery target is not at the actor's current place.")
        if target.components.get("hidden") is not True:
            raise InvalidEvent("Discovery target is not hidden or has already been discovered.")
        known_places = list(discoverer.components.get("known_places", []))
        if str(event.place_id) not in {str(item) for item in known_places}:
            known_places.append(str(event.place_id))
        discoverer_components = {**discoverer.components, "known_places": known_places}
        entities = {
            **state.entities,
            discoverer.id: discoverer.model_copy(update={"components": discoverer_components}),
        }
        if event.reveal_scope == "world":
            entities[target.id] = target.model_copy(update={"components": {**target.components, "hidden": False}})
        return _validated_state(state, {"entities": entities})

    if isinstance(event, ImprovisedCheckResolvedEvent):
        try:
            event = ImprovisedCheckResolvedEvent.model_validate(event.model_dump(mode="python"))
        except Exception as exc:
            raise InvalidEvent(f"Improvised check audit failed registered-policy validation: {exc}") from exc
        actor = state.entities.get(event.actor_id)
        target = state.entities.get(event.target_id)
        if actor is None or actor.kind not in {"character", "person"} or actor.status in {"dead", "retired"}:
            raise InvalidEvent("Improvised check actor must be an active character.")
        if state.current_tick != event.resolved_tick:
            raise InvalidEvent("Improvised check must resolve at its recorded current world tick.")
        if actor.location_id != event.location_id:
            raise InvalidEvent("Improvised check actor must remain at its recorded local place.")
        place = state.entities.get(event.location_id)
        if place is None or place.kind not in {"place", "location"} or place.status in {"dead", "retired"}:
            raise InvalidEvent("Improvised check location must be an active registered place.")
        if target is None or target.kind not in {"place", "location", "item"} or target.status in {"dead", "retired"}:
            raise InvalidEvent("Improvised examination target must be an active local place or item.")
        target_parent = target.components.get("parent_id")
        target_place = target.components.get("place")
        nested_parent = target_place.get("parent_id") if isinstance(target_place, dict) else None
        if (
            target.id != event.location_id
            and target.location_id != event.location_id
            and str(target_parent or nested_parent) != str(event.location_id)
        ):
            raise InvalidEvent("Improvised examination target is not local to the actor's current place.")
        if target.components.get("hidden") is True and not any(
            actor.id in {record.owner_id, record.holder_id}
            and record.subject_id == target.id
            and record.kind == "observation"
            and record.received_tick <= state.current_tick
            and (record.valid_from_tick is None or record.valid_from_tick <= state.current_tick)
            for record in state.knowledge
        ):
            raise InvalidEvent("Improvised examination target is not visible to this actor.")
        description = target.components.get("public_description")
        if not isinstance(description, str) or not description.strip():
            raise InvalidEvent("Improvised examination requires an existing public description.")
        if source_text_digest(description) != event.source_description_hash:
            raise InvalidEvent("Improvised examination description hash no longer matches the authored source.")
        if event.historical_evidence_id is not None:
            try:
                artifact = HistoricalEvidenceArtifact.model_validate(target.components.get("historical_evidence"))
            except Exception as exc:
                raise InvalidEvent("Historical evidence audit does not identify a registered item artifact.") from exc
            subject = state.entities.get(artifact.subject_id)
            if (
                artifact.evidence_id != event.historical_evidence_id
                or historical_evidence_digest(artifact) != event.historical_evidence_hash
                or artifact.fact.effective_tick > event.started_tick
                or subject is None
                or not entity_visible_to_actor(state, actor.id, subject.id)
            ):
                raise InvalidEvent("Historical evidence audit is stale or its subject is unavailable.")
        try:
            skills = SkillsComponent.model_validate(actor.components.get("skills", {}))
        except Exception as exc:
            raise InvalidEvent("Improvised check actor has invalid registered skills.") from exc
        if event.skill_id not in {"skill.investigation", "skill.local_knowledge"}:
            raise InvalidEvent("Improvised check competency is outside the registered examination policy.")
        skill = skills.skills.get(event.skill_id)
        if skill is None:
            raise InvalidEvent("Improvised check competency is not registered for the actor.")
        if skills.revision != event.expected_skills_revision or skill.level != event.skill_level:
            raise InvalidEvent("Improvised check skill compare-and-swap data is stale.")
        if event.chance_percent != improvised_examine_chance(skill.level):
            raise InvalidEvent("Improvised check chance does not match the current skill and policy.")
        # This event is an audit record only. Success observations are appended
        # separately and must be bound to this event by storage batch validation.
        return state

    if isinstance(event, CombatExchangeResolvedEvent):
        try:
            event = CombatExchangeResolvedEvent.model_validate(event.model_dump(mode="python"))
        except Exception as exc:
            raise InvalidEvent(f"Combat exchange failed registered-policy validation: {exc}") from exc
        if event.event_id != uuid5(event.command_id, f"{event.ruleset_version}:combat.exchange"):
            raise InvalidEvent("Combat exchange event id must be bound to its command.")
        actor = state.entities.get(event.actor_id)
        target = state.entities.get(event.target_id)
        if (
            actor is None or actor.kind not in {"character", "person"} or actor.status != "active"
            or target is None or target.kind not in {"character", "person"} or target.status != "active"
        ):
            raise InvalidEvent("Combat participants must be active characters.")
        if (
            state.current_tick != event.resolved_tick
            or event.started_tick != state.current_tick - COMBAT_EXCHANGE_DURATION
        ):
            raise InvalidEvent("Combat exchange must resolve at its recorded ten-second world-time boundary.")
        if actor.location_id != event.location_id or target.location_id != event.location_id:
            raise InvalidEvent("Combat participants must remain together at the recorded encounter location.")
        location = state.entities.get(event.location_id)
        if location is None or location.kind not in {"place", "location"} or location.status in {"dead", "retired"}:
            raise InvalidEvent("Combat location must be an active registered place.")
        if target.components.get("hidden") is True and not any(
            record.subject_id == target.id
            and actor.id in {record.holder_id, record.owner_id}
            and record.kind == "observation"
            and record.received_tick <= state.current_tick
            and (record.valid_from_tick is None or record.valid_from_tick <= state.current_tick)
            for record in state.knowledge
        ):
            raise InvalidEvent("A hidden combat target must have been discovered by the actor.")
        try:
            capabilities = CapabilitiesComponent.model_validate(actor.components.get("capabilities", {}))
            target_health = HealthComponent.model_validate(target.components.get("health"))
        except Exception as exc:
            raise InvalidEvent("Combat requires valid actor capabilities and target health.") from exc
        if "world.combat" in capabilities.restrictions:
            raise InvalidEvent("This actor is restricted from combat.")
        expected_chance = combat_attack_chance(actor, target)
        if event.policy_version != COMBAT_ATTACK_POLICY or event.chance_percent != expected_chance:
            raise InvalidEvent("Combat chance does not match the current registered attack policy.")
        if event.outcome == "hit":
            expected_damage = combat_damage(actor)
            if (
                target_health.revision != event.expected_target_health_revision
                or event.damage != expected_damage
                or event.encounter_status != ("resolved" if event.damage >= target_health.current else "active")
            ):
                raise InvalidEvent("Combat hit damage or target health revision is stale or inconsistent.")
        elif event.encounter_status != "active":
            raise InvalidEvent("A missed exchange cannot resolve an encounter.")

        actor_raw = actor.components.get("combat_encounter")
        target_raw = target.components.get("combat_encounter")
        try:
            actor_encounter = CombatEncounterComponent.model_validate(actor_raw) if actor_raw is not None else None
            target_encounter = CombatEncounterComponent.model_validate(target_raw) if target_raw is not None else None
        except Exception as exc:
            raise InvalidEvent("Combat participant encounter state is invalid.") from exc
        if (actor_encounter.revision if actor_encounter else None) != event.expected_actor_encounter_revision:
            raise InvalidEvent("Combat actor encounter revision is stale.")
        if (target_encounter.revision if target_encounter else None) != event.expected_target_encounter_revision:
            raise InvalidEvent("Combat target encounter revision is stale.")
        actor_pair = active_combat_opponent(state, actor)
        target_pair = active_combat_opponent(state, target)
        if actor_pair is not None and actor_pair[1].id != target.id:
            raise InvalidEvent("An active combatant cannot begin a second encounter.")
        if target_pair is not None and target_pair[1].id != actor.id:
            raise InvalidEvent("An active target cannot begin a second encounter.")
        if actor_pair is not None:
            if (
                target_pair is None
                or actor_pair[0].encounter_id != event.encounter_id
                or target_pair[0].encounter_id != event.encounter_id
                or actor_pair[0].exchange_count + 1 != event.exchange_number
                or actor_pair[0].started_tick >= event.started_tick
            ):
                raise InvalidEvent("Combat continuation does not match the active encounter history.")
            encounter_started_tick = actor_pair[0].started_tick
        else:
            expected_encounter_id = uuid5(event.command_id, f"{event.ruleset_version}:combat.encounter")
            if event.encounter_id != expected_encounter_id or event.exchange_number != 1:
                raise InvalidEvent("A new encounter must start with the registered command-bound identity.")
            encounter_started_tick = event.started_tick

        def next_component(previous: CombatEncounterComponent | None, opponent_id: UUID) -> dict[str, object]:
            return CombatEncounterComponent(
                revision=(previous.revision + 1 if previous else 1),
                encounter_id=event.encounter_id,
                opponent_id=opponent_id,
                location_id=event.location_id,
                started_tick=encounter_started_tick,
                status=event.encounter_status,
                exchange_count=event.exchange_number,
                last_event_id=event.event_id,
                last_damage_event_id=event.damage_event_id,
                last_outcome=event.outcome,
                resolved_tick=(event.resolved_tick if event.encounter_status == "resolved" else None),
            ).model_dump(mode="json")

        updated_actor = actor.model_copy(update={
            "components": {**actor.components, "combat_encounter": next_component(actor_encounter, target.id)},
        })
        updated_target = target.model_copy(update={
            "components": {**target.components, "combat_encounter": next_component(target_encounter, actor.id)},
        })
        return _validated_state(state, {"entities": {
            **state.entities,
            actor.id: updated_actor,
            target.id: updated_target,
        }})

    if isinstance(event, CombatTruceChangedEvent):
        offer = event.resulting_offer
        expected = plan_combat_truce(
            state, operation=event.operation, actor_id=event.actor_id,
            world_id=event.world_id, branch_id=event.branch_id,
            command_id=event.command_id, ruleset_version=event.ruleset_version,
            existing_offers=state.combat_truce_offers,
            offer_id=None if event.operation == "offer" else offer.offer_id,
            requested_by=event.requested_by,
        )
        if event != expected:
            raise InvalidEvent("Stored truce does not match its authoritative transition.")
        offers = ([*state.combat_truce_offers, offer] if event.operation == "offer" else [
            offer if item.offer_id == offer.offer_id else item for item in state.combat_truce_offers
        ])
        if event.operation != "accept":
            return _validated_state(state, {"combat_truce_offers": offers})
        entities = dict(state.entities)
        for participant_id in (offer.proposer_id, offer.recipient_id):
            participant = entities[participant_id]
            encounter = CombatEncounterComponent.model_validate(participant.components["combat_encounter"])
            closed = encounter.model_copy(update={
                "revision": encounter.revision + 1, "status": "resolved",
                "last_event_id": event.event_id, "last_outcome": "truce",
                "resolved_tick": state.current_tick,
            }).model_dump(mode="json")
            entities[participant_id] = participant.model_copy(update={
                "components": {**participant.components, "combat_encounter": closed},
            })
        return _validated_state(state, {"combat_truce_offers": offers, "entities": entities})

    if isinstance(event, CombatRetreatResolvedEvent):
        try:
            event = CombatRetreatResolvedEvent.model_validate(event.model_dump(mode="python"))
        except Exception as exc:
            raise InvalidEvent(f"Combat retreat failed registered-policy validation: {exc}") from exc
        if event.event_id != uuid5(event.command_id, f"{event.ruleset_version}:combat.retreat"):
            raise InvalidEvent("Combat retreat event id must be bound to its command.")
        actor = state.entities.get(event.actor_id)
        opponent = state.entities.get(event.opponent_id)
        if (
            actor is None or actor.kind not in {"character", "person"} or actor.status != "active"
            or opponent is None or opponent.kind not in {"character", "person"} or opponent.status != "active"
        ):
            raise InvalidEvent("Both retreat participants must be active characters.")
        if actor.location_id != event.from_place_id or opponent.location_id != event.from_place_id:
            raise InvalidEvent("A retreat must begin while both participants remain at the encounter location.")
        if event.arrived_tick != state.current_tick:
            raise InvalidEvent("Retreat outcome must commit at the recorded route arrival tick.")
        origin = state.entities.get(event.from_place_id)
        destination = state.entities.get(event.to_place_id)
        if origin is None or destination is None or destination.kind not in {"place", "location"}:
            raise InvalidEvent("Retreat endpoints must be registered places.")
        try:
            place = PlaceComponent.model_validate(origin.components.get("place", {}))
        except Exception as exc:
            raise InvalidEvent("Retreat origin has no registered route map.") from exc
        route = next((item for item in place.routes if item.route_id == event.route_id), None)
        if (
            route is None or not route.is_open
            or route.from_place_id != event.from_place_id
            or route.to_place_id != event.to_place_id
            or route.travel_ticks != event.arrived_tick - event.started_tick
        ):
            raise InvalidEvent("Retreat must use an open registered route and its exact duration.")
        if destination.status in {"dead", "retired"}:
            raise InvalidEvent("Retreat destination is retired.")
        if destination.components.get("hidden") is True and not any(
            record.subject_id == destination.id
            and actor.id in {record.holder_id, record.owner_id}
            and record.kind == "observation"
            and record.received_tick <= state.current_tick
            and (record.valid_from_tick is None or record.valid_from_tick <= state.current_tick)
            for record in state.knowledge
        ):
            raise InvalidEvent("Retreat cannot use an undiscovered destination.")
        travel_config = state.metadata.get("travel")
        if travel_config is False or isinstance(travel_config, dict) and travel_config.get("enabled") is False:
            raise InvalidEvent("Retreat is disabled by this world's travel policy.")
        try:
            capabilities = CapabilitiesComponent.model_validate(actor.components.get("capabilities", {}))
        except Exception as exc:
            raise InvalidEvent("Retreat actor has invalid travel capabilities.") from exc
        grants = set(capabilities.grants)
        restrictions = set(capabilities.restrictions)
        if "world.travel" not in grants or "world.travel" in restrictions:
            raise InvalidEvent("Retreat actor lacks registered travel capability.")
        if set(route.access_tags) & restrictions or not set(route.access_tags).issubset(grants):
            raise InvalidEvent("Retreat actor lacks a registered route access capability.")
        mode_map = {"travel.walk": "world.travel", "travel.ride": "travel.ride", "travel.boat": "travel.boat"}
        modes = {mode_map.get(mode, mode) for mode in route.travel_modes}
        if modes & restrictions or not modes.issubset(grants):
            raise InvalidEvent("Retreat actor cannot use this route's registered travel mode.")
        try:
            destination_place = PlaceComponent.model_validate(destination.components.get("place", {}))
        except Exception:
            destination_place = None
        policy = destination_place.access_policy if destination_place is not None else "open"
        required_access = {"restricted": "place.enter_restricted", "private": "place.enter_private"}.get(policy)
        if required_access is not None and (
            required_access in restrictions or required_access not in grants
        ):
            raise InvalidEvent("Retreat destination requires an unavailable access capability.")
        try:
            actor_encounter = CombatEncounterComponent.model_validate(actor.components.get("combat_encounter"))
            opponent_encounter = CombatEncounterComponent.model_validate(opponent.components.get("combat_encounter"))
        except Exception as exc:
            raise InvalidEvent("Retreat requires the registered participant encounter state.") from exc
        if (
            actor_encounter.status != "active" or opponent_encounter.status != "active"
            or actor_encounter.encounter_id != event.encounter_id
            or opponent_encounter.encounter_id != event.encounter_id
            or actor_encounter.opponent_id != opponent.id
            or opponent_encounter.opponent_id != actor.id
            or actor_encounter.location_id != event.from_place_id
            or opponent_encounter.location_id != event.from_place_id
            or actor_encounter.exchange_count != opponent_encounter.exchange_count
            or actor_encounter.last_event_id != opponent_encounter.last_event_id
            or actor_encounter.revision != event.expected_actor_encounter_revision
            or opponent_encounter.revision != event.expected_opponent_encounter_revision
        ):
            raise InvalidEvent("Retreat encounter identity or compare-and-swap data is stale.")

        def close_encounter(current: CombatEncounterComponent) -> dict[str, object]:
            return current.model_copy(update={
                "revision": current.revision + 1,
                "status": "retreated",
                "last_event_id": event.event_id,
                "last_outcome": "retreated",
                "resolved_tick": event.arrived_tick,
            }).model_dump(mode="json")

        updated_actor = actor.model_copy(update={
            "components": {**actor.components, "combat_encounter": close_encounter(actor_encounter)},
        })
        updated_opponent = opponent.model_copy(update={
            "components": {**opponent.components, "combat_encounter": close_encounter(opponent_encounter)},
        })
        return _validated_state(state, {"entities": {
            **state.entities,
            actor.id: updated_actor,
            opponent.id: updated_opponent,
        }})

    if isinstance(event, DamageAppliedEvent):
        target = state.entities.get(event.target_id)
        if target is None or target.kind not in {"character", "person"}:
            raise InvalidEvent("Damage target must be an existing character.")
        if target.status in {"dead", "retired"}:
            raise InvalidEvent("A terminal character cannot take damage.")
        if event.source_id is not None and event.source_id not in state.entities:
            raise InvalidEvent("Damage source entity does not exist.")
        if event.source_event_ids:
            if len(event.source_event_ids) != 1 or event.source_id is None:
                raise InvalidEvent("A causally linked combat injury must cite exactly one exchange and source character.")
            try:
                encounter = CombatEncounterComponent.model_validate(target.components.get("combat_encounter"))
            except Exception as exc:
                raise InvalidEvent("A combat injury must follow its registered exchange event.") from exc
            source = state.entities.get(event.source_id)
            if (
                source is None
                or encounter.last_event_id != event.source_event_ids[0]
                or encounter.last_damage_event_id != event.event_id
                or encounter.opponent_id != source.id
                or encounter.last_outcome != "hit"
                or event.damage != combat_damage(source)
            ):
                raise InvalidEvent("Combat injury does not match the referenced exchange outcome.")
        try:
            health = HealthComponent.model_validate(target.components.get("health"))
        except Exception as exc:
            raise InvalidEvent("Damage target has no valid health component.") from exc
        if health.revision != event.expected_health_revision:
            raise InvalidEvent("Damage expected_health_revision does not match current health revision.")
        remaining = max(0, health.current - event.damage)
        status = target.status
        incapacitating_injury_id = health.incapacitating_injury_id
        if remaining == 0:
            if event.source_event_ids and combat_death_protection_enabled(state, target):
                remaining = 1
                status = "incapacitated"
                incapacitating_injury_id = event.event_id
            else:
                status = "dead"
                incapacitating_injury_id = None
        severity = min(100, max(1, (event.damage * 100 + health.maximum - 1) // health.maximum))
        injury = InjuryRecord(
            injury_id=event.event_id,
            injury_type=event.injury_type,
            severity=severity,
            started_tick=state.current_tick,
            cause_event_id=event.event_id,
            treated=False,
            description=event.description,
        )
        updated_health = health.model_copy(update={
            "current": remaining,
            "injuries": [*health.injuries, injury],
            "incapacitating_injury_id": incapacitating_injury_id,
            "revision": health.revision + 1,
        })
        components = {**target.components, "health": updated_health.model_dump(mode="json")}
        updated_target = target.model_copy(update={"components": components, "status": status})
        entities = {**state.entities, event.target_id: updated_target}
        if remaining == 0:
            entities = _apply_population_departure(
                state,
                target,
                updated_target,
                expected_population_revision=event.expected_population_revision,
            )
        return _validated_state(state, {"entities": entities})

    if isinstance(event, InjuryTreatedEvent):
        try:
            event = InjuryTreatedEvent.model_validate(event.model_dump(mode="python"))
        except Exception as exc:
            raise InvalidEvent(f"Injury treatment failed registered-policy validation: {exc}") from exc
        healer = state.entities.get(event.healer_id)
        target = state.entities.get(event.target_id)
        if (
            healer is None or healer.kind not in {"character", "person"} or healer.status != "active"
            or target is None or target.kind not in {"character", "person"}
            or target.status not in {"active", "incapacitated"}
        ):
            raise InvalidEvent("Injury treatment requires an active healer and a living patient eligible for treatment.")
        if (
            state.current_tick != event.resolved_tick
            or event.started_tick != state.current_tick - INJURY_TREATMENT_DURATION
        ):
            raise InvalidEvent("Injury treatment must resolve at its registered thirty-minute world-time boundary.")
        if (
            healer.location_id != event.location_id
            or target.location_id != event.location_id
            or settled_treatment_place(state, event.location_id) is None
        ):
            raise InvalidEvent("Injury treatment requires healer and patient at an active settlement or building.")
        if target.id != healer.id and target.components.get("hidden") is True and not any(
            record.subject_id == target.id
            and healer.id in {record.holder_id, record.owner_id}
            and record.kind == "observation"
            and record.received_tick <= state.current_tick
            and (record.valid_from_tick is None or record.valid_from_tick <= state.current_tick)
            for record in state.knowledge
        ):
            raise InvalidEvent("An injury treatment target must be visible to the healer.")
        skill = care_skill(healer)
        if (
            skill is None
            or skill[0] != event.expected_skills_revision
            or skill[1] != event.skill_level
            or skill[1] < INJURY_TREATMENT_MIN_CARE
        ):
            raise InvalidEvent("Injury treatment requires current registered skill.care at level 10 or higher.")
        if not is_medicine_resource(event.medicine_resource_id):
            raise InvalidEvent("Injury treatment requires a registered medicine resource.")
        try:
            health = HealthComponent.model_validate(target.components.get("health"))
        except Exception as exc:
            raise InvalidEvent("Injury treatment target has no valid health component.") from exc
        if target.status == "incapacitated" and health.incapacitating_injury_id != event.injury_id:
            raise InvalidEvent("An incapacitated patient must receive treatment for the exact incapacitating injury.")
        if health.revision != event.expected_health_revision:
            raise InvalidEvent("Injury treatment health revision is stale.")
        if event.healing_amount != min(
            INJURY_TREATMENT_HEALING, health.maximum - health.current
        ):
            raise InvalidEvent("Injury treatment healing amount does not match the current health deficit.")
        injury_index = next((index for index, injury in enumerate(health.injuries)
                             if injury.injury_id == event.injury_id), None)
        if injury_index is None or health.injuries[injury_index].treated:
            raise InvalidEvent("Injury treatment must select one existing untreated injury.")
        injuries = list(health.injuries)
        injuries[injury_index] = injuries[injury_index].model_copy(update={
            "treated": True,
            "treated_event_id": event.event_id,
            "treated_tick": state.current_tick,
        })
        updated_health = health.model_copy(update={
            "injuries": injuries,
            "incapacitating_injury_id": None,
            "revision": health.revision + 1,
        })
        return _validated_state(state, {"entities": {
            **state.entities,
            target.id: target.model_copy(update={
                "components": {**target.components, "health": updated_health.model_dump(mode="json")},
                "status": "active",
            }),
        }})

    if isinstance(event, HealthRestoredEvent):
        target = state.entities.get(event.target_id)
        if target is None or target.kind not in {"character", "person"}:
            raise InvalidEvent("Healing target must be an existing character.")
        if target.status in {"dead", "retired"}:
            raise InvalidEvent("A terminal character cannot be healed.")
        try:
            health = HealthComponent.model_validate(target.components.get("health"))
        except Exception as exc:
            raise InvalidEvent("Healing target has no valid health component.") from exc
        if health.revision != event.expected_health_revision:
            raise InvalidEvent("Healing expected_health_revision does not match current health revision.")
        if health.current == health.maximum:
            raise InvalidEvent("A fully healthy character cannot benefit from healing.")
        if event.method == "core.injury_treatment":
            if len(event.source_event_ids) != 2 or event.amount > INJURY_TREATMENT_HEALING:
                raise InvalidEvent("Injury healing must cite its supply and treatment event and stay within the registered cap.")
            if event.expected_health_revision < 2:
                raise InvalidEvent("Injury healing must follow its health-revision treatment event.")
            if not any(
                injury.treated
                and injury.treated_event_id == event.source_event_ids[1]
                and injury.treated_tick == state.current_tick
                for injury in health.injuries
            ):
                raise InvalidEvent("Injury healing must follow treatment of a selected injury in this event batch.")
            if event.amount != min(INJURY_TREATMENT_HEALING, health.maximum - health.current):
                raise InvalidEvent("Injury healing amount does not match the registered health-recovery cap.")
        updated_health = health.model_copy(
            update={"current": min(health.maximum, health.current + event.amount), "revision": health.revision + 1}
        )
        components = {**target.components, "health": updated_health.model_dump(mode="json")}
        updated_target = target.model_copy(update={"components": components})
        return _validated_state(state, {"entities": {**state.entities, event.target_id: updated_target}})

    if isinstance(event, CultivationBreakthroughResolvedEvent):
        plan = event.plan
        actor = state.entities.get(plan.actor_id)
        if (
            actor is None
            or actor.kind not in {"character", "person"}
            or actor.status != "active"
            or state.current_tick != plan.resolved_tick
        ):
            raise InvalidEvent("A breakthrough outcome must resolve for its active actor at the registered tick.")
        try:
            progression = ProgressionComponent.model_validate(actor.components.get("progression"))
            skills = SkillsComponent.model_validate(actor.components.get("skills"))
            health = HealthComponent.model_validate(actor.components.get("health"))
            inventory = InventoryComponent.model_validate(actor.components.get("inventory"))
        except Exception as exc:
            raise InvalidEvent("A breakthrough outcome requires valid actor progression, skills, health and inventory.") from exc
        if (
            progression.revision != plan.expected_progression_revision
            or progression.milestones != plan.expected_progression_milestones
            or skills.revision != plan.expected_skills_revision
            or health.revision != plan.expected_health_revision
            or inventory != plan.resulting_inventory
        ):
            raise InvalidEvent("Breakthrough outcome component expectations do not match its pre-state and FIFO effect.")
        for expectation in plan.account_expectations:
            account = state.resources.get(expectation.account_id)
            if (
                account is None
                or account.resource_id != expectation.resource_id
                or account.account_type != expectation.account_type
                or account.owner_id != expectation.owner_id
                or account.balance != expectation.resulting_balance
            ):
                raise InvalidEvent("Breakthrough outcome qi ledger does not match its exact consumed flows.")
        if plan.outcome == "failed":
            return state
        next_progression = plan.resulting_progression
        if next_progression is None:
            raise InvalidEvent("A successful breakthrough must carry its registered progression result.")
        components = {**actor.components, "progression": next_progression.model_dump(mode="json")}
        return _validated_state(state, {"entities": {
            **state.entities,
            actor.id: actor.model_copy(update={"components": components}),
        }})

    if isinstance(event, LifeEndedEvent):
        target = state.entities.get(event.entity_id)
        if target is None or target.kind not in {"character", "person"}:
            raise InvalidEvent("Life-expiry target must be an existing character.")
        if target.status in {"dead", "retired"}:
            raise InvalidEvent("A terminal character's lifespan cannot be resolved again.")
        try:
            life = LifeComponent.model_validate(target.components.get("life"))
        except Exception as exc:
            raise InvalidEvent("Life-expiry target has no valid registered life component.") from exc
        if life.revision != event.expected_life_revision:
            raise InvalidEvent("Life-expiry expected_life_revision does not match stored life.")
        if life.is_immortal or life.lifespan_ticks is None:
            raise InvalidEvent("An immortal or open-ended character has no fixed lifespan expiry.")
        if state.current_tick < life.birth_tick + life.lifespan_ticks:
            raise InvalidEvent("A character cannot die of lifespan before its registered lifespan expires.")
        components = dict(target.components)
        raw_health = components.get("health")
        if raw_health is not None:
            try:
                health = HealthComponent.model_validate(raw_health)
            except Exception as exc:
                raise InvalidEvent("Life-expiry target has an invalid health component.") from exc
            if health.current > 0:
                health = health.model_copy(update={
                    "current": 0, "incapacitating_injury_id": None, "revision": health.revision + 1,
                })
            components["health"] = health.model_dump(mode="json")
        updated = target.model_copy(update={"status": "dead", "components": components})
        entities = _apply_population_departure(
            state,
            target,
            updated,
            expected_population_revision=event.expected_population_revision,
        )
        return _validated_state(state, {"entities": entities})

    if isinstance(event, PlayerCharacterContinuedEvent):
        validate_player_character_continued_batch(
            state,
            event.actor_id,
            [event],
            world_id=event.world_id,
            branch_id=event.branch_id,
            command_id=event.command_id,
            ruleset_version=event.ruleset_version,
            command_type=None,
            command_payload=None,
        )
        return _validated_state(state, {"player_id": event.new_player_id})

    if isinstance(event, CohortMemberPromotedEvent):
        return _apply_cohort_member_promotion(state, event)

    if isinstance(event, PopulationYearAdvancedEvent):
        return _apply_population_year_advance(state, event)

    if isinstance(event, OrganizationMembershipReviewedEvent):
        return _apply_organization_membership_review(state, event)

    if isinstance(event, BackgroundNeedsAdvancedEvent):
        return _apply_background_needs_advanced(state, event)

    if isinstance(event, BackgroundRoutineAdvancedEvent):
        return _apply_background_routine_advanced(state, event)

    if isinstance(event, BackgroundDeprivationResolvedEvent):
        return _apply_background_deprivation(state, event)

    if isinstance(event, GovernanceSuccessionEvent):
        return _apply_governance_succession(state, event)

    if isinstance(event, OrganizationMemberResignedEvent):
        return _apply_organization_member_resignation(state, event)

    if isinstance(event, GovernanceLeadershipHandedOverEvent):
        return _apply_governance_leadership_handover(state, event)

    if isinstance(event, ProductionQuotaRenewedEvent):
        return _apply_production_quota_renewal(state, event)

    if isinstance(event, RenewableResourceHarvestedEvent):
        return _apply_renewable_resource_harvest(state, event)

    if isinstance(event, ScheduledEventCompletedEvent):
        return _apply_scheduled_event_completion(state, event)

    if isinstance(event, EnvironmentalSourceAccountOpenedEvent):
        return apply_environmental_source_account_opened(state, event)

    if isinstance(event, ResourceAccountOpenedEvent):
        if event.owner_id not in state.entities:
            raise InvalidEvent("A holder account owner must be an existing entity.")
        if event.account_id in state.resources:
            raise InvalidEvent(f"Resource account {event.account_id} already exists.")
        if any(
            account.account_type == "holder"
            and account.owner_id == event.owner_id
            and account.resource_id == event.resource_id
            for account in state.resources.values()
        ):
            raise InvalidEvent("The owner already has a holder account for this resource.")
        resources = {
            **state.resources,
            event.account_id: ResourceAccount(
                resource_id=event.resource_id,
                balance="0",
                owner_id=event.owner_id,
                account_type="holder",
            ),
        }
        return _validated_state(state, {"resources": resources})

    if isinstance(event, MarketTradeExecutedEvent):
        return _apply_market_trade(state, event)

    if isinstance(event, ShipmentDispatchedEvent):
        return _apply_shipment_dispatched(state, event)

    if isinstance(event, ShipmentArrivalDeliveredEvent):
        return _apply_shipment_arrival_delivered(state, event)

    if isinstance(event, ShipmentArrivalDeferredEvent):
        return _apply_shipment_arrival_deferred(state, event)

    if isinstance(event, ShipmentReceiptAcceptedEvent):
        return _apply_shipment_receipt_accepted(state, event)

    if isinstance(event, ShipmentSpoilageResolvedEvent):
        return _apply_shipment_spoilage_resolved(state, event)

    if isinstance(event, HistoricalEvidenceDestroyedEvent):
        if event.event_id != uuid5(event.command_id, f"{event.ruleset_version}:historical_evidence.destroyed"):
            raise InvalidEvent("Historical evidence destruction event is not bound to its command.")
        if event.destroyed_tick != state.current_tick:
            raise InvalidEvent("Historical evidence must be destroyed at the current world tick.")
        try:
            target, _artifact, location_id = validate_destroyable_historical_item(
                state,
                actor_id=event.actor_id,
                target_id=event.target_id,
                expected_evidence_id=event.historical_evidence_id,
                expected_evidence_hash=event.historical_evidence_hash,
            )
        except ValueError as exc:
            raise InvalidEvent("Historical evidence source is unavailable or has changed.") from exc
        if event.location_id != location_id:
            raise InvalidEvent("Historical evidence destruction location does not match the actor's current place.")
        entities = {
            **state.entities,
            target.id: target.model_copy(update={"status": "destroyed"}),
        }
        return _validated_state(state, {"entities": entities})

    if isinstance(event, ComponentReplacedEvent):
        entity = state.entities.get(event.entity_id)
        if entity is None:
            raise InvalidEvent(f"Entity {event.entity_id} does not exist.")
        if entity.status in {"dead", "retired"}:
            raise InvalidEvent(f"Entity {event.entity_id} is {entity.status}; its state is terminal.")
        try:
            replacement = validate_replacement(entity, event)
        except ValueError as exc:
            raise InvalidEvent(str(exc)) from exc
        if not hasattr(replacement, "model_dump"):
            raise InvalidEvent("Only registered typed components can be replaced.")
        if event.component_name == "life":
            raw_current_life = entity.components.get("life")
            current_origin = (
                LifeComponent.model_validate(raw_current_life).population_origin
                if isinstance(raw_current_life, dict) else None
            )
            if replacement.population_origin != current_origin:
                raise InvalidEvent("Population origin is reducer-owned and cannot be component-replaced.")
        components = {
            **entity.components,
            event.component_name: replacement.model_dump(mode="json"),
        }
        entities = {**state.entities, event.entity_id: entity.model_copy(update={"components": components})}
        return _validated_state(state, {"entities": entities})

    if isinstance(event, AuthorComponentRevisedEvent):
        entity = state.entities.get(event.entity_id)
        if entity is None:
            raise InvalidEvent(f"Entity {event.entity_id} does not exist.")
        current_value = entity.components.get(event.component_name)
        if author_value_hash(current_value, component_name=event.component_name) != event.expected_previous_hash:
            raise InvalidEvent("Author revision expected_previous_hash does not match stored value.")
        try:
            replacement = validate_author_replacement(entity, event)
        except ValueError as exc:
            raise InvalidEvent(str(exc)) from exc
        if (
            entity.id == state.player_id
            and isinstance(replacement, CapabilitiesComponent)
            and isinstance(event.replacement, dict)
            and event.replacement.get("combat_death_protection") is True
        ):
            raise InvalidEvent("Combat death protection can only be enabled for a non-player character.")
        if event.component_name == "life":
            raw_current_life = entity.components.get("life")
            current_origin = (
                LifeComponent.model_validate(raw_current_life).population_origin
                if isinstance(raw_current_life, dict) else None
            )
            if replacement.population_origin != current_origin:
                raise InvalidEvent("Population origin is reducer-owned and cannot be author-revised.")
        component_schema = COMPONENT_REGISTRY[event.component_name].schema
        if hasattr(replacement, "model_dump"):
            canonical_value = replacement.model_dump(mode="json")
        else:
            canonical_value = TypeAdapter(component_schema).dump_python(replacement, mode="json")
        updated_components = {**entity.components, event.component_name: canonical_value}
        updated_entity = entity.model_copy(update={"components": updated_components})
        entities = {**state.entities, event.entity_id: updated_entity}
        return _validated_state(state, {"entities": entities})

    if isinstance(event, KnowledgeAcquiredEvent):
        if any(record.id == event.record.id for record in state.knowledge):
            raise InvalidEvent(f"Knowledge record {event.record.id} already exists.")
        if event.record.disclosure is not None:
            try:
                validate_disclosure_record(event.record, state)
            except ValueError as exc:
                raise InvalidEvent(f"NPC claim disclosure failed registered source validation: {exc}") from exc
        if event.record.assessment is not None:
            if event.record.owner_id is None:
                raise InvalidEvent("Evidence assessment must be owned by the assessing character.")
            try:
                validate_assessment_record(
                    event.record,
                    state.knowledge,
                    actor_id=event.record.owner_id,
                    at_tick=state.current_tick,
                )
            except ValueError as exc:
                raise InvalidEvent(f"Evidence assessment failed registered source validation: {exc}") from exc
        return _validated_state(state, {"knowledge": [*state.knowledge, event.record]})

    if isinstance(event, RelationEstablishedEvent):
        if any(item.id == event.relation.id for item in state.relations):
            raise InvalidEvent(f"Relation {event.relation.id} already exists.")
        return _validated_state(state, {"relations": [*state.relations, event.relation]})

    if isinstance(event, RelationDimensionChangedEvent):
        relations = list(state.relations)
        index = next((i for i, item in enumerate(relations) if item.id == event.relation_id), None)
        if index is None:
            raise InvalidEvent(f"Relation {event.relation_id} does not exist.")
        relation = relations[index]
        current_value = relation.dimensions.get(event.dimension)
        if current_value != event.expected_previous:
            raise InvalidEvent("Relation dimension expected_previous does not match stored value.")
        dimensions = {**relation.dimensions, event.dimension: event.value}
        evidence = list(dict.fromkeys([*relation.evidence_refs, *event.evidence_refs]))
        relations[index] = relation.model_copy(
            update={
                "dimensions": dimensions,
                "evidence_refs": evidence,
                "updated_tick": state.current_tick,
                "revision": relation.revision + 1,
            }
        )
        return _validated_state(state, {"relations": relations})

    if isinstance(event, GoalCreatedEvent):
        if any(goal.id == event.goal.id for goal in state.goals):
            raise InvalidEvent(f"Goal {event.goal.id} already exists.")
        return _validated_state(state, {"goals": [*state.goals, event.goal]})

    if isinstance(event, GoalUpdatedEvent):
        goals = list(state.goals)
        index = next((i for i, item in enumerate(goals) if item.id == event.goal_id), None)
        if index is None:
            raise InvalidEvent(f"Goal {event.goal_id} does not exist.")
        goal = goals[index]
        if goal.revision != event.expected_revision:
            raise InvalidEvent("Goal expected_revision does not match stored revision.")
        if goal.status in {"succeeded", "failed", "abandoned"}:
            raise InvalidEvent("A terminal goal cannot be changed.")
        if event.progress is not None and event.progress < goal.progress:
            raise InvalidEvent("Goal progress cannot move backwards.")
        status = event.status if event.status is not None else goal.status
        evidence_refs = list(dict.fromkeys([*goal.evidence_refs, *(event.evidence_refs or [])]))
        if status == "succeeded" and not evidence_refs:
            raise InvalidEvent("A succeeded goal requires evidence references.")
        goals[index] = goal.model_copy(
            update={
                "progress": event.progress if event.progress is not None else goal.progress,
                "status": status,
                "evidence_refs": evidence_refs,
                "updated_tick": state.current_tick,
                "revision": goal.revision + 1,
            }
        )
        return _validated_state(state, {"goals": goals})

    if isinstance(event, InvestigationCaseOpenedEvent):
        case_file = event.case_file
        anchor = state.entities.get(case_file.anchor_id)
        if (
            event.actor_id != case_file.owner_id
            or case_file.opened_tick != state.current_tick
            or anchor is None
            or anchor.kind != "item"
        ):
            raise InvalidEvent("Investigation case opening does not match the current actor, tick, and item anchor.")
        if any(item.id == case_file.id for item in state.investigation_cases):
            raise InvalidEvent("Investigation case file ID already exists.")
        if any(item.owner_id == case_file.owner_id and item.case_id == case_file.case_id
               for item in state.investigation_cases):
            raise InvalidEvent("This actor already has a file for the authored investigation case.")
        return _validated_state(state, {"investigation_cases": [*state.investigation_cases, case_file]})

    if isinstance(event, InvestigationCaseEvidenceAttachedEvent):
        files = list(state.investigation_cases)
        index = next((i for i, item in enumerate(files) if item.id == event.case_file_id), None)
        if index is None:
            raise InvalidEvent("Investigation case file does not exist.")
        case_file = files[index]
        assessment = next((record for record in state.knowledge if record.id == event.assessment_id), None)
        if (
            event.actor_id != case_file.owner_id
            or case_file.status != "open"
            or case_file.revision != event.expected_revision
            or event.updated_tick != state.current_tick
            or event.assessment_id in case_file.evidence_refs
            or assessment is None
            or assessment.owner_id != case_file.owner_id
            or assessment.holder_id is not None
            or assessment.assessment is None
        ):
            raise InvalidEvent("Case evidence attachment is stale, unavailable, or already recorded.")
        files[index] = case_file.model_copy(update={
            "evidence_refs": [*case_file.evidence_refs, event.assessment_id],
            "revision": case_file.revision + 1,
            "updated_tick": state.current_tick,
        })
        return _validated_state(state, {"investigation_cases": files})

    if isinstance(event, InvestigationCaseClosedEvent):
        files = list(state.investigation_cases)
        index = next((i for i, item in enumerate(files) if item.id == event.case_file_id), None)
        if index is None:
            raise InvalidEvent("Investigation case file does not exist.")
        case_file = files[index]
        if (
            event.actor_id != case_file.owner_id
            or case_file.status != "open"
            or case_file.revision != event.expected_revision
            or event.closed_tick != state.current_tick
        ):
            raise InvalidEvent("Investigation case closure is stale or does not match the current actor and tick.")
        files[index] = case_file.model_copy(update={
            "status": "closed",
            "revision": case_file.revision + 1,
            "updated_tick": event.closed_tick,
            "closed_tick": event.closed_tick,
            "outcome": event.outcome,
            "suspect_id": event.suspect_id,
            "public_accusation": event.public_accusation,
        })
        return _validated_state(state, {"investigation_cases": files})

    if isinstance(event, PublicAccusationCorrectionRecordedEvent):
        record = event.record
        case_file = next((item for item in state.investigation_cases if item.id == record.case_file_id), None)
        actor = state.entities.get(record.actor_id)
        suspect = state.entities.get(record.suspect_id)
        location = state.entities.get(record.location_id)
        if (
            record.actor_id != event.actor_id
            or record.event_id != event.event_id
            or record.corrected_tick != state.current_tick
            or record.source_closure_event_id == event.event_id
            or case_file is None
            or case_file.owner_id != record.actor_id
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
            or not entity_visible_to_actor(state, record.actor_id, record.suspect_id)
        ):
            raise InvalidEvent("Public accusation correction is unavailable.")
        if any(item.case_file_id == record.case_file_id for item in state.public_accusation_corrections):
            raise InvalidEvent("Public accusation correction is unavailable.")
        return _validated_state(state, {
            "public_accusation_corrections": [*state.public_accusation_corrections, record],
        })

    if isinstance(event, AccusationRepairOfferRecordedEvent):
        record = event.record
        correction = next((
            item for item in state.public_accusation_corrections
            if item.event_id == record.correction_event_id
        ), None)
        case_file = next((item for item in state.investigation_cases if item.id == record.case_file_id), None)
        actor = state.entities.get(record.accuser_id)
        suspect = state.entities.get(record.suspect_id)
        location = state.entities.get(actor.location_id) if actor is not None and actor.location_id is not None else None
        if (
            record.offer_id != event.event_id
            or event.actor_id != record.accuser_id
            or record.offered_tick != state.current_tick
            or record.updated_tick != state.current_tick
            or record.revision != 1
            or record.status != "pending"
            or record.last_operation is not None
            or correction is None
            or correction.actor_id != record.accuser_id
            or correction.suspect_id != record.suspect_id
            or correction.case_file_id != record.case_file_id
            or correction.expected_revision != record.case_revision
            or correction.source_closure_event_id != record.source_closure_event_id
            or correction.corrected_tick > record.offered_tick
            or case_file is None
            or case_file.owner_id != record.accuser_id
            or case_file.status != "closed"
            or case_file.outcome != "disconfirmed"
            or not case_file.public_accusation
            or case_file.suspect_id != record.suspect_id
            or case_file.revision != record.case_revision
            or any(item.correction_event_id == record.correction_event_id for item in state.accusation_repair_offers)
            or actor is None or actor.kind not in {"character", "person"} or actor.status != "active"
            or suspect is None or suspect.kind not in {"character", "person"} or suspect.status != "active"
            or actor.location_id is None
            or actor.location_id != suspect.location_id
            or location is None or location.kind not in {"place", "location"} or location.status != "active"
            or not entity_visible_to_actor(state, record.accuser_id, record.suspect_id)
        ):
            raise InvalidEvent("Accusation repair is unavailable.")
        return _validated_state(state, {
            "accusation_repair_offers": [*state.accusation_repair_offers, record],
        })

    if isinstance(event, AccusationRepairResolvedEvent):
        updated = event.resulting_offer
        offers = list(state.accusation_repair_offers)
        index = next((i for i, item in enumerate(offers) if item.offer_id == updated.offer_id), None)
        if index is None:
            raise InvalidEvent("Accusation repair is unavailable.")
        current = offers[index]
        accuser = state.entities.get(current.accuser_id)
        suspect = state.entities.get(current.suspect_id)
        if (
            current.status != "pending"
            or event.actor_id != current.suspect_id
            or accuser is None or accuser.kind not in {"character", "person"} or accuser.status != "active"
            or suspect is None or suspect.kind not in {"character", "person"} or suspect.status != "active"
            or event.expected_revision != current.revision
            or updated.offer_id != current.offer_id
            or updated.correction_event_id != current.correction_event_id
            or updated.source_closure_event_id != current.source_closure_event_id
            or updated.case_file_id != current.case_file_id
            or updated.case_revision != current.case_revision
            or updated.accuser_id != current.accuser_id
            or updated.suspect_id != current.suspect_id
            or updated.statement != current.statement
            or updated.offered_tick != current.offered_tick
            or updated.revision != current.revision + 1
            or updated.updated_tick != state.current_tick
            or updated.status != {"accept": "accepted", "decline": "declined", "defer": "pending"}[event.operation]
            or updated.last_operation != event.operation
            or event.requested_by is not None and event.requested_by != current.accuser_id
        ):
            raise InvalidEvent("Accusation repair is unavailable.")
        offers[index] = updated
        return _validated_state(state, {"accusation_repair_offers": offers})

    if isinstance(event, (ResourceConsumedEvent, ResourceProducedEvent)):
        return _apply_resource_flow_event(state, event)

    if isinstance(event, ResourceTransferredEvent):
        if event.from_account_id == event.to_account_id:
            raise InvalidEvent("A resource transfer must use two different accounts.")
        source = state.resources.get(event.from_account_id)
        target = state.resources.get(event.to_account_id)
        if source is None or target is None:
            raise InvalidEvent("Both resource accounts must exist before transfer.")
        if source.resource_id != event.resource_id or target.resource_id != event.resource_id:
            raise InvalidEvent("Both accounts and the transfer must use the same resource.")
        if source.account_type != "holder" or target.account_type != "holder":
            raise InvalidEvent("Direct transfers must move resources between holder accounts.")
        if source.balance < event.quantity:
            raise InsufficientResources(
                f"Account {event.from_account_id} has {source.balance} {event.resource_id}; "
                f"transfer requires {event.quantity}."
            )
        resources = dict(state.resources)
        with localcontext() as context:
            context.prec = 40
            source_balance = source.balance - event.quantity
            target_balance = target.balance + event.quantity
        resources[event.from_account_id] = source.model_copy(update={"balance": source_balance})
        resources[event.to_account_id] = target.model_copy(update={"balance": target_balance})
        return _validated_state(state, {"resources": resources})

    if isinstance(event, TimeAdvancedEvent):
        if event.to_tick <= state.current_tick:
            raise InvalidEvent("World time must advance monotonically.")
        # The parsed event validates the range. A strictly higher tick preserves
        # state invariants, including timestamp bounds on existing records.
        return state.model_copy(update={"current_tick": event.to_tick})

    raise InvalidEvent(f"No reducer is registered for event {event.type!r}.")


def _apply_population_departure(
    state: WorldState,
    previous: WorldEntity,
    terminal: WorldEntity,
    *,
    expected_population_revision: int | None,
) -> dict[UUID, WorldEntity]:
    """Remove one linked living person from a cohort's total exactly once."""
    raw_life = previous.components.get("life")
    if raw_life is None:
        return {**state.entities, terminal.id: terminal}
    try:
        life = LifeComponent.model_validate(raw_life)
    except Exception as exc:
        raise InvalidEvent("Population departure target has an invalid life component.") from exc
    cohort_id = life.population_group_id
    entities = {**state.entities, terminal.id: terminal}
    if cohort_id is None:
        return entities
    cohort = state.entities.get(cohort_id)
    if cohort is None or cohort.kind not in {"group", "population", "population_group", "cohort"}:
        raise InvalidEvent("Population departure cohort does not exist.")
    try:
        population = PopulationComponent.model_validate(cohort.components.get("population"))
    except Exception as exc:
        raise InvalidEvent("Population departure cohort has no valid population component.") from exc
    if population.revision != expected_population_revision:
        raise InvalidEvent("Population departure expected_population_revision does not match the cohort.")
    if population.headcount <= 0:
        raise InvalidEvent("A cohort with no living population cannot record a member departure.")
    updated_population = population.model_copy(
        update={"headcount": population.headcount - 1, "revision": population.revision + 1}
    )
    entities[cohort.id] = cohort.model_copy(
        update={"components": {**cohort.components, "population": updated_population.model_dump(mode="json")}}
    )
    return entities


def _apply_cohort_member_promotion(state: WorldState, event: CohortMemberPromotedEvent) -> WorldState:
    if event.cohort_id in {event.character.id} or event.character.id in state.entities:
        raise InvalidEvent("A promoted cohort member must have a new, unique entity id.")
    cohort = state.entities.get(event.cohort_id)
    if cohort is None or cohort.kind not in {"group", "population", "population_group", "cohort"}:
        raise InvalidEvent("Promotion cohort must be a registered population group.")
    if cohort.status in {"dead", "retired"}:
        raise InvalidEvent("A terminal population group cannot provide cohort members.")
    try:
        population = PopulationComponent.model_validate(cohort.components.get("population"))
        life = LifeComponent.model_validate(event.character.components.get("life"))
    except Exception as exc:
        raise InvalidEvent("Promotion requires valid population and life components.") from exc
    if population.revision != event.expected_population_revision:
        raise InvalidEvent("Promotion expected_population_revision does not match the cohort.")
    if population.unmodeled_headcount is None:
        raise InvalidEvent("Legacy cohort has no explicit unmodeled member pool to promote from.")
    if population.unmodeled_headcount < 1:
        raise InvalidEvent("The population cohort has no unmodeled members available for promotion.")
    age_cohorts = list(population.age_cohorts)
    if population.age_cohorts:
        if event.age_years is None:
            raise InvalidEvent("Age-structured cohort promotion requires the selected age_years bucket.")
        age_index = next((i for i, item in enumerate(age_cohorts) if item.age_years == event.age_years), None)
        if age_index is None or age_cohorts[age_index].headcount < 1:
            raise InvalidEvent("The selected age cohort has no anonymous member to promote.")
        year_seconds = state.calendar.day_seconds * state.calendar.year_days
        expected_birth_tick = state.current_tick - event.age_years * year_seconds
        expected_stage = "child" if event.age_years < 18 else "adult" if event.age_years < 65 else "elder"
        if life.birth_tick != expected_birth_tick or life.life_stage != expected_stage:
            raise InvalidEvent("Promoted character age and life stage must match the selected cohort and calendar.")
        age_cohorts[age_index] = age_cohorts[age_index].model_copy(
            update={"headcount": age_cohorts[age_index].headcount - 1, "revision": age_cohorts[age_index].revision + 1}
        )
    elif event.age_years is not None:
        raise InvalidEvent("A legacy population group has no age-structured cohort bucket.")
    if (
        life.population_group_id != event.cohort_id
        or life.species_id != population.species_id
        or life.birth_tick != state.current_tick - (event.age_years or 0) * state.calendar.day_seconds * state.calendar.year_days
        or life.revision != 1
        or life.population_origin is not None
    ):
        raise InvalidEvent("Promoted character life data must match the cohort and selected age.")
    if cohort.location_id is None or not _place_is_within_group(state, event.character.location_id, cohort.location_id):
        raise InvalidEvent("Promoted character must begin within its cohort's registered region.")
    updated_population = population.model_copy(
        update={
            "unmodeled_headcount": population.unmodeled_headcount - 1,
            "age_cohorts": age_cohorts,
            "revision": population.revision + 1,
        }
    )
    promoted_character = event.character
    if _membership_recruitment_enabled(state):
        components = dict(event.character.components)
        components["life"] = life.model_copy(
            update={
                "population_origin": PopulationOrigin(
                    source_event_id=event.event_id,
                    source_event_type=event.type,
                    population_group_id=event.cohort_id,
                )
            }
        ).model_dump(mode="json")
        promoted_character = event.character.model_copy(update={"components": components})
    entities = {
        **state.entities,
        cohort.id: cohort.model_copy(
            update={"components": {**cohort.components, "population": updated_population.model_dump(mode="json")}}
        ),
        event.character.id: promoted_character,
    }
    return _validated_state(state, {"entities": entities})


def _apply_population_year_advance(state: WorldState, event: PopulationYearAdvancedEvent) -> WorldState:
    year_seconds = state.calendar.day_seconds * state.calendar.year_days
    if state.current_tick % year_seconds != 0 or event.year_index != state.current_tick // year_seconds:
        raise InvalidEvent("Population progression must occur at its exact registered world-year boundary.")
    scheduled = next((item for item in state.scheduled_events if item.id == event.scheduled_event_id), None)
    if (
        scheduled is None
        or scheduled.status != "queued"
        or scheduled.due_tick != state.current_tick
        or scheduled.handler_id != "population.year_advance"
        or scheduled.handler_version != "1.0.0"
        or scheduled.payload
        or scheduled.repeat_ticks != year_seconds
        or scheduled.completed_runs + 1 != event.year_index
        or any(
            next(item for item in state.scheduled_events if item.id == dependency).status != "completed"
            for dependency in scheduled.dependencies
        )
    ):
        raise InvalidEvent("Population-year event must match the due finite world-level schedule.")
    expected_revisions = {item.cohort_id: item.expected_revision for item in event.expected_populations}
    age_groups = [
        entity for entity in state.entities.values()
        if entity.kind in {"group", "population", "population_group", "cohort"}
        and isinstance(entity.components.get("population"), dict)
        and entity.components["population"].get("age_cohorts")
    ]
    age_groups.sort(key=lambda entity: str(entity.id))
    if set(expected_revisions) != {entity.id for entity in age_groups}:
        raise InvalidEvent("Population-year event must include every age-structured cohort exactly once.")
    entities = dict(state.entities)
    for group in age_groups:
        try:
            population = PopulationComponent.model_validate(group.components.get("population"))
        except Exception as exc:
            raise InvalidEvent("Population-year cohort has an invalid registered population component.") from exc
        if population.revision != expected_revisions[group.id]:
            raise InvalidEvent("Population-year expected revision does not match every cohort.")
        if population.last_advanced_year != event.year_index - 1:
            raise InvalidEvent("Population-year progression must advance exactly one period at a time.")

        births_by_age = [0] * 201
        deaths_by_age = [0] * 201
        birth_remainders = [Decimal("0")] * 201
        death_remainders = [Decimal("0")] * 201
        with localcontext() as context:
            context.prec = 80
            for cohort, rates in zip(population.age_cohorts, population.age_rates, strict=True):
                expected_births = Decimal(cohort.headcount) * rates.annual_birth_rate + cohort.birth_remainder
                births = int(expected_births.to_integral_value(rounding=ROUND_FLOOR))
                birth_remainder = expected_births - Decimal(births)
                expected_deaths = Decimal(cohort.headcount) * rates.annual_death_rate + cohort.death_remainder
                deaths = min(cohort.headcount, int(expected_deaths.to_integral_value(rounding=ROUND_FLOOR)))
                death_remainder = (
                    Decimal("0") if deaths >= cohort.headcount
                    else expected_deaths - Decimal(int(expected_deaths.to_integral_value(rounding=ROUND_FLOOR)))
                )
                destination_age = min(200, cohort.age_years + 1)
                births_by_age[cohort.age_years] += births
                deaths_by_age[cohort.age_years] += deaths
                birth_remainders[destination_age] += birth_remainder
                death_remainders[destination_age] += death_remainder

            births_total = sum(births_by_age)
            deaths_total = sum(deaths_by_age)
            next_cohorts: list[PopulationAgeCohort] = [
                PopulationAgeCohort(
                    age_years=0,
                    headcount=births_total,
                    revision=population.age_cohorts[0].revision + 1,
                )
            ]
            for age_years in range(1, 201):
                source_age = age_years - 1
                survivor_count = population.age_cohorts[source_age].headcount - deaths_by_age[source_age]
                if age_years == 200:
                    survivor_count += population.age_cohorts[200].headcount - deaths_by_age[200]
                if survivor_count > 2**63 - 1:
                    raise InvalidEvent("Age-cohort count exceeds the signed 64-bit population bound.")
                next_cohorts.append(
                    PopulationAgeCohort(
                        age_years=age_years,
                        headcount=survivor_count,
                        birth_remainder=birth_remainders[age_years],
                        death_remainder=death_remainders[age_years],
                        revision=population.age_cohorts[age_years].revision + 1,
                    )
                )
            next_headcount = population.headcount + births_total - deaths_total
            next_unmodeled = population.unmodeled_headcount + births_total - deaths_total
            if next_headcount < 0 or next_headcount > 2**63 - 1:
                raise InvalidEvent("Population total is outside the signed 64-bit count range.")
            if next_unmodeled < 0 or next_unmodeled > 2**63 - 1:
                raise InvalidEvent("Population anonymous count is outside the signed 64-bit count range.")
        updated_population = population.model_copy(
            update={
                "headcount": next_headcount,
                "unmodeled_headcount": next_unmodeled,
                "age_cohorts": next_cohorts,
                "last_advanced_year": event.year_index,
                "revision": population.revision + 1,
            }
        )
        entities[group.id] = group.model_copy(
            update={"components": {**group.components, "population": updated_population.model_dump(mode="json")}}
        )
    result = _validated_state(state, {"entities": entities})
    return result


def _apply_organization_membership_review(
    state: WorldState, event: OrganizationMembershipReviewedEvent
) -> WorldState:
    year_seconds = state.calendar.day_seconds * state.calendar.year_days
    if state.current_tick % year_seconds or event.year_index != state.current_tick // year_seconds:
        raise InvalidEvent("Organization membership review must occur at its exact world-year boundary.")
    scheduled = next((item for item in state.scheduled_events if item.id == event.scheduled_event_id), None)
    if (
        scheduled is None
        or scheduled.status != "queued"
        or scheduled.due_tick != state.current_tick
        or scheduled.handler_id != "population.year_advance"
        or scheduled.handler_version != "1.0.0"
        or scheduled.payload
        or scheduled.repeat_ticks != year_seconds
        or scheduled.completed_runs + 1 != event.year_index
    ):
        raise InvalidEvent("Organization membership review must use the due annual census schedule.")

    age_groups: list[WorldEntity] = []
    for entity in state.entities.values():
        raw = entity.components.get("population")
        if (
            entity.kind not in {"group", "population", "population_group", "cohort"}
            or not isinstance(raw, dict)
            or not raw.get("age_cohorts")
        ):
            continue
        try:
            population = PopulationComponent.model_validate(raw)
        except Exception as exc:
            raise InvalidEvent("Membership review found an invalid age-structured population.") from exc
        if population.last_advanced_year != event.year_index:
            raise InvalidEvent("Membership review must follow the completed census for this year.")
        age_groups.append(entity)
    if not age_groups:
        raise InvalidEvent("Membership review requires the annual age-structured population census.")

    try:
        review_input = build_membership_recruitment_review(state)
        expected_review = plan_membership_recruitment(review_input)
    except Exception as exc:
        raise InvalidEvent(f"Membership review input is invalid: {exc}") from exc
    if not expected_review.expected_organizations:
        raise InvalidEvent("Membership review event cannot be emitted without an opted-in organization.")
    if event.review != expected_review:
        raise InvalidEvent("Membership review must match the deterministic policy, roster, life and lineage plan.")

    enrollments_by_org: dict[UUID, list[UUID]] = {}
    for item in event.review.enrollments:
        enrollments_by_org.setdefault(item.organization_id, []).append(item.member_id)

    entities = dict(state.entities)
    for organization_id, member_ids in enrollments_by_org.items():
        organization = state.entities.get(organization_id)
        if organization is None or organization.status != "active":
            raise InvalidEvent("Membership enrollment requires an active registered organization.")
        try:
            governance = GovernanceComponent.model_validate(organization.components.get("governance"))
        except Exception as exc:
            raise InvalidEvent("Membership enrollment organization has no valid governance component.") from exc
        if not governance.membership_recruitment:
            raise InvalidEvent("Membership enrollment policy is no longer active.")
        if any(member_id in governance.members for member_id in member_ids):
            raise InvalidEvent("Membership enrollment cannot append an existing organization member.")
        updated_governance = governance.model_copy(
            update={
                "members": [*governance.members, *member_ids],
                "revision": governance.revision + 1,
            }
        )
        entities[organization_id] = organization.model_copy(
            update={
                "components": {
                    **organization.components,
                    "governance": updated_governance.model_dump(mode="json"),
                }
            }
        )
    return _validated_state(state, {"entities": entities})


def _membership_recruitment_enabled(state: WorldState) -> bool:
    for entity in state.entities.values():
        raw = entity.components.get("governance")
        if not isinstance(raw, dict):
            continue
        try:
            governance = GovernanceComponent.model_validate(raw)
        except Exception as exc:
            raise InvalidEvent("World contains an invalid governance component.") from exc
        if governance.membership_recruitment:
            return True
    return False


def _apply_background_needs_advanced(state: WorldState, event: BackgroundNeedsAdvancedEvent) -> WorldState:
    if event.to_tick != state.current_tick:
        raise InvalidEvent("Background needs must advance through the exact current world tick.")
    actor = state.entities.get(event.actor_id)
    if not event.include_actor and (actor is None or actor.kind not in {"character", "person"}):
        raise InvalidEvent("Background needs event must identify an existing acting character.")

    background_agents = [
        entity for entity in state.entities.values()
        if (event.include_actor or entity.id != event.actor_id)
        and entity.kind in {"character", "person"}
        and entity.status not in {"dead", "retired"}
        and entity.components.get("needs") is not None
    ]
    background_agents.sort(key=lambda entity: str(entity.id))
    expected_ids = [item.entity_id for item in event.expected_agents]
    if expected_ids != [entity.id for entity in background_agents]:
        raise InvalidEvent(
            "Background needs manifest must include every active nonacting character with needs, "
            "or the included focal character when requested."
        )

    entities = dict(state.entities)
    resources = dict(state.resources)
    account_deltas: dict[UUID, Decimal] = {}
    required_accounts: dict[UUID, ResourceAccount] = {}
    account_expectations = {item.account_id: item.expected_balance for item in event.expected_accounts}
    for entity, action in zip(background_agents, event.expected_agents, strict=True):
        try:
            needs = NeedsComponent.model_validate(entity.components.get("needs"))
        except Exception as exc:
            raise InvalidEvent("Background character has an invalid registered needs component.") from exc
        if needs.revision != action.expected_revision:
            raise InvalidEvent("Background needs expected_revision does not match stored state.")
        boundary = next_deprivation_damage_tick(
            needs,
            from_tick=event.from_tick,
            day_seconds=state.calendar.day_seconds,
        )
        if boundary is not None and (boundary <= event.from_tick or boundary < event.to_tick):
            raise InvalidEvent("Background needs event cannot cross an unresolved NPC deprivation boundary.")
        restore: tuple[str, int] | None = None
        inventory: InventoryComponent | None = None
        allocations: list[FifoLotAllocation] = []
        if action.action in {"eat", "drink"}:
            need_name = "food" if action.action == "eat" else "water"
            if action.resource_id is None or action.consumed_quantity is None:
                raise InvalidEvent("Background consumption has no registered resource or quantity.")
            if not resource_matches_need(state.metadata, action.resource_id, need_name):
                raise InvalidEvent(f"The selected resource cannot restore background {need_name}.")
            need_value = getattr(needs, need_name)
            if need_value is None or need_value.value >= 100:
                raise InvalidEvent(f"Background character has no deficit for {need_name}.")
            with localcontext() as decimal_context:
                decimal_context.prec = 80
                points_decimal = action.consumed_quantity * Decimal("10")
                points = int(points_decimal.to_integral_value(rounding=ROUND_FLOOR))
            if points <= 0 or points > 100 - need_value.value:
                raise InvalidEvent("Background food or water quantity must restore a positive amount without waste.")
            try:
                inventory = InventoryComponent.model_validate(entity.components.get("inventory"))
                if inventory.revision != action.expected_inventory_revision:
                    raise InvalidEvent("Background inventory expected_revision does not match stored state.")
                allocations = allocate_fifo_lots(
                    inventory,
                    resource_id=action.resource_id,
                    quantity=action.consumed_quantity,
                    at_tick=event.from_tick,
                    account_id=action.consumption_account_id,
                )
            except InvalidEvent:
                raise
            except Exception as exc:
                raise InvalidEvent("Background resource is not backed by valid FIFO inventory lots.") from exc
            actual_lots = [
                (lot.lot_id, lot.account_id, lot.quantity)
                for lot in allocations
            ]
            recorded_lots = [
                (lot.lot_id, lot.account_id, lot.quantity)
                for lot in action.consumption_lots
            ]
            if actual_lots != recorded_lots:
                raise InvalidEvent("Background consumption lots must match the exact FIFO allocation.")
            sink_account_id = action.consumption_sink_account_id
            if sink_account_id is None:
                raise InvalidEvent("Background consumption has no sink account id.")
            sink = state.resources.get(sink_account_id)
            if (
                sink is None
                or sink.account_type != "consumption_sink"
                or sink.resource_id != action.resource_id
            ):
                raise InvalidEvent("Background consumption sink does not match the selected resource.")
            required_accounts[sink_account_id] = sink
            for allocation in allocations:
                account = state.resources.get(allocation.account_id)
                if (
                    account is None
                    or account.account_type != "holder"
                    or account.owner_id != entity.id
                    or account.resource_id != action.resource_id
                ):
                    raise InvalidEvent("Background FIFO lot account must be a matching holder account owned by its agent.")
                required_accounts[allocation.account_id] = account
                account_deltas[allocation.account_id] = account_deltas.get(allocation.account_id, Decimal("0")) - allocation.quantity
                account_deltas[sink_account_id] = (
                    account_deltas.get(sink_account_id, Decimal("0")) + allocation.quantity
                )
            restore = (need_name, points)
        advance = advance_passive_needs(
            needs,
            from_tick=event.from_tick,
            to_tick=event.to_tick,
            day_seconds=state.calendar.day_seconds,
            action=action.action,
            restore=restore,
        )
        components = {**entity.components}
        if advance.changed:
            components["needs"] = advance.needs.model_dump(mode="json")
        if inventory is not None:
            try:
                reduced_inventory = reduce_fifo_inventory(inventory, allocations)
            except Exception as exc:
                raise InvalidEvent("Background FIFO inventory reduction is invalid.") from exc
            components["inventory"] = reduced_inventory.model_dump(mode="json")
        if components != entity.components:
            entities[entity.id] = entity.model_copy(update={"components": components})

    expected_account_ids = sorted(required_accounts, key=str)
    if list(account_expectations) != expected_account_ids:
        raise InvalidEvent("Background resource account manifest must include each affected holder and sink exactly once.")
    with localcontext() as decimal_context:
        decimal_context.prec = 80
        for account_id in expected_account_ids:
            account = required_accounts[account_id]
            if account.balance != account_expectations[account_id]:
                raise InvalidEvent("Background resource account expected balance does not match stored state.")
            next_balance = account.balance + account_deltas[account_id]
            if account.account_type == "holder" and next_balance < 0:
                raise InvalidEvent("Background consumption cannot overdraw a holder account.")
            resources[account_id] = account.model_copy(update={"balance": next_balance})
    return _validated_state(state, {"entities": entities, "resources": resources})


def _apply_background_routine_advanced(state: WorldState, event: BackgroundRoutineAdvancedEvent) -> WorldState:
    if event.to_tick != state.current_tick or event.to_tick <= event.from_tick:
        raise InvalidEvent("Background routine must cover the exact positive interval ending at current world time.")
    if event.to_tick - event.from_tick > state.calendar.day_seconds * 30:
        raise InvalidEvent("Background routine batches cannot exceed 30 world days.")
    actor = state.entities.get(event.actor_id)
    if not event.include_actor and (actor is None or actor.kind not in {"character", "person"}):
        raise InvalidEvent("Background routine event must identify an existing acting character.")

    agents = [
        entity for entity in state.entities.values()
        if (event.include_actor or entity.id != event.actor_id)
        and entity.kind in {"character", "person"}
        and entity.status not in {"dead", "retired"}
        and entity.components.get("needs") is not None
    ]
    agents.sort(key=lambda entity: str(entity.id))
    if [entity.id for entity in agents] != [item.entity_id for item in event.expected_agents]:
        raise InvalidEvent("Background routine manifest must include every eligible active character with needs.")

    entities = dict(state.entities)
    computed_accounts: dict[UUID, ResourceAccount] = {}
    account_deltas: dict[UUID, Decimal] = {}
    occupied = occupied_npc_ids(state, event.from_tick)
    for entity, expected in zip(agents, event.expected_agents, strict=True):
        try:
            needs = NeedsComponent.model_validate(entity.components.get("needs"))
            inventory_raw = entity.components.get("inventory")
            inventory = InventoryComponent.model_validate(inventory_raw) if inventory_raw is not None else None
            routine_raw = entity.components.get("daily_routine")
            routine = DailyRoutineComponent.model_validate(routine_raw) if routine_raw is not None else None
        except Exception as exc:
            raise InvalidEvent("Background routine character has invalid typed needs, inventory or daily routine.") from exc
        if (
            expected.expected_needs_revision != needs.revision
            or expected.expected_inventory_revision != (inventory.revision if inventory is not None else None)
            or expected.expected_routine_revision != (routine.revision if routine is not None else None)
            or expected.expected_location_id != entity.location_id
        ):
            raise InvalidEvent("Background routine component or location compare-and-swap data is stale.")

        try:
            advanced = advance_background_routine(
                needs,
                inventory=inventory,
                resources=state.resources,
                metadata=state.metadata,
                routine=None if entity.id in occupied else routine,
                location_id=entity.location_id,
                from_tick=event.from_tick,
                to_tick=event.to_tick,
                day_seconds=state.calendar.day_seconds,
            )
        except Exception as exc:
            raise InvalidEvent(f"Background routine policy could not be recomputed: {exc}") from exc
        if advanced.deprivation_boundary is not None and advanced.deprivation_boundary < event.to_tick:
            raise InvalidEvent("Background routine cannot cross a deprivation damage boundary after stock runs out.")

        computed_care = {item.need: item for item in advanced.care}
        for name in ("food", "water"):
            care = computed_care.get(name)
            actual_expectation = getattr(expected, name)
            if care is None:
                if actual_expectation is not None:
                    raise InvalidEvent(f"Background routine cannot include an unregistered {name} care policy.")
                continue
            lots = [
                BackgroundConsumptionLot(
                    lot_id=item.lot_id,
                    account_id=item.account_id,
                    quantity=item.quantity,
                )
                for item in care.lots
            ]
            expected_care = BackgroundRoutineNeedExpectation(
                resource_id=care.resource_id,
                threshold=care.threshold,
                action_count=care.action_count,
                sink_account_id=care.sink_account_id,
                consumption_lots=lots,
            )
            if actual_expectation != expected_care:
                raise InvalidEvent(f"Background {name} care count, resource, sink or FIFO lots differ from the registered policy.")

        for account_id, account in advanced.expected_accounts.items():
            prior = computed_accounts.setdefault(account_id, account)
            if prior.balance != account.balance or prior.resource_id != account.resource_id:
                raise InvalidEvent("Background routine account expectations diverge within the batch.")
        for account_id, delta in advanced.account_deltas.items():
            account_deltas[account_id] = account_deltas.get(account_id, Decimal("0")) + delta

        components = dict(entity.components)
        if advanced.needs != needs:
            components["needs"] = advanced.needs.model_dump(mode="json")
        if advanced.inventory is not None and advanced.inventory != inventory:
            components["inventory"] = advanced.inventory.model_dump(mode="json")
        if components != entity.components:
            entities[entity.id] = entity.model_copy(update={"components": components})

    expected_account_ids = sorted(computed_accounts, key=str)
    if expected_account_ids != [item.account_id for item in event.expected_accounts]:
        raise InvalidEvent("Background routine account manifest must include each affected holder and sink exactly once.")
    expected_balances = {item.account_id: item.expected_balance for item in event.expected_accounts}
    resources = dict(state.resources)
    with localcontext() as context:
        context.prec = 80
        for account_id in expected_account_ids:
            account = computed_accounts[account_id]
            if account.balance != expected_balances[account_id]:
                raise InvalidEvent("Background routine account expected balance does not match stored state.")
            next_balance = account.balance + account_deltas[account_id]
            if account.account_type == "holder" and next_balance < 0:
                raise InvalidEvent("Background routine cannot overdraw a holder account.")
            resources[account_id] = account.model_copy(update={"balance": next_balance})
    return _validated_state(state, {"entities": entities, "resources": resources})


def _apply_background_deprivation(state: WorldState, event: BackgroundDeprivationResolvedEvent) -> WorldState:
    if event.resolved_tick != state.current_tick:
        raise InvalidEvent("Deprivation consequences must resolve at the exact current world tick.")
    actor = state.entities.get(event.actor_id)
    if not event.include_actor and (actor is None or actor.kind not in {"character", "person"}):
        raise InvalidEvent("Deprivation batch must identify an existing acting character.")

    due_agents: list[tuple[WorldEntity, NeedsComponent, int]] = []
    for entity in state.entities.values():
        if (
            not event.include_actor and entity.id == event.actor_id
            or entity.kind not in {"character", "person"}
            or entity.status in {"dead", "retired"}
            or entity.components.get("needs") is None
        ):
            continue
        try:
            needs = NeedsComponent.model_validate(entity.components.get("needs"))
        except Exception as exc:
            raise InvalidEvent("Background character has an invalid registered needs component.") from exc
        boundary = next_deprivation_damage_tick(
            needs,
            from_tick=state.current_tick,
            day_seconds=state.calendar.day_seconds,
        )
        if boundary is not None and boundary <= state.current_tick:
            due_agents.append((entity, needs, boundary))
    due_agents.sort(key=lambda item: str(item[0].id))
    if [entity.id for entity, _, _ in due_agents] != [subject.entity_id for subject in event.subjects]:
        raise InvalidEvent(
            "Deprivation batch must include every due active nonacting subject exactly once, "
            "or the included focal subject when requested."
        )

    entities = dict(state.entities)
    departures: dict[UUID, tuple[PopulationComponent, int]] = {}
    for (entity, needs, boundary), subject in zip(due_agents, event.subjects, strict=True):
        if subject.boundary_tick != boundary or subject.expected_needs_revision != needs.revision:
            raise InvalidEvent("Deprivation boundary or needs revision is stale.")
        consequence = resolve_due_deprivation(
            needs,
            at_tick=state.current_tick,
            day_seconds=state.calendar.day_seconds,
        )
        if consequence.damage <= 0 or subject.expected_damage != consequence.damage:
            raise InvalidEvent("Deprivation damage does not match complete accumulated critical days.")

        raw_health = entity.components.get("health")
        health: HealthComponent | None = None
        expected_health_revision: int | None = None
        remaining_health: int | None = None
        terminal_status = "active"
        if raw_health is not None:
            try:
                health = HealthComponent.model_validate(raw_health)
            except Exception as exc:
                raise InvalidEvent("Deprivation subject has an invalid registered health component.") from exc
            expected_health_revision = health.revision
            if health.current <= 0:
                raise InvalidEvent("An active deprivation subject cannot begin with terminal health.")
            remaining_health = max(0, health.current - consequence.damage)
            if remaining_health == 0:
                terminal_status = "dead"
        if subject.expected_health_revision != expected_health_revision:
            raise InvalidEvent("Deprivation expected health revision does not match stored state.")
        if subject.terminal_status != terminal_status:
            raise InvalidEvent("Deprivation terminal status does not match computed health loss.")

        population_revision: int | None = None
        life_raw = entity.components.get("life")
        if terminal_status == "dead" and life_raw is not None:
            try:
                life = LifeComponent.model_validate(life_raw)
            except Exception as exc:
                raise InvalidEvent("Deprivation death subject has an invalid registered life component.") from exc
            if life.population_group_id is not None:
                group = state.entities.get(life.population_group_id)
                if group is None:
                    raise InvalidEvent("Deprivation death population group does not exist.")
                try:
                    population = PopulationComponent.model_validate(group.components.get("population"))
                except Exception as exc:
                    raise InvalidEvent("Deprivation death population group has no valid population component.") from exc
                population_revision = population.revision
                previous = departures.get(group.id)
                if previous is not None and previous[0].revision != population.revision:
                    raise InvalidEvent("Deprivation death population revision changed during batch validation.")
                departures[group.id] = (population, (previous[1] if previous else 0) + 1)
        if subject.expected_population_revision != population_revision:
            raise InvalidEvent("Deprivation expected population revision does not match the deceased subject's cohort.")

        components = {**entity.components, "needs": consequence.needs.model_dump(mode="json")}
        status = terminal_status
        if health is not None and remaining_health is not None:
            incapacitating_injury_id = health.incapacitating_injury_id if remaining_health > 0 else None
            if incapacitating_injury_id is not None:
                status = entity.status
            severity = min(100, max(1, (consequence.damage * 100 + health.maximum - 1) // health.maximum))
            injury = InjuryRecord(
                injury_id=uuid5(event.event_id, f"deprivation:{entity.id}"),
                injury_type="core.injury.deprivation",
                severity=severity,
                started_tick=state.current_tick,
                cause_event_id=event.event_id,
                treated=False,
                description="Repeated critical hunger or thirst caused injury.",
            )
            updated_health = health.model_copy(
                update={
                    "current": remaining_health,
                    "injuries": [*health.injuries, injury],
                    "incapacitating_injury_id": incapacitating_injury_id,
                    "revision": health.revision + 1,
                }
            )
            components["health"] = updated_health.model_dump(mode="json")
        entities[entity.id] = entity.model_copy(
            update={"components": components, "status": status}
        )

    for group_id, (population, departures_count) in departures.items():
        group = entities[group_id]
        remaining = population.headcount - departures_count
        if remaining < 0:
            raise InvalidEvent("Deprivation deaths exceed the linked population count.")
        updated_population = population.model_copy(
            update={"headcount": remaining, "revision": population.revision + 1}
        )
        entities[group_id] = group.model_copy(
            update={"components": {**group.components, "population": updated_population.model_dump(mode="json")}}
        )
    return _validated_state(state, {"entities": entities})


def _place_is_within_group(state: WorldState, place_id: UUID | None, region_id: UUID) -> bool:
    current_id = place_id
    seen: set[UUID] = set()
    while current_id is not None and current_id not in seen:
        if current_id == region_id:
            return True
        seen.add(current_id)
        entity = state.entities.get(current_id)
        if entity is None:
            return False
        raw_place = entity.components.get("place")
        next_id: UUID | None = None
        if raw_place is not None:
            try:
                place = PlaceComponent.model_validate(raw_place)
            except Exception:
                return False
            if place.region_id == region_id:
                return True
            next_id = place.parent_id or place.region_id
        if next_id is None:
            parent = entity.components.get("parent_id")
            try:
                next_id = UUID(str(parent)) if parent is not None else None
            except ValueError:
                return False
        current_id = next_id
    return False


def _apply_governance_succession(state: WorldState, event: GovernanceSuccessionEvent) -> WorldState:
    organization = state.entities.get(event.organization_id)
    organization_kinds = {"organization", "polity", "faction", "company", "government"}
    if organization is None or organization.kind not in organization_kinds or organization.status in {"dead", "retired"}:
        raise InvalidEvent("Succession organization must be an active governance entity.")
    try:
        governance = GovernanceComponent.model_validate(organization.components.get("governance"))
    except Exception as exc:
        raise InvalidEvent("Succession organization has no valid governance component.") from exc
    if governance.revision != event.expected_governance_revision:
        raise InvalidEvent("Succession expected_governance_revision does not match stored governance.")
    if governance.succession_policy != event.decision_basis:
        raise InvalidEvent("Succession decision basis does not match the registered policy.")
    if event.decision_basis == "none" and (
        event.successor_id is not None or event.decision_option_id != "leave_vacant"
    ):
        raise InvalidEvent("A policy-none organization may only clear a terminal holder and leave the slot vacant.")
    predecessor = state.entities.get(event.predecessor_id)
    if predecessor is None or predecessor.status not in {"dead", "retired"}:
        raise InvalidEvent("Succession predecessor must be a terminal character.")
    offices = list(governance.offices)
    if event.slot == "leader":
        if governance.leader_id != event.predecessor_id:
            raise InvalidEvent("Succession predecessor is not the current organization leader.")
        target_office_index = None
    else:
        target_office_index = next(
            (index for index, office in enumerate(offices) if office.office_id == event.office_id), None
        )
        if target_office_index is None or offices[target_office_index].holder_id != event.predecessor_id:
            raise InvalidEvent("Succession predecessor does not hold the stated office.")
    if event.successor_id is not None:
        successor = state.entities.get(event.successor_id)
        if (
            successor is None
            or successor.kind not in {"character", "person"}
            or successor.status in {"dead", "retired"}
            or successor.id == event.predecessor_id
            or successor.id not in governance.members
        ):
            raise InvalidEvent("Succession successor must be an active organization member.")
        expected_option = (
            f"leader_holder_{successor.id.hex}"
            if event.slot == "leader"
            else f"office_{event.office_id}_holder_{successor.id.hex}"
        )
        if event.decision_option_id != expected_option:
            raise InvalidEvent("Succession option id does not match the recorded successor.")
    linked = event.linked_leadership_office_ids
    if linked is not None:
        expected_linked = leadership_office_ids_held_by(offices, event.predecessor_id)
        if event.slot != "leader" or linked != expected_linked:
            raise InvalidEvent("Succession must list exactly the predecessor's registered leadership offices.")
        linked_ids = set(linked)
        offices = [
            office.model_copy(update={"holder_id": event.successor_id, "revision": office.revision + 1})
            if office.office_id in linked_ids else office
            for office in offices
        ]
    new_governance = governance.model_copy(
        update={
            "leader_id": event.successor_id if event.slot == "leader" else governance.leader_id,
            "offices": offices,
            "revision": governance.revision + 1,
        }
    )
    if target_office_index is not None:
        office = offices[target_office_index]
        offices[target_office_index] = office.model_copy(
            update={"holder_id": event.successor_id, "revision": office.revision + 1}
        )
        new_governance = new_governance.model_copy(update={"offices": offices})
    return _validated_state(
        state,
        {"entities": {**state.entities, organization.id: organization.model_copy(
            update={"components": {**organization.components, "governance": new_governance.model_dump(mode="json")}}
        )}},
    )


def _apply_leadership_vacancy_review(
    state: WorldState,
    event: LeadershipVacancyReviewRecordedEvent,
) -> WorldState:
    year_ticks = state.calendar.year_days * state.calendar.day_seconds
    current_period = state.current_tick // year_ticks
    if current_period < 1 or event.period_index != current_period:
        raise InvalidEvent("Leadership vacancy review period must match the current positive calendar year.")

    prior = state.leadership_vacancy_reviews.get(event.organization_id)
    if prior is not None and prior.period_index >= event.period_index:
        raise InvalidEvent("An organization may record at most one monotonic leadership vacancy review per period.")

    expected_ballot_id = leadership_vacancy_review_ballot_id(event.decision_request_id)
    if event.ballot_id is None:
        if any(ballot.ballot_id == expected_ballot_id for ballot in state.leadership_ballots):
            raise InvalidEvent("A deferred vacancy review cannot have opened its request-bound ballot.")
        opportunity_state = state
        ballot = None
    else:
        if event.ballot_id != expected_ballot_id:
            raise InvalidEvent("A vacancy ballot ID must be bound to its decision request.")
        ballot = next((row for row in state.leadership_ballots if row.ballot_id == event.ballot_id), None)
        if (
            ballot is None
            or ballot.organization_id != event.organization_id
            or ballot.incumbent_id is not None
            or ballot.vacancy_proposer_id != event.proposer_id
            or ballot.opened_tick != state.current_tick
            or ballot.updated_tick != state.current_tick
            or ballot.governance_revision != event.expected_governance_revision
            or ballot.status != "open"
            or ballot.votes
            or ballot.revision != 1
        ):
            raise InvalidEvent("A vacancy review must reference its same-tick, newly opened ballot with no votes.")
        opportunity_state = state.model_copy(update={
            "leadership_ballots": [row for row in state.leadership_ballots if row.ballot_id != event.ballot_id],
        })

    opportunity = vacancy_opportunity(opportunity_state, event.organization_id)
    if opportunity is None or (
        opportunity.period_index != event.period_index
        or opportunity.governance_revision != event.expected_governance_revision
        or opportunity.proposer_id != event.proposer_id
    ):
        raise InvalidEvent("Leadership vacancy review does not match an independently eligible opportunity.")

    if ballot is not None:
        if ballot.nominee_id not in opportunity.candidate_ids:
            raise InvalidEvent("The vacancy ballot nominee is outside the eligible opportunity candidates.")
        try:
            expected_ballot = open_vacant_leadership_ballot(
                opportunity_state,
                ballot_id=expected_ballot_id,
                organization_id=event.organization_id,
                nominee_id=ballot.nominee_id,
                actor_id=event.proposer_id,
                existing_ballots=opportunity_state.leadership_ballots,
            )
        except (ValueError, TypeError) as exc:
            raise InvalidEvent("The vacancy ballot is incompatible with its independently eligible opportunity.") from exc
        if expected_ballot != ballot:
            raise InvalidEvent("The vacancy ballot must exactly match the registered opportunity snapshot.")

    reviews = dict(state.leadership_vacancy_reviews)
    reviews[event.organization_id] = LeadershipVacancyReviewRecord(
        organization_id=event.organization_id,
        period_index=event.period_index,
        expected_governance_revision=event.expected_governance_revision,
        proposer_id=event.proposer_id,
        decision_request_id=event.decision_request_id,
        ballot_id=event.ballot_id,
        reviewed_tick=state.current_tick,
    )
    return _validated_state(state, {"leadership_vacancy_reviews": reviews})


def _apply_organization_member_resignation(
    state: WorldState,
    event: OrganizationMemberResignedEvent,
) -> WorldState:
    organization = state.entities.get(event.organization_id)
    organization_kinds = {"organization", "polity", "faction", "company", "government"}
    if (
        organization is None
        or organization.kind not in organization_kinds
        or organization.status != "active"
    ):
        raise InvalidEvent("Resignation organization must be an active governance entity.")
    try:
        governance = GovernanceComponent.model_validate(organization.components.get("governance"))
    except Exception as exc:
        raise InvalidEvent("Resignation organization has no valid governance component.") from exc
    if governance.revision != event.expected_governance_revision:
        raise InvalidEvent("Resignation expected_governance_revision does not match stored governance.")

    actor = state.entities.get(event.actor_id)
    if (
        actor is None
        or actor.kind not in {"character", "person"}
        or actor.status != "active"
    ):
        raise InvalidEvent("Resignation actor must be an active character.")
    if actor.id not in governance.members:
        raise InvalidEvent("Resignation actor is not an organization member.")
    if governance.leader_id == actor.id:
        raise InvalidEvent("An active organization leader must complete handover before resigning.")

    expected_offices = sorted(
        office.office_id for office in governance.offices if office.holder_id == actor.id
    )
    if event.cleared_office_ids != expected_offices:
        raise InvalidEvent("Resignation must clear exactly the actor's current subordinate offices.")
    offices = [
        office.model_copy(update={"holder_id": None, "revision": office.revision + 1})
        if office.holder_id == actor.id else office
        for office in governance.offices
    ]
    updated_governance = governance.model_copy(update={
        "members": [member_id for member_id in governance.members if member_id != actor.id],
        "offices": offices,
        "revision": governance.revision + 1,
    })
    updated_organization = organization.model_copy(update={
        "components": {
            **organization.components,
            "governance": updated_governance.model_dump(mode="json"),
        }
    })
    return _validated_state(state, {"entities": {**state.entities, organization.id: updated_organization}})


def _apply_governance_leadership_handover(
    state: WorldState,
    event: GovernanceLeadershipHandedOverEvent,
) -> WorldState:
    organization = state.entities.get(event.organization_id)
    organization_kinds = {"organization", "polity", "faction", "company", "government"}
    if (
        organization is None
        or organization.kind not in organization_kinds
        or organization.status != "active"
    ):
        raise InvalidEvent("Leadership handover organization must be active.")
    try:
        governance = GovernanceComponent.model_validate(organization.components.get("governance"))
    except Exception as exc:
        raise InvalidEvent("Leadership handover organization has no valid governance component.") from exc
    if governance.revision != event.expected_governance_revision:
        raise InvalidEvent("Handover expected_governance_revision does not match stored governance.")
    if governance.succession_policy != "appointment":
        raise InvalidEvent("Leadership handover requires the appointment policy.")
    if governance.leader_id != event.predecessor_id:
        raise InvalidEvent("Handover predecessor is not the current organization leader.")

    predecessor = state.entities.get(event.predecessor_id)
    successor = state.entities.get(event.successor_id)
    if (
        predecessor is None
        or predecessor.kind not in {"character", "person"}
        or predecessor.status != "active"
        or predecessor.id not in governance.members
    ):
        raise InvalidEvent("Handover predecessor must be the active incumbent member.")
    if (
        successor is None
        or successor.kind not in {"character", "person"}
        or successor.status != "active"
        or successor.id not in governance.members
        or successor.id == predecessor.id
    ):
        raise InvalidEvent("Handover successor must be a distinct active organization member.")

    expected_office_ids = sorted(
        office.office_id for office in governance.offices
        if office.holder_id == predecessor.id and is_leadership_office(office)
    )
    if event.transferred_office_ids != expected_office_ids:
        raise InvalidEvent("Handover must transfer exactly the incumbent's registered leadership offices.")
    offices = [
        office.model_copy(update={"holder_id": successor.id, "revision": office.revision + 1})
        if office.office_id in event.transferred_office_ids else office
        for office in governance.offices
    ]
    updated_governance = governance.model_copy(update={
        "leader_id": successor.id,
        "offices": offices,
        "revision": governance.revision + 1,
    })
    updated_organization = organization.model_copy(update={
        "components": {
            **organization.components,
            "governance": updated_governance.model_dump(mode="json"),
        }
    })
    return _validated_state(state, {"entities": {**state.entities, organization.id: updated_organization}})


def _apply_production_quota_renewal(state: WorldState, event: ProductionQuotaRenewedEvent) -> WorldState:
    producer = state.entities.get(event.producer_id)
    if producer is None or producer.status in {"dead", "retired"}:
        raise InvalidEvent("Quota renewal producer must be an active entity.")
    try:
        production = ProductionComponent.model_validate(producer.components.get("production"))
    except Exception as exc:
        raise InvalidEvent("Quota renewal producer has no valid production component.") from exc
    if production.revision != event.expected_production_revision:
        raise InvalidEvent("Quota renewal expected_production_revision does not match stored production.")
    quota_index = next((index for index, quota in enumerate(production.quotas) if quota.quota_id == event.quota_id), None)
    if quota_index is None:
        raise InvalidEvent("Quota renewal policy is not registered for this producer.")
    quota = production.quotas[quota_index]
    if event.period_index != state.current_tick // quota.period_ticks or state.current_tick % quota.period_ticks != 0:
        raise InvalidEvent("Quota renewal must occur at its exact registered period boundary in world seconds.")
    if event.period_index != quota.last_issued_period + 1:
        raise InvalidEvent("Quota renewal periods must be issued once, sequentially.")
    scheduled = next((item for item in state.scheduled_events if item.id == event.scheduled_event_id), None)
    if (
        scheduled is None
        or scheduled.status != "queued"
        or scheduled.due_tick != state.current_tick
        or scheduled.handler_id != "production.quota_renewal"
        or (scheduled.repeat_ticks is not None and scheduled.repeat_ticks != quota.period_ticks)
        or scheduled.payload.get("producer_id") != str(event.producer_id)
        or scheduled.payload.get("quota_id") != event.quota_id
        or any(
            next((item for item in state.scheduled_events if item.id == dependency), None) is None
            or next(item for item in state.scheduled_events if item.id == dependency).status != "completed"
            for dependency in scheduled.dependencies
        )
    ):
        raise InvalidEvent("Quota renewal must match one due registered schedule whose dependencies completed.")
    source = state.resources.get(quota.source_account_id)
    if (
        source is None
        or source.account_type != "production_source"
        or source.owner_id != producer.id
        or source.resource_id != quota.resource_id
        or source.credit_limit is None
    ):
        raise InvalidEvent("Quota renewal policy must match a finite producer-owned source account.")
    with localcontext() as context:
        context.prec = 80
        expected_limit = quota.base_credit_limit + quota.issued_total
        remaining = quota.maximum_renewal - quota.issued_total
        grant = min(quota.per_period, remaining)
        if source.credit_limit != expected_limit or grant <= 0:
            raise InvalidEvent("Quota source limit diverged or its finite renewal allowance is exhausted.")
        new_limit = source.credit_limit + grant
        new_issued = quota.issued_total + grant
    quota_values = list(production.quotas)
    quota_values[quota_index] = quota.model_copy(
        update={
            "issued_total": new_issued,
            "last_issued_period": event.period_index,
            "revision": quota.revision + 1,
        }
    )
    next_production = production.model_copy(
        update={"quotas": quota_values, "revision": production.revision + 1}
    )
    entities = {
        **state.entities,
        producer.id: producer.model_copy(
            update={"components": {**producer.components, "production": next_production.model_dump(mode="json")}}
        ),
    }
    resources = {
        **state.resources,
        quota.source_account_id: source.model_copy(update={"credit_limit": new_limit}),
    }
    return _validated_state(state, {"entities": entities, "resources": resources})


def _apply_renewable_resource_harvest(
    state: WorldState, event: RenewableResourceHarvestedEvent
) -> WorldState:
    """Distribute one finite scheduled yield to registered active characters."""
    period_ticks = state.calendar.day_seconds * 30
    if event.period_index != state.current_tick // period_ticks or state.current_tick % period_ticks != 0:
        raise InvalidEvent("Renewable harvest must occur at its exact 30-world-day period boundary.")
    scheduled = next((item for item in state.scheduled_events if item.id == event.scheduled_event_id), None)
    if (
        scheduled is None
        or scheduled.status != "queued"
        or scheduled.due_tick != state.current_tick
        or scheduled.handler_id != "environmental.harvest"
        or scheduled.handler_version != "1.0.0"
        or scheduled.payload
        or scheduled.repeat_ticks != period_ticks
        or scheduled.completed_runs + 1 != event.period_index
        or any(
            (dependency := next((item for item in state.scheduled_events if item.id == dependency_id), None)) is None
            or dependency.status != "completed"
            for dependency_id in scheduled.dependencies
        )
    ):
        raise InvalidEvent("Renewable harvest must match one due 30-day finite world-level schedule.")

    seat_plan = plan_population_seat_promotions(state, event.period_index)
    if list(seat_plan.promotions) != event.promotions:
        raise InvalidEvent("Renewable harvest seat promotions must match the finite regional vacancy and age-bucket policy.")
    state = seat_plan.state

    expected_policy_keys = [(item.region_id, item.policy_id) for item in event.items]
    actual_policies: list[tuple[WorldEntity, PlaceComponent, object]] = []
    for region in state.entities.values():
        raw_place = region.components.get("place")
        if not isinstance(raw_place, dict):
            continue
        try:
            place = PlaceComponent.model_validate(raw_place)
        except Exception as exc:
            raise InvalidEvent("A place has an invalid registered component.") from exc
        for policy in place.renewable_harvests:
            if policy.period_ticks != period_ticks:
                raise InvalidEvent("Renewable harvest policy period must be exactly 30 world days.")
            if policy.issued_total < policy.maximum_total:
                actual_policies.append((region, place, policy))
    actual_policies.sort(key=lambda entry: (str(entry[0].id), entry[2].policy_id))
    if expected_policy_keys != [(region.id, policy.policy_id) for region, _, policy in actual_policies]:
        raise InvalidEvent("Renewable harvest batch must include every nonexhausted policy in stable order.")

    entities = dict(state.entities)
    if _membership_recruitment_enabled(state):
        for promotion in event.promotions:
            promoted = entities.get(promotion.character.id)
            if promoted is None:
                raise InvalidEvent("Harvest promotion output disappeared before lineage recording.")
            try:
                life = LifeComponent.model_validate(promoted.components.get("life"))
            except Exception as exc:
                raise InvalidEvent("Harvest promotion output has an invalid life component.") from exc
            if life.population_origin is not None:
                raise InvalidEvent("Newly promoted residents cannot carry a preexisting population origin.")
            components = dict(promoted.components)
            components["life"] = life.model_copy(
                update={
                    "population_origin": PopulationOrigin(
                        source_event_id=event.event_id,
                        source_event_type=event.type,
                        population_group_id=life.population_group_id,
                    )
                }
            ).model_dump(mode="json")
            entities[promoted.id] = promoted.model_copy(update={"components": components})
    resources = dict(state.resources)
    place_updates: dict[UUID, tuple[PlaceComponent, list[object]]] = {}
    recipient_lots: dict[UUID, list[InventoryLot]] = {}
    recipient_inventories: dict[UUID, InventoryComponent] = {}
    required_accounts: dict[UUID, ResourceAccount] = {}
    account_deltas: dict[UUID, Decimal] = {}

    with localcontext() as context:
        context.prec = 80
        for (region, place, policy), expected in zip(actual_policies, event.items, strict=True):
            if (
                expected.region_id != region.id
                or expected.policy_id != policy.policy_id
                or expected.resource_id != policy.resource_id
                or expected.source_account_id != policy.source_account_id
                or expected.expected_place_revision != place.revision
                or expected.expected_policy_revision != policy.revision
            ):
                raise InvalidEvent("Renewable policy compare-and-swap data does not match stored state.")
            if policy.last_issued_period != event.period_index - 1:
                raise InvalidEvent("Renewable policies must issue each due period once and in sequence.")
            source = state.resources.get(policy.source_account_id)
            if (
                source is None
                or source.account_type != "environmental_source"
                or source.owner_id != region.id
                or source.resource_id != policy.resource_id
                or source.credit_limit is None
                or source.balance != expected.source_expected_balance
                or source.credit_limit != expected.source_expected_credit_limit
                or source.balance.copy_negate() != policy.issued_total
                or source.credit_limit != policy.issued_total
            ):
                raise InvalidEvent("Renewable source is stale or unauthorized for the registered region policy.")

            active_recipients = [
                recipient for recipient in policy.recipients
                if (target := state.entities.get(recipient.entity_id)) is not None
                and target.kind in {"character", "person"}
                and target.status not in {"dead", "retired"}
            ]
            active_recipients.sort(key=lambda item: str(item.entity_id))
            remaining = policy.maximum_total - policy.issued_total
            expected_allocations: list[tuple[object, Decimal]] = []
            for recipient in active_recipients:
                quantity = min(recipient.per_period, remaining)
                remaining -= quantity
                if quantity > 0:
                    expected_allocations.append((recipient, quantity))
            if [item.entity_id for item in expected.recipients] != [item.entity_id for item, _ in expected_allocations]:
                raise InvalidEvent("Renewable event must include every positive active named ration exactly once.")

            grant_total = Decimal("0")
            for (recipient, quantity), recipient_expectation in zip(
                expected_allocations, expected.recipients, strict=True
            ):
                target = state.entities.get(recipient.entity_id)
                account = state.resources.get(recipient.account_id)
                if (
                    target is None
                    or target.kind not in {"character", "person"}
                    or target.status in {"dead", "retired"}
                    or recipient_expectation.account_id != recipient.account_id
                    or recipient_expectation.quantity != quantity
                ):
                    raise InvalidEvent("Renewable recipient differs from the active typed ration policy.")
                if (
                    account is None
                    or account.account_type != "holder"
                    or account.owner_id != target.id
                    or account.resource_id != policy.resource_id
                    or account.balance != recipient_expectation.expected_balance
                ):
                    raise InvalidEvent("Renewable recipient account is stale or has the wrong owner/resource.")
                try:
                    inventory = InventoryComponent.model_validate(target.components.get("inventory"))
                except Exception as exc:
                    raise InvalidEvent("Renewable recipient requires a typed inventory component.") from exc
                if inventory.revision != recipient_expectation.expected_inventory_revision:
                    raise InvalidEvent("Renewable recipient inventory revision is stale.")
                lot_id = uuid5(
                    event.event_id,
                    f"harvest:{region.id}:{policy.policy_id}:{event.period_index}:{target.id}",
                )
                lot = InventoryLot(
                    lot_id=lot_id,
                    account_id=recipient.account_id,
                    resource_id=policy.resource_id,
                    quantity=quantity,
                    origin_event_id=event.event_id,
                )
                recipient_lots.setdefault(target.id, []).append(lot)
                recipient_inventories[target.id] = inventory
                required_accounts[recipient.account_id] = account
                account_deltas[recipient.account_id] = account_deltas.get(recipient.account_id, Decimal("0")) + quantity
                grant_total += quantity

            required_accounts[policy.source_account_id] = source
            account_deltas[policy.source_account_id] = account_deltas.get(policy.source_account_id, Decimal("0")) - grant_total
            issued_total = policy.issued_total + grant_total
            if issued_total > policy.maximum_total:
                raise InvalidEvent("Renewable event exceeds its finite total quota.")
            source_balance = source.balance - grant_total
            source_limit = source.credit_limit + grant_total
            if source_balance.copy_negate() != issued_total or source_limit != issued_total:
                raise InvalidEvent("Renewable issue would violate environmental source balance/limit invariants.")

            policy_index = next(
                index for index, current_policy in enumerate(place.renewable_harvests)
                if current_policy.policy_id == policy.policy_id
            )
            policies = place_updates.setdefault(region.id, (place, list(place.renewable_harvests)))[1]
            policies[policy_index] = policy.model_copy(
                update={
                    "issued_total": issued_total,
                    "last_issued_period": event.period_index,
                    "revision": policy.revision + 1,
                }
            )
            # Balances are applied once after all policies have contributed to
            # the aggregate, keeping a shared regional source CAS deterministic.

    for entity_id, lots in recipient_lots.items():
        inventory = recipient_inventories[entity_id]
        next_inventory = InventoryComponent(lots=[*inventory.lots, *lots], revision=inventory.revision + 1)
        # Promotions were inserted and may have received their reducer-owned
        # population origin above. Read the current working entity so adding
        # its ration lots does not replace that authoritative marker with the
        # pre-origin candidate from the seat planner's projected state.
        entity = entities[entity_id]
        entities[entity_id] = entity.model_copy(
            update={"components": {**entity.components, "inventory": next_inventory.model_dump(mode="json")}}
        )
    for region_id, (place, policies) in place_updates.items():
        region = state.entities[region_id]
        next_place = place.model_copy(update={"renewable_harvests": policies, "revision": place.revision + 1})
        entities[region_id] = region.model_copy(
            update={"components": {**region.components, "place": next_place.model_dump(mode="json")}}
        )
    for account_id, account in required_accounts.items():
        with localcontext() as context:
            context.prec = 80
            delta = account_deltas.get(account_id, Decimal("0"))
            next_balance = account.balance + delta
            account_update: dict[str, object] = {"balance": next_balance}
            if account.account_type == "environmental_source":
                if account.credit_limit is None:
                    raise InvalidEvent("Renewable environmental source is missing its finite credit limit.")
                account_update["credit_limit"] = account.credit_limit - delta
        resources[account_id] = account.model_copy(update=account_update)
    return _validated_state(state, {"entities": entities, "resources": resources})


def _apply_scheduled_event_completion(state: WorldState, event: ScheduledEventCompletedEvent, *, npc_review_validated=False) -> WorldState:
    scheduled_events = list(state.scheduled_events)
    index = next((i for i, item in enumerate(scheduled_events) if item.id == event.scheduled_event_id), None)
    if index is None:
        raise InvalidEvent("Completed scheduled event does not exist.")
    scheduled = scheduled_events[index]
    if scheduled.handler_id == "npc.goal_review" and not npc_review_validated:
        raise InvalidEvent("NPC plan reviews require their typed, validated review result.")
    if scheduled.handler_id == PRICE_REVIEW_HANDLER_ID:
        raise InvalidEvent("Market price reviews require the typed atomic review event.")
    if scheduled.revision != event.expected_revision:
        raise InvalidEvent("Scheduled completion expected_revision does not match stored state.")
    if scheduled.status != "queued" or scheduled.due_tick != state.current_tick:
        raise InvalidEvent("Only a scheduled event at its exact due tick can be completed.")
    by_id = {item.id: item for item in scheduled_events}
    if any(
        dependency not in by_id or by_id[dependency].status != "completed"
        for dependency in scheduled.dependencies
    ):
        raise InvalidEvent("A scheduled event cannot complete before its dependencies.")
    completed_runs = scheduled.completed_runs + 1
    repeats = scheduled.repeat_ticks is not None and completed_runs < scheduled.max_runs
    next_due_tick = scheduled.due_tick + scheduled.repeat_ticks if repeats else scheduled.due_tick
    if next_due_tick > 2**63 - 1:
        raise InvalidEvent("Recurring schedule exceeds the signed world-clock range.")
    replacement_data = scheduled.model_dump(mode="python") | {
        "status": "queued" if repeats else "completed",
        "due_tick": next_due_tick,
        "completed_runs": completed_runs,
        "revision": scheduled.revision + 1,
    }
    try:
        replacement = ScheduledEventRecord.model_validate(replacement_data)
    except Exception as exc:
        raise InvalidEvent(f"Scheduled completion produced an invalid schedule: {exc}") from exc

    # Completion changes only run state. These fields anchor schedule identity,
    # dependencies, handler policy, and the cross-record promise/shipment
    # references validated by WorldState; they must remain byte-for-byte equal.
    immutable_fields = (
        "id", "phase", "priority", "dependencies", "handler_id", "handler_version",
        "repeat_ticks", "max_runs", "payload",
    )
    if any(getattr(replacement, field) != getattr(scheduled, field) for field in immutable_fields):
        raise InvalidEvent("Scheduled completion changed immutable schedule policy.")
    if (
        replacement.status != ("queued" if repeats else "completed")
        or replacement.due_tick != next_due_tick
        or replacement.completed_runs != completed_runs
        or replacement.revision != scheduled.revision + 1
    ):
        raise InvalidEvent("Scheduled completion did not produce its expected run state.")

    # Promise deadline and shipment arrival/spoilage links require their own
    # registered handler IDs in WorldState validation. The two handlers below
    # cannot be referenced by those records, and their linked policy/component
    # data is untouched. Since the ID and dependencies above are also fixed,
    # a typed state copy is equivalent to a full WorldState reconstruction.
    if (scheduled.handler_id, scheduled.handler_version) in {
        ("population.year_advance", "1.0.0"),
        ("environmental.harvest", "1.0.0"),
    }:
        scheduled_events[index] = replacement
        return state.model_copy(update={"scheduled_events": scheduled_events})

    scheduled_events[index] = replacement
    return _validated_state(state, {"scheduled_events": scheduled_events})


def ledger_postings(event: DomainEvent) -> tuple[LedgerPosting, ...]:
    """Return resource ledger rows for an event, preserving zero-sum per resource."""
    if isinstance(event, (PromiseItemReturnedWithTrustEvent, PromiseItemReturnedLateEvent)):
        event = event.settlement
    if isinstance(event, WorkShiftPaidEvent):
        postings = tuple(LedgerPosting(entry.account_id, entry.resource_id, entry.amount) for entry in event.plan.ledger_entries)
    elif isinstance(event, NpcAttendanceSettledEvent):
        postings = (
            tuple(LedgerPosting(entry.account_id, entry.resource_id, entry.amount) for entry in event.payment.ledger_entries)
            if event.payment is not None else ()
        )
    elif isinstance(event, PromiseItemReturnedEvent):
        postings = tuple(LedgerPosting(item.account_id, item.resource_id, item.delta) for item in promise_ledger_postings(event))
    elif isinstance(event, PrivateGiftChangedEvent) and event.transfer is not None:
        transfer = event.transfer
        postings = (
            LedgerPosting(transfer.from_account_id, transfer.resource_id, transfer.quantity.copy_negate()),
            LedgerPosting(transfer.to_account_id, transfer.resource_id, transfer.quantity),
        )
    elif isinstance(event, InstitutionalCreditChangedEvent) and event.transfer is not None:
        transfer = event.transfer
        amount = Decimal(transfer.amount_minor)
        postings = (
            LedgerPosting(transfer.from_account_id, transfer.currency_resource_id, amount.copy_negate()),
            LedgerPosting(transfer.to_account_id, transfer.currency_resource_id, amount),
        )
    elif isinstance(event, InstitutionalTreasuryMobilizedEvent):
        postings = (
            *(LedgerPosting(leg.account_id, event.currency_resource_id, leg.quantity.copy_negate()) for leg in event.allocations),
            LedgerPosting(event.destination_account_id, event.currency_resource_id, Decimal(event.amount_minor)),
        )
    elif isinstance(event, ResourceTransferredEvent):
        postings = (
            LedgerPosting(event.from_account_id, event.resource_id, event.quantity.copy_negate()),
            LedgerPosting(event.to_account_id, event.resource_id, event.quantity),
        )
    elif isinstance(event, ShipmentReliefDistributedEvent):
        plan = event.plan
        postings = (
            LedgerPosting(plan.source_account_id, plan.resource_id, plan.quantity.copy_negate()),
            LedgerPosting(plan.recipient_account_id, plan.resource_id, plan.quantity),
        )
    elif isinstance(event, ResourceConsumedEvent):
        postings = tuple(
            posting
            for flow in event.flows
            for posting in (
                LedgerPosting(flow.holder_account_id, flow.resource_id, flow.quantity.copy_negate()),
                LedgerPosting(flow.sink_account_id, flow.resource_id, flow.quantity),
            )
        )
    elif isinstance(event, BackgroundNeedsAdvancedEvent):
        postings = tuple(
            posting
            for agent in event.expected_agents
            if agent.action in {"eat", "drink"}
            for lot in agent.consumption_lots
            for posting in (
                LedgerPosting(lot.account_id, agent.resource_id, lot.quantity.copy_negate()),
                LedgerPosting(agent.consumption_sink_account_id, agent.resource_id, lot.quantity),
            )
        )
    elif isinstance(event, BackgroundRoutineAdvancedEvent):
        postings = tuple(
            posting
            for agent in event.expected_agents
            for care in (agent.water, agent.food)
            if care is not None and care.action_count > 0 and care.sink_account_id is not None
            for lot in care.consumption_lots
            for posting in (
                LedgerPosting(lot.account_id, care.resource_id, lot.quantity.copy_negate()),
                LedgerPosting(care.sink_account_id, care.resource_id, lot.quantity),
            )
        )
    elif isinstance(event, ResourceProducedEvent):
        postings = tuple(
            posting
            for flow in event.consumed_inputs
            for posting in (
                LedgerPosting(flow.holder_account_id, flow.resource_id, flow.quantity.copy_negate()),
                LedgerPosting(flow.sink_account_id, flow.resource_id, flow.quantity),
            )
        ) + tuple(
            posting
            for flow in event.produced_outputs
            for posting in (
                LedgerPosting(flow.source_account_id, flow.resource_id, flow.quantity.copy_negate()),
                LedgerPosting(flow.holder_account_id, flow.resource_id, flow.quantity),
            )
        )
    elif isinstance(event, RenewableResourceHarvestedEvent):
        postings = tuple(
            posting
            for item in event.items
            for recipient in item.recipients
            for posting in (
                LedgerPosting(item.source_account_id, item.resource_id, recipient.quantity.copy_negate()),
                LedgerPosting(recipient.account_id, item.resource_id, recipient.quantity),
            )
        )
    elif isinstance(event, MarketTradeExecutedEvent):
        with localcontext() as context:
            context.prec = 80
            payment = Decimal(event.unit_price_minor) * event.quantity
        if payment != payment.to_integral_value():
            raise InvalidEvent("Market payment must resolve to an integer number of minor currency units.")
        postings = (
            LedgerPosting(event.stock_account_id, event.resource_id, event.quantity.copy_negate()),
            LedgerPosting(event.buyer_resource_account_id, event.resource_id, event.quantity),
            LedgerPosting(event.buyer_currency_account_id, event.currency_resource_id, payment.copy_negate()),
            LedgerPosting(event.seller_currency_account_id, event.currency_resource_id, payment),
        )
    elif isinstance(event, ShipmentDispatchedEvent):
        postings = (
            LedgerPosting(event.source_account_id, event.resource_id, event.quantity.copy_negate()),
            LedgerPosting(event.transit_account_id, event.resource_id, event.quantity),
        )
    elif isinstance(event, (ShipmentArrivalDeliveredEvent, ShipmentReceiptAcceptedEvent)):
        postings = tuple(
            posting
            for flow in event.flows
            for posting in (
                LedgerPosting(flow.transit_account_id, flow.resource_id, flow.quantity.copy_negate()),
                LedgerPosting(flow.destination_account_id, flow.resource_id, flow.quantity),
            )
        )
    elif isinstance(event, ShipmentSpoilageResolvedEvent):
        postings = tuple(
            posting
            for lot in event.spoiled_lots
            for posting in (
                LedgerPosting(event.transit_account_id, event.resource_id, lot.quantity.copy_negate()),
                LedgerPosting(lot.destruction_sink_account_id, event.resource_id, lot.quantity),
            )
        )
    else:
        return ()
    with localcontext() as context:
        context.prec = _ledger_precision(postings)
        for resource_id in {posting.resource_id for posting in postings}:
            total = sum(
                (posting.delta for posting in postings if posting.resource_id == resource_id),
                Decimal("0"),
            )
            if total != 0:
                raise InvalidEvent(f"Resource ledger postings for {resource_id} must sum to zero.")
    return postings


def _apply_market_trade(state: WorldState, event: MarketTradeExecutedEvent) -> WorldState:
    """Atomically settle one listing against account balances and inventory lots."""
    buyer = state.entities.get(event.buyer_id)
    seller = state.entities.get(event.seller_id)
    market_entity = state.entities.get(event.market_id)
    if buyer is None or buyer.kind not in {"character", "person"} or buyer.status != "active":
        raise InvalidEvent("Market buyer must be an active character.")
    if seller is None or seller.status != "active" or seller.id == buyer.id:
        raise InvalidEvent("Market seller must be another active entity.")
    if market_entity is None or market_entity.status != "active":
        raise InvalidEvent("Market must be an active registered entity.")
    if buyer.location_id != event.market_id or seller.location_id != event.market_id:
        raise InvalidEvent("Both buyer and seller must be present at the market.")
    try:
        market = MarketComponent.model_validate(market_entity.components.get("market"))
        seller_inventory = InventoryComponent.model_validate(seller.components.get("inventory"))
        buyer_inventory_raw = buyer.components.get("inventory")
        buyer_inventory = InventoryComponent.model_validate(buyer_inventory_raw) if buyer_inventory_raw is not None else None
    except Exception as exc:
        raise InvalidEvent("Trade participants or market have invalid registered components.") from exc
    offer = next((item for item in market.offers if item.listing_id == event.listing_id), None)
    if (
        offer is None
        or not offer.active
        or offer.seller_id != event.seller_id
        or offer.resource_id != event.resource_id
        or offer.stock_account_id != event.stock_account_id
        or market.currency_resource_id != event.currency_resource_id
        or offer.unit_price_minor != event.unit_price_minor
        or event.quantity < offer.minimum_quantity
    ):
        raise InvalidEvent("Trade does not match an active registered market offer.")

    if event.expected_seller_inventory_revision != seller_inventory.revision:
        raise InvalidEvent("Trade seller inventory revision does not match stored inventory.")
    current_buyer_revision = buyer_inventory.revision if buyer_inventory is not None else None
    if event.expected_buyer_inventory_revision != current_buyer_revision:
        raise InvalidEvent("Trade buyer inventory revision does not match stored inventory.")

    stock = state.resources.get(event.stock_account_id)
    buyer_goods = state.resources.get(event.buyer_resource_account_id)
    buyer_money = state.resources.get(event.buyer_currency_account_id)
    seller_money = state.resources.get(event.seller_currency_account_id)
    expected_accounts = (
        (stock, event.resource_id, "holder", event.seller_id, "market stock"),
        (buyer_goods, event.resource_id, "holder", event.buyer_id, "buyer goods"),
        (buyer_money, event.currency_resource_id, "holder", event.buyer_id, "buyer currency"),
        (seller_money, event.currency_resource_id, "holder", event.seller_id, "seller currency"),
    )
    for account, resource_id, account_type, owner_id, label in expected_accounts:
        if (
            account is None
            or account.resource_id != resource_id
            or account.account_type != account_type
            or account.owner_id != owner_id
        ):
            raise InvalidEvent(f"Trade {label} account is missing or has the wrong owner/resource.")

    eligible = [
        lot for lot in seller_inventory.lots
        if lot.account_id == event.stock_account_id
        and lot.resource_id == event.resource_id
        and (lot.expiry_tick is None or lot.expiry_tick > state.current_tick)
    ]
    remaining = event.quantity
    sold_ids: list[UUID] = []
    seller_lots: list[InventoryLot] = []
    with localcontext() as decimal_context:
        decimal_context.prec = 50
        for lot in seller_inventory.lots:
            if lot not in eligible or remaining <= 0:
                seller_lots.append(lot)
                continue
            taken = min(lot.quantity, remaining)
            if taken <= 0:
                seller_lots.append(lot)
                continue
            sold_ids.append(lot.lot_id)
            remaining -= taken
            if taken < lot.quantity:
                seller_lots.append(lot.model_copy(update={"quantity": lot.quantity - taken, "revision": lot.revision + 1}))
        if remaining != 0:
            raise InsufficientResources("The seller does not have enough unexpired listed stock lots.")
        if sold_ids != event.source_lot_ids:
            raise InvalidEvent("Trade source_lot_ids must identify the exact deterministic FIFO lots sold.")

    buyer_lot_ids = {lot.lot_id for lot in (buyer_inventory.lots if buyer_inventory else [])}
    if event.buyer_lot_id in buyer_lot_ids:
        raise InvalidEvent("Trade buyer_lot_id already exists in buyer inventory.")
    if event.resource_id == event.currency_resource_id:
        raise InvalidEvent("A market listing cannot exchange a resource for itself.")
    with localcontext() as decimal_context:
        decimal_context.prec = 80
        payment = Decimal(event.unit_price_minor) * event.quantity

    # Currency may also have an inventory subledger. Keep those lots in sync
    # with the debited buyer balance and represent seller proceeds as a new lot.
    buyer_lots = list(buyer_inventory.lots if buyer_inventory else [])
    remaining_payment = payment
    paid_currency_lots: list[InventoryLot] = []
    with localcontext() as decimal_context:
        decimal_context.prec = 80
        for lot in buyer_lots:
            if (
                remaining_payment > 0
                and lot.account_id == event.buyer_currency_account_id
                and lot.resource_id == event.currency_resource_id
                and (lot.expiry_tick is None or lot.expiry_tick > state.current_tick)
            ):
                taken = min(lot.quantity, remaining_payment)
                remaining_payment -= taken
                if taken < lot.quantity:
                    paid_currency_lots.append(
                        lot.model_copy(update={"quantity": lot.quantity - taken, "revision": lot.revision + 1})
                    )
            else:
                paid_currency_lots.append(lot)
        buyer_lot = InventoryLot(
            lot_id=event.buyer_lot_id,
            account_id=event.buyer_resource_account_id,
            resource_id=event.resource_id,
            quantity=event.quantity,
            quality=min((lot.quality for lot in eligible if lot.lot_id in sold_ids), default=100),
            origin_event_id=event.event_id,
        )
        next_buyer_inventory = InventoryComponent(
            revision=(buyer_inventory.revision + 1 if buyer_inventory else 1),
            lots=[*paid_currency_lots, buyer_lot],
        )
        seller_lot_id = uuid5(event.event_id, "market.seller_currency_lot")
        if payment > 0:
            if any(lot.lot_id == seller_lot_id for lot in seller_lots):
                raise InvalidEvent("Trade seller currency lot id already exists in seller inventory.")
            seller_lots.append(
                InventoryLot(
                    lot_id=seller_lot_id,
                    account_id=event.seller_currency_account_id,
                    resource_id=event.currency_resource_id,
                    quantity=payment,
                    origin_event_id=event.event_id,
                )
            )
        next_seller_inventory = seller_inventory.model_copy(
            update={"revision": seller_inventory.revision + 1, "lots": seller_lots}
        )

    postings = ledger_postings(event)
    validate_ledger_postings(postings)
    deltas: dict[UUID, Decimal] = {}
    with localcontext() as decimal_context:
        decimal_context.prec = _ledger_precision(postings)
        for posting in postings:
            deltas[posting.account_id] = deltas.get(posting.account_id, Decimal("0")) + posting.delta
        resources = dict(state.resources)
        for account_id, delta in deltas.items():
            account = state.resources[account_id]
            balance = account.balance + delta
            if account.account_type == "holder" and balance < 0:
                raise InsufficientResources(f"Account {account_id} has insufficient {account.resource_id}.")
            resources[account_id] = account.model_copy(update={"balance": balance})

    entities = dict(state.entities)
    entities[seller.id] = seller.model_copy(
        update={"components": {**seller.components, "inventory": next_seller_inventory.model_dump(mode="json")}}
    )
    entities[buyer.id] = buyer.model_copy(
        update={"components": {**buyer.components, "inventory": next_buyer_inventory.model_dump(mode="json")}}
    )
    return _validated_state(state, {"entities": entities, "resources": resources})


def _apply_shipment_dispatched(state: WorldState, event: ShipmentDispatchedEvent) -> WorldState:
    if state.current_tick != event.departure_tick:
        raise InvalidEvent("Shipment dispatch must occur at its recorded departure tick.")
    if event.shipment_id in state.entities or event.transit_account_id in state.resources:
        raise InvalidEvent("Shipment entity or transit account already exists.")
    if any(item.id == event.arrival_schedule_id for item in state.scheduled_events):
        raise InvalidEvent("Shipment arrival schedule already exists.")
    sender = state.entities.get(event.sender_id)
    origin = state.entities.get(event.origin_place_id)
    destination = state.entities.get(event.destination_place_id)
    receiver = state.entities.get(event.destination_owner_id)
    if sender is None or sender.kind not in {"character", "person"} or sender.status != "active":
        raise InvalidEvent("Shipment sender must be an active character.")
    if event.source_owner_id != sender.id or sender.location_id != event.origin_place_id:
        raise InvalidEvent("Shipment source must be the sender's current physical location and stock owner.")
    if origin is None or origin.kind not in {"place", "location"}:
        raise InvalidEvent("Shipment origin must be a registered place.")
    if destination is None or destination.kind not in {"place", "location"} or destination.status != "active":
        raise InvalidEvent("Shipment destination must be an active registered place.")
    if not shipment_destination_available(state, event.destination_owner_id, event.destination_place_id):
        raise InvalidEvent("The named shipment receiver must be present at dispatch.")
    try:
        origin_place = PlaceComponent.model_validate(origin.components.get("place"))
    except Exception as exc:
        raise InvalidEvent("Shipment origin has no valid registered route map.") from exc
    route = next((item for item in origin_place.routes if item.route_id == event.route_id), None)
    if (
        route is None
        or not route.is_open
        or route.from_place_id != event.origin_place_id
        or route.to_place_id != event.destination_place_id
        or event.arrival_tick - event.departure_tick != route.travel_ticks
    ):
        raise InvalidEvent("Shipment must use an open registered route and its exact duration.")
    if route.capacity is not None and active_shipments_on_route(state, route.route_id) >= route.capacity:
        raise InvalidEvent("The registered route has no free shipment slot.")
    source_account = state.resources.get(event.source_account_id)
    destination_account = state.resources.get(event.destination_account_id)
    if (
        source_account is None
        or source_account.account_type != "holder"
        or source_account.owner_id != sender.id
        or source_account.resource_id != event.resource_id
        or source_account.balance != event.expected_source_account_balance
        or source_account.balance < event.quantity
    ):
        raise InvalidEvent("Shipment source holder account is missing, stale, or underfunded.")
    if (
        destination_account is None
        or destination_account.account_type != "holder"
        or destination_account.owner_id != event.destination_owner_id
        or destination_account.resource_id != event.resource_id
        or event.destination_account_id in {event.source_account_id, event.transit_account_id}
    ):
        raise InvalidEvent("Shipment destination account is missing or has the wrong owner/resource.")
    if event.transit_account_id == event.source_account_id or event.transit_account_id == event.destination_account_id:
        raise InvalidEvent("Shipment source, transit, and destination accounts must be distinct.")
    try:
        source_inventory = InventoryComponent.model_validate(sender.components.get("inventory"))
        InventoryComponent.model_validate(receiver.components.get("inventory"))
    except Exception as exc:
        raise InvalidEvent("Shipment source and receiver must have valid typed inventories.") from exc
    if source_inventory.revision != event.expected_source_inventory_revision:
        raise InvalidEvent("Shipment source inventory expected_revision does not match stored inventory.")
    try:
        allocations = plan_dispatch_lots(
            source_inventory,
            source_account_id=event.source_account_id,
            resource_id=event.resource_id,
            quantity=event.quantity,
            arrival_tick=event.arrival_tick,
        )
    except Exception as exc:
        raise InvalidEvent("Shipment must be backed by exact FIFO lots that remain unexpired at arrival.") from exc
    if len(allocations) != len(event.lot_transfers):
        raise InvalidEvent("Shipment lot manifest does not match the exact FIFO allocation.")
    for allocated, supplied in zip(allocations, event.lot_transfers, strict=True):
        if allocated.lot_id != supplied.source_lot_id or allocated.quantity != supplied.quantity:
            raise InvalidEvent("Shipment lot manifest does not match the exact FIFO allocation.")
    if len({lot.transit_lot_id for lot in event.lot_transfers}) != len(event.lot_transfers):
        raise InvalidEvent("Shipment transit lot IDs must be unique.")

    original_lots = {lot.lot_id: lot for lot in source_inventory.lots}
    transit_lots = [
        InventoryLot(
            lot_id=transfer.transit_lot_id,
            account_id=event.transit_account_id,
            resource_id=event.resource_id,
            quantity=transfer.quantity,
            quality=original_lots[transfer.source_lot_id].quality,
            expiry_tick=original_lots[transfer.source_lot_id].expiry_tick,
            origin_event_id=event.event_id,
        )
        for transfer in event.lot_transfers
    ]
    expected_spoilage_due = min(
        (lot.expiry_tick for lot in transit_lots if lot.expiry_tick is not None),
        default=None,
    )
    if event.lifecycle_version == "expiry_v2":
        if event.spoilage_due_tick != expected_spoilage_due or (
            (event.spoilage_schedule_id is None) != (expected_spoilage_due is None)
        ):
            raise InvalidEvent("Shipment dispatch must schedule its exact earliest in-transit lot expiry.")
    elif event.spoilage_schedule_id is not None or event.spoilage_due_tick is not None:
        raise InvalidEvent("Legacy shipment dispatch replay cannot introduce spoilage schedules.")
    shipment = ShipmentComponent(
        dispatch_event_id=event.event_id,
        arrival_schedule_id=event.arrival_schedule_id,
        sender_id=event.sender_id,
        source_owner_id=event.source_owner_id,
        destination_owner_id=event.destination_owner_id,
        origin_place_id=event.origin_place_id,
        destination_place_id=event.destination_place_id,
        route_id=event.route_id,
        departure_tick=event.departure_tick,
        arrival_tick=event.arrival_tick,
        lifecycle_version=event.lifecycle_version,
        spoilage_schedule_id=event.spoilage_schedule_id,
        cargo=[ShipmentCargoManifest(
            resource_id=event.resource_id,
            source_account_id=event.source_account_id,
            transit_account_id=event.transit_account_id,
            destination_account_id=event.destination_account_id,
            quantity=event.quantity,
            lots=event.lot_transfers,
        )],
    )
    shipment_entity = WorldEntity(
        id=event.shipment_id,
        kind="shipment",
        name=f"Shipment {event.shipment_id.hex[:8]}",
        status="active",
        location_id=None,
        components={
            "shipment": shipment.model_dump(mode="json"),
            "inventory": InventoryComponent(lots=transit_lots).model_dump(mode="json"),
            "hidden": True,
        },
    )
    schedule = ScheduledEventRecord(
        id=event.arrival_schedule_id,
        due_tick=event.arrival_tick,
        phase="delivery",
        handler_id="shipment.arrival",
        handler_version="1.0.0",
        max_runs=1,
        payload={"shipment_id": str(event.shipment_id)},
    )
    added_schedules = [schedule]
    if event.spoilage_schedule_id is not None and event.spoilage_due_tick is not None:
        added_schedules.append(ScheduledEventRecord(
            id=event.spoilage_schedule_id,
            due_tick=event.spoilage_due_tick,
            phase="delivery",
            dependencies=[event.arrival_schedule_id],
            handler_id="shipment.spoilage",
            handler_version="1.0.0",
            max_runs=1,
            payload={"shipment_id": str(event.shipment_id)},
        ))
    reduced_source_inventory = reduce_fifo_inventory(
        source_inventory,
        [FifoLotAllocation(item.source_lot_id, event.source_account_id, item.quantity) for item in event.lot_transfers],
    )
    updated_sender = sender.model_copy(update={
        "components": {**sender.components, "inventory": reduced_source_inventory.model_dump(mode="json")}
    })
    resources = dict(state.resources)
    resources[event.source_account_id] = source_account.model_copy(
        update={"balance": source_account.balance - event.quantity}
    )
    resources[event.transit_account_id] = ResourceAccount(
        resource_id=event.resource_id,
        balance=event.quantity,
        owner_id=event.shipment_id,
        account_type="holder",
    )
    entities = {**state.entities, sender.id: updated_sender, event.shipment_id: shipment_entity}
    return _validated_state(state, {
        "entities": entities,
        "resources": resources,
        "scheduled_events": [*state.scheduled_events, *added_schedules],
    })


def _validate_shipment_arrival_schedule(
    state: WorldState,
    *,
    shipment_id: UUID,
    shipment: ShipmentComponent,
    schedule_id: UUID,
    expected_schedule_revision: int,
) -> tuple[list[ScheduledEventRecord], int]:
    schedules = list(state.scheduled_events)
    index = next((i for i, item in enumerate(schedules) if item.id == schedule_id), None)
    if index is None:
        raise InvalidEvent("Shipment arrival schedule does not exist.")
    schedule = schedules[index]
    if (
        schedule.revision != expected_schedule_revision
        or schedule.status != "queued"
        or schedule.phase != "delivery"
        or schedule.handler_id != "shipment.arrival"
        or schedule.handler_version != "1.0.0"
        or schedule.due_tick != state.current_tick
        or schedule.due_tick != shipment.arrival_tick
        or schedule.max_runs != 1
        or schedule.repeat_ticks is not None
        or schedule.completed_runs != 0
        or schedule.payload != {"shipment_id": str(shipment_id)}
    ):
        raise InvalidEvent("Shipment arrival schedule is stale or does not match its exact delivery boundary.")
    by_id = {item.id: item for item in schedules}
    if any(by_id[dependency].status != "completed" for dependency in schedule.dependencies):
        raise InvalidEvent("Shipment delivery cannot precede its scheduled dependencies.")
    schedules[index] = schedule.model_copy(update={
        "status": "completed",
        "completed_runs": 1,
        "revision": schedule.revision + 1,
    })
    return schedules, index


def _apply_shipment_arrival_deferred(state: WorldState, event: ShipmentArrivalDeferredEvent) -> WorldState:
    shipment_entity = state.entities.get(event.shipment_id)
    if shipment_entity is None or shipment_entity.kind != "shipment":
        raise InvalidEvent("Deferred shipment does not exist.")
    try:
        shipment = ShipmentComponent.model_validate(shipment_entity.components.get("shipment"))
    except Exception as exc:
        raise InvalidEvent("Deferred shipment component is invalid.") from exc
    if (
        shipment.revision != event.expected_shipment_revision
        or shipment.status != "in_transit"
        or shipment.arrival_tick != event.arrival_tick
        or event.arrival_tick != state.current_tick
        or shipment.arrival_schedule_id != event.scheduled_event_id
        or shipment_destination_available(state, shipment.destination_owner_id, shipment.destination_place_id)
    ):
        raise InvalidEvent("Shipment deferral requires its exact due tick and an unavailable receiver.")
    schedules, _ = _validate_shipment_arrival_schedule(
        state,
        shipment_id=shipment_entity.id,
        shipment=shipment,
        schedule_id=event.scheduled_event_id,
        expected_schedule_revision=event.expected_schedule_revision,
    )
    next_shipment = shipment.model_copy(update={
        "status": "awaiting_receipt",
        "arrived_tick": state.current_tick,
        "revision": shipment.revision + 1,
    })
    updated = shipment_entity.model_copy(update={
        "location_id": shipment.destination_place_id,
        "components": {**shipment_entity.components, "shipment": next_shipment.model_dump(mode="json")},
    })
    return _validated_state(state, {
        "entities": {**state.entities, shipment_entity.id: updated},
        "scheduled_events": schedules,
    })


def _apply_shipment_arrival_delivered(state: WorldState, event: ShipmentArrivalDeliveredEvent) -> WorldState:
    shipment_entity = state.entities.get(event.shipment_id)
    if shipment_entity is None or shipment_entity.kind != "shipment":
        raise InvalidEvent("Delivered shipment does not exist.")
    try:
        shipment = ShipmentComponent.model_validate(shipment_entity.components.get("shipment"))
    except Exception as exc:
        raise InvalidEvent("Delivered shipment component is invalid.") from exc
    if (
        shipment.revision != event.expected_shipment_revision
        or shipment.status != "in_transit"
        or shipment.arrival_tick != event.arrival_tick
        or event.arrival_tick != state.current_tick
        or shipment.arrival_schedule_id != event.scheduled_event_id
        or not shipment_destination_available(state, shipment.destination_owner_id, shipment.destination_place_id)
    ):
        raise InvalidEvent("Shipment delivery requires its exact due tick and a present receiver.")
    return _apply_shipment_receipt(
        state,
        shipment_entity=shipment_entity,
        shipment=shipment,
        receiver_id=shipment.destination_owner_id,
        expected_transit_inventory_revision=event.expected_transit_inventory_revision,
        expected_destination_inventory_revision=event.expected_destination_inventory_revision,
        expected_destination_account_balance=event.expected_destination_account_balance,
        expected_spoilage_schedule_revision=event.expected_spoilage_schedule_revision,
        received_tick=event.arrival_tick,
        flows=event.flows,
        receipts=event.receipts,
        expected_status="in_transit",
        schedule_id=event.scheduled_event_id,
        expected_schedule_revision=event.expected_schedule_revision,
    )


def _apply_shipment_receipt_accepted(state: WorldState, event: ShipmentReceiptAcceptedEvent) -> WorldState:
    shipment_entity = state.entities.get(event.shipment_id)
    if shipment_entity is None or shipment_entity.kind != "shipment":
        raise InvalidEvent("Received shipment does not exist.")
    try:
        shipment = ShipmentComponent.model_validate(shipment_entity.components.get("shipment"))
    except Exception as exc:
        raise InvalidEvent("Received shipment component is invalid.") from exc
    if (
        shipment.revision != event.expected_shipment_revision
        or shipment.status != "awaiting_receipt"
        or event.received_tick != state.current_tick
        or shipment.arrived_tick is None
        or event.receiver_id != shipment.destination_owner_id
        or not shipment_destination_available(state, event.receiver_id, shipment.destination_place_id)
    ):
        raise InvalidEvent("Manual shipment receipt requires its designated present receiver.")
    return _apply_shipment_receipt(
        state,
        shipment_entity=shipment_entity,
        shipment=shipment,
        receiver_id=event.receiver_id,
        expected_transit_inventory_revision=event.expected_transit_inventory_revision,
        expected_destination_inventory_revision=event.expected_destination_inventory_revision,
        expected_destination_account_balance=event.expected_destination_account_balance,
        expected_spoilage_schedule_revision=event.expected_spoilage_schedule_revision,
        received_tick=event.received_tick,
        flows=event.flows,
        receipts=event.receipts,
        expected_status="awaiting_receipt",
        schedule_id=None,
        expected_schedule_revision=None,
    )


def _apply_shipment_relief(state: WorldState, event: ShipmentReliefDistributedEvent) -> WorldState:
    recorded = event.plan
    plan = plan_shipment_relief(
        state, actor_id=event.actor_id, organization_id=recorded.organization_id,
        shipment_id=recorded.shipment_id, recipient_id=recorded.recipient_id, quantity=recorded.quantity,
    )
    if plan != recorded:
        raise InvalidEvent("Stored shipment relief does not match the authorized stock allocation.")
    depot = state.entities[plan.depot_id]
    recipient = state.entities[plan.recipient_id]
    depot_inventory = InventoryComponent.model_validate(depot.components["inventory"])
    recipient_inventory = InventoryComponent.model_validate(recipient.components.get("inventory", {}))
    lots = {lot.lot_id: lot for lot in depot_inventory.lots}
    next_depot = reduce_fifo_inventory(depot_inventory, [
        FifoLotAllocation(row.destination_lot_id, row.account_id, row.quantity) for row in plan.allocations
    ])
    received = [
        InventoryLot(
            lot_id=uuid5(event.event_id, f"relief:{row.destination_lot_id}"),
            account_id=plan.recipient_account_id, resource_id=plan.resource_id, quantity=row.quantity,
            quality=lots[row.destination_lot_id].quality,
            expiry_tick=lots[row.destination_lot_id].expiry_tick, origin_event_id=event.event_id,
        )
        for row in plan.allocations
    ]
    next_recipient = InventoryComponent(
        lots=[*recipient_inventory.lots, *received],
        revision=recipient_inventory.revision + 1 if plan.expected_recipient_inventory_revision is not None else 1,
    )
    entities = dict(state.entities)
    for entity, inventory in ((depot, next_depot), (recipient, next_recipient)):
        entities[entity.id] = entity.model_copy(update={
            "components": {**entity.components, "inventory": inventory.model_dump(mode="json")},
        })
    resources = dict(state.resources)
    with localcontext() as context:
        context.prec = 80
        source, sink = resources[plan.source_account_id], resources[plan.recipient_account_id]
        resources[plan.source_account_id] = source.model_copy(update={"balance": source.balance - plan.quantity})
        resources[plan.recipient_account_id] = sink.model_copy(update={"balance": sink.balance + plan.quantity})
    return _validated_state(state, {"entities": entities, "resources": resources})


def _apply_shipment_receipt(
    state: WorldState,
    *,
    shipment_entity: WorldEntity,
    shipment: ShipmentComponent,
    receiver_id: UUID,
    expected_transit_inventory_revision: int,
    expected_destination_inventory_revision: int,
    expected_destination_account_balance: Decimal,
    expected_spoilage_schedule_revision: int | None,
    received_tick: int,
    flows,
    receipts,
    expected_status: str,
    schedule_id: UUID | None,
    expected_schedule_revision: int | None,
) -> WorldState:
    if len(shipment.cargo) != 1 or len(flows) != 1:
        raise InvalidEvent("This shipment receipt handler accepts one registered resource manifest.")
    cargo = shipment.cargo[0]
    flow = flows[0]
    remaining_quantity = shipment.remaining_quantity
    if (
        flow.resource_id != cargo.resource_id
        or flow.transit_account_id != cargo.transit_account_id
        or flow.destination_account_id != cargo.destination_account_id
        or flow.quantity != remaining_quantity
    ):
        raise InvalidEvent("Shipment receipt account flow does not match its stored manifest.")
    receiver = state.entities.get(receiver_id)
    if receiver is None:
        raise InvalidEvent("Shipment receiver does not exist.")
    try:
        transit_inventory = InventoryComponent.model_validate(shipment_entity.components.get("inventory"))
        destination_inventory = InventoryComponent.model_validate(receiver.components.get("inventory"))
    except Exception as exc:
        raise InvalidEvent("Shipment or receiver inventory is invalid.") from exc
    if transit_inventory.revision != expected_transit_inventory_revision:
        raise InvalidEvent("Shipment transit inventory expected_revision does not match stored inventory.")
    if destination_inventory.revision != expected_destination_inventory_revision:
        raise InvalidEvent("Shipment destination inventory expected_revision does not match stored inventory.")
    transit_account = state.resources.get(cargo.transit_account_id)
    destination_account = state.resources.get(cargo.destination_account_id)
    if (
        transit_account is None
        or transit_account.account_type != "holder"
        or transit_account.owner_id != shipment_entity.id
        or transit_account.resource_id != cargo.resource_id
        or transit_account.balance != remaining_quantity
    ):
        raise InvalidEvent("Shipment transit account is missing or does not hold the exact manifest.")
    if (
        destination_account is None
        or destination_account.account_type != "holder"
        or destination_account.owner_id != receiver_id
        or destination_account.resource_id != cargo.resource_id
        or destination_account.balance != expected_destination_account_balance
    ):
        raise InvalidEvent("Shipment destination holder account is missing or stale.")
    if destination_account.balance + remaining_quantity > 10**30 - 1:
        raise InvalidEvent("Shipment receipt exceeds the destination resource account limit.")
    transit_by_id = {lot.lot_id: lot for lot in transit_inventory.lots}
    expected_receipts: list[tuple[UUID, Decimal]] = []
    remaining_lot_quantities = shipment.remaining_lot_quantities()
    for manifest_lot in cargo.lots:
        lot_remaining_quantity = remaining_lot_quantities.get(manifest_lot.transit_lot_id)
        if lot_remaining_quantity is None:
            continue
        transit_lot = transit_by_id.get(manifest_lot.transit_lot_id)
        if (
            transit_lot is None
            or transit_lot.account_id != cargo.transit_account_id
            or transit_lot.resource_id != cargo.resource_id
            or transit_lot.quantity != lot_remaining_quantity
        ):
            raise InvalidEvent("Shipment transit lot does not match the stored FIFO manifest.")
        if transit_lot.expiry_tick is not None and transit_lot.expiry_tick <= state.current_tick:
            raise InvalidEvent("Expired shipment lots require an explicit spoilage resolution before receipt.")
        expected_receipts.append((transit_lot.lot_id, lot_remaining_quantity))
    actual_receipts = [(item.transit_lot_id, item.quantity) for item in receipts]
    if actual_receipts != expected_receipts:
        raise InvalidEvent("Shipment receipt lots must match the exact stored transit FIFO manifest.")
    existing_lot_ids = {lot.lot_id for lot in destination_inventory.lots}
    if any(item.destination_lot_id in existing_lot_ids for item in receipts):
        raise InvalidEvent("Shipment receipt destination lot id already exists.")
    destination_lot_ids = {item.transit_lot_id: item.destination_lot_id for item in receipts}
    new_lots = [
        InventoryLot(
            lot_id=destination_lot_ids[lot.lot_id],
            account_id=cargo.destination_account_id,
            resource_id=lot.resource_id,
            quantity=lot.quantity,
            quality=lot.quality,
            expiry_tick=lot.expiry_tick,
            origin_event_id=shipment.dispatch_event_id,
        )
        for lot in transit_inventory.lots
    ]
    new_destination_inventory = destination_inventory.model_copy(update={
        "lots": [*destination_inventory.lots, *new_lots],
        "revision": destination_inventory.revision + 1,
    })
    new_transit_inventory = transit_inventory.model_copy(update={
        "lots": [],
        "revision": transit_inventory.revision + 1,
    })
    shipment_update: dict[str, object] = {
        "status": "delivered",
        "arrived_tick": shipment.arrival_tick,
        "revision": shipment.revision + 1,
    }
    if shipment.lifecycle_version == "expiry_v2":
        shipment_update["delivered_lots"] = [
            ShipmentDeliveredLot(
                transit_lot_id=item.transit_lot_id,
                destination_lot_id=item.destination_lot_id,
                quantity=item.quantity,
                received_tick=received_tick,
            )
            for item in sorted(receipts, key=lambda row: str(row.transit_lot_id))
        ]
        shipment_update["spoilage_schedule_id"] = None
    next_shipment = shipment.model_copy(update=shipment_update)
    entities = dict(state.entities)
    entities[receiver_id] = receiver.model_copy(update={
        "components": {**receiver.components, "inventory": new_destination_inventory.model_dump(mode="json")}
    })
    entities[shipment_entity.id] = shipment_entity.model_copy(update={
        "location_id": shipment.destination_place_id,
        "components": {
            **shipment_entity.components,
            "shipment": next_shipment.model_dump(mode="json"),
            "inventory": new_transit_inventory.model_dump(mode="json"),
        },
    })
    resources = dict(state.resources)
    resources[cargo.transit_account_id] = transit_account.model_copy(update={
        "balance": transit_account.balance - remaining_quantity
    })
    resources[cargo.destination_account_id] = destination_account.model_copy(update={
        "balance": destination_account.balance + remaining_quantity
    })
    updates = {"entities": entities, "resources": resources}
    schedules = list(state.scheduled_events)
    if schedule_id is not None:
        schedules, _ = _validate_shipment_arrival_schedule(
            state,
            shipment_id=shipment_entity.id,
            shipment=shipment,
            schedule_id=schedule_id,
            expected_schedule_revision=expected_schedule_revision,
        )
    if shipment.spoilage_schedule_id is not None:
        spoilage_index = next(
            (index for index, item in enumerate(schedules) if item.id == shipment.spoilage_schedule_id),
            None,
        )
        if spoilage_index is None:
            raise InvalidEvent("Shipment spoilage schedule is missing at delivery.")
        spoilage_schedule = schedules[spoilage_index]
        if (
            expected_spoilage_schedule_revision is None
            or spoilage_schedule.revision != expected_spoilage_schedule_revision
            or spoilage_schedule.status != "queued"
            or spoilage_schedule.handler_id != "shipment.spoilage"
            or spoilage_schedule.due_tick <= state.current_tick
        ):
            raise InvalidEvent("Shipment spoilage schedule is stale or already due at receipt.")
        schedules[spoilage_index] = spoilage_schedule.model_copy(update={
            "status": "cancelled",
            "revision": spoilage_schedule.revision + 1,
        })
    elif expected_spoilage_schedule_revision is not None:
        raise InvalidEvent("Unexpected shipment spoilage schedule revision on a nonperishable shipment.")
    if schedule_id is not None or shipment.spoilage_schedule_id is not None:
        updates["scheduled_events"] = schedules
    return _validated_state(state, updates)


def _apply_shipment_spoilage_resolved(
    state: WorldState,
    event: ShipmentSpoilageResolvedEvent,
) -> WorldState:
    if event.spoiled_tick != state.current_tick:
        raise InvalidEvent("Shipment spoilage must commit at its exact world-time boundary.")
    shipment_entity = state.entities.get(event.shipment_id)
    if shipment_entity is None or shipment_entity.kind != "shipment":
        raise InvalidEvent("Shipment spoilage target does not exist.")
    try:
        shipment = ShipmentComponent.model_validate(shipment_entity.components.get("shipment"))
        inventory = InventoryComponent.model_validate(shipment_entity.components.get("inventory"))
    except Exception as exc:
        raise InvalidEvent("Shipment spoilage target has invalid custody components.") from exc
    if (
        shipment.lifecycle_version != "expiry_v2"
        or shipment.revision != event.expected_shipment_revision
        or shipment.status != "awaiting_receipt"
        or shipment.spoilage_schedule_id != event.scheduled_event_id
        or shipment.arrived_tick is None
        or len(shipment.cargo) != 1
    ):
        raise InvalidEvent("Shipment spoilage event does not match an active deferred manifest.")
    arrival_schedule = next((item for item in state.scheduled_events if item.id == shipment.arrival_schedule_id), None)
    if arrival_schedule is None or arrival_schedule.status != "completed":
        raise InvalidEvent("Shipment arrival must complete before its cargo spoilage schedule.")
    schedule_index = next(
        (index for index, item in enumerate(state.scheduled_events) if item.id == event.scheduled_event_id),
        None,
    )
    if schedule_index is None:
        raise InvalidEvent("Shipment spoilage schedule does not exist.")
    schedule = state.scheduled_events[schedule_index]
    if (
        schedule.revision != event.expected_schedule_revision
        or schedule.status != "queued"
        or schedule.handler_id != "shipment.spoilage"
        or schedule.handler_version != "1.0.0"
        or schedule.phase != "delivery"
        or schedule.due_tick != state.current_tick
        or schedule.max_runs != 1
        or schedule.repeat_ticks is not None
        or schedule.payload != {"shipment_id": str(shipment_entity.id)}
        or schedule.dependencies != [shipment.arrival_schedule_id]
    ):
        raise InvalidEvent("Shipment spoilage schedule is stale or does not match the exact expiry boundary.")
    schedules_by_id = {item.id: item for item in state.scheduled_events}
    if any(
        dependency not in schedules_by_id or schedules_by_id[dependency].status != "completed"
        for dependency in schedule.dependencies
    ):
        raise InvalidEvent("Shipment spoilage schedule cannot precede its arrival dependency.")

    cargo = shipment.cargo[0]
    transit_account = state.resources.get(cargo.transit_account_id)
    if (
        event.resource_id != cargo.resource_id
        or event.transit_account_id != cargo.transit_account_id
        or inventory.revision != event.expected_transit_inventory_revision
        or transit_account is None
        or transit_account.account_type != "holder"
        or transit_account.owner_id != shipment_entity.id
        or transit_account.resource_id != cargo.resource_id
        or transit_account.balance != event.expected_transit_account_balance
        or transit_account.balance != shipment.remaining_quantity
    ):
        raise InvalidEvent("Shipment spoilage custody or account compare-and-swap failed.")
    next_expiry = min(
        (lot.expiry_tick for lot in inventory.lots if lot.expiry_tick is not None),
        default=None,
    )
    if next_expiry != state.current_tick:
        raise InvalidEvent("Shipment spoilage must run at the earliest remaining lot expiry.")
    due_lots = sorted(
        (lot for lot in inventory.lots if lot.expiry_tick is not None and lot.expiry_tick <= state.current_tick),
        key=lambda item: str(item.lot_id),
    )
    if not due_lots or len(due_lots) != len(event.spoiled_lots):
        raise InvalidEvent("Shipment spoilage receipt must list every lot due at this boundary exactly once.")
    for stored, supplied in zip(due_lots, event.spoiled_lots, strict=True):
        if (
            stored.lot_id != supplied.transit_lot_id
            or stored.quantity != supplied.quantity
            or supplied.spoiled_tick != state.current_tick
            or supplied.event_id != event.event_id
            or supplied.destruction_sink_account_id != event.destruction_sink_account_id
        ):
            raise InvalidEvent("Shipment spoilage lot record does not match its exact FIFO custody lot.")
    manifest_quantities = shipment.remaining_lot_quantities()
    if any(manifest_quantities.get(lot.lot_id) != lot.quantity for lot in due_lots):
        raise InvalidEvent("Shipment spoilage lot quantities do not match the immutable dispatch manifest remainder.")

    roles = state.metadata.get("resource_roles", {})
    configured_sinks = roles.get("destruction_sink_account_ids", {}) if isinstance(roles, dict) else {}
    configured = isinstance(configured_sinks, dict) and cargo.resource_id in configured_sinks
    if configured:
        try:
            configured_id = UUID(str(configured_sinks[cargo.resource_id]))
        except (TypeError, ValueError) as exc:
            raise InvalidEvent("Configured resource destruction sink ID is invalid.") from exc
        if configured_id != event.destruction_sink_account_id:
            raise InvalidEvent("Shipment spoilage must use the configured resource destruction sink.")
    else:
        expected_sink_id = uuid5(event.world_id, f"resource-destruction-sink:{cargo.resource_id}")
        if event.destruction_sink_account_id != expected_sink_id:
            raise InvalidEvent("Unconfigured destruction sink must use its stable world/resource UUID.")
    sink = state.resources.get(event.destruction_sink_account_id)
    if event.create_destruction_sink:
        if configured or sink is not None or event.expected_destruction_sink_balance is not None:
            raise InvalidEvent("Shipment spoilage may create only an absent deterministic unconfigured sink.")
        sink = ResourceAccount(
            resource_id=cargo.resource_id,
            balance="0",
            owner_id=None,
            account_type="destruction_sink",
        )
    elif (
        sink is None
        or sink.account_type != "destruction_sink"
        or sink.owner_id is not None
        or sink.resource_id != cargo.resource_id
        or sink.balance != event.expected_destruction_sink_balance
    ):
        raise InvalidEvent("Shipment spoilage destruction sink is missing, incompatible or stale.")
    assert sink is not None

    spoiled_total = sum((lot.quantity for lot in due_lots), Decimal("0"))
    if transit_account.balance < spoiled_total:
        raise InvalidEvent("Shipment transit account cannot cover the expired lot quantity.")
    new_sink_balance = sink.balance + spoiled_total
    try:
        updated_sink = sink.model_copy(update={"balance": new_sink_balance})
        ResourceAccount.model_validate(updated_sink.model_dump(mode="json"))
    except Exception as exc:
        raise InvalidEvent("Shipment destruction sink would exceed its bounded resource balance.") from exc

    remaining_inventory_lots = [lot for lot in inventory.lots if lot.lot_id not in {item.lot_id for item in due_lots}]
    next_due_tick = min(
        (lot.expiry_tick for lot in remaining_inventory_lots if lot.expiry_tick is not None),
        default=None,
    )
    if event.next_spoilage_due_tick != next_due_tick:
        raise InvalidEvent("Shipment spoilage event does not name the exact next remaining expiry.")
    if next_due_tick is None:
        if event.next_spoilage_schedule_id is not None:
            raise InvalidEvent("Shipment with no expiring cargo cannot schedule another spoilage occurrence.")
    else:
        expected_next_id = uuid5(
            event.command_id,
            f"{event.ruleset_version}:shipment.spoilage-schedule:{shipment_entity.id}:{next_due_tick}",
        )
        if event.next_spoilage_schedule_id != expected_next_id:
            raise InvalidEvent("Shipment next spoilage schedule ID is not deterministic for this command.")

    completed_schedule = schedule.model_copy(update={
        "status": "completed",
        "completed_runs": 1,
        "revision": schedule.revision + 1,
    })
    schedules = list(state.scheduled_events)
    schedules[schedule_index] = completed_schedule
    if event.next_spoilage_schedule_id is not None and next_due_tick is not None:
        if any(item.id == event.next_spoilage_schedule_id for item in schedules):
            raise InvalidEvent("Shipment next spoilage schedule ID already exists.")
        schedules.append(ScheduledEventRecord(
            id=event.next_spoilage_schedule_id,
            due_tick=next_due_tick,
            phase="delivery",
            dependencies=[shipment.arrival_schedule_id],
            handler_id="shipment.spoilage",
            handler_version="1.0.0",
            max_runs=1,
            payload={"shipment_id": str(shipment_entity.id)},
        ))

    spoiled_records = sorted(
        [*shipment.spoiled_lots, *event.spoiled_lots],
        key=lambda item: str(item.transit_lot_id),
    )
    remaining_quantity = shipment.remaining_quantity - spoiled_total
    next_shipment = shipment.model_copy(update={
        "status": "spoiled" if remaining_quantity == 0 else shipment.status,
        "spoilage_schedule_id": event.next_spoilage_schedule_id,
        "spoiled_lots": spoiled_records,
        "revision": shipment.revision + 1,
    })
    next_inventory = inventory.model_copy(update={
        "lots": remaining_inventory_lots,
        "revision": inventory.revision + 1,
    })
    entities = dict(state.entities)
    entities[shipment_entity.id] = shipment_entity.model_copy(update={
        "components": {
            **shipment_entity.components,
            "shipment": next_shipment.model_dump(mode="json"),
            "inventory": next_inventory.model_dump(mode="json"),
        },
    })
    resources = dict(state.resources)
    resources[cargo.transit_account_id] = transit_account.model_copy(update={
        "balance": transit_account.balance - spoiled_total,
    })
    resources[event.destruction_sink_account_id] = updated_sink
    return _validated_state(state, {
        "entities": entities,
        "resources": resources,
        "scheduled_events": schedules,
    })


def author_value_hash(value: object | None, *, component_name: str | None = None) -> str:
    """Hash one canonical author-managed component value for compare-and-swap edits."""
    canonical_value = _canonical_component_compatibility(component_name, value)
    encoded = json.dumps(
        canonical_value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _apply_resource_flow_event(
    state: WorldState,
    event: ResourceConsumedEvent | ResourceProducedEvent,
) -> WorldState:
    postings = ledger_postings(event)
    validate_ledger_postings(postings)
    deltas: dict[UUID, Decimal] = {}
    with localcontext() as context:
        context.prec = _ledger_precision(postings)
        for posting in postings:
            account = state.resources.get(posting.account_id)
            if account is None:
                raise InvalidEvent(f"Resource account {posting.account_id} does not exist.")
            if account.resource_id != posting.resource_id:
                raise InvalidEvent(f"Resource account {posting.account_id} has the wrong resource.")
            deltas[posting.account_id] = deltas.get(posting.account_id, Decimal("0")) + posting.delta

        if isinstance(event, ResourceConsumedEvent):
            if event.consumer_id is not None and event.consumer_id not in state.entities:
                raise InvalidEvent(f"Resource consumer {event.consumer_id} does not exist.")
            for flow in event.flows:
                _validate_flow_accounts(
                    state,
                    flow,
                    source_account_id=flow.holder_account_id,
                    target_account_id=flow.sink_account_id,
                    expected_source_type="holder",
                    expected_target_type="consumption_sink",
                )
                if (
                    event.consumer_id is not None
                    and state.resources[flow.holder_account_id].owner_id != event.consumer_id
                ):
                    raise InvalidEvent("Consumed holder account must belong to the declared consumer.")
        else:
            if event.producer_id is not None:
                _validate_production_recipe(state, event)
            for flow in event.consumed_inputs:
                _validate_flow_accounts(
                    state,
                    flow,
                    source_account_id=flow.holder_account_id,
                    target_account_id=flow.sink_account_id,
                    expected_source_type="holder",
                    expected_target_type="consumption_sink",
                )
                if (
                    event.producer_id is not None
                    and state.resources[flow.holder_account_id].owner_id != event.producer_id
                ):
                    raise InvalidEvent("Production input account must belong to the declared producer.")
            for flow in event.produced_outputs:
                source = state.resources.get(flow.source_account_id)
                target = state.resources.get(flow.holder_account_id)
                if source is None or target is None:
                    raise InvalidEvent("Production source and destination accounts must exist.")
                if source.resource_id != flow.resource_id or target.resource_id != flow.resource_id:
                    raise InvalidEvent("Production accounts and flow must use the same resource.")
                if source.account_type != "production_source" or target.account_type != "holder":
                    raise InvalidEvent("Production output requires a production source and holder account.")
                if event.producer_id is not None and source.owner_id != event.producer_id:
                    raise InvalidEvent("Production source quota must belong to the declared producer.")
                if event.producer_id is not None and target.owner_id != event.producer_id:
                    raise InvalidEvent("Production output holder must belong to the declared producer.")

        resources = dict(state.resources)
        for account_id, delta in deltas.items():
            account = state.resources[account_id]
            new_balance = account.balance + delta
            if account.account_type == "holder" and new_balance < 0:
                raise InsufficientResources(
                    f"Account {account_id} has {account.balance} {account.resource_id}; "
                    f"flow requires {-delta}."
                )
            producer_scoped_production = isinstance(event, ResourceProducedEvent) and event.producer_id is not None
            if account.account_type in {"production_source", "issuance_source"} and producer_scoped_production and (
                account.credit_limit is None or new_balance < account.credit_limit.copy_negate()
            ):
                raise InsufficientResources(
                    f"Source account {account_id} has exhausted its finite {account.resource_id} quota."
                )
            resources[account_id] = account.model_copy(update={"balance": new_balance})
    return _validated_state(state, {"resources": resources})


def _validate_production_recipe(state: WorldState, event: ResourceProducedEvent) -> None:
    producer = state.entities.get(event.producer_id)
    if producer is None:
        raise InvalidEvent(f"Production entity {event.producer_id} does not exist.")
    raw_production = producer.components.get("production")
    if raw_production is None:
        raise InvalidEvent("The declared producer has no registered production component.")
    try:
        production = ProductionComponent.model_validate(raw_production)
    except Exception as exc:
        raise InvalidEvent(f"The producer has an invalid production component: {exc}") from exc
    recipe = next((item for item in production.recipes if item.recipe_id == event.recipe_id), None)
    if recipe is None:
        raise InvalidEvent(f"Production recipe {event.recipe_id!r} is not registered for the producer.")

    with localcontext() as context:
        context.prec = 50
        expected_inputs: dict[str, Decimal] = {}
        expected_outputs: dict[str, Decimal] = {}
        for flow in recipe.inputs:
            expected_inputs[flow.resource_id] = (
                expected_inputs.get(flow.resource_id, Decimal("0")) + flow.quantity * event.batch_count
            )
        for flow in recipe.outputs:
            expected_outputs[flow.resource_id] = (
                expected_outputs.get(flow.resource_id, Decimal("0")) + flow.quantity * event.batch_count
            )
        actual_inputs = _flow_totals(event.consumed_inputs)
        actual_outputs = _flow_totals(event.produced_outputs)
    if actual_inputs != expected_inputs or actual_outputs != expected_outputs:
        raise InvalidEvent("Production flows must exactly match the registered recipe for the selected batch count.")


class _ResourceFlow(Protocol):
    resource_id: str
    quantity: Decimal


def _flow_totals(flows: Iterable[_ResourceFlow]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    with localcontext() as context:
        context.prec = 50
        for flow in flows:
            totals[flow.resource_id] = totals.get(flow.resource_id, Decimal("0")) + flow.quantity
    return totals


def _validate_flow_accounts(
    state: WorldState,
    flow: ConsumedResourceFlow,
    *,
    source_account_id: UUID,
    target_account_id: UUID,
    expected_source_type: str,
    expected_target_type: str,
) -> None:
    if source_account_id == target_account_id:
        raise InvalidEvent("A resource flow requires separate holder and system accounts.")
    source = state.resources.get(source_account_id)
    target = state.resources.get(target_account_id)
    if source is None or target is None:
        raise InvalidEvent("Resource flow source and destination accounts must exist.")
    if source.resource_id != flow.resource_id or target.resource_id != flow.resource_id:
        raise InvalidEvent("Resource flow accounts and flow must use the same resource.")
    if source.account_type != expected_source_type or target.account_type != expected_target_type:
        raise InvalidEvent("Resource flow account roles do not match the event type.")


def validate_ledger_postings(postings: tuple[LedgerPosting, ...]) -> None:
    totals: dict[str, Decimal] = {}
    with localcontext() as context:
        context.prec = _ledger_precision(postings)
        for posting in postings:
            if not posting.delta.is_finite():
                raise InvalidEvent("Ledger postings must be finite decimal values.")
            totals[posting.resource_id] = totals.get(posting.resource_id, Decimal("0")) + posting.delta
    unbalanced = {resource: total for resource, total in totals.items() if total != 0}
    if unbalanced:
        raise InvalidEvent(f"Ledger batch is unbalanced: {unbalanced}.")


def _ledger_precision(postings: tuple[LedgerPosting, ...]) -> int:
    max_digits = max((len(posting.delta.as_tuple().digits) for posting in postings), default=1)
    aggregation_margin = len(str(max(1, len(postings)))) + 2
    return max(40, max_digits + aggregation_margin)


STATE_HASH_COMPATIBILITY_VERSION = 1


def canonical_state_payload(
    state: WorldState,
    *,
    compatibility_version: int = STATE_HASH_COMPATIBILITY_VERSION,
) -> dict[str, object]:
    """Return the stable state-hash view, omitting only compatible defaults.

    Version 1 preserves hashes from saves written before NeedValue's fractional
    decay fields, cohort membership, quota-policy, ResourceAccount's finite
    source credit_limit fields, and KnowledgeRecord's typed investigation data
    existed. The
    canonical payload follows ResourceAccount's original field-presence convention;
    explicit ``credit_limit: null`` remains present for worlds already written with
    that field, while absent fields stay absent. The same canonical form is persisted
    so a fork or reopen retains the source schema's hash behavior.
    """
    return _canonical_state_payload(state, compatibility_version=compatibility_version)


def _canonical_state_payload(
    state: WorldState,
    *,
    compatibility_version: int = STATE_HASH_COMPATIBILITY_VERSION,
    excluded_fields: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Build the hash view while optionally skipping reducer-replaced fields."""
    if compatibility_version != STATE_HASH_COMPATIBILITY_VERSION:
        raise ValueError(f"Unsupported state hash compatibility version {compatibility_version}.")
    payload = cast(dict[str, object], state.model_dump(mode="json", exclude=excluded_fields))
    if not state.investigation_cases:
        payload.pop("investigation_cases", None)
    if not state.relationship_promises:
        payload.pop("relationship_promises", None)
    resources = payload.get("resources")
    if isinstance(resources, dict):
        for account_id, account_json in resources.items():
            account = state.resources.get(UUID(account_id))
            if (
                isinstance(account_json, dict)
                and account is not None
                and account.credit_limit is None
                and "credit_limit" not in account.model_fields_set
            ):
                account_json.pop("credit_limit", None)
    knowledge = payload.get("knowledge")
    if isinstance(knowledge, list):
        payload["knowledge"] = [_canonical_knowledge_record(item) for item in knowledge]
    entities = payload.get("entities")
    if isinstance(entities, dict):
        for entity in entities.values():
            if isinstance(entity, dict):
                components = entity.get("components")
                if isinstance(components, dict):
                    for component_name, value in list(components.items()):
                        components[component_name] = _canonical_component_compatibility(component_name, value)
    scheduled_events = payload.get("scheduled_events")
    if isinstance(scheduled_events, list):
        payload["scheduled_events"] = [_canonical_scheduled_event(item) for item in scheduled_events]
    return payload


def _canonical_needs_value(value: object) -> object:
    """Omit the new defaults only from typed NeedValue records."""
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for key, item in list(result.items()):
        if key == "custom" and isinstance(item, dict):
            result[key] = {name: _canonical_need_value(need) for name, need in item.items()}
        elif isinstance(item, dict) and "value" in item and "critical_below" in item:
            result[key] = _canonical_need_value(item)
    return result


def _canonical_need_value(value: dict[str, object]) -> dict[str, object]:
    result = dict(value)
    if result.get("decay_remainder") == 0:
        result.pop("decay_remainder", None)
    if result.get("last_critical_damage_tick") is None:
        result.pop("last_critical_damage_tick", None)
    return result


def _canonical_component_compatibility(component_name: str | None, value: object) -> object:
    """Omit only newly introduced typed defaults from their registered components."""
    if component_name == "needs":
        return _canonical_needs_value(value)
    if not isinstance(value, dict):
        return value
    result = dict(value)
    if component_name == "life":
        if result.get("population_group_id") is None:
            result.pop("population_group_id", None)
        if result.get("population_origin") is None:
            result.pop("population_origin", None)
    elif component_name == "personality" and result.get("locked_fields") == []:
        result.pop("locked_fields", None)
    elif component_name == "governance":
        if result.get("charter_clause_ids") == []:
            result.pop("charter_clause_ids", None)
        if result.get("membership_recruitment") == []:
            result.pop("membership_recruitment", None)
    elif component_name == "population":
        if result.get("unmodeled_headcount") is None:
            result.pop("unmodeled_headcount", None)
        if result.get("age_rates") == []:
            result.pop("age_rates", None)
        if result.get("age_cohorts") == []:
            result.pop("age_cohorts", None)
        if result.get("last_advanced_year") is None:
            result.pop("last_advanced_year", None)
        if result.get("seat_policy") is None:
            result.pop("seat_policy", None)
    elif component_name == "production" and result.get("quotas") == []:
        result.pop("quotas", None)
    elif component_name == "place" and result.get("renewable_harvests") == []:
        result.pop("renewable_harvests", None)
    elif component_name == "shipment":
        if result.get("lifecycle_version", "legacy_v1") == "legacy_v1":
            result.pop("lifecycle_version", None)
            if result.get("spoilage_schedule_id") is None:
                result.pop("spoilage_schedule_id", None)
            if result.get("spoiled_lots") == []:
                result.pop("spoiled_lots", None)
            if result.get("delivered_lots") == []:
                result.pop("delivered_lots", None)
    elif component_name == "capabilities" and result.get("combat_death_protection") is False:
        result.pop("combat_death_protection", None)
    elif component_name == "health":
        if result.get("incapacitating_injury_id") is None:
            result.pop("incapacitating_injury_id", None)
        injuries = result.get("injuries")
        if isinstance(injuries, list):
            result["injuries"] = [
                {
                    key: field
                    for key, field in item.items()
                    if key not in {"treated_event_id", "treated_tick"} or field is not None
                }
                if isinstance(item, dict) else item
                for item in injuries
            ]
    return result


def _canonical_scheduled_event(value: object) -> object:
    if not isinstance(value, dict):
        return value
    result = dict(value)
    if result.get("repeat_ticks") is None:
        result.pop("repeat_ticks", None)
    if result.get("max_runs") == 1:
        result.pop("max_runs", None)
    if result.get("completed_runs") == 0:
        result.pop("completed_runs", None)
    return result


def _canonical_knowledge_record(value: object) -> object:
    """Omit optional investigation fields when they carry no data.

    These fields were added after persisted worlds already contained knowledge
    records. Null values have no semantics, so dropping them preserves original
    state hashes and keeps canonical reducer state stable.
    """
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for key in ("fact", "assessment", "disclosure"):
        if result.get(key) is None:
            result.pop(key, None)
    if result.get("disclosure_policy") == "never":
        result.pop("disclosure_policy", None)
    return result


def state_hash(state: WorldState) -> str:
    return state_payload_hash(canonical_state_payload(state))


def state_payload_hash(payload: dict[str, object]) -> str:
    """Hash a payload already normalized by the canonical state serializer."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def state_json(state: WorldState) -> dict[str, object]:
    return canonical_state_payload(state)


def _canonical_dump_included(state: WorldState, field_name: str) -> bool:
    """Return whether the original canonical dump had this top-level field."""
    field = WorldState.model_fields.get(field_name)
    if field is None or field.exclude is True:
        return False
    value = getattr(state, field_name)
    if field.exclude_if is not None and field.exclude_if(value):
        return False
    # These compatibility omissions predate Pydantic's exclude_if support.
    if field_name in {"investigation_cases", "relationship_promises"} and not value:
        return False
    return True


def _merge_serialized_state_updates(
    state: WorldState,
    payload: dict[str, object],
    serialized_updates: dict[str, object],
) -> dict[str, object]:
    """Apply reducer replacements without changing canonical dump key order."""
    if not serialized_updates:
        return payload
    merged: dict[str, object] = {}
    for field_name in WorldState.model_fields:
        if field_name in serialized_updates:
            if _canonical_dump_included(state, field_name):
                merged[field_name] = serialized_updates[field_name]
        elif field_name in payload:
            merged[field_name] = payload[field_name]
    # dict.update appends fields absent from the old canonical payload (including
    # extra update keys) in update order. Retain that behavior without dumping
    # replaced old values just to recover their positions.
    for field_name, value in serialized_updates.items():
        if field_name not in merged:
            merged[field_name] = value
    return merged


def _validated_state(state: WorldState, updates: dict[str, object]) -> WorldState:
    # Reducers provide replacement values for updated top-level fields. Skip
    # serializing their old values; the complete merged state is still validated.
    payload = _canonical_state_payload(state, excluded_fields=frozenset(updates))
    serialized_updates = to_jsonable_python(updates)
    if isinstance(updates.get("resources"), dict) and isinstance(serialized_updates.get("resources"), dict):
        for account_id, account in updates["resources"].items():
            account_json = serialized_updates["resources"].get(str(account_id))
            if (
                isinstance(account, ResourceAccount)
                and isinstance(account_json, dict)
                and account.credit_limit is None
                and "credit_limit" not in account.model_fields_set
            ):
                account_json.pop("credit_limit", None)
    if isinstance(updates.get("entities"), dict) and isinstance(serialized_updates.get("entities"), dict):
        for entity_id, entity in updates["entities"].items():
            entity_json = serialized_updates["entities"].get(str(entity_id))
            if isinstance(entity, WorldEntity) and isinstance(entity_json, dict):
                components = entity_json.get("components")
                if isinstance(components, dict):
                    for component_name, value in list(components.items()):
                        components[component_name] = _canonical_component_compatibility(component_name, value)
    if isinstance(updates.get("scheduled_events"), list) and isinstance(serialized_updates.get("scheduled_events"), list):
        serialized_updates["scheduled_events"] = [
            _canonical_scheduled_event(item) for item in serialized_updates["scheduled_events"]
        ]
    if isinstance(updates.get("knowledge"), list) and isinstance(serialized_updates.get("knowledge"), list):
        serialized_updates["knowledge"] = [
            _canonical_knowledge_record(item) for item in serialized_updates["knowledge"]
        ]
    payload = _merge_serialized_state_updates(state, payload, serialized_updates)
    try:
        serialized_payload = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return WorldState.model_validate_json(serialized_payload)
    except Exception as exc:
        raise InvalidEvent(str(exc)) from exc


def _require_location(state: WorldState, location_id: UUID) -> WorldEntity:
    location = state.entities.get(location_id)
    if location is None or location.kind not in {"location", "place"}:
        raise InvalidEvent(f"Location {location_id} does not exist or is not a location entity.")
    if location.status in {"dead", "retired"}:
        raise InvalidEvent(f"Location {location_id} is retired.")
    return location
